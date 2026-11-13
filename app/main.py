from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import (
    build_preview,
    get_db,
    get_pipeline,
    init_db,
    list_pipelines,
    list_specs,
    run_clean,
    run_extract,
    run_load,
    save_shape,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-etl-session")

app = FastAPI(title="cc-etl-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("etl_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _result_response(result: dict, ok_key: str = "item"):
    """把 db 层结果映射成 HTTP 响应；失败时带状态码与错误文案。"""
    if result.get("ok"):
        return result[ok_key]
    return JSONResponse(
        {"ok": False, "error": result["error"], "item": result.get("item")},
        status_code=result["status"],
    )


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "etl_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("etl_session")
    return resp


@app.get("/api/me")
def api_me(user: str = Depends(require_user)):
    return {"user": user, "lab": Config.LAB_NAME, "domain": "数据工程"}


@app.get("/api/pipelines")
def api_pipelines(user: str = Depends(require_user)):
    with get_db() as conn:
        return {"items": list_pipelines(conn)}


@app.get("/api/shape-specs")
def api_shape_specs(user: str = Depends(require_user)):
    """落库方案册：各管道形态说明一览。"""
    with get_db() as conn:
        return {"items": list_specs(conn)}


@app.get("/api/pipelines/{pipeline_id}")
def api_pipeline_detail(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = get_pipeline(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.get("/api/pipelines/{pipeline_id}/preview")
def api_pipeline_preview(pipeline_id: int, user: str = Depends(require_user)):
    """形态预览：服务端定形态，前端对照渲染宽表/窄表。"""
    with get_db() as conn:
        item = build_preview(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.put("/api/pipelines/{pipeline_id}/shape")
async def api_save_shape(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    """形态设置：定一种形态写进说明；落库中禁改，本日禁止改口。"""
    body = await request.json()
    shape = str(body.get("shape", "")).strip()
    spec_name = str(body.get("spec_name", "")).strip()
    metrics = body.get("metrics") or []
    required_cols = body.get("required_cols") or []
    if isinstance(metrics, str):
        metrics = [m for m in metrics.replace("，", ",").split(",")]
    if isinstance(required_cols, str):
        required_cols = [c for c in required_cols.replace("，", ",").split(",")]
    with get_db() as conn:
        result = save_shape(conn, pipeline_id, user, shape, spec_name, metrics, required_cols)
    if result is None:
        raise HTTPException(status_code=404, detail="管道不存在")
    return _result_response(result)


@app.post("/api/pipelines/{pipeline_id}/extract")
def api_extract(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = run_extract(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/clean")
def api_clean(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = run_clean(conn, pipeline_id)
    if not item:
        raise HTTPException(status_code=404, detail="管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/load")
async def api_load(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    """落库：须先清洗通过；形态偏离说明即拦；同管道仅先到者成功。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    req_shape = body.get("shape") if isinstance(body, dict) else None
    if req_shape is not None:
        req_shape = str(req_shape).strip() or None
    with get_db() as conn:
        result = run_load(conn, pipeline_id, actor=user, req_shape=req_shape)
    if result is None:
        raise HTTPException(status_code=404, detail="管道不存在")
    return _result_response(result)


def create_app() -> FastAPI:
    return app
