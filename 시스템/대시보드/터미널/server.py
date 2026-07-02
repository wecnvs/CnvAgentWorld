# -*- coding: utf-8 -*-
"""CnvAgentWorld 터미널 서버(:8687)."""
import argparse
import asyncio
import os
import platform

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

import session_registry as registry
from config import STATIC_DIR, WORKSPACE_ROOT
from shells import default_shell, shell_candidates
from terminal_session import set_main_loop
from uploads import save_raw_upload

import auth_guard

app = FastAPI(title="CnvAgentWorld Terminal Server")
# allow_credentials=True + "*" 조합은 금지 패턴(자격증명 실린 교차출처 요청 허용) — 쿠키는
# 같은 출처(iframe 내 직접 탐색)로만 흐르면 되므로 credentials 없이 개방한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AuthGuard(BaseHTTPMiddleware):
    """옵트인 접근 토큰 가드 — 토큰 파일이 있을 때만 활성, 루프백 통과 (auth_guard.py)."""

    async def dispatch(self, request, call_next):
        ok, set_cookie = auth_guard.check_request(request)
        if not ok:
            return JSONResponse(status_code=401, content={"detail": "인증 필요 — ?auth=<토큰>으로 1회 접속"})
        resp = await call_next(request)
        if set_cookie:
            resp.set_cookie(auth_guard.COOKIE_NAME, auth_guard.load_token(),
                            max_age=365 * 24 * 3600, httponly=True, samesite="lax")
        return resp


app.add_middleware(AuthGuard)


class NoCache(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        return resp


app.add_middleware(NoCache)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup():
    set_main_loop(asyncio.get_event_loop())


@app.get("/")
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "os": platform.system(),
        "workspace": WORKSPACE_ROOT,
        "sessions": registry.count(),
    }


@app.get("/api/shells")
def api_shells():
    return {"default": default_shell(), "shells": shell_candidates()}


@app.get("/api/sessions")
def api_list():
    return {"sessions": registry.list_sessions(), "workspace": WORKSPACE_ROOT}


@app.post("/api/sessions")
async def api_create(payload: dict = None):
    payload = payload or {}
    shell = (payload.get("shell") or "").strip() or default_shell()
    cwd = (payload.get("cwd") or "").strip() or WORKSPACE_ROOT
    if not os.path.isdir(cwd):
        cwd = WORKSPACE_ROOT
    cols = int(payload.get("cols") or 120)
    rows = int(payload.get("rows") or 30)
    title = (payload.get("title") or "").strip()
    try:
        loop = asyncio.get_event_loop()
        sess = await loop.run_in_executor(None, registry.add_session, shell, cwd, cols, rows, title)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"세션 생성 실패: {exc}") from exc
    return sess.info()


@app.delete("/api/sessions/{sid}")
def api_kill(sid: str):
    sess = registry.pop_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="세션 없음")
    sess.kill()
    return {"ok": True}


@app.post("/api/sessions/{sid}/rename")
def api_rename(sid: str, payload: dict):
    sess = registry.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="세션 없음")
    title = (payload.get("title") or "").strip()
    if title:
        sess.title = title
    return sess.info()


@app.post("/api/upload")
async def api_upload(request: Request):
    try:
        data, status = await save_raw_upload(request)
        return JSONResponse(data, status_code=status) if status != 200 else data
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.websocket("/ws/{sid}")
async def ws_attach(ws: WebSocket, sid: str):
    # HTTP 미들웨어는 WS 핸드셰이크를 못 거르므로 여기서 직접 검사(쿠키/쿼리, 루프백 통과).
    ok, _ = auth_guard.check_request(ws)
    if not ok:
        await ws.close(code=4401)
        return
    await ws.accept()
    sess = registry.get_session(sid)
    if not sess:
        await ws.send_json({"type": "error", "message": "세션 없음"})
        await ws.close()
        return

    q, snapshot = sess.subscribe()
    if snapshot:
        await ws.send_json({"type": "output", "data": snapshot.decode("utf-8", "replace")})

    async def pump_out():
        try:
            while True:
                item = await q.get()
                if isinstance(item, dict) and item.get("__exit__"):
                    await ws.send_json({"type": "exit"})
                    break
                await ws.send_json({"type": "output", "data": item})
        except Exception:
            pass

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "input":
                sess.write(msg.get("data", ""))
            elif kind == "resize":
                sess.resize(int(msg.get("cols", 120)), int(msg.get("rows", 30)))
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sess.unsubscribe(q)
        out_task.cancel()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8687)
    args = parser.parse_args()
    print(f"[terminal_server] OS={platform.system()} workspace={WORKSPACE_ROOT}")
    print(f"[terminal_server] http://{args.host}:{args.port}  (독립 데몬, 8686 와 무관 / tailscale serve 로 외부 노출)")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
