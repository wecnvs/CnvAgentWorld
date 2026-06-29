# -*- coding: utf-8 -*-
"""병렬 채팅 응답 후보 큐 v0."""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from pathlib import Path

from .paths import ROOT, SPACES
from .transcript import now_iso


class CandidateQueueError(RuntimeError):
    """CandidateQueue 계약을 만족하지 못했다."""


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _queue_path(space: str) -> Path:
    return SPACES / space / "public_reply_candidates.jsonl"


def _lock_path(space: str) -> Path:
    return SPACES / space / ".candidate_queue.lock"


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


def _latest_by_candidate(rows: list[dict]) -> dict:
    latest = {}
    for idx, row in enumerate(rows):
        candidate_id = row.get("candidate_id")
        if candidate_id:
            latest[candidate_id] = {**row, "_row_index": idx}
    return latest


def _latest_in_rows(rows: list[dict], candidate_id: str) -> dict:
    wanted = str(candidate_id or "").strip()
    if not wanted:
        raise CandidateQueueError("candidate_id required")
    for row in reversed(rows):
        if row.get("candidate_id") == wanted:
            return row
    raise CandidateQueueError(f"candidate not found: {wanted}")


def _candidate_context(row: dict) -> dict:
    return {
        "space_id": row.get("space_id", ""),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
        "source_event_seq": row.get("source_event_seq"),
        "source_message_id": row.get("source_message_id", ""),
        "reply_to_message_id": row.get("reply_to_message_id") or row.get("source_message_id", ""),
    }


def candidate_context(row: dict) -> dict:
    return _candidate_context(row)


def get_candidate(space: str, candidate_id: str) -> dict:
    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        return _latest_in_rows(rows, candidate_id)

    return _with_lock(space, mutate)


def _pending_or_duplicate(latest: dict, allowed_terminal: set[str]) -> str:
    state = str(latest.get("state") or "")
    if state == "pending_synthesis":
        return "pending"
    if state in allowed_terminal:
        return "duplicate"
    raise CandidateQueueError(f"candidate is not pending: {latest.get('candidate_id', '')} state={state}")


def _transition_event(
    *,
    space: str,
    latest: dict,
    action: str,
    state: str,
    actor: str,
    reason: str,
    publish_effect_id: str = "",
    published_message_id: str = "",
    event_seq=None,
    synthesis_id: str = "",
    public_summary: str = "",
    manager_claim_context: dict | None = None,
) -> dict:
    manager_claim_context = manager_claim_context or {}
    return {
        **latest,
        "schema": "PublicReplyCandidate.v1",
        "event_id": _stable_id(
            "candidate_event",
            space,
            latest.get("candidate_id", ""),
            action,
            publish_effect_id,
            synthesis_id,
        ),
        "event": action,
        "state": state,
        "selected_by": actor if action == "candidate_selected" else latest.get("selected_by", ""),
        "discarded_by": actor if action == "candidate_discarded" else latest.get("discarded_by", ""),
        "synthesized_by": actor if action == "candidate_synthesized" else latest.get("synthesized_by", ""),
        "transition_reason": str(reason or "")[:500],
        "publish_effect_id": publish_effect_id,
        "published_message_id": published_message_id,
        "published_event_seq": event_seq,
        "synthesis_id": synthesis_id,
        "public_summary": str(public_summary or "")[:4000],
        "transition_manager_claim_token": manager_claim_context.get("claim_token", ""),
        "transition_manager_fencing_token": manager_claim_context.get("fencing_token", ""),
        "transition_owner_boot_id": manager_claim_context.get("owner_boot_id", ""),
        "transition_claim_seq": manager_claim_context.get("claim_seq", ""),
        "transitioned_at": now_iso(),
    }


