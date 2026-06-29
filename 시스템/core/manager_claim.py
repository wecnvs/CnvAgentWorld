# -*- coding: utf-8 -*-
"""공간관리 실행 claim/lease/fencing 관리."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from .paths import SPACES
from .spaces import MANAGER_DIRNAME

MANAGER_LEASE_SECONDS = 20 * 60
OWNER_BOOT_ID = f"boot_{uuid4().hex[:12]}"
OWNER_ID = f"space_manager:{os.getpid()}:{OWNER_BOOT_ID}"

_LOCKS: dict[str, Lock] = {}
_LOCKS_GUARD = Lock()


def _manager_dir(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME


def _claim_path(space: str) -> Path:
    return _manager_dir(space) / "manager_claim.json"


def _history_path(space: str) -> Path:
    return _manager_dir(space) / "manager_claim_history.jsonl"


def _lock_path(space: str) -> Path:
    return _manager_dir(space) / ".manager_claim.lock"


def _inprocess_lock(space: str) -> Lock:
    key = str(_manager_dir(space).resolve())
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = Lock()
        return _LOCKS[key]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_claim(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {
            "state": "corrupt",
            "claim_file_corrupt": True,
            "claim_token": "corrupt_claim_file",
            "manager_redrive_required": True,
        }
    except Exception:
        return {
            "state": "corrupt",
            "claim_file_corrupt": True,
            "claim_token": "corrupt_claim_file",
            "manager_redrive_required": True,
        }


def _write_claim(path: Path, data: dict):
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_history(space: str, event: dict):
    path = _history_path(space)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at_utc": _iso(_now()), **event}, ensure_ascii=False) + "\n")


def _active(claim: dict, now: datetime | None = None) -> bool:
    now = now or _now()
    if claim.get("state") != "running":
        return False
    expires = _parse_iso(claim.get("lease_expires_at_utc"))
    return bool(expires and expires > now)


def _expired_running(claim: dict, now: datetime | None = None) -> bool:
    now = now or _now()
    if claim.get("state") != "running":
        return False
    expires = _parse_iso(claim.get("lease_expires_at_utc"))
    return bool(expires and expires <= now)


def _public_claim(claim: dict) -> dict:
    if not claim:
        return {"active": False}
    return {
        "active": _active(claim),
        "state": claim.get("state", ""),
        "claim_token": claim.get("claim_token", ""),
        "fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "lease_started_at_utc": claim.get("lease_started_at_utc", ""),
        "lease_expires_at_utc": claim.get("lease_expires_at_utc", ""),
        "lease_duration_sec": claim.get("lease_duration_sec", 0),
        "read_until_event_seq": claim.get("read_until_event_seq"),
        "source_event": claim.get("source_event", ""),
        "source_event_seq": claim.get("source_event_seq"),
        "intent_id": claim.get("intent_id", ""),
        "conversation_thread_id": claim.get("conversation_thread_id", ""),
        "room_generation": claim.get("room_generation"),
        "source_message_id": claim.get("source_message_id", ""),
        "manager_redrive_required": bool(claim.get("manager_redrive_required")),
        "redrive_count": len(claim.get("redrive_events") or []),
        "redrive_events": list(claim.get("redrive_events") or [])[-20:],
        "claim_seq": claim.get("claim_seq", 0),
        "released_at_utc": claim.get("released_at_utc", ""),
        "release_outcome": claim.get("release_outcome", ""),
        "claim_file_corrupt": bool(claim.get("claim_file_corrupt")),
    }


def _with_lock(space: str, fn):
    manager = _manager_dir(space)
    manager.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(space)
    lock.touch(exist_ok=True)
    with _inprocess_lock(space):
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                path = _claim_path(space)
                claim = _load_claim(path)
                result, new_claim = fn(claim, _now())
                if new_claim is not None:
                    _write_claim(path, new_claim)
                return result
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def snapshot(space: str) -> dict:
    claim = _load_claim(_claim_path(space))
    return _public_claim(claim)


def expire_foreign_boot_claim(space: str) -> dict:
    """부팅 복구용: 다른 프로세스(boot_id)가 잡고 있던 running claim을 만료 처리한다.

    서버가 새로 떴다는 것은 이전 프로세스가 죽었다는 뜻이다. 죽은 프로세스의 claim은
    lease가 아직 안 끝났어도(최대 20분) 더는 진행되지 않으므로, 새 프로세스에서만
    안전하게 만료시켜 다음 acquire가 인수(takeover)할 수 있게 한다.
    같은 boot_id(현 프로세스)의 claim은 절대 건드리지 않는다.
    """
    def mutate(claim: dict, now: datetime):
        if not claim or not claim.get("claim_token") or claim.get("claim_file_corrupt"):
            return {"expired": False, "reason": "no_claim"}, None
        if claim.get("owner_boot_id") == OWNER_BOOT_ID:
            return {"expired": False, "reason": "same_boot"}, None
        if claim.get("state") != "running":
            return {"expired": False, "reason": "not_running"}, None
        new_claim = {
            **claim,
            "state": "expired",
            "expired_at_utc": _iso(now),
            "release_outcome": "boot_recovery_foreign_claim",
        }
        _append_history(space, {
            "type": "manager_claim_boot_recovered",
            "claim_token": claim.get("claim_token"),
            "previous_owner_boot_id": claim.get("owner_boot_id", ""),
        })
        return {"expired": True, "claim": _public_claim(new_claim)}, new_claim

    return _with_lock(space, mutate)


def mark_redrive(space: str, event: str, event_seq: int | None = None, context: dict | None = None) -> dict:
    def mutate(claim: dict, now: datetime):
        if claim.get("claim_file_corrupt"):
            return {"marked": False, "active": False, "claim": _public_claim(claim), "corrupt": True}, None
        if not _active(claim, now):
            return {"marked": False, "active": False, "claim": _public_claim(claim)}, None
        redrive_events = list(claim.get("redrive_events") or [])
        redrive_events.append({
            "event": event,
            "event_seq": event_seq,
            "context": context or {},
            "marked_at_utc": _iso(now),
        })
        claim = {
            **claim,
            "manager_redrive_required": True,
            "redrive_events": redrive_events[-20:],
            "last_redrive_at_utc": _iso(now),
            "last_redrive_event": event,
            "last_redrive_event_seq": event_seq,
            "last_redrive_context": context or {},
        }
        _append_history(space, {
            "type": "manager_redrive_required",
            "claim_token": claim.get("claim_token"),
            "event_seq": event_seq,
        })
        return {"marked": True, "active": True, "claim": _public_claim(claim)}, claim

    return _with_lock(space, mutate)


def acquire(space: str, event: str, read_until_event_seq: int | None = None, context: dict | None = None) -> dict:
    context = context or {}

    def mutate(claim: dict, now: datetime):
        if claim.get("claim_file_corrupt"):
            _append_history(space, {
                "type": "manager_claim_corrupt_blocked",
                "event_seq": read_until_event_seq,
            })
            return {"acquired": False, "busy": True, "corrupt": True, "claim": _public_claim(claim)}, None
        if _active(claim, now):
            redrive_events = list(claim.get("redrive_events") or [])
            redrive_events.append({
                "event": event,
                "event_seq": read_until_event_seq,
                "context": context,
                "marked_at_utc": _iso(now),
            })
            claim = {
                **claim,
                "manager_redrive_required": True,
                "redrive_events": redrive_events[-20:],
                "last_redrive_at_utc": _iso(now),
                "last_redrive_event": event,
                "last_redrive_event_seq": read_until_event_seq,
                "last_redrive_context": context,
            }
            _append_history(space, {
                "type": "manager_claim_busy",
                "claim_token": claim.get("claim_token"),
                "event_seq": read_until_event_seq,
            })
            return {"acquired": False, "busy": True, "claim": _public_claim(claim)}, claim

        previous = claim
        if _expired_running(claim, now):
            claim = {
                **claim,
                "state": "expired",
                "expired_at_utc": _iso(now),
                "release_outcome": "lease_expired",
            }
            _append_history(space, {
                "type": "manager_claim_expired",
                "claim_token": claim.get("claim_token"),
                "event_seq": read_until_event_seq,
            })

        claim_seq = int(claim.get("claim_seq") or 0) + 1
        started = now
        expires = now + timedelta(seconds=MANAGER_LEASE_SECONDS)
        new_claim = {
            "state": "running",
            "claim_seq": claim_seq,
            "claim_token": f"manager_claim_{claim_seq:08d}_{uuid4().hex[:8]}",
            "fencing_token": f"manager_fence_{claim_seq:08d}_{uuid4().hex[:8]}",
            "owner_id": OWNER_ID,
            "owner_boot_id": OWNER_BOOT_ID,
            "lease_started_at_utc": _iso(started),
            "lease_expires_at_utc": _iso(expires),
            "lease_duration_sec": MANAGER_LEASE_SECONDS,
            "source_event": event,
            "source_event_seq": read_until_event_seq,
            "read_until_event_seq": read_until_event_seq,
            "intent_id": context.get("intent_id", ""),
            "conversation_thread_id": context.get("conversation_thread_id", ""),
            "room_generation": context.get("room_generation"),
            "source_message_id": context.get("source_message_id", ""),
            "manager_redrive_required": False,
            "redrive_events": [],
            "previous_claim_token": previous.get("claim_token", ""),
        }
        _append_history(space, {
            "type": "manager_claim_acquired",
            "claim_token": new_claim["claim_token"],
            "fencing_token": new_claim["fencing_token"],
            "event_seq": read_until_event_seq,
        })
        return {"acquired": True, "busy": False, "claim": _public_claim(new_claim)}, new_claim

    return _with_lock(space, mutate)


def is_current(space: str, claim: dict) -> bool:
    current = _load_claim(_claim_path(space))
    return (
        current.get("state") == "running"
        and current.get("claim_token") == claim.get("claim_token")
        and current.get("fencing_token") == claim.get("fencing_token")
        and current.get("owner_boot_id") == claim.get("owner_boot_id")
        and _active(current)
    )


def release(space: str, claim: dict, outcome: str) -> dict:
    def mutate(current: dict, now: datetime):
        if (
            current.get("claim_token") != claim.get("claim_token")
            or current.get("fencing_token") != claim.get("fencing_token")
            or current.get("owner_boot_id") != claim.get("owner_boot_id")
        ):
            _append_history(space, {
                "type": "manager_claim_stale_release_rejected",
                "claim_token": claim.get("claim_token"),
                "current_claim_token": current.get("claim_token"),
            })
            return {
                "released": False,
                "stale": True,
                "redrive_required": False,
                "claim": _public_claim(current),
            }, None

        redrive_required = bool(current.get("manager_redrive_required"))
        released = {
            **current,
            "state": "released",
            "released_at_utc": _iso(now),
            "release_outcome": outcome,
        }
        _append_history(space, {
            "type": "manager_claim_released",
            "claim_token": current.get("claim_token"),
            "outcome": outcome,
            "redrive_required": redrive_required,
        })
        return {
            "released": True,
            "stale": False,
            "redrive_required": redrive_required,
            "redrive_event": current.get("last_redrive_event", ""),
            "redrive_event_seq": current.get("last_redrive_event_seq"),
            "redrive_context": current.get("last_redrive_context") or {},
            "redrive_events": list(current.get("redrive_events") or [])[-20:],
            "claim": _public_claim(released),
        }, released

    return _with_lock(space, mutate)
