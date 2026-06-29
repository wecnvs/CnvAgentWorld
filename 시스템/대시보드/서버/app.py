# -*- coding: utf-8 -*-
"""대시보드 진입점. 라우터를 엮고 정적 프론트엔드를 서빙만 한다 (얇게 유지)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 시스템/대시보드/서버
SYS = HERE.parent.parent                          # 시스템 (core·엔진·대시보드 묶음)
sys.path.insert(0, str(SYS))                     # core 임포트용
sys.path.insert(0, str(HERE))                    # routers 임포트용

from fastapi import FastAPI                       # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles       # noqa: E402
from routers import people, spaces, meta, files, watch, runtime, cases    # noqa: E402

app = FastAPI(title="CnvAgentWorld")
app.include_router(meta.router)
app.include_router(people.router)
app.include_router(spaces.router)
app.include_router(files.router)
app.include_router(watch.router)
app.include_router(runtime.router)
app.include_router(cases.router)


@app.on_event("startup")
def _recover_stalled_on_startup():
    """서버가 진행 중 tick을 끊고 재시작되면 멈춘 공간을 부팅 시 복구한다(백그라운드, 비차단).

    새 프로세스이므로 이전(죽은) 프로세스의 claim은 안전하게 만료시킬 수 있다.
    claude를 spawn할 수 있어 startup을 막지 않도록 별도 스레드에서 돌린다."""
    import threading
    import core.room_manager as room_manager

    def run():
        try:
            room_manager.recover_stalled_spaces()
        except Exception:
            pass

    threading.Thread(target=run, name="recover-stalled-spaces", daemon=True).start()


@app.exception_handler(ValueError)
async def _value_error(_request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.middleware("http")
async def _no_store_dashboard(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


WEB = SYS / "대시보드" / "웹"
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB / "index.html"))