def enqueue_candidate(
    space: str,
    *,
    turn_id: str,
    target_agent: str,
    manager_message: str,
    reply: str,
    context: dict,
    work_dir: Path | None = None,
    context_pack: dict | None = None,
    turn_handoff_pack: dict | None = None,
    manager_claim_context: dict | None = None,
    reason: str = "",
    join_policy: str = "timeout_then_partial",
    presentation_mode: str = "silent_reference",
    structured_result: dict | None = None,
) -> dict:
    context_pack = context_pack or {}
    turn_handoff_pack = turn_handoff_pack or {}
    manager_claim_context = manager_claim_context or {}
    context = context or {}
    candidate_id = _stable_id(
        "candidate",
        space,
        turn_id,
        target_agent,
        context.get("intent_id", ""),
        context.get("source_event_seq", ""),
    )
    event = {
        "schema": "PublicReplyCandidate.v1",
        "event_id": _stable_id("candidate_event", space, candidate_id, "candidate_created"),
        "event": "candidate_created",
        "state": "pending_synthesis",
        "candidate_id": candidate_id,
        "space_id": space,
        "turn_id": turn_id,
        "target_agent": target_agent,
        "manager_message": str(manager_message or "")[:2000],
        "reply": str(reply or "")[:12000],
        "reply_preview": str(reply or "")[:500],
        "reason": str(reason or "")[:500],
        "join_policy": join_policy,
        "presentation_mode": presentation_mode,
        "work_dir": _rel(work_dir) if work_dir else "",
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        "turn_handoff_checksum": turn_handoff_pack.get("turn_handoff_checksum", ""),
        "manager_claim_token": manager_claim_context.get("claim_token", ""),
        "manager_fencing_token": manager_claim_context.get("fencing_token", ""),
        "structured_action": (structured_result or {}).get("action", ""),
        "structured_public_reply": str((structured_result or {}).get("public_reply") or "")[:12000],
        "created_at": now_iso(),
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "reply_to_message_id": context.get("reply_to_message_id", ""),
    }

    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        for row in reversed(rows):
            if row.get("event_id") == event["event_id"]:
                return {"event": row, "duplicate": True}
        _append_jsonl(_queue_path(space), event)
        return {"event": event, "duplicate": False}

    return _with_lock(space, mutate)


def record_candidate_error(
    space: str,
    *,
    turn_id: str,
    target_agent: str,
    manager_message: str,
    error: str,
    context: dict,
    context_pack: dict | None = None,
    turn_handoff_pack: dict | None = None,
    manager_claim_context: dict | None = None,
    reason: str = "",
    join_policy: str = "timeout_then_partial",
    presentation_mode: str = "silent_reference",
) -> dict:
    context_pack = context_pack or {}
    turn_handoff_pack = turn_handoff_pack or {}
    manager_claim_context = manager_claim_context or {}
    context = context or {}
    candidate_id = _stable_id(
        "candidate",
        space,
        turn_id,
        target_agent,
        context.get("intent_id", ""),
        context.get("source_event_seq", ""),
    )
    event = {
        "schema": "PublicReplyCandidate.v1",
        "event_id": _stable_id("candidate_event", space, candidate_id, "candidate_error"),
        "event": "candidate_error",
        "state": "error",
        "candidate_id": candidate_id,
        "space_id": space,
        "turn_id": turn_id,
        "target_agent": target_agent,
        "manager_message": str(manager_message or "")[:2000],
        "error": str(error or "")[:1000],
        "reason": str(reason or "")[:500],
        "join_policy": join_policy,
        "presentation_mode": presentation_mode,
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        "manager_claim_token": manager_claim_context.get("claim_token", ""),
        "created_at": now_iso(),
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "reply_to_message_id": context.get("reply_to_message_id", ""),
    }

    def mutate():
        rows, error_text = _rows_with_error(_queue_path(space))
        if error_text:
            raise CandidateQueueError(error_text)
        for row in reversed(rows):
            if row.get("event_id") == event["event_id"]:
                return {"event": row, "duplicate": True}
        _append_jsonl(_queue_path(space), event)
        return {"event": event, "duplicate": False}

    return _with_lock(space, mutate)


