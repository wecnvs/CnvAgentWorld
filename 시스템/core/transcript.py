# -*- coding: utf-8 -*-
"""대화 기록. 공간 정본 <-> 에이전트별 자리에 동일 줄을 기계적으로 무결 연계한다."""
import json
import fcntl
from datetime import datetime
from threading import Lock
from uuid import uuid4
from .paths import PEOPLE, SPACES

_LOCKS: dict[str, Lock] = {}
_LOCKS_GUARD = Lock()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append(path, record):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_rows(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _read_members(space_dir):
    path = space_dir / "멤버.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _event_state_path(space_dir):
    return space_dir / "이벤트상태.json"


def _lock_path(space_dir):
    return space_dir / ".transcript.lock"


def _inprocess_lock(space_dir):
    key = str(space_dir.resolve())
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = Lock()
        return _LOCKS[key]


def with_space_lock(space: str, fn):
    """Run fn while holding the same transcript lock used by record()."""
    sdir = SPACES / space
    lock = _lock_path(sdir)
    lock.touch(exist_ok=True)
    with _inprocess_lock(sdir):
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _max_event_seq(rows):
    max_seq = 0
    for row in rows:
        try:
            max_seq = max(max_seq, int(row.get("event_seq") or 0))
        except Exception:
            continue
    return max_seq


def _next_event_seq(space_dir, rows):
    path = _event_state_path(space_dir)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        state = {}
    current = max(int(state.get("last_event_seq") or 0), _max_event_seq(rows), len(rows))
    next_seq = current + 1
    path.write_text(
        json.dumps({"last_event_seq": next_seq, "updated": now_iso()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return next_seq


def _find_duplicate(rows, field, value):
    if not value:
        return None
    for row in reversed(rows):
        if row.get(field) == value:
            return row
    return None


def state(space: str) -> dict:
    """Return additive delivery metadata for dashboards and recovery code."""
    sdir = SPACES / space
    rows = _read_rows(sdir / "대화.jsonl")
    last = rows[-1] if rows else {}
    try:
        persisted = json.loads(_event_state_path(sdir).read_text(encoding="utf-8"))
    except Exception:
        persisted = {}
    last_seq = max(_max_event_seq(rows), int(persisted.get("last_event_seq") or 0), len(rows))
    last_message_id = last.get("message_id") or (f"legacy_event_{last_seq}" if rows else "")
    return {
        "last_event_seq": last_seq,
        "last_message_id": last_message_id,
        "last_message_legacy": bool(rows and not last.get("message_id")),
        "message_count": len(rows),
    }


def record(
    space: str,
    record_dict: dict,
    *,
    dedupe_client_message_id: bool = False,
    dedupe_effect_id: bool = False,
) -> dict:
    sdir = SPACES / space
    lock = _lock_path(sdir)
    lock.touch(exist_ok=True)
    with _inprocess_lock(sdir):
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            rows = _read_rows(sdir / "대화.jsonl")
            client_message_id = str(record_dict.get("client_message_id") or "").strip()
            effect_id = str(record_dict.get("effect_id") or "").strip()
            duplicate = _find_duplicate(rows, "client_message_id", client_message_id) if dedupe_client_message_id else None
            duplicate_by = "client_message_id" if duplicate else ""
            if duplicate is None and dedupe_effect_id:
                duplicate = _find_duplicate(rows, "effect_id", effect_id)
                duplicate_by = "effect_id" if duplicate else ""
            if duplicate:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                return {"record": duplicate, "duplicate": True, "duplicate_by": duplicate_by}

            stored = dict(record_dict)
            seq = _next_event_seq(sdir, rows)
            stored.setdefault("event_seq", seq)
            stored.setdefault("message_id", f"msg_{seq:08d}_{uuid4().hex[:8]}")
            stored.setdefault("recorded_at", now_iso())
            if client_message_id:
                stored["client_message_id"] = client_message_id

            _append(sdir / "대화.jsonl", stored)
            members = _read_members(sdir)
            for m in members:
                if not isinstance(m, dict) or not m.get("토큰"):
                    continue
                seat = PEOPLE / m["토큰"] / "공간" / space
                if seat.exists():
                    _append(seat / "대화.jsonl", stored)
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            return {"record": stored, "duplicate": False, "duplicate_by": ""}


def read(space: str, limit: int = None):
    p = SPACES / space / "대화.jsonl"
    if not p.exists():
        return []
    rows = _read_rows(p)
    return rows[-limit:] if limit else rows
