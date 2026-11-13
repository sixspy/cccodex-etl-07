import threading

from app.db import connect, get_db, get_pipeline, run_load

# 种子管道：1=orders_daily(宽表,列齐) 2=user_profile(宽表,缺active_days)
#           3=inventory_snap(窄表,缺in_transit) 4=pay_clean(窄表,列齐)


def _prepare_clean(authed, pid):
    assert authed.post(f"/api/pipelines/{pid}/extract").status_code == 200
    assert authed.post(f"/api/pipelines/{pid}/clean").status_code == 200


def _set_status(pid, status, actor="bob"):
    conn = connect()
    try:
        conn.execute(
            "UPDATE pipelines SET status = ?, load_actor = ? WHERE id = ?",
            (status, actor, pid),
        )
        conn.commit()
    finally:
        conn.close()


def test_list_shows_shape_fields(authed):
    items = authed.get("/api/pipelines").json()["items"]
    by_code = {p["code"]: p for p in items}
    wide = by_code["orders_daily"]
    assert wide["shape_name"] == "宽表"
    assert wide["spec_name"] == "订单宽表方案"
    assert wide["planned_rows"] == 1
    assert wide["unlanded_metrics"] == []
    narrow = by_code["inventory_snap"]
    assert narrow["shape_name"] == "窄表"
    assert narrow["spec_name"] == "库存窄表方案"
    assert narrow["planned_rows"] == 3


def test_spec_book(authed):
    items = authed.get("/api/shape-specs").json()["items"]
    assert len(items) == 4
    book = {s["code"]: s for s in items}
    assert book["orders_daily"]["shape"] == "wide"
    assert book["orders_daily"]["required_cols"] == ["gmv", "order_cnt", "buyer_cnt"]
    assert book["inventory_snap"]["shape"] == "narrow"
    assert book["inventory_snap"]["metrics"] == ["stock_qty", "lock_qty", "in_transit"]


def test_preview_wide_vs_narrow(authed):
    wide = authed.get("/api/pipelines/1/preview").json()
    assert wide["shape_name"] == "宽表"
    assert wide["planned_rows"] == 1
    assert wide["preview"]["type"] == "wide"
    assert wide["preview"]["columns"] == ["gmv", "order_cnt", "buyer_cnt"]
    assert len(wide["preview"]["rows"]) == 3  # 一行宽表的各列取值

    narrow = authed.get("/api/pipelines/3/preview").json()
    assert narrow["shape_name"] == "窄表"
    assert narrow["planned_rows"] == 3
    assert narrow["preview"]["type"] == "narrow"
    assert [r["metric"] for r in narrow["preview"]["rows"]] == [
        "stock_qty", "lock_qty", "in_transit",
    ]
    # 未抽取时全部不可落；抽取后对照源端可用指标
    assert all(not r["will_land"] for r in narrow["preview"]["rows"])
    authed.post("/api/pipelines/3/extract")
    narrow = authed.get("/api/pipelines/3/preview").json()
    will = {r["metric"]: r["will_land"] for r in narrow["preview"]["rows"]}
    assert will == {"stock_qty": True, "lock_qty": True, "in_transit": False}


def test_page_distinguishes_wide_narrow(authed):
    r = authed.get("/app/")
    assert r.status_code == 200
    assert "宽表预览" in r.text
    assert "窄表预览" in r.text


def test_wide_load_success(authed):
    _prepare_clean(authed, 1)
    r = authed.post("/api/pipelines/1/load", json={"shape": "wide"})
    assert r.status_code == 200
    item = r.json()
    assert item["status"] == "已落库"
    assert item["loaded"] == 1
    assert item["last_run"]["result"] == "成功"


def test_wide_missing_required_col_fails_whole_batch(authed):
    _prepare_clean(authed, 2)  # user_profile 缺 active_days
    r = authed.post("/api/pipelines/2/load", json={"shape": "wide"})
    assert r.status_code == 422
    body = r.json()
    assert "整批失败" in body["error"]
    assert "active_days" in body["error"]
    assert body["item"]["status"] == "落库失败"
    assert body["item"]["loaded"] == 0


def test_narrow_partial_load_lists_unlanded(authed):
    _prepare_clean(authed, 3)  # inventory_snap 缺 in_transit
    r = authed.post("/api/pipelines/3/load", json={"shape": "narrow"})
    assert r.status_code == 200
    item = r.json()
    assert item["status"] == "部分落库"
    assert item["loaded"] == 2
    assert item["unlanded_metrics"] == ["in_transit"]
    assert item["last_run"]["result"] == "部分成功"
    # 列表里也能看到未落指标
    items = authed.get("/api/pipelines").json()["items"]
    inv = [p for p in items if p["code"] == "inventory_snap"][0]
    assert inv["unlanded_metrics"] == ["in_transit"]