def mark_selected(
    space: str,
    candidate_id: str,
    *,
    actor: str,
    reason: str = "",
    publish_effect_id: str,
    published_message_id: str,
    event_seq=None,
    discard_turn_peers: bool = True,
    manager_claim_context: dict | None = None,
) -> dict:
    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        latest_by_candidate = _latest_by_candidate(rows)
        latest = _latest_in_rows(rows, candidate_id)
        status = _pending_or_duplicate(latest, {"selected_published"})
        if status == "duplicate":
            if latest.get("publish_effect_id") and latest.get("publish_effect_id") != publish_effect_id:
                raise CandidateQueueError("candidate already selected with different publish_effect_id")
            peer_events = []
            if discard_turn_peers:
                for peer in latest_by_candidate.values():
                    if peer.get("candidate_id") == candidate_id:
                        continue
                    if peer.get("turn_id") != latest.get("turn_id"):
                        continue
                    if peer.get("state") != "pending_synthesis":
                        continue
                    peer_event = _transition_event(
                        space=space,
                        latest=peer,
                        action="candidate_discarded",
                        state="discarded",
                        actor=actor,
                        reason=f"peer_selected:{candidate_id}",
                        manager_claim_context=manager_claim_context,
                    )
                    _append_jsonl(_queue_path(space), peer_event)
                    peer_events.append(peer_event)
            return {"event": latest, "peer_events": peer_events, "duplicate": True}
        event = _transition_event(
            space=space,
            latest=latest,
            action="candidate_selected",
            state="selected_published",
            actor=actor,
            reason=reason,
            publish_effect_id=publish_effect_id,
            published_message_id=published_message_id,
            event_seq=event_seq,
            public_summary=latest.get("reply", ""),
            manager_claim_context=manager_claim_context,
        )
        _append_jsonl(_queue_path(space), event)
        peer_events = []
        if discard_turn_peers:
            for peer in latest_by_candidate.values():
                if peer.get("candidate_id") == candidate_id:
                    continue
                if peer.get("turn_id") != latest.get("turn_id"):
                    continue
                if peer.get("state") != "pending_synthesis":
                    continue
                peer_event = _transition_event(
                    space=space,
                    latest=peer,
                    action="candidate_discarded",
                    state="discarded",
                    actor=actor,
                    reason=f"peer_selected:{candidate_id}",
                    manager_claim_context=manager_claim_context,
                )
                _append_jsonl(_queue_path(space), peer_event)
                peer_events.append(peer_event)
        return {"event": event, "peer_events": peer_events, "duplicate": False}

    return _with_lock(space, mutate)


