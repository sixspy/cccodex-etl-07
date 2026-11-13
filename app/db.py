import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from app.config import Config

SEED_PIPELINES = [
    ("订单明细日抽", "orders_daily", "待抽取", "mock://orders/day"),
    ("用户画像宽表", "user_profile", "待抽取", "mock://users/profile"),
    ("库存快照落地", "inventory_snap", "待抽取", "mock://stock/snap"),
    ("支付流水清洗", "pay_clean", "待抽取", "mock://pay/flow"),
]

# mock 源：抽取后各管道可用的指标（模拟源端缺列/缺指标）
MOCK_SOURCE_METRICS = {
    "orders_daily": ["gmv", "order_cnt", "buyer_cnt", "refund_cnt"],
    "user_profile": ["uv", "pv", "new_user"],
    "inventory_snap": ["stock_qty", "lock_qty"],
    "pay_clean": ["pay_amt", "pay_cnt", "fee"],
}

# 落库方案册种子：每管道定一种形态写进说明
# (code, 说明名, 形态, 指标清单, 宽表必填列)
SEED_SPECS = [
    ("orders_daily", "订单宽表方案", "wide",
     ["gmv", "order_cnt", "buyer_cnt"], ["gmv", "order_cnt", "buyer_cnt"]),
    ("user_profile", "画像宽表方案", "wide",
     ["uv", "pv", "active_days"], ["uv", "pv", "active_days"]),
    ("inventory_snap", "库存窄表方案", "narrow",
     ["stock_qty", "lock_qty", "in_transit"], []),
    ("pay_clean", "支付窄表方案", "narrow",
     ["pay_amt", "pay_cnt", "fee"], []),
]

SHAPE_NAMES = {"wide": "宽表", "narrow": "窄表"}

STATUS_FLOW = ("待抽取", "已抽取待清洗", "已清洗待落库", "落库中", "已落库", "部分落库", "落库失败")


