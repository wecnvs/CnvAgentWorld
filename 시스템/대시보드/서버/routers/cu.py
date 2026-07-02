# -*- coding: utf-8 -*-
"""컴퓨터유즈/원격제어 API — 세션별 락 + 화면(스크린샷)·입력 프록시.

- **락**: 같은 세션(타깃)은 1 액터만(하드 거부), 다른 세션은 병렬. (대표 요구)
- **화면 보기**(/view/screenshot·/view/status)는 누구나(읽기).
- **입력**(/view/input: 클릭/타이핑/키)은 **그 타깃 락 보유자만** — 미보유면 409.

연결정보(host/port/cred)는 core가 서버 내부에서만 쓰고, 브라우저엔 타깃 별칭만 오간다(law §7).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import core.cu_lock as cu_lock
import core.cu_remote as cu_remote

router = APIRouter(prefix="/api/cu", tags=["cu"])


# ── 락 ──────────────────────────────────────────────────────
class LockReq(BaseModel):
    agent_id: str
    target: str = "host"
    ttl: int | None = None
    note: str = ""
    agent_name: str = ""


@router.post("/acquire")
def acquire(body: LockReq):
    res = cu_lock.acquire(body.agent_id, body.target, body.ttl, body.note, body.agent_name)
    if not res.get("acquired"):
        return JSONResponse(res, status_code=409 if res.get("busy") else 400)
    return res


@router.post("/heartbeat")
def heartbeat(body: LockReq):
    res = cu_lock.heartbeat(body.agent_id, body.target, body.ttl)
    return JSONResponse(res, status_code=409) if not res.get("ok") else res


@router.post("/release")
def release(body: LockReq):
    res = cu_lock.release(body.agent_id, body.target)
    return JSONResponse(res, status_code=409) if not res.get("ok") else res


@router.get("/status")
def status(target: str = ""):
    return cu_lock.status(target)


# ── 화면 보기 / 입력 ────────────────────────────────────────
class InputReq(BaseModel):
    agent_id: str
    target: str
    action: str                 # move | click | scroll | type | key
    x: int | None = None
    y: int | None = None
    button: str | None = None
    double: bool | None = None
    amount: int | None = None
    text: str | None = None
    keys: str | None = None


@router.get("/view/status")
def view_status(target: str):
    try:
        return cu_remote.view_status(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"원격 상태 실패: {exc}")


@router.get("/view/screenshot")
def view_screenshot(target: str, w: int = 1280, q: int = 70):
    try:
        data, ctype = cu_remote.screenshot(target, max_width=w, quality=q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"원격 스크린샷 실패: {exc}")
    return Response(content=data, media_type=ctype, headers={"Cache-Control": "no-store"})


@router.post("/view/input")
def view_input(body: InputReq):
    # 입력은 그 세션 락 보유자만 — 미보유면 하드 거부(다른 액터가 사용 중이거나 미획득).
    if not cu_lock.holds(body.agent_id, body.target):
        st = cu_lock.status(body.target)
        return JSONResponse({
            "ok": False, "denied": True, "reason": "lock_not_held",
            "target": cu_lock.norm_target(body.target),
            "holder_name": st.get("holder_name"),
            "message": "이 세션의 컴퓨터유즈 락을 보유하고 있지 않습니다 — 먼저 acquire 하세요"
                       + (f" (현재 '{st.get('holder_name')}'가 사용 중)" if st.get("holder_name") else "."),
        }, status_code=409)
    params = {}
    for k in ("x", "y", "button", "double", "amount", "text", "keys"):
        v = getattr(body, k)
        if v is not None:
            params[k] = v
    try:
        out = cu_remote.send_input(body.target, body.action, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"원격 입력 실패: {exc}")
    return {"ok": True, "result": out}
