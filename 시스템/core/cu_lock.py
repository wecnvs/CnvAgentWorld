# -*- coding: utf-8 -*-
"""컴퓨터유즈 락 — 세션(타깃) 단위 상호배제.

같은 세션(target)은 **1 액터만**(하드 거부 — 큐잉 아님), 서로 다른 세션은 병렬.
→ 맥계정/도윤호스트/도윤VM 등 서로 다른 세션의 동시 컴퓨터유즈는 충돌하지 않으나,
   같은 세션에 둘이 들어오면 즉시 거부한다. (대표 요구: "다른 에이전트가 CU 중이면 불가능")

brainbase `agent-dashboard-v2`의 per-target cu_lock을 우리 대시보드(모듈식 라우터)로 포팅.
모놀리식의 asyncio.Lock+loop-attr 대신 threading.Lock으로 단순화(동기 라우터에서도 안전).
"""
from __future__ import annotations

import os
import threading
import time

DEFAULT_TTL = int(os.environ.get("CU_LOCK_TTL", "600"))      # 기본 임대(초). heartbeat로 연장.
MAX_TTL = int(os.environ.get("CU_LOCK_MAX_TTL", "3600"))

_locks: dict = {}          # target -> {holder_id, holder_name, since, expires_at, note}
_mutex = threading.Lock()


def norm_target(t) -> str:
    t = (str(t).strip().lower() if t is not None else "") or "host"
    return t


def _free_if_expired(target, now) -> None:
    st = _locks.get(target)
    if st and st.get("holder_id") and now >= st.get("expires_at", 0):
        _locks.pop(target, None)


def acquire(agent_id, target, ttl=None, note="", agent_name="") -> dict:
    """세션 락 획득. 같은 target을 다른 액터가 보유 중이면 busy(하드 거부)."""
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return {"acquired": False, "error": "agent_id required"}
    target = norm_target(target)
    name = (agent_name or agent_id).strip()
    try:
        ttl = int(ttl) if ttl is not None else DEFAULT_TTL
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    ttl = max(30, min(ttl, MAX_TTL))
    now = time.time()
    with _mutex:
        _free_if_expired(target, now)
        st = _locks.get(target)
        if st and st.get("holder_id") and st["holder_id"] != agent_id:
            return {
                "acquired": False, "busy": True, "target": target,
                "holder_id": st["holder_id"], "holder_name": st["holder_name"],
                "held_sec": int(now - st["since"]), "expires_in": int(st["expires_at"] - now),
                "note": st.get("note", ""),
                "message": f"'{target}' 세션은 지금 '{st['holder_name']}'가 컴퓨터유즈 중이라 "
                           f"불가합니다(최대 {int(st['expires_at']-now)}초 후 자동만료). "
                           f"다른 세션이면 병렬로 쓸 수 있습니다.",
            }
        first = (st["since"] if st else 0) or now
        _locks[target] = {"holder_id": agent_id, "holder_name": name, "since": first,
                          "expires_at": now + ttl,
                          "note": note or (st.get("note") if st else "") or ""}
        return {"acquired": True, "target": target, "holder_id": agent_id, "holder_name": name,
                "expires_at": _locks[target]["expires_at"], "ttl": ttl}


def heartbeat(agent_id, target, ttl=None) -> dict:
    """임대 연장(긴 작업 중 주기 호출). 보유자만."""
    agent_id = (agent_id or "").strip()
    target = norm_target(target)
    try:
        ttl = int(ttl) if ttl is not None else DEFAULT_TTL
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    ttl = max(30, min(ttl, MAX_TTL))
    now = time.time()
    with _mutex:
        _free_if_expired(target, now)
        st = _locks.get(target)
        if not st or st.get("holder_id") != agent_id:
            return {"ok": False, "target": target,
                    "message": "보유한 락이 아닙니다(만료/타인 보유). 다시 acquire 하세요."}
        st["expires_at"] = now + ttl
        return {"ok": True, "target": target, "expires_at": st["expires_at"]}


def release(agent_id, target) -> dict:
    """락 해제(작업 끝나면 반드시). 보유자만."""
    agent_id = (agent_id or "").strip()
    target = norm_target(target)
    with _mutex:
        st = _locks.get(target)
        if st and st.get("holder_id") and st["holder_id"] != agent_id:
            return {"ok": False, "target": target, "message": "보유한 락이 아닙니다."}
        _locks.pop(target, None)
        return {"ok": True, "released": True, "target": target}


def status(target="") -> dict:
    """락 현황. target 주면 그 세션만, 없으면 전체(누가 어느 세션 CU중인지)."""
    now = time.time()
    with _mutex:
        for t in list(_locks.keys()):
            _free_if_expired(t, now)
        if target:
            tt = norm_target(target)
            st = _locks.get(tt)
            return {"target": tt, "locked": bool(st),
                    "holder_id": st["holder_id"] if st else None,
                    "holder_name": st["holder_name"] if st else None,
                    "held_sec": int(now - st["since"]) if st else 0,
                    "expires_in": int(st["expires_at"] - now) if st else 0,
                    "note": st.get("note", "") if st else ""}
        return {"locks": {t: {"holder_id": s["holder_id"], "holder_name": s["holder_name"],
                              "held_sec": int(now - s["since"]), "expires_in": int(s["expires_at"] - now),
                              "note": s.get("note", "")} for t, s in _locks.items()},
                "active_count": len(_locks)}


def holds(agent_id, target) -> bool:
    """이 액터가 지금 그 세션 락을 (만료 전) 보유 중인가 — 입력 게이트용."""
    agent_id = (agent_id or "").strip()
    target = norm_target(target)
    now = time.time()
    with _mutex:
        _free_if_expired(target, now)
        st = _locks.get(target)
        return bool(st and st.get("holder_id") == agent_id)
