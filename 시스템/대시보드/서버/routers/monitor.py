# -*- coding: utf-8 -*-
"""단톡방 감시모드 API. core.watch로 세션 스펙을 만들어 8687 터미널 서버에 인터랙티브 세션을 띄운다.

8687은 독립 데몬(launchd 상시가동). 8686은 stdlib urllib로 127.0.0.1:8687에 세션 생성만 위임하고
세션 ID를 돌려준다(추가 의존성 없음). 브라우저는 같은 호스트의 8687 attach 페이지를 iframe으로 붙인다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import core.watch as watch

router = APIRouter(prefix="/api/spaces", tags=["monitor"])

TERMINAL_PORT = 8687
TERMINAL_BASE = f"http://127.0.0.1:{TERMINAL_PORT}"


class WatchLaunch(BaseModel):
    engine: str | None = None
    model: str | None = None


@router.post("/{space}/watch")
def launch_watch(space: str, body: WatchLaunch):
    """관리자에이전트를 '이 방' 감시 컨텍스트로 8687 인터랙티브 세션으로 띄운다."""
    try:
        spec = watch.build_watch_session(space, body.engine, body.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    payload = json.dumps({
        "shell": spec["shell"],
        "cwd": spec["cwd"],
        "cols": spec["cols"],
        "rows": spec["rows"],
        "title": spec["title"],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{TERMINAL_BASE}/api/sessions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            session = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"터미널 서버(:{TERMINAL_PORT}) 연결 실패 — 감시 세션을 띄울 수 없습니다: {exc}",
        )
    return {
        "ok": True,
        "session_id": session.get("id"),
        "engine": spec["engine"],
        "model": spec["model"],
        "title": spec["title"],
        "terminal_port": TERMINAL_PORT,
    }
