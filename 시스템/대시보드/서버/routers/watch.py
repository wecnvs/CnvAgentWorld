# -*- coding: utf-8 -*-
"""파일 변경 감시 SSE. watchfiles로 루트폴더를 보고, 변경 순간에만 푸시한다.

상시 폴링을 없애기 위한 이벤트 기반 채널 — 변경이 없으면 트래픽도 없다.
보낸 데이터는 '목록이 바뀐 폴더'들의 루트폴더 기준 상대경로 배열이다.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from watchfiles import awatch
from watchfiles.filters import DefaultFilter
import core.files as files

router = APIRouter(prefix="/api", tags=["watch"])


def _affected_dirs(changes):
    """변경된 절대경로들 → 목록이 바뀐 '폴더'(부모)들의 루트기준 상대경로 집합."""
    dirs = set()
    for _change, p in changes:
        try:
            rel = Path(p).resolve().relative_to(files.ROOT)
        except ValueError:
            continue                      # 루트폴더 밖이면 무시
        parent = rel.parent
        dirs.add("" if str(parent) == "." else str(parent))
    return sorted(dirs)


@router.get("/watch")
async def watch(request: Request):
    async def gen():
        # yield_on_timeout: 변경이 없어도 주기적으로 깨어나 하트비트·연결끊김을 확인한다.
        async for changes in awatch(files.ROOT, watch_filter=DefaultFilter(),
                                    yield_on_timeout=True):
            if await request.is_disconnected():
                break
            if not changes:
                yield ": ping\n\n"        # 하트비트(프록시 idle 끊김 방지). EventSource는 무시한다.
                continue
            dirs = _affected_dirs(changes)
            if dirs:
                yield f"data: {json.dumps(dirs, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
