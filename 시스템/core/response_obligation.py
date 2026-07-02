# -*- coding: utf-8 -*-
"""공개 대화의 응답 의무 원장."""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .paths import SPACES
from .transcript import now_iso


class ResponseObligationError(RuntimeError):
    """ResponseObligation 원장 계약을 만족하지 못했다."""


TERMINAL_STATES = {"answered", "superseded", "cancelled", "timed_out", "manager_closed"}
ACTIVE_STATES = {"open", "assigned", "delegated"}
MAX_COMPACT_TEXT = 300
DEFAULT_OPEN_TIMEOUT_MS = 5 * 60 * 1000
DEFAULT_ASSIGNED_TIMEOUT_MS = 5 * 60 * 1000
DEFAULT_DELEGATED_TIMEOUT_MS = 0

_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _ledger_path(space: str) -> Path:
    return SPACES / space / "response_obligations.jsonl"


def _lock_path(space: str) -> Path:
    return SPACES / space / ".response_obligations.lock"


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


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


def _latest_by_obligation(rows: list[dict]) -> dict[str, dict]:
    latest = {}
    for idx, row in enumerate(rows):
        obligation_id = row.get("obligation_id")
        if obligation_id:
            latest[obligation_id] = {**row, "_row_index": idx}
    return latest


def _source_fields_from_message(row: dict) -> dict:
    return {
        "source_event_seq": row.get("event_seq"),
        "source_message_id": row.get("message_id", ""),
        "source_client_message_id": row.get("client_message_id", ""),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
        "source_speaker": row.get("화자", ""),
        "source_role": row.get("역할", ""),
        "source_text_preview": str(row.get("내용") or "")[:MAX_COMPACT_TEXT],
    }


def _source_fields_from_context(context: dict | None) -> dict:
    context = context or {}
    return {
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
    }


def _obligation_id(space: str, source_message_id: str, source_event_seq=None) -> str:
    key = source_message_id or source_event_seq
    if not key:
        raise ResponseObligationError("source_message_id or source_event_seq required")
    return _stable_id("obl", space, key)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _timeout_threshold_ms(state: str) -> int:
    if state == "open":
        return DEFAULT_OPEN_TIMEOUT_MS
    if state == "assigned":
        return DEFAULT_ASSIGNED_TIMEOUT_MS
    if state == "delegated":
        return DEFAULT_DELEGATED_TIMEOUT_MS
    return 0


def _policy_fields(row: dict) -> dict:
    state = str(row.get("state") or "")
    created = _parse_time(row.get("created_at", ""))
    now = datetime.now(created.tzinfo) if created and created.tzinfo else datetime.now()
    age_ms = 0
    if created:
        age_ms = max(0, int((now - created).total_seconds() * 1000))
    threshold = _timeout_threshold_ms(state)
    deadline_at = ""
    remaining_ms = None
    overdue = False
    blockers = []
    reason = "terminal" if state in TERMINAL_STATES else "observe_only"
    if state == "delegated":
        blockers.append("delegated_task_or_release")
        reason = "delegated_obligation_requires_task_release_state"
    if threshold > 0 and created:
        deadline = created + timedelta(milliseconds=threshold)
        deadline_at = deadline.isoformat(timespec="seconds")
        remaining_ms = int((deadline - now).total_seconds() * 1000)
        overdue = remaining_ms <= 0 and state in ACTIVE_STATES and not blockers
        if remaining_ms < 0:
            remaining_ms = 0
    return {
        "age_ms": age_ms,
        "timeout_threshold_ms": threshold,
        "deadline_at": deadline_at,
        "remaining_ms": remaining_ms,
        "overdue": overdue,
        "auto_policy": "observe_only",
        "policy_reason": reason,
        "policy_blockers": blockers,
    }


def open_for_message(
    space: str,
    message: dict,
    *,
    target_actor: str = "space",
    reason: str = "manager_requested_input",
) -> dict:
    fields = _source_fields_from_message(message)
    obligation_id = _obligation_id(space, fields.get("source_message_id", ""), fields.get("source_event_seq"))
    event = {
        "schema": "ResponseObligationEvent.v1",
        "event_id": _stable_id("obligation_event", obligation_id, "opened"),
        "event": "obligation_opened",
        "state": "open",
        "obligation_id": obligation_id,
        "space_id": space,
        "target_actor": target_actor,
        "opened_by": fields.get("source_speaker", ""),
        "reason": reason,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        **fields,
    }

    def mutate():
        path = _ledger_path(space)
        rows, error = _rows_with_error(path)
        if error:
            raise ResponseObligationError(error)
        latest = _latest_by_obligation(rows).get(obligation_id)
        if latest:
            return {"ok": True, "duplicate": True, "event": latest}
        _append_jsonl(path, event)
        return {"ok": True, "duplicate": False, "event": event}

    return _with_lock(space, mutate)