def mark_synthesized(
    space: str,
    candidate_ids: list[str],
    *,
    actor: str,
    reason: str = "",
    public_summary: str,
    publish_effect_id: str,
    published_message_id: str,
    event_seq=None,
    discard_turn_peers: bool = True,
    manager_claim_context: dict | None = None,
) -> dict:
    ids = [str(item or "").strip() for item in candidate_ids if str(item or "").strip()]
    if len(ids) < 2:
        raise CandidateQueueError("synthesize_candidates requires at least two candidate_ids")
    synthesis_id = _stable_id("synthesis", space, *sorted(ids))

    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        latest_by_candidate = _latest_by_candidate(rows)
        latest_rows = [_latest_in_rows(rows, candidate_id) for candidate_id in ids]
        base = latest_rows[0]
        for row in latest_rows[1:]:
            if (
                row.get("turn_id") != base.get("turn_id")
                or row.get("intent_id") != base.get("intent_id")
                or row.get("conversation_thread_id") != base.get("conversation_thread_id")
                or row.get("room_generation") != base.get("room_generation")
                or row.get("source_event_seq") != base.get("source_event_seq")
            ):
                raise CandidateQueueError("synthesize candidates must share turn/intent/thread/generation")
        for row in latest_rows:
            state = str(row.get("state") or "")
            if state == "synthesized_published":
                if row.get("publish_effect_id") != publish_effect_id:
                    raise CandidateQueueError("candidate already synthesized with different publish_effect_id")
                continue
            _pending_or_duplicate(row, {"synthesized_published"})
        if all(row.get("state") == "synthesized_published" for row in latest_rows):
            peer_events = []
            if discard_turn_peers:
                selected_ids = set(ids)
                for peer in latest_by_candidate.values():
                    if peer.get("candidate_id") in selected_ids:
                        continue
                    if peer.get("turn_id") != base.get("turn_id"):
                        continue
                    if peer.get("state") != "pending_synthesis":
                        continue
                    peer_event = _transition_event(
                        space=space,
                        latest=peer,
                        action="candidate_discarded",
                        state="discarded",
                        actor=actor,
                        reason=f"peer_synthesized:{synthesis_id}",
                        manager_claim_context=manager_claim_context,
                    )
                    _append_jsonl(_queue_path(space), peer_event)
                    peer_events.append(peer_event)
            return {
                "events": latest_rows,
                "peer_events": peer_events,
                "duplicate": True,
                "synthesis_id": latest_rows[0].get("synthesis_id") or synthesis_id,
            }
        events = []
        for row in latest_rows:
            if row.get("state") == "synthesized_published":
                events.append(row)
                continue
            event = _transition_event(
                space=space,
                latest=row,
                action="candidate_synthesized",
                state="synthesized_published",
                actor=actor,
                reason=reason,
                publish_effect_id=publish_effect_id,
                published_message_id=published_message_id,
                event_seq=event_seq,
                synthesis_id=synthesis_id,
                public_summary=public_summary,
                manager_claim_context=manager_claim_context,
            )
            _append_jsonl(_queue_path(space), event)
            events.append(event)
        peer_events = []
        if discard_turn_peers:
            selected_ids = set(ids)
            for peer in latest_by_candidate.values():
                if peer.get("candidate_id") in selected_ids:
                    continue
                if peer.get("turn_id") != base.get("turn_id"):
                    continue
                if peer.get("state") != "pending_synthesis":
                    continue
                peer_event = _transition_event(
                    space=space,
                    latest=peer,
                    action="candidate_discarded",
                    state="discarded",
                    actor=actor,
                    reason=f"peer_synthesized:{synthesis_id}",
                    manager_claim_context=manager_claim_context,
                )
                _append_jsonl(_queue_path(space), peer_event)
                peer_events.append(peer_event)
        return {"events": events, "peer_events": peer_events, "duplicate": False, "synthesis_id": synthesis_id}

    return _with_lock(space, mutate)


def discard_candidates(
    space: str,
    candidate_ids: list[str],
    *,
    actor: str,
    reason: str = "",
    manager_claim_context: dict | None = None,
) -> dict:
    ids = [str(item or "").strip() for item in candidate_ids if str(item or "").strip()]
    if not ids:
        raise CandidateQueueError("discard_candidate requires candidate_id")

    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        latest_rows = [_latest_in_rows(rows, candidate_id) for candidate_id in ids]
        if all(row.get("state") == "discarded" for row in latest_rows):
            return {"events": latest_rows, "duplicate": True}
        events = []
        for row in latest_rows:
            status = _pending_or_duplicate(row, {"discarded"})
            if status == "duplicate":
                events.append(row)
                continue
            event = _transition_event(
                space=space,
                latest=row,
                action="candidate_discarded",
                state="discarded",
                actor=actor,
                reason=reason,
                manager_claim_context=manager_claim_context,
            )
            _append_jsonl(_queue_path(space), event)
            events.append(event)
        return {"events": events, "duplicate": False}

    return _with_lock(space, mutate)