def today_str() -> str:
    return date.today().isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              code TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              source_uri TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0,
              loaded INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shape_specs (
              pipeline_id INTEGER PRIMARY KEY REFERENCES pipelines(id),
              spec_name TEXT NOT NULL,
              shape TEXT NOT NULL CHECK (shape IN ('wide','narrow')),
              metrics TEXT NOT NULL DEFAULT '[]',
              required_cols TEXT NOT NULL DEFAULT '[]',
              decided_on TEXT NOT NULL,
              updated_by TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS extracted_metrics (
              pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
              metric TEXT NOT NULL,
              PRIMARY KEY (pipeline_id, metric)
            );
            CREATE TABLE IF NOT EXISTS load_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
              shape TEXT NOT NULL,
              planned_rows INTEGER NOT NULL,
              landed_rows INTEGER NOT NULL,
              unlanded TEXT NOT NULL DEFAULT '[]',
              result TEXT NOT NULL,
              actor TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS shape_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
              kind TEXT NOT NULL,
              actor TEXT NOT NULL DEFAULT '',
              expected_shape TEXT NOT NULL DEFAULT '',
              actual_shape TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _ensure_column(conn, "pipelines", "cleaned", "cleaned INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "pipelines", "load_actor", "load_actor TEXT NOT NULL DEFAULT ''")

        n = conn.execute("SELECT COUNT(*) AS c FROM pipelines").fetchone()["c"]
        if n == 0:
            conn.executemany(
                "INSERT INTO pipelines (title, code, status, source_uri) VALUES (?,?,?,?)",
                SEED_PIPELINES,
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
            (Config.LAB_NAME,),
        )
        _seed_specs(conn)


def _seed_specs(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) AS c FROM shape_specs").fetchone()["c"]
    if n:
        return
    today = today_str()
    for code, spec_name, shape, metrics, required in SEED_SPECS:
        row = conn.execute("SELECT id FROM pipelines WHERE code = ?", (code,)).fetchone()
        if not row:
            continue
        conn.execute(
            "INSERT INTO shape_specs (pipeline_id, spec_name, shape, metrics, required_cols, decided_on, updated_by)"
            " VALUES (?,?,?,?,?,?,?)",
            (row["id"], spec_name, shape, json.dumps(metrics, ensure_ascii=False),
             json.dumps(required, ensure_ascii=False), today, "seed"),
        )


# ---------- 读取 ----------

def _spec_for(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT pipeline_id, spec_name, shape, metrics, required_cols, decided_on, updated_by "
        "FROM shape_specs WHERE pipeline_id = ?",
        (pipeline_id,),
    ).fetchone()
    if not row:
        return None
    spec = dict(row)
    spec["metrics"] = json.loads(spec["metrics"])
    spec["required_cols"] = json.loads(spec["required_cols"])
    spec["shape_name"] = SHAPE_NAMES.get(spec["shape"], spec["shape"])
    return spec


def _available_metrics(conn: sqlite3.Connection, pipeline_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT metric FROM extracted_metrics WHERE pipeline_id = ? ORDER BY metric",
        (pipeline_id,),
    ).fetchall()
    return [r["metric"] for r in rows]


def _last_run(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, pipeline_id, shape, planned_rows, landed_rows, unlanded, result, actor, created_at "
        "FROM load_runs WHERE pipeline_id = ? ORDER BY id DESC LIMIT 1",
        (pipeline_id,),
    ).fetchone()
    if not row:
        return None
    run = dict(row)
    run["unlanded"] = json.loads(run["unlanded"])
    run["shape_name"] = SHAPE_NAMES.get(run["shape"], run["shape"])
    return run


def _events_for(conn: sqlite3.Connection, pipeline_id: int, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, actor, expected_shape, actual_shape, detail, created_at "
        "FROM shape_events WHERE pipeline_id = ? ORDER BY id DESC LIMIT ?",
        (pipeline_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _planned_rows(spec: dict | None) -> int:
    if not spec:
        return 0
    if spec["shape"] == "wide":
        return 1
    return len(spec["metrics"])


def _shape_public(spec: dict | None) -> dict:
    if not spec:
        return {"shape": "", "shape_name": "未定", "spec_name": "", "planned_rows": 0}
    return {
        "shape": spec["shape"],
        "shape_name": spec["shape_name"],
        "spec_name": spec["spec_name"],
        "planned_rows": _planned_rows(spec),
    }


def list_pipelines(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, cleaned, loaded, load_actor, created_at "
        "FROM pipelines ORDER BY id"
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        spec = _spec_for(conn, item["id"])
        item.update(_shape_public(spec))
        last = _last_run(conn, item["id"])
        item["unlanded_metrics"] = last["unlanded"] if last else []
        items.append(item)
    return items


def get_pipeline(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, code, status, source_uri, extracted, cleaned, loaded, load_actor, created_at "
        "FROM pipelines WHERE id = ?",
        (pipeline_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    spec = _spec_for(conn, pipeline_id)
    item.update(_shape_public(spec))
    item["spec"] = spec
    item["available_metrics"] = _available_metrics(conn, pipeline_id)
    item["missing_metrics"] = (
        [m for m in spec["metrics"] if m not in item["available_metrics"]] if spec else []
    )
    last = _last_run(conn, pipeline_id)
    item["unlanded_metrics"] = last["unlanded"] if last else []
    item["last_run"] = last
    item["events"] = _events_for(conn, pipeline_id)
    return item


def list_specs(conn: sqlite3.Connection) -> list[dict]:
    """落库方案册：全部说明一览。"""
    rows = conn.execute(
        "SELECT p.id AS pipeline_id, p.title, p.code, p.status, "
        "s.spec_name, s.shape, s.metrics, s.required_cols, s.decided_on, s.updated_by "
        "FROM pipelines p LEFT JOIN shape_specs s ON s.pipeline_id = p.id ORDER BY p.id"
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        if item["shape"]:
            item["metrics"] = json.loads(item["metrics"])
            item["required_cols"] = json.loads(item["required_cols"])
            item["shape_name"] = SHAPE_NAMES.get(item["shape"], item["shape"])
        else:
            item.update({"metrics": [], "required_cols": [], "shape_name": "未定",
                         "spec_name": "", "decided_on": "", "updated_by": ""})
        items.append(item)
    return items


def build_preview(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, title, code, status FROM pipelines WHERE id = ?", (pipeline_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    spec = _spec_for(conn, pipeline_id)
    available = _available_metrics(conn, pipeline_id)
    preview: dict = {"type": "none", "columns": [], "rows": []}
    if spec:
        if spec["shape"] == "wide":
            # 宽表：打成一行，指标作列
            preview = {
                "type": "wide",
                "columns": list(spec["metrics"]),
                "rows": [
                    {
                        "metric": m,
                        "value": _mock_value(pipeline_id, m),
                        "will_land": m in available,
                    }
                    for m in spec["metrics"]
                ],
            }
        else:
            # 窄表：按指标拆多行
            preview = {
                "type": "narrow",
                "columns": ["metric", "value"],
                "rows": [
                    {
                        "metric": m,
                        "value": _mock_value(pipeline_id, m),
                        "will_land": m in available,
                    }
                    for m in spec["metrics"]
                ],
            }
    last = _last_run(conn, pipeline_id)
    item.update(_shape_public(spec))
    item["spec"] = spec
    item["available_metrics"] = available
    item["missing_metrics"] = [m for m in (spec["metrics"] if spec else []) if m not in available]
    item["preview"] = preview
    item["unlanded_metrics"] = last["unlanded"] if last else []
    # 抢占/冲突痕迹进预览
    item["events"] = _events_for(conn, pipeline_id, limit=5)
    return item


def _mock_value(pipeline_id: int, metric: str) -> int:
    return (pipeline_id * 31 + sum(ord(c) for c in metric)) % 997 + 1


# ---------- 流程 ----------

def run_extract(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, code, status FROM pipelines WHERE id = ?", (pipeline_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    if item["status"] in ("落库中",):
        return get_pipeline(conn, pipeline_id)
    metrics = MOCK_SOURCE_METRICS.get(item["code"], ["metric_a", "metric_b"])
    extracted = 12 + (pipeline_id % 5)
    conn.execute("DELETE FROM extracted_metrics WHERE pipeline_id = ?", (pipeline_id,))
    conn.executemany(
        "INSERT INTO extracted_metrics (pipeline_id, metric) VALUES (?,?)",
        [(pipeline_id, m) for m in metrics],
    )
    conn.execute(
        "UPDATE pipelines SET status = ?, extracted = ?, cleaned = 0, loaded = 0, load_actor = '' WHERE id = ?",
        ("已抽取待清洗", extracted, pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)


def run_clean(conn: sqlite3.Connection, pipeline_id: int) -> dict | None:
    row = conn.execute("SELECT id, status, extracted FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    if item["status"] != "已抽取待清洗":
        return get_pipeline(conn, pipeline_id)
    conn.execute(
        "UPDATE pipelines SET status = ?, cleaned = extracted WHERE id = ?",
        ("已清洗待落库", pipeline_id),
    )
    return get_pipeline(conn, pipeline_id)


def _add_event(conn: sqlite3.Connection, pipeline_id: int, kind: str, actor: str,
               expected: str = "", actual: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO shape_events (pipeline_id, kind, actor, expected_shape, actual_shape, detail)"
        " VALUES (?,?,?,?,?,?)",
        (pipeline_id, kind, actor, expected, actual, detail),
    )


def run_load(conn: sqlite3.Connection, pipeline_id: int, actor: str = "",
             req_shape: str | None = None) -> dict | None:
    """落库。返回 {"ok": bool, ...}；None 表示管道不存在。"""
    row = conn.execute(
        "SELECT id, code, status, load_actor FROM pipelines WHERE id = ?", (pipeline_id,)
    ).fetchone()
    if not row:
        return None
    item = dict(row)

    if item["status"] == "落库中":
        holder = item["load_actor"] or "他人"
        _add_event(conn, pipeline_id, "形态争用", actor,
                   detail=f"{actor or '匿名'} 试图落库，{holder} 的落库进行中，仅先到者可落")
        return {"ok": False, "status": 409,
                "error": f"形态争用：{holder} 正在落库，仅先到者成功",
                "item": get_pipeline(conn, pipeline_id)}

    if item["status"] in ("待抽取", "已抽取待清洗"):
        return {"ok": False, "status": 400,
                "error": "未清洗不能落库，请先完成抽取与清洗",
                "item": get_pipeline(conn, pipeline_id)}

    if item["status"] not in ("已清洗待落库", "落库失败"):
        return {"ok": False, "status": 400,
                "error": f"当前状态（{item['status']}）不可落库",
                "item": get_pipeline(conn, pipeline_id)}

    spec = _spec_for(conn, pipeline_id)
    if not spec:
        return {"ok": False, "status": 400,
                "error": "尚未在落库方案册配置形态说明",
                "item": get_pipeline(conn, pipeline_id)}

    shape = req_shape or spec["shape"]
    if shape != spec["shape"]:
        _add_event(conn, pipeline_id, "形态冲突", actor,
                   expected=spec["shape"], actual=shape,
                   detail=f"说明定的是{SHAPE_NAMES[spec['shape']]}，实际按{SHAPE_NAMES.get(shape, shape)}落库")
        return {"ok": False, "status": 400,
                "error": f"落库形态写歪了：说明定的是{SHAPE_NAMES[spec['shape']]}，"
                         f"实际按{SHAPE_NAMES.get(shape, shape)}落库",
                "item": get_pipeline(conn, pipeline_id)}

    # 占位：先到先占，条件更新保证并发下只有一人成功
    cur = conn.execute(
        "UPDATE pipelines SET status = '落库中', load_actor = ? WHERE id = ? AND status IN ('已清洗待落库','落库失败')",
        (actor, pipeline_id),
    )
    if cur.rowcount == 0:
        _add_event(conn, pipeline_id, "形态争用", actor,
                   detail=f"{actor or '匿名'} 抢占落库失败，他人先到")
        return {"ok": False, "status": 409,
                "error": "形态争用：他人正在落库，仅先到者成功",
                "item": get_pipeline(conn, pipeline_id)}
    conn.commit()  # 让并发请求立刻看到落库中

    try:
        time.sleep(Config.LOAD_DELAY)  # 模拟落库耗时，撑开争用窗口
        available = set(_available_metrics(conn, pipeline_id))
        planned = _planned_rows(spec)
        if spec["shape"] == "wide":
            missing = [c for c in spec["required_cols"] if c not in available]
            if missing:
                # 宽表缺任一必填列：整批失败
                conn.execute(
                    "INSERT INTO load_runs (pipeline_id, shape, planned_rows, landed_rows, unlanded, result, actor)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (pipeline_id, "wide", planned, 0,
                     json.dumps(missing, ensure_ascii=False), "整批失败", actor),
                )
                conn.execute(
                    "UPDATE pipelines SET status = '落库失败', loaded = 0, load_actor = '' WHERE id = ?",
                    (pipeline_id,),
                )
                conn.commit()
                return {"ok": False, "status": 422,
                        "error": "宽表缺必填列，整批失败：" + "、".join(missing),
                        "item": get_pipeline(conn, pipeline_id)}
            landed = 1
            unlanded: list[str] = []
            result = "成功"
            status = "已落库"
        else:
            landed_metrics = [m for m in spec["metrics"] if m in available]
            unlanded = [m for m in spec["metrics"] if m not in available]
            landed = len(landed_metrics)
            if unlanded:
                result = "部分成功"
                status = "部分落库"
            else:
                result = "成功"
                status = "已落库"
        conn.execute(
            "INSERT INTO load_runs (pipeline_id, shape, planned_rows, landed_rows, unlanded, result, actor)"
            " VALUES (?,?,?,?,?,?,?)",
            (pipeline_id, spec["shape"], planned, landed,
             json.dumps(unlanded, ensure_ascii=False), result, actor),
        )
        conn.execute(
            "UPDATE pipelines SET status = ?, loaded = ?, load_actor = '' WHERE id = ?",
            (status, landed, pipeline_id),
        )
        conn.commit()
    except Exception:
        conn.execute(
            "UPDATE pipelines SET status = '落库失败', load_actor = '' WHERE id = ?",
            (pipeline_id,),
        )
        conn.commit()
        raise
    return {"ok": True, "item": get_pipeline(conn, pipeline_id)}


def save_shape(conn: sqlite3.Connection, pipeline_id: int, actor: str, shape: str,
               spec_name: str, metrics: list[str], required_cols: list[str]) -> dict | None:
    row = conn.execute("SELECT id, status FROM pipelines WHERE id = ?", (pipeline_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    if item["status"] == "落库中":
        return {"ok": False, "status": 409,
                "error": "落库进行中禁止改形态，等落库结束再改",
                "item": get_pipeline(conn, pipeline_id)}
    if shape not in SHAPE_NAMES:
        return {"ok": False, "status": 400,
                "error": "形态只支持 宽表(wide) / 窄表(narrow)",
                "item": get_pipeline(conn, pipeline_id)}
    metrics = [m.strip() for m in metrics if str(m).strip()]
    if not metrics:
        return {"ok": False, "status": 400,
                "error": "指标清单不能为空",
                "item": get_pipeline(conn, pipeline_id)}
    if not spec_name.strip():
        return {"ok": False, "status": 400,
                "error": "说明名不能为空",
                "item": get_pipeline(conn, pipeline_id)}
    if shape == "wide":
        required_cols = [c.strip() for c in required_cols if str(c).strip()] or list(metrics)
        bad = [c for c in required_cols if c not in metrics]
        if bad:
            return {"ok": False, "status": 400,
                    "error": "必填列须包含在指标清单内：" + "、".join(bad),
                    "item": get_pipeline(conn, pipeline_id)}
    else:
        required_cols = []

    existing = _spec_for(conn, pipeline_id)
    today = today_str()
    if existing and existing["decided_on"] == today and existing["shape"] != shape:
        return {"ok": False, "status": 409,
                "error": f"本日禁止改口：形态已定〈{existing['shape_name']}〉写进说明，次日方可改",
                "item": get_pipeline(conn, pipeline_id)}

    conn.execute(
        "INSERT INTO shape_specs (pipeline_id, spec_name, shape, metrics, required_cols, decided_on, updated_by)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(pipeline_id) DO UPDATE SET"
        " spec_name = excluded.spec_name, shape = excluded.shape, metrics = excluded.metrics,"
        " required_cols = excluded.required_cols, decided_on = excluded.decided_on,"
        " updated_by = excluded.updated_by",
        (pipeline_id, spec_name.strip(), shape, json.dumps(metrics, ensure_ascii=False),
         json.dumps(required_cols, ensure_ascii=False), today, actor),
    )
    return {"ok": True, "item": get_pipeline(conn, pipeline_id)}