def _resolve_active_obligation_id(rows: list[dict], context: dict | None) -> str:
    """source id가 없는 context를 intent/thread로 활성 의무에 매칭한다(최신 우선).

    실증(레빗_bcd7): request_work 위임 경로가 source_message_id/source_event_seq 없는 context로
    delegate_to_task를 불러 ResponseObligationError로 3회 연속 실패 — 의무가 위임 기록 없이 남았다.
    id를 못 만들 때 예외로 죽는 대신, 같은 intent(없으면 같은 thread)의 활성 의무를 찾아 그걸 전이한다.
    """
    context = context or {}
    intent = str(context.get("intent_id") or "").strip()
    thread = str(context.get("conversation_thread_id") or "").strip()
    if not intent and not thread:
        return ""
    candidates = [
        row for row in _latest_by_obligation(rows).values()
        if row.get("state") in ACTIVE_STATES
        and (
            (intent and row.get("intent_id") == intent)
            or (not intent and thread and row.get("conversation_thread_id") == thread)
        )
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda row: row.get("_row_index", 0))
    return candidates[-1].get("obligation_id", "")


def _transition(
    space: str,
    context: dict | None,
    *,
    state: str,
    event: str,
    actor: str,
    reason: str = "",
    **extra,
) -> dict:
    if state not in ACTIVE_STATES and state not in TERMINAL_STATES:
        raise ResponseObligationError(f"unsupported obligation state: {state}")
    source = _source_fields_from_context(context)
    try:
        obligation_id = _obligation_id(space, source.get("source_message_id", ""), source.get("source_event_seq"))
    except ResponseObligationError:
        obligation_id = ""
        # source id 없는 폴백 매칭에서는 빈 source 필드로 원본 값을 덮어쓰지 않는다.
        source = {k: v for k, v in source.items() if v not in (None, "")}

    def mutate():
        path = _ledger_path(space)
        rows, error = _rows_with_error(path)
        if error:
            raise ResponseObligationError(error)
        resolved_id = obligation_id or _resolve_active_obligation_id(rows, context)
        if not resolved_id:
            return {"ok": True, "missing": True, "unresolved_context": True, "event": {}}
        latest = _latest_by_obligation(rows).get(resolved_id)
        if not latest:
            return {"ok": True, "missing": True, "event": {}}
        if latest.get("state") in TERMINAL_STATES:
            return {"ok": True, "duplicate": True, "terminal": True, "event": latest}
        row = {
            **latest,
            "schema": "ResponseObligationEvent.v1",
            "event_id": _stable_id("obligation_event", resolved_id, event, state, actor, reason, extra),
            "event": event,
            "state": state,
            "transition_actor": actor,
            "transition_reason": str(reason or "")[:500],
            "updated_at": now_iso(),
            **source,
            **extra,
        }
        if state in TERMINAL_STATES:
            row["closed_at"] = now_iso()
            row["closed_by"] = actor
            row["close_outcome"] = state
        _append_jsonl(path, row)
        return {"ok": True, "duplicate": False, "event": row}

    return _with_lock(space, mutate)


def assign_for_context(
    space: str,
    context: dict | None,
    *,
    assignee: str,
    actor: str = "공간관리",
    reason: str = "",
    wake_id: str = "",
    turn_handoff_id: str = "",
) -> dict:
    return _transition(
        space,
        context,
        state="assigned",
        event="obligation_assigned",
        actor=actor,
        reason=reason,
        assigned_to=assignee,
        wake_id=wake_id,
        turn_handoff_id=turn_handoff_id,
    )


