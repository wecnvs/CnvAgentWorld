# -*- coding: utf-8 -*-
"""작업 결과 공개 후보 ReleaseQueue v0."""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from pathlib import Path

from .paths import ROOT, SPACES
from .transcript import now_iso


class ReleaseQueueError(RuntimeError):
    """ReleaseQueue 계약을 만족하지 못했다."""


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _queue_path(space: str) -> Path:
    return SPACES / space / "release_queue.jsonl"


def _lock_path(space: str) -> Path:
    return SPACES / space / ".release_queue.lock"


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _rows_with_error(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], ""
    rows = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], f"{path.name}: {type(exc).__name__}"
    bad_lines = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            bad_lines += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad_lines += 1
    if bad_lines:
        return rows, f"{path.name}: invalid_json_lines={bad_lines}"
    return rows, ""


def _append_unique(path: Path, data: dict, id_field: str) -> dict:
    rows, error = _rows_with_error(path)
    if error:
        raise ReleaseQueueError(error)
    wanted = data.get(id_field)
    if wanted:
        for row in reversed(rows):
            if row.get(id_field) == wanted:
                return {"record": row, "duplicate": True}
    _append_jsonl(path, data)
    return {"record": data, "duplicate": False}


def _latest_by_release(rows: list[dict]) -> dict:
    latest = {}
    for idx, row in enumerate(rows):
        release_id = row.get("release_id")
        if release_id:
            latest[release_id] = {**row, "_row_index": idx}
    return latest