def supersede_candidates(
    space: str,
    candidate_ids: list[str],
    *,
    actor: str,
    reason: str = "",
    manager_claim_context: dict | None = None,
) -> dict:
    ids = [str(item or "").strip() for item in candidate_ids if str(item or "").strip()]
    if not ids:
        return {"events": [], "duplicate": False}

    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        events = []
        for candidate_id in ids:
            row = _latest_in_rows(rows, candidate_id)
            if row.get("state") != "pending_synthesis":
                events.append(row)
                continue
            event = _transition_event(
                space=space,
                latest=row,
                action="candidate_superseded",
                state="superseded",
                actor=actor,
                reason=reason,
                manager_claim_context=manager_claim_context,
            )
            _append_jsonl(_queue_path(space), event)
            events.append(event)
        return {"events": events, "duplicate": False}

    return _with_lock(space, mutate)


def pending_ids_for_turn(space: str, turn_id: str) -> list[str]:
    wanted = str(turn_id or "").strip()
    if not wanted:
        return []

    def mutate():
        rows, error = _rows_with_error(_queue_path(space))
        if error:
            raise CandidateQueueError(error)
        latest = _latest_by_candidate(rows)
        return [
            candidate_id
            for candidate_id, row in latest.items()
            if row.get("turn_id") == wanted and row.get("state") == "pending_synthesis"
        ]

    return _with_lock(space, mutate)


def snapshot(space: str) -> dict:
    rows, error = _rows_with_error(_queue_path(space))
    latest_by_candidate = _latest_by_candidate(rows)
    latest = list(latest_by_candidate.values())
    pending = [row for row in latest if row.get("state") == "pending_synthesis"]
    errored = [row for row in latest if row.get("state") == "error"]
    state_counts = {}
    for row in latest:
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    latest_row = latest[-1] if latest else {}
    return {
        "schema": "CandidateQueueSnapshot.v1",
        "ledger_corrupt": bool(error),
        "ledger_errors": [error] if error else [],
        "event_count": len(rows),
        "candidate_count": len(latest),
        "pending_count": len(pending),
        "error_count": len(errored),
        "state_counts": state_counts,
        "latest_candidate_id": latest_row.get("candidate_id", ""),
        "latest_turn_id": latest_row.get("turn_id", ""),
        "latest_target_agent": latest_row.get("target_agent", ""),
        "latest_state": latest_row.get("state", ""),
        "latest_reply_preview": latest_row.get("reply_preview", ""),
        "latest_error": latest_row.get("error", ""),
        "pending_items": [
            {k: row.get(k, "") for k in (
                "candidate_id", "turn_id", "target_agent", "state", "reply_preview",
                "structured_action", "presentation_mode", "intent_id",
                "conversation_thread_id", "room_generation", "_row_index",
            )}
            for row in pending[-8:]
        ],
        "prompt_items": [
            {
                "candidate_id": row.get("candidate_id", ""),
                "turn_id": row.get("turn_id", ""),
                "target_agent": row.get("target_agent", ""),
                "state": row.get("state", ""),
                "reply": str(row.get("reply") or "")[:2500],
                "structured_action": row.get("structured_action", ""),
                "structured_public_reply": str(row.get("structured_public_reply") or "")[:2500],
                "manager_message": str(row.get("manager_message") or "")[:500],
                "reason": row.get("reason", ""),
                "intent_id": row.get("intent_id", ""),
                "conversation_thread_id": row.get("conversation_thread_id", ""),
                "room_generation": row.get("room_generation"),
                "_row_index": row.get("_row_index"),
            }
            for row in pending[-6:]
        ],
        "error_items": [
            {k: row.get(k, "") for k in (
                "candidate_id", "turn_id", "target_agent", "state", "error",
                "intent_id", "conversation_thread_id", "room_generation", "_row_index",
            )}
            for row in errored[-5:]
        ],
        "latest": latest[-12:],
    }