def test_narrow_full_load(authed):
    _prepare_clean(authed, 4)  # pay_clean 指标齐全
    r = authed.post("/api/pipelines/4/load", json={"shape": "narrow"})
    assert r.status_code == 200
    item = r.json()
    assert item["status"] == "已落库"
    assert item["loaded"] == 3
    assert item["unlanded_metrics"] == []


def test_shape_deviation_blocked(authed):
    _prepare_clean(authed, 1)  # 说明定的是宽表
    r = authed.post("/api/pipelines/1/load", json={"shape": "narrow"})
    assert r.status_code == 400
    assert "落库形态写歪了" in r.json()["error"]
    # 详情可读形态冲突记录
    detail = authed.get("/api/pipelines/1").json()
    conflicts = [e for e in detail["events"] if e["kind"] == "形态冲突"]
    assert conflicts, "应记录形态冲突"
    assert conflicts[0]["expected_shape"] == "wide"
    assert conflicts[0]["actual_shape"] == "narrow"
    # 状态未被污染，仍可正确落库
    ok = authed.post("/api/pipelines/1/load", json={"shape": "wide"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "已落库"


def test_not_cleaned_cannot_load(authed):
    # 未抽取
    r = authed.post("/api/pipelines/1/load", json={"shape": "wide"})
    assert r.status_code == 400
    assert "未清洗不能落库" in r.json()["error"]
    # 已抽取未清洗
    authed.post("/api/pipelines/1/extract")
    r = authed.post("/api/pipelines/1/load", json={"shape": "wide"})
    assert r.status_code == 400
    assert "未清洗不能落库" in r.json()["error"]


def test_shape_locked_while_loading(authed):
    _prepare_clean(authed, 1)
    _set_status(1, "落库中", actor="bob")
    r = authed.put("/api/pipelines/1/shape", json={
        "shape": "narrow", "spec_name": "改口方案", "metrics": ["gmv"],
    })
    assert r.status_code == 409
    assert "落库进行中禁止改形态" in r.json()["error"]


def test_no_change_of_mind_same_day(authed, monkeypatch):
    # 种子说明决定日为今天：同日改形态被拦
    r = authed.put("/api/pipelines/1/shape", json={
        "shape": "narrow", "spec_name": "改口方案", "metrics": ["gmv"],
    })
    assert r.status_code == 409
    assert "本日禁止改口" in r.json()["error"]
    # 同日同形态改说明名/指标：允许（不算改口）
    r = authed.put("/api/pipelines/1/shape", json={
        "shape": "wide", "spec_name": "订单宽表方案v2",
        "metrics": ["gmv", "order_cnt", "buyer_cnt"],
    })
    assert r.status_code == 200
    assert r.json()["spec"]["spec_name"] == "订单宽表方案v2"
    # 次日可改
    import app.db as db
    monkeypatch.setattr(db, "today_str", lambda: "2099-01-01")
    r = authed.put("/api/pipelines/1/shape", json={
        "shape": "narrow", "spec_name": "订单窄表方案", "metrics": ["gmv", "order_cnt"],
    })
    assert r.status_code == 200
    assert r.json()["shape"] == "narrow"
    assert r.json()["spec"]["decided_on"] == "2099-01-01"


def test_race_second_gets_contention_http(authed):
    _prepare_clean(authed, 1)
    _set_status(1, "落库中", actor="bob")  # bob 先到，落库进行中
    r = authed.post("/api/pipelines/1/load", json={"shape": "wide"})
    assert r.status_code == 409
    assert "形态争用" in r.json()["error"]
    # 抢占记录在详情与预览都留痕
    detail = authed.get("/api/pipelines/1").json()
    races = [e for e in detail["events"] if e["kind"] == "形态争用"]
    assert races and "bob" in races[0]["detail"]
    preview = authed.get("/api/pipelines/1/preview").json()
    assert any(e["kind"] == "形态争用" for e in preview["events"])


def test_race_first_wins_concurrent(authed):
    _prepare_clean(authed, 1)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name):
        barrier.wait()
        with get_db() as conn:
            results[name] = run_load(conn, 1, actor=name, req_shape="wide")

    t1 = threading.Thread(target=worker, args=("alice",))
    t2 = threading.Thread(target=worker, args=("bob",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    oks = [n for n, r in results.items() if r["ok"]]
    denied = [n for n, r in results.items() if not r["ok"]]
    assert len(oks) == 1, f"仅先到者成功：{results}"
    assert len(denied) == 1
    loser = results[denied[0]]
    assert loser["status"] == 409
    assert "形态争用" in loser["error"]
    with get_db() as conn:
        item = get_pipeline(conn, 1)
    assert item["status"] == "已落库"
    assert any(e["kind"] == "形态争用" for e in item["events"])