def _strip_internal(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _find_latest(space: str, release_id_or_queue_id: str) -> dict:
    rows, error = _rows_with_error(_queue_path(space))
    if error:
        raise ReleaseQueueError(error)
    wanted = str(release_id_or_queue_id or "").strip()
    if not wanted:
        raise ReleaseQueueError("release_id required")
    for row in reversed(rows):
        if row.get("release_id") == wanted or row.get("release_queue_id") == wanted:
            return row
    raise ReleaseQueueError(f"release not found: {wanted}")


def get_release(space: str, release_id_or_queue_id: str) -> dict:
    return _find_latest(space, release_id_or_queue_id)


def _copy_release_fields(latest: dict) -> dict:
    return {
        "release_queue_id": latest.get("release_queue_id", ""),
        "release_id": latest.get("release_id", ""),
        "source_task_id": latest.get("source_task_id", ""),
        "worker_agent": latest.get("worker_agent", ""),
        "task_pack_id": latest.get("task_pack_id", ""),
        "task_pack_checksum_seen": latest.get("task_pack_checksum_seen", ""),
        "release_kind": latest.get("release_kind", ""),
        "public_summary": latest.get("public_summary", "")[:6000],
        "work_dir": latest.get("work_dir", ""),
        "intent_id": latest.get("intent_id", ""),
        "conversation_thread_id": latest.get("conversation_thread_id", ""),
        "room_generation": latest.get("room_generation"),
        "source_event_seq": latest.get("source_event_seq"),
        "source_message_id": latest.get("source_message_id", ""),
    }


def _compact_item(row: dict) -> dict:
    return _strip_internal({
        **row,
        "public_summary": str(row.get("public_summary", ""))[:300],
    })


def _with_lock(space: str, fn):
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(space, threading.RLock())
    lock = _lock_path(space)
    lock.touch(exist_ok=True)
    with local_lock:
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _latest_in_rows(rows: list[dict], release_id_or_queue_id: str) -> dict:
    wanted = str(release_id_or_queue_id or "").strip()
    if not wanted:
        raise ReleaseQueueError("release_id required")
    for row in reversed(rows):
        if row.get("release_id") == wanted or row.get("release_queue_id") == wanted:
            return row
    raise ReleaseQueueError(f"release not found: {wanted}")


def _append_transition(space: str, release_id: str, build_event) -> dict:
    def mutate():
        path = _queue_path(space)
        rows, error = _rows_with_error(path)
        if error:
            raise ReleaseQueueError(error)
        latest = _latest_in_rows(rows, release_id)
        event_or_result = build_event(latest)
        if isinstance(event_or_result, dict) and event_or_result.get("_duplicate_result"):
            return {"record": event_or_result["event"], "duplicate": True}
        event = event_or_result
        wanted = event.get("event_id")
        if wanted:
            for row in reversed(rows):
                if row.get("event_id") == wanted:
                    return {"record": row, "duplicate": True}
        _append_jsonl(path, event)
        return {"record": event, "duplicate": False}

    return _with_lock(space, mutate)


def _validate_request(release_request: dict):
    if release_request.get("schema") != "ReleaseRequest.v1":
        raise ReleaseQueueError("release_request schema must be ReleaseRequest.v1")
    required = ["release_id", "source_task_id", "task_pack_id", "task_pack_checksum_seen"]
    missing = [field for field in required if not release_request.get(field)]
    if missing:
        raise ReleaseQueueError("release_request missing: " + ", ".join(missing))


def enqueue_release(space: str, *, release_request: dict, work_dir: Path, task_pack: dict) -> dict:
    _validate_request(release_request)
    release_id = release_request["release_id"]
    approval_required = True
    approval_state = "pending"
    queue_state = "approval_pending"
    updated_request = {
        **release_request,
        "release_state": queue_state,
        "queue_state": "enqueued",
        "approval_required": True,
        "approval_state": "pending",
        "publish_blocked_until_approval": True,
        "draft_only": False,
        "release_queue_id": _stable_id("release_queue", space, release_id),
        "not_publishable_reason": "approval pending",
        "enqueued_at": now_iso(),
    }
    event = {
        "schema": "ReleaseQueueEvent.v1",
        "event_id": _stable_id(
            "release_event",
            space,
            release_id,
            "release_enqueued",
            release_request.get("task_pack_checksum_seen", ""),
        ),
        "event": "release_enqueued",
        "state": queue_state,
        "release_queue_id": updated_request["release_queue_id"],
        "release_id": release_id,
        "source_task_id": release_request.get("source_task_id", ""),
        "task_pack_id": release_request.get("task_pack_id", ""),
        "task_pack_checksum_seen": release_request.get("task_pack_checksum_seen", ""),
        "release_kind": release_request.get("release_kind", ""),
        "approval_phase": "space_manager_review",
        "approval_required": approval_required,
        "approval_state": approval_state,
        "publish_blocked_until_approval": True,
        "worker_agent": release_request.get("worker_agent", ""),
        "public_summary": release_request.get("public_summary", "")[:6000],
        "work_dir": _rel(work_dir),
        "created_at": now_iso(),
        "intent_id": release_request.get("intent_id", ""),
        "conversation_thread_id": release_request.get("conversation_thread_id", ""),
        "room_generation": release_request.get("room_generation"),
        "source_event_seq": release_request.get("source_event_seq"),
        "source_message_id": release_request.get("source_message_id", ""),
    }

    def mutate():
        result = _append_unique(_queue_path(space), event, "event_id")
        return {
            "release_request": updated_request,
            "event": result.get("record") or event,
            "duplicate": bool(result.get("duplicate")),
        }

    return _with_lock(space, mutate)


def approve_release(space: str, release_id: str, *, actor: str = "대표", reason: str = "") -> dict:
    def build(latest: dict) -> dict:
        if latest.get("state") == "published":
            raise ReleaseQueueError("release already published")
        if latest.get("approval_state") == "rejected" or latest.get("state") == "rejected":
            raise ReleaseQueueError("release already rejected")
        if latest.get("approval_state") == "granted":
            return {"_duplicate_result": True, "event": latest}
        if latest.get("approval_state") != "pending" and latest.get("state") not in {"approval_pending", "approved", "ready_to_publish"}:
            raise ReleaseQueueError("release is not pending approval")
        return {
            "schema": "ReleaseQueueEvent.v1",
            "event_id": _stable_id("release_event", space, latest.get("release_id", ""), "release_approved"),
            "event": "release_approved",
            "state": "approved",
            "approval_phase": "space_manager_review",
            "approval_required": bool(latest.get("approval_required", True)),
            "approval_state": "granted",
            "approved_by": actor,
            "review_reason": str(reason or "")[:500],
            "reviewed_at": now_iso(),
            **_copy_release_fields(latest),
        }

    result = _append_transition(space, release_id, build)
    return {"event": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def reject_release(space: str, release_id: str, *, actor: str = "대표", reason: str = "") -> dict:
    def build(latest: dict) -> dict:
        if latest.get("state") == "published":
            raise ReleaseQueueError("release already published")
        if latest.get("approval_state") == "rejected" or latest.get("state") == "rejected":
            return {"_duplicate_result": True, "event": latest}
        if latest.get("approval_state") == "granted":
            raise ReleaseQueueError("approved release cannot be rejected in v0; request revision in a later flow")
        if latest.get("approval_state") != "pending" and latest.get("state") not in {"approval_pending", "approved", "ready_to_publish"}:
            raise ReleaseQueueError("release is not pending approval")
        return {
            "schema": "ReleaseQueueEvent.v1",
            "event_id": _stable_id("release_event", space, latest.get("release_id", ""), "release_rejected"),
            "event": "release_rejected",
            "state": "rejected",
            "approval_phase": "space_manager_review",
            "approval_required": bool(latest.get("approval_required", True)),
            "approval_state": "rejected",
            "rejected_by": actor,
            "review_reason": str(reason or "")[:500],
            "reviewed_at": now_iso(),
            **_copy_release_fields(latest),
        }

    result = _append_transition(space, release_id, build)
    return {"event": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def mark_published(
    space: str,
    release_id: str,
    *,
    actor: str,
    publish_effect_id: str,
    published_message_id: str,
    event_seq,
) -> dict:
    def build(latest: dict) -> dict:
        if latest.get("state") == "published":
            return {"_duplicate_result": True, "event": latest}
        if latest.get("approval_state") != "granted":
            raise ReleaseQueueError("release must be approved before publish")
        return {
            "schema": "ReleaseQueueEvent.v1",
            "event_id": _stable_id("release_event", space, latest.get("release_id", ""), "release_published", publish_effect_id),
            "event": "release_published",
            "state": "published",
            "approval_phase": "published",
            "approval_required": bool(latest.get("approval_required", True)),
            "approval_state": "granted",
            "published_by": actor,
            "published_at": now_iso(),
            "publish_effect_id": publish_effect_id,
            "published_message_id": published_message_id,
            "published_event_seq": event_seq,
            **_copy_release_fields(latest),
        }

    result = _append_transition(space, release_id, build)
    return {"event": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def snapshot(space: str) -> dict:
    rows, error = _rows_with_error(_queue_path(space))
    latest_by_release = _latest_by_release(rows)
    state_counts = {}
    approval_state_counts = {}
    for row in latest_by_release.values():
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        approval_state = row.get("approval_state", "unknown")
        approval_state_counts[approval_state] = approval_state_counts.get(approval_state, 0) + 1
    latest = rows[-1] if rows else {}
    latest_items = sorted(latest_by_release.values(), key=lambda row: row.get("_row_index", 0))
    compact_items = [_compact_item(row) for row in latest_items[-20:]]
    pending_items = [
        _compact_item(row) for row in latest_items
        if row.get("approval_state") == "pending" or row.get("state") == "approval_pending"
    ]
    approved_items = [_compact_item(row) for row in latest_items if row.get("approval_state") == "granted"]
    pending_count = sum(
        1 for row in latest_by_release.values()
        if row.get("approval_state") == "pending" or row.get("state") == "approval_pending"
    )
    return {
        "release_count": len(latest_by_release),
        "release_event_count": len(rows),
        "pending_count": pending_count,
        "state_counts": state_counts,
        "approval_state_counts": approval_state_counts,
        "latest_release_id": latest.get("release_id", ""),
        "latest_source_task_id": latest.get("source_task_id", ""),
        "latest_state": latest.get("state", ""),
        "latest_approval_state": latest.get("approval_state", ""),
        "latest_public_summary": latest.get("public_summary", ""),
        "items": compact_items,
        "pending_items": pending_items,
        "approved_items": approved_items,
        "ledger_corrupt": bool(error),
        "ledger_errors": [error] if error else [],
    }
