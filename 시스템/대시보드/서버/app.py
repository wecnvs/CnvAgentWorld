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
from routers import people, spaces, meta, files, watch, runtime, cases, monitor, apps, cu    # noqa: E402

app = FastAPI(title="CnvAgentWorld")
app.include_router(meta.router)
app.include_router(people.router)
app.include_router(spaces.router)
app.include_router(files.router)
app.include_router(watch.router)
app.include_router(runtime.router)
app.include_router(cases.router)
app.include_router(monitor.router)
app.include_router(apps.router)
app.include_router(cu.router)


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


# 완료·미게시 작업 결과를 주기적으로 방에 회수·공개하는 백스톱 주기(초). (설계_대화작업분리 Phase B)
REFLOW_BACKSTOP_INTERVAL_SEC = 30


@app.on_event("startup")
def _reflow_backstop():
    """완료됐지만 방에 안 뜬 (비동기) 작업 결과를 주기적으로 회수·공개하는 백스톱.

    reflow는 대표 메시지·작업완료(run_work finally)에만 트리거되는데, 작업완료 시점의 reflow가 매니저
    claim 경합('manager claim busy')이나 프로세스 하드킬로 실패하면 결과가 release_queue에 pending으로
    남아 '대표의 다음 발화'까지 방에 안 뜬다(실증 2026-06-29: win 이식 완료 후 ~11분 침묵). 이 루프가 그
    공백을 메워, 외부 트리거 없이도 미게시 완료를 최대 ~30초 내 자동 공개한다.
    reflow_all_spaces는 멱등·예외안전이라 빈 큐에서는 사실상 no-op다(부하 무시 가능)."""
    import threading
    import time
    import core.room_manager as room_manager

    def run():
        while True:
            try:
                time.sleep(REFLOW_BACKSTOP_INTERVAL_SEC)
                # (1) 자동복구 reaper: 엔진 타임아웃/워커 하드킬로 '완료했는데 finalize 안 돼 active에 박제된'
                #     작업을 상태.json/체크포인트 근거로 강제 finalize한다(release 생성). 신선 heartbeat 작업은
                #     건드리지 않는다. reflow보다 먼저 돌려, 이번에 푼 release를 같은 주기에 공개·배포한다.
                room_manager.reap_stale_tasks_all_spaces()
                # (2) 완료·미게시 결과를 방으로 회수·공개.
                room_manager.reflow_all_spaces()
            except Exception:
                pass

    threading.Thread(target=run, name="reflow-backstop", daemon=True).start()


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