def reopen_for_context(
    space: str,
    context: dict | None,
    *,
    actor: str = "공간관리",
    reason: str = "",
) -> dict:
    """assigned/delegated로 잡아둔 의무를 다시 'open'으로 되돌린다.

    지목한 응답이 실패(예: wake_failed — 엔진 타임아웃/모델오류로 에이전트 턴이 답을 못 냄)해 아직 답이
    안 나갔을 때, 의무가 'assigned'로 고아가 되면 미응답 sweep(_open_user_obligations는 state=='open'만
    재구동)이 못 잡아 대표 메시지가 영영 무응답·무재시도로 스트랜드된다(대표 신고 흐름). open으로 되돌리면
    같은/다음 체인의 sweep이 재구동해 자가치유한다 — manager_failure가 의무를 open으로 유지하는 것과 대칭.
    이미 종결된(terminal) 의무는 _transition이 no-op으로 보존한다(이미 답한 걸 되살리지 않음)."""
    return _transition(
        space,
        context,
        state="open",
        event="obligation_reopened",
        actor=actor,
        reason=reason,
    )


def delegate_to_task(
    space: str,
    context: dict | None,
    *,
    task_id: str,
    worker_agent: str,
    actor: str = "공간관리",
    reason: str = "",
) -> dict:
    return _transition(
        space,
        context,
        state="delegated",
        event="obligation_delegated_to_task",
        actor=actor,
        reason=reason,
        task_id=task_id,
        assigned_to=worker_agent,
    )


def close_for_context(
    space: str,
    context: dict | None,
    *,
    outcome: str,
    actor: str,
    reason: str = "",
    published_message_id: str = "",
    responder: str = "",
    task_id: str = "",
) -> dict:
    if outcome not in TERMINAL_STATES:
        raise ResponseObligationError(f"unsupported close outcome: {outcome}")
    return _transition(
        space,
        context,
        state=outcome,
        event="obligation_closed",
        actor=actor,
        reason=reason,
        published_message_id=published_message_id,
        responder=responder,
        task_id=task_id,
    )


def compact_item(row: dict) -> dict:
    return {
        "obligation_id": row.get("obligation_id", ""),
        "state": row.get("state", ""),
        "target_actor": row.get("target_actor", ""),
        "assigned_to": row.get("assigned_to", ""),
        "responder": row.get("responder", ""),
        "task_id": row.get("task_id", ""),
        "published_message_id": row.get("published_message_id", ""),
        "source_event_seq": row.get("source_event_seq"),
        "source_message_id": row.get("source_message_id", ""),
        "source_speaker": row.get("source_speaker", ""),
        "source_text_preview": row.get("source_text_preview", ""),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
        "transition_reason": row.get("transition_reason", row.get("reason", "")),
        "updated_at": row.get("updated_at", row.get("created_at", "")),
        "closed_at": row.get("closed_at", ""),
        "closed_by": row.get("closed_by", ""),
        "close_outcome": row.get("close_outcome", ""),
        "_row_index": row.get("_row_index", 0),
        **_policy_fields(row),
    }


def snapshot(space: str) -> dict:
    rows, error = _rows_with_error(_ledger_path(space))
    latest = _latest_by_obligation(rows)
    latest_items = [compact_item(item) for item in latest.values()]
    counts = {}
    for item in latest_items:
        state = item.get("state") or "unknown"
        counts[state] = counts.get(state, 0) + 1
    open_items = [item for item in latest_items if item.get("state") in ACTIVE_STATES]
    terminal_items = [item for item in latest_items if item.get("state") in TERMINAL_STATES]
    overdue_items = [item for item in open_items if item.get("overdue")]
    deadline_items = [item for item in open_items if item.get("deadline_at")]
    oldest_open_age_ms = max([int(item.get("age_ms") or 0) for item in open_items], default=0)
    next_deadline_at = ""
    if deadline_items:
        next_deadline_at = sorted(str(item.get("deadline_at") or "") for item in deadline_items if item.get("deadline_at"))[0]
    return {
        "schema": "ResponseObligationSnapshot.v1",
        "obligation_count": len(latest_items),
        "open_count": len(open_items),
        "terminal_count": len(terminal_items),
        "overdue_open_count": len(overdue_items),
        "oldest_open_age_ms": oldest_open_age_ms,
        "next_deadline_at": next_deadline_at,
        "timed_out_count": counts.get("timed_out", 0),
        "superseded_count": counts.get("superseded", 0),
        "auto_closed_count": 0,
        "state_counts": counts,
        "overdue_items": overdue_items[-12:],
        "open_items": open_items[-12:],
        "latest": latest_items[-12:],
        "latest_obligation_id": latest_items[-1].get("obligation_id", "") if latest_items else "",
        "latest_state": latest_items[-1].get("state", "") if latest_items else "",
        "ledger_corrupt": bool(error),
        "ledger_errors": [error] if error else [],
    }
