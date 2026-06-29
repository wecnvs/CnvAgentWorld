# -*- coding: utf-8 -*-
"""TaskPack v0 adapter와 작업 상태 원장."""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import lesson_ledger, orchestration, release_queue, work_settings
from .paths import ROOT, SPACES
from .transcript import now_iso, read


class TaskRegistryError(RuntimeError):
    """작업 원장/TaskPack 계약을 만족하지 못했다."""


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
TASK_HEARTBEAT_STALE_MS = work_settings.DEFAULT_WORK_SETTINGS["heartbeat_stale_ms"]
TASK_PROGRESS_REPORT_DUE_MS = work_settings.DEFAULT_WORK_SETTINGS["progress_report_due_ms"]
TASK_PROGRESS_REPORT_DUE_REASON_CODE = "progress_report_due"
TASK_RUNTIME_HEARTBEAT_LABELS = {
    "steering_progress_seen": ("progress_seen", "진행보고 요청 확인"),
    "steering_revise_detected": ("revise_detected", "재지시 감지"),
    "steering_revise_detected_after_return": ("revise_detected", "반환 직후 재지시 감지"),
    "engine_restarting_for_revise": ("revise_restarting", "재지시 재실행 중"),
    "engine_restart": ("revise_restarting", "재지시 재실행 중"),
    "steering_revise_applied": ("revise_applied", "재지시 반영 완료"),
}
TASK_RUNTIME_STEERING_LABELS = {
    "request_progress": ("progress_requested", "작업 부분 보고 요청"),
    "revise_task": ("revise_requested", "작업 재지시 요청"),
}
RELEASE_FOLLOWUP_EVENTS = {
    "task_release_cancel_requested",
    "task_release_steering_unacknowledged",
    "task_release_stale_generation",
    "task_release_enqueued",
    "task_release_enqueue_failed",
}


def _space_dir(space: str) -> Path:
    return SPACES / space


def _registry_path(space: str) -> Path:
    return _space_dir(space) / "task_registry.jsonl"


def _manifest_path(space: str) -> Path:
    return _space_dir(space) / "task_pack_manifest.jsonl"


def _lock_path(space: str) -> Path:
    return _space_dir(space) / ".task_registry.lock"


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _append_unique(path: Path, data: dict, id_field: str) -> dict:
    rows, error = _rows_with_error(path)
    if error:
        raise TaskRegistryError(error)
    wanted = data.get(id_field)
    if wanted:
        for row in reversed(rows):
            if row.get(id_field) == wanted:
                return {"record": row, "duplicate": True}
    _append_jsonl(path, data)
    return {"record": data, "duplicate": False}


def _append_event(path: Path, data: dict):
    rows, error = _rows_with_error(path)
    if error:
        raise TaskRegistryError(error)
    _append_jsonl(path, data)
    return data


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _parse_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _heartbeat_age_ms(value: str | None, *, now: datetime | None = None) -> int | None:
    ts = _parse_time(value)
    if not ts:
        return None
    if ts.tzinfo is not None:
        base = datetime.now(ts.tzinfo)
    else:
        base = now or datetime.now()
    return max(0, int((base - ts).total_seconds() * 1000))


def _work_policy_overrides_from_row(row: dict) -> dict:
    data = {}
    for key in (
        "runner_timeout_sec",
        "heartbeat_interval_sec",
        "heartbeat_stale_ms",
        "progress_report_due_ms",
    ):
        if row.get(key) not in (None, ""):
            data[key] = row.get(key)
    if row.get("heartbeat_stale_threshold_ms") not in (None, ""):
        data["heartbeat_stale_ms"] = row.get("heartbeat_stale_threshold_ms")
    if row.get("progress_report_due_threshold_ms") not in (None, ""):
        data["progress_report_due_ms"] = row.get("progress_report_due_threshold_ms")
    if row.get("work_settings_source") not in (None, ""):
        data["settings_source"] = row.get("work_settings_source")
    if row.get("work_settings_source_chain") not in (None, ""):
        data["source_chain"] = row.get("work_settings_source_chain")
    return data


def _work_policy_from_row(row: dict) -> dict:
    data = _work_policy_overrides_from_row(row)
    return work_settings.normalize_settings(data)


def _heartbeat_stale_threshold_ms(row: dict) -> int:
    return int(_work_policy_from_row(row)["heartbeat_stale_ms"])


def _progress_report_due_threshold_ms(row: dict) -> int:
    return int(_work_policy_from_row(row)["progress_report_due_ms"])


def _work_policy_fields(policy: dict | None) -> dict:
    data = work_settings.normalize_settings(policy or {})
    return {
        "runner_timeout_sec": data["runner_timeout_sec"],
        "heartbeat_interval_sec": data["heartbeat_interval_sec"],
        "heartbeat_stale_ms": data["heartbeat_stale_ms"],
        "progress_report_due_ms": data["progress_report_due_ms"],
        "heartbeat_stale_threshold_ms": data["heartbeat_stale_ms"],
        "progress_report_due_threshold_ms": data["progress_report_due_ms"],
        "work_settings_source": data.get("settings_source", ""),
        "work_settings_source_chain": data.get("source_chain", []),
    }


def _heartbeat_status(row: dict, *, now: datetime | None = None) -> dict:
    last_heartbeat_at = row.get("last_heartbeat_at", "")
    state = row.get("state", "")
    active_state = state in {"running", "cancel_requested"} or bool(row.get("cancel_requested"))
    age_ms = _heartbeat_age_ms(last_heartbeat_at, now=now)
    threshold_ms = _heartbeat_stale_threshold_ms(row)
    missing = active_state and not bool(last_heartbeat_at)
    stale = bool(active_state and (missing or age_ms is None or age_ms > threshold_ms))
    return {
        "heartbeat_age_ms": age_ms,
        "heartbeat_missing": missing,
        "heartbeat_stale": stale,
        "heartbeat_stale_threshold_ms": threshold_ms,
    }


def _steering_status(row: dict) -> dict:
    return {
        "latest_steering_seq": row.get("latest_steering_seq", row.get("steering_seq", 0)),
        "latest_steering_action": row.get("latest_steering_action", row.get("steering_action", "")),
        "latest_steering_instruction": str(row.get("latest_steering_instruction", row.get("steering_instruction", "")))[:240],
        "latest_steering_requested_at": row.get("latest_steering_requested_at", row.get("steering_requested_at", "")),
        "latest_steering_requested_by": row.get("latest_steering_requested_by", row.get("steering_requested_by", "")),
        "latest_steering_reason_code": row.get("latest_steering_reason_code", row.get("steering_reason_code", "")),
        "latest_steering_dedupe_key": row.get("latest_steering_dedupe_key", row.get("steering_dedupe_key", "")),
        "latest_steering_control_request_source_event_seq": row.get("latest_steering_control_request_source_event_seq", row.get("control_request_source_event_seq")),
        "latest_steering_control_request_room_generation": row.get("latest_steering_control_request_room_generation", row.get("control_request_room_generation")),
        "latest_steering_requires_ack": bool(row.get("latest_steering_requires_ack", row.get("requires_worker_ack", False))),
        "pending_steering_ack": bool(row.get("pending_steering_ack", False)),
        "pending_ack_steering_seq": row.get("pending_ack_steering_seq", 0),
        "pending_ack_steering_action": row.get("pending_ack_steering_action", ""),
        "pending_ack_steering_instruction": str(row.get("pending_ack_steering_instruction", ""))[:240],
        "pending_ack_steering_event_id": row.get("pending_ack_steering_event_id", ""),
    }


def _progress_requested_since_heartbeat(row: dict) -> bool:
    if row.get("latest_steering_action", row.get("steering_action", "")) != "request_progress":
        return False
    requested_at = _parse_time(row.get("latest_steering_requested_at", row.get("steering_requested_at", "")))
    if not requested_at:
        return False
    last_heartbeat = _parse_time(row.get("last_heartbeat_at", ""))
    if not last_heartbeat:
        return True
    return requested_at > last_heartbeat


def _progress_report_due_key(row: dict) -> str:
    return _stable_id(
        "progress_due",
        row.get("space_id", ""),
        row.get("task_id", ""),
        row.get("task_pack_id", ""),
        row.get("last_heartbeat_at", ""),
        row.get("heartbeat_phase", ""),
        _progress_report_due_threshold_ms(row),
    )


def _progress_report_due_status(row: dict, *, now: datetime | None = None) -> dict:
    heartbeat = _heartbeat_status(row, now=now)
    active_running = row.get("state") == "running" and not bool(row.get("cancel_requested"))
    age_ms = heartbeat.get("heartbeat_age_ms")
    threshold_ms = _progress_report_due_threshold_ms(row)
    age_due = age_ms is None or age_ms > threshold_ms
    reason = ""
    if heartbeat.get("heartbeat_missing"):
        reason = "heartbeat_missing"
    elif age_due:
        reason = "heartbeat_age_exceeded"
    progress_requested = _progress_requested_since_heartbeat(row)
    due = bool(
        active_running
        and reason
        and not bool(row.get("pending_steering_ack"))
        and not progress_requested
    )
    return {
        "progress_report_due": due,
        "progress_report_due_reason": reason if due else "",
        "progress_report_due_threshold_ms": threshold_ms,
        "progress_report_due_key": _progress_report_due_key(row) if due else "",
        "progress_report_requested_since_heartbeat": bool(progress_requested),
    }


def _steering_runtime_status(row: dict) -> dict:
    phase = str(row.get("heartbeat_phase") or "")
    action = str(row.get("latest_steering_action", row.get("steering_action", "")) or "")
    pending = bool(row.get("pending_steering_ack"))
    if pending:
        state = "ack_wait"
        label = "재지시 확인 대기"
    elif phase in {"steering_revise_detected", "steering_revise_detected_after_return"}:
        state = "revise_detected"
        label = "재지시 감지"
    elif phase in {"engine_restarting_for_revise", "engine_restart"}:
        state = "revise_restarting"
        label = "재지시 재실행 중"
    elif phase == "steering_revise_applied":
        state = "revise_applied"
        label = "재지시 반영 완료"
    elif phase == "steering_progress_seen":
        state = "progress_seen"
        label = "진행보고 요청 확인"
    elif action == "request_progress" and _progress_requested_since_heartbeat(row):
        state = "progress_requested"
        label = "진행보고 요청됨"
    else:
        state = ""
        label = ""
    return {
        "steering_runtime_state": state,
        "steering_runtime_label": label,
    }


def _task_runtime_at(row: dict) -> str:
    event = str(row.get("event") or "")
    if event == "task_steering_requested":
        return row.get("steering_requested_at") or row.get("latest_steering_requested_at") or ""
    if event == "task_cancel_requested":
        return row.get("cancel_requested_at") or ""
    if event == "task_heartbeat":
        return row.get("last_heartbeat_at") or ""
    return row.get("created_at") or row.get("updated_at") or ""


def _task_runtime_projection(row: dict, row_index: int) -> dict:
    event = str(row.get("event") or "")
    state = ""
    label = ""
    detail = ""
    if event == "task_steering_requested":
        action = str(row.get("steering_action") or row.get("latest_steering_action") or "")
        state, label = TASK_RUNTIME_STEERING_LABELS.get(action, ("", ""))
        detail = str(row.get("steering_instruction") or row.get("latest_steering_instruction") or "")[:240]
    elif event == "task_cancel_requested":
        state, label = "cancel_requested", "작업 취소 요청"
        detail = str(row.get("cancellation_reason") or "")[:240]
    elif event == "task_heartbeat":
        phase = str(row.get("heartbeat_phase") or "")
        state, label = TASK_RUNTIME_HEARTBEAT_LABELS.get(phase, ("", ""))
        detail = str(row.get("heartbeat_note") or "")[:240]
    if not label:
        return {}
    return _public_task({
        "type": "task_runtime",
        "event": event,
        "state": state,
        "label": label,
        "detail": detail,
        "at": _task_runtime_at(row),
        "row_index": row_index,
        "event_id": row.get("event_id", ""),
        "task_id": row.get("task_id", ""),
        "task_pack_id": row.get("task_pack_id", ""),
        "worker_agent": row.get("worker_agent", ""),
        "work_dir": row.get("work_dir", ""),
        "steering_action": row.get("steering_action", row.get("latest_steering_action", "")),
        "steering_seq": row.get("steering_seq", row.get("latest_steering_seq", 0)),
        "steering_requested_at": row.get("steering_requested_at", ""),
        "latest_steering_requested_at": row.get("latest_steering_requested_at", ""),
        "steering_reason_code": row.get("steering_reason_code", row.get("latest_steering_reason_code", "")),
        "last_heartbeat_at": row.get("last_heartbeat_at", ""),
        "heartbeat_phase": row.get("heartbeat_phase", ""),
        "heartbeat_note": str(row.get("heartbeat_note", ""))[:240],
        "cancel_requested": bool(row.get("cancel_requested")),
        "cancellation_request_id": row.get("cancellation_request_id", ""),
        "cancel_requested_at": row.get("cancel_requested_at", ""),
        **_context_fields(row),
    })


def _task_runtime_activity_rows(rows: list[dict], *, limit: int = 12) -> list[dict]:
    try:
        wanted = max(0, int(limit))
    except Exception:
        wanted = 12
    if wanted <= 0:
        return []
    projected = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        item = _task_runtime_projection(row, idx)
        if item:
            projected.append(item)
    return projected[-wanted:][::-1]


def runtime_activity(space: str, limit: int = 12) -> list[dict]:
    rows, _error = _rows_with_error(_registry_path(space))
    return _task_runtime_activity_rows(rows, limit=limit)


def _pending_ack_steering_from_rows(rows: list[dict], task_id: str, work_status: dict) -> dict:
    latest = {}
    for row in reversed(rows):
        if (
            row.get("task_id") == task_id
            and row.get("event") == "task_steering_requested"
            and row.get("requires_worker_ack")
        ):
            latest = row
            break
    if not latest:
        return {"pending": False}
    try:
        steering_seq = int(latest.get("steering_seq") or 0)
    except Exception:
        steering_seq = 0
    try:
        last_seen = int(work_status.get("last_seen_steering_seq") or 0)
    except Exception:
        last_seen = 0
    return {
        "pending": steering_seq > last_seen,
        "steering_seq": steering_seq,
        "last_seen_steering_seq": last_seen,
        "steering_action": latest.get("steering_action", ""),
        "steering_instruction": latest.get("steering_instruction", ""),
        "steering_event_id": latest.get("steering_event_id", ""),
    }


def _pending_ack_fields(info: dict | None) -> dict:
    info = info or {}
    if not info.get("pending"):
        return {
            "pending_ack_steering_seq": 0,
            "pending_ack_steering_action": "",
            "pending_ack_steering_instruction": "",
            "pending_ack_steering_event_id": "",
        }
    return {
        "pending_ack_steering_seq": info.get("steering_seq", 0),
        "pending_ack_steering_action": info.get("steering_action", ""),
        "pending_ack_steering_instruction": str(info.get("steering_instruction", ""))[:1000],
        "pending_ack_steering_event_id": info.get("steering_event_id", ""),
    }


def _checksum(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def _context_fields(context: dict | None) -> dict:
    context = context or {}
    return {
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "reply_to_message_id": context.get("reply_to_message_id", ""),
    }


def _public_task(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _compact_task(row: dict, *, now: datetime | None = None) -> dict:
    return _public_task({
        "task_id": row.get("task_id", ""),
        "task_pack_id": row.get("task_pack_id", ""),
        "worker_agent": row.get("worker_agent", ""),
        "state": row.get("state", ""),
        "work_dir": row.get("work_dir", ""),
        "cancel_requested": bool(row.get("cancel_requested")),
        "cancellation_request_id": row.get("cancellation_request_id", ""),
        "cancellation_reason": str(row.get("cancellation_reason", ""))[:240],
        "last_heartbeat_at": row.get("last_heartbeat_at", ""),
        "heartbeat_phase": row.get("heartbeat_phase", ""),
        "heartbeat_note": str(row.get("heartbeat_note", ""))[:240],
        **_work_policy_fields(_work_policy_from_row(row)),
        **_heartbeat_status(row, now=now),
        **_steering_status(row),
        **_progress_report_due_status(row, now=now),
        **_steering_runtime_status(row),
        "release_queue_state": row.get("release_queue_state", ""),
        **_context_fields(row),
    })


def task_attention_score(item: dict) -> int:
    if item.get("pending_steering_ack"):
        return 100
    runtime = item.get("steering_runtime_state") or ""
    if runtime in {"revise_detected", "revise_restarting"}:
        return 95
    if runtime in {"revise_applied", "progress_seen", "progress_requested"}:
        return 80
    if item.get("progress_report_due"):
        return 70
    if item.get("heartbeat_stale"):
        return 60
    if item.get("cancel_requested") or item.get("state") == "cancel_requested":
        return 50
    if item.get("progress_report_requested_since_heartbeat"):
        return 40
    return 0


def prioritized_active_items(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda item: (-task_attention_score(item), str(item.get("task_id") or "")),
    )


def _latest_tasks(rows: list[dict]) -> dict:
    latest_by_task = {}
    for idx, row in enumerate(rows):
        task_id = row.get("task_id")
        if task_id:
            latest_by_task[task_id] = {**row, "_row_index": idx}
    return latest_by_task


def _release_followup_missing_items(rows: list[dict]) -> list[dict]:
    finalized = {}
    followups = {}
    for idx, row in enumerate(rows):
        task_id = row.get("task_id")
        if not task_id:
            continue
        event = str(row.get("event") or "")
        if event == "task_finalized" and row.get("state") == "done":
            finalized[task_id] = {**row, "_row_index": idx}
        elif event in RELEASE_FOLLOWUP_EVENTS:
            followups[task_id] = idx
    missing = []
    for task_id, row in finalized.items():
        if followups.get(task_id, -1) > row.get("_row_index", -1):
            continue
        missing.append(_public_task({
            "task_id": task_id,
            "task_pack_id": row.get("task_pack_id", ""),
            "worker_agent": row.get("worker_agent", ""),
            "work_dir": row.get("work_dir", ""),
            "finalized_at": row.get("finalized_at", ""),
            "release_queue_state": row.get("release_queue_state", ""),
            "release_enqueue_error": row.get("release_enqueue_error", ""),
            **_context_fields(row),
        }))
    return missing


def _latest_task(space: str, task_id: str) -> dict:
    rows, error = _rows_with_error(_registry_path(space))
    if error:
        raise TaskRegistryError(error)
    wanted = str(task_id or "").strip()
    if not wanted:
        raise TaskRegistryError("task_id required")
    for row in reversed(rows):
        if row.get("task_id") == wanted:
            return row
    raise TaskRegistryError(f"task not found: {wanted}")


def get_task(space: str, task_id: str) -> dict:
    return _with_lock(space, lambda: _latest_task(space, task_id))


def default_context(space: str) -> dict:
    rows = read(space, None)
    if rows:
        return orchestration.context_from_message(rows[-1], space)
    return {
        "space_id": space,
        "intent_id": "",
        "conversation_thread_id": "",
        "room_generation": orchestration.current_generation(space),
        "source_event_seq": None,
        "source_message_id": "",
        "reply_to_message_id": "",
    }


def _discovery_manifest(hits: list[tuple[int, dict]] | None) -> dict:
    considered = []
    for score, item in hits or []:
        considered.append({
            "score": score,
            "type": item.get("type", ""),
            "name": item.get("name", ""),
            "path": item.get("path", ""),
            "description": item.get("description", ""),
            "entry": item.get("entry", ""),
        })
    return {
        "schema": "DiscoveryManifest.v1",
        "considered": considered,
        "selected": [],
        "rejected": [],
        "rediscovery_queries": [],
    }


def runtime_capabilities(runtime_info: dict, work_policy: dict | None = None) -> dict:
    engine = runtime_info.get("engine", "")
    model = runtime_info.get("model", "")
    supports_shell = engine == "codex"
    supports_parallel = engine == "codex"
    policy = work_settings.normalize_settings(work_policy or {})
    return {
        "schema": "RuntimeCapabilityManifest.v1",
        "engine": engine,
        "model": model,
        "runner_timeout_sec": policy["runner_timeout_sec"],
        "heartbeat_interval_sec": policy["heartbeat_interval_sec"],
        "heartbeat_stale_ms": policy["heartbeat_stale_ms"],
        "progress_report_due_ms": policy["progress_report_due_ms"],
        "supports_file_edit": True,
        "supports_shell": supports_shell,
        "supports_network": False,
        "supports_planning": True,
        "supports_native_subagents": False,
        "supports_parallel_tool_calls": supports_parallel,
        "supports_mcp_resources": False,
        "supports_image_inspection": False,
        "max_context_tokens_class": "unknown",
        "known_limitations": [
            "native CnvAgentWorld child task creation is reserved for TaskRegistry/space manager",
            "network capability is conservative false unless a later runtime probe enables it",
        ],
        "source": "runtime.resolve_runtime+conservative_v0",
        "detected_at": now_iso(),
        "validated_by": "task_registry_v0_adapter",
    }


def execution_strategy(task_id: str, task_pack_id: str, capabilities: dict, objective: str) -> dict:
    available = []
    unavailable = []
    if capabilities.get("supports_planning"):
        available.append("planning")
    if capabilities.get("supports_file_edit"):
        available.append("file_edit")
    if capabilities.get("supports_shell"):
        available.append("shell")
    if capabilities.get("supports_parallel_tool_calls"):
        available.append("parallel_tool_calls")
    if not capabilities.get("supports_native_subagents"):
        unavailable.append("native_subagents")
    if not capabilities.get("supports_network"):
        unavailable.append("network")
    return {
        "schema": "ExecutionStrategy.v1",
        "task_id": task_id,
        "task_pack_id": task_pack_id,
        "strategy": "plan_then_execute",
        "why": "TaskPack v0 adapter default: 먼저 범위를 확인하고 실행/검증 후 release_request 초안을 남긴다.",
        "objective_excerpt": objective[:240],
        "planned_steps": [
            "TaskPack, runtime_capabilities, 지시, 발견후보를 확인한다.",
            "허용된 작업 폴더 범위 안에서 작업한다.",
            "결과.md와 상태.json을 갱신하고 필요한 경우 레슨적용보고.json을 남긴다.",
        ],
        "capabilities_to_use": available,
        "capabilities_not_available": unavailable,
        "needs_manager_child_tasks": False,
        "child_task_requests": [],
        "created_at": now_iso(),
    }


def create_task(
    space: str,
    *,
    worker: str,
    task_id: str,
    objective: str,
    work_dir: Path,
    runtime_info: dict,
    context: dict | None = None,
    discovery_hits: list[tuple[int, dict]] | None = None,
    work_policy: dict | None = None,
    requested_by: str = "legacy_engine_work",
    approved_by: str = "task_registry_v0_adapter",
) -> dict:
    context = context or default_context(space)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "steering").mkdir(exist_ok=True)
    task_settings_path = work_dir / work_settings.SETTINGS_FILENAME
    preexisting_task_settings = task_settings_path.exists() and not (work_dir / "task_pack.json").exists()
    work_policy = (
        work_settings.normalize_settings(work_policy)
        if work_policy
        else work_settings.resolve_work_settings(space, worker, work_dir=work_dir if preexisting_task_settings else None)
    )
    work_policy_fields = _work_policy_fields(work_policy)
    role_path = Path("../../../../role.md")
    law_path = Path("../../../../../../law.md")
    law_work_path = Path("../../../../../../law_work.md")
    discovery_path = Path("발견후보.md")
    instruction_path = Path("지시.md")
    lesson_pack = lesson_ledger.build_lesson_pack(
        space,
        mode="work",
        context=context,
        event=objective,
        target_agent=worker,
    )
    discovery_manifest = _discovery_manifest(discovery_hits)
    identity = {
        "space_id": space,
        "task_id": task_id,
        "worker_agent": worker,
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "lesson_pack_hash": _checksum(lesson_pack),
    }
    task_pack_id = _stable_id("taskpack", identity)
    release_request_path = "release_request.json"
    pack = {
        "schema": "TaskPack.compat_minimal.v1",
        "task_pack_id": task_pack_id,
        "created_at": now_iso(),
        **identity,
        "requested_by": requested_by,
        "approved_by": approved_by,
        "objective": objective,
        "inputs": [{"type": "legacy_task_text", "id": task_id}],
        "constraints": [
            "다른 공간이나 다른 intent의 기억과 섞지 않는다.",
            "결과는 직접 방에 공개하지 않고 ReleaseRequest 초안으로 공간관리에게 제출한다.",
            "TaskPack scope 밖 파일/도구/외부 부작용은 사용하지 않는다.",
        ],
        "allowed_paths": [_rel(work_dir)],
        "instruction_files": [
            str(role_path),
            str(law_path),
            str(law_work_path),
            str(instruction_path),
            str(discovery_path),
            "task_pack.json",
            "task_handoff_pack.json",
            "runtime_capabilities.json",
            "execution_strategy.json",
        ],
        "scope": {
            "read_paths": [
                _rel(work_dir),
                _rel((work_dir / role_path).resolve()),
                _rel((work_dir / law_path).resolve()),
                _rel((work_dir / law_work_path).resolve()),
            ],
            "write_paths": [_rel(work_dir)],
            "execute_paths": [],
            "forbidden_paths": ["공간/*/대화.jsonl", "에이전트/*/공간/*/대화.jsonl"],
            "allowed_tools": [],
            "network_policy": "none",
            "external_side_effects": "forbidden",
        },
        "output_contract": {
            "result_path": "결과.md",
            "status_path": "상태.json",
            "work_status_path": "work_status.json",
            "release_request_path": release_request_path,
            "lesson_application_report_path": "레슨적용보고.json",
        },
        "release_policy": {
            "release_to": "space_manager",
            "do_not_publish_directly": True,
            "release_request_schema": "ReleaseRequest.v1",
            "enqueue_release_queue": True,
            "enqueue_when": ["done"],
            "adapter_note": "ReleaseQueue v0 enqueues completed work for space-manager approval. partial/blocked/error stay draft/not_enqueued.",
        },
        "steering": {
            "queue_path": "steering/",
            "poll_interval_sec": work_policy["heartbeat_interval_sec"],
            "last_seen_steering_seq": 0,
            "on_cancel": "checkpoint_then_stop",
            "on_generation_mismatch": "stop_and_submit_stale_status",
        },
        "work_runtime_policy": work_policy,
        "cancellation": {
            "check_file": "취소요청.json",
            "policy": "cooperative_first",
        },
        "lesson_pack": lesson_pack,
        "discovery_manifest": discovery_manifest,
    }
    pack["task_pack_checksum"] = _checksum(pack)
    capabilities = runtime_capabilities(runtime_info, work_policy)
    strategy = execution_strategy(task_id, task_pack_id, capabilities, objective)
    handoff = {
        "schema": "TaskHandoffPack.compat_minimal.v1",
        "task_handoff_id": _stable_id("taskhandoff", task_pack_id, worker),
        "task_pack_id": task_pack_id,
        "task_pack_checksum": pack["task_pack_checksum"],
        "origin_space_id": space,
        "origin_intent_id": context.get("intent_id", ""),
        "origin_thread_id": context.get("conversation_thread_id", ""),
        "requested_by_agent_id": requested_by,
        "approved_by": approved_by,
        "representative_request_summary": objective[:500],
        "chat_context_summary": "legacy engine.work adapter context",
        "reply_after_done": pack["release_policy"],
        "created_at": now_iso(),
    }
    status = {
        "schema": "WorkStatus.v1",
        "task_id": task_id,
        "task_pack_id": task_pack_id,
        "state": "running",
        "worker_agent": worker,
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "last_heartbeat_at": now_iso(),
        "heartbeat_phase": "task_created",
        "heartbeat_count": 0,
        "cancel_requested": False,
        "cancellation_request_id": "",
        "cancellation_reason": "",
        "steering_queue_path": "steering/",
        "last_seen_steering_seq": 0,
        **work_policy_fields,
        "verification": {"status": "not_run", "not_run_reason": "작업 실행 전"},
        "discovery_manifest_path": "discovery_manifest.json",
        **_context_fields(context),
    }
    _write_json(work_dir / "task_pack.json", pack)
    _write_json(work_dir / "task_handoff_pack.json", handoff)
    _write_json(work_dir / "runtime_capabilities.json", capabilities)
    _write_json(work_dir / "execution_strategy.json", strategy)
    _write_json(work_dir / "discovery_manifest.json", discovery_manifest)
    _write_json(work_dir / work_settings.SETTINGS_FILENAME, work_policy)
    _write_json(work_dir / "work_status.json", status)
    _write_json(work_dir / "상태.json", {"상태": "running", "시작": status["started_at"], "task_pack_id": task_pack_id})

    row = {
        "schema": "TaskRegistryEvent.v1",
        "event_id": _stable_id("task_event", space, task_id, "task_created", task_pack_id, pack["task_pack_checksum"]),
        "event": "task_created",
        "state": "running",
        "space_id": space,
        "task_id": task_id,
        "task_pack_id": task_pack_id,
        "task_pack_checksum": pack["task_pack_checksum"],
        "worker_agent": worker,
        "work_dir": _rel(work_dir),
        "last_heartbeat_at": status["last_heartbeat_at"],
        "heartbeat_phase": status["heartbeat_phase"],
        "heartbeat_count": status["heartbeat_count"],
        **work_policy_fields,
        "cancel_requested": False,
        "cancellation_request_id": "",
        "lesson_pack_status": lesson_pack.get("lesson_pack_status", ""),
        "included_lessons": lesson_pack.get("included_lessons", []),
        "must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in lesson_pack.get("must_apply", [])
            if lesson.get("lesson_id")
        ],
        "created_at": now_iso(),
        **_context_fields(context),
    }
    manifest = {
        "schema": "TaskPackManifest.v1",
        "manifest_id": _stable_id("task_manifest", space, task_id, task_pack_id, pack["task_pack_checksum"]),
        "state": "task_pack_delivered",
        "delivered_at": now_iso(),
        **{k: row.get(k) for k in (
            "space_id", "task_id", "task_pack_id", "task_pack_checksum", "worker_agent",
            "work_dir", "lesson_pack_status", "included_lessons", "must_apply_lessons",
            "intent_id", "conversation_thread_id", "room_generation", "source_event_seq", "source_message_id",
        )},
    }

    def mutate():
        event_result = _append_unique(_registry_path(space), row, "event_id")
        manifest_result = _append_unique(_manifest_path(space), manifest, "manifest_id")
        row.update({"duplicate": bool(event_result.get("duplicate"))})
        manifest.update({"duplicate": bool(manifest_result.get("duplicate"))})
        return {"task_pack": pack, "handoff": handoff, "capabilities": capabilities, "strategy": strategy, "status": status, "manifest": manifest}

    return _with_lock(space, mutate)


def _read_work_status(work_dir: Path) -> dict:
    return _read_json(work_dir / "work_status.json", {})


def _cancel_info(space: str, task_id: str, work_dir: Path) -> dict:
    info = {
        "cancel_requested": False,
        "cancellation_request_id": "",
        "cancellation_reason": "",
        "cancel_requested_at": "",
    }
    status = _read_work_status(work_dir)
    if status.get("cancel_requested"):
        info.update({
            "cancel_requested": True,
            "cancellation_request_id": status.get("cancellation_request_id", ""),
            "cancellation_reason": status.get("cancellation_reason", ""),
            "cancel_requested_at": status.get("cancel_requested_at", ""),
        })
    cancel_file = _read_json(work_dir / "취소요청.json", {})
    if cancel_file:
        info.update({
            "cancel_requested": True,
            "cancellation_request_id": cancel_file.get("cancellation_request_id", info.get("cancellation_request_id", "")),
            "cancellation_reason": cancel_file.get("reason", info.get("cancellation_reason", "")),
            "cancel_requested_at": cancel_file.get("requested_at", info.get("cancel_requested_at", "")),
        })
    rows, error = _rows_with_error(_registry_path(space))
    if error:
        raise TaskRegistryError(error)
    for row in reversed(rows):
        if row.get("task_id") != task_id:
            continue
        if row.get("cancel_requested") or row.get("state") == "cancel_requested" or row.get("event") == "task_cancel_requested":
            info.update({
                "cancel_requested": True,
                "cancellation_request_id": row.get("cancellation_request_id", info.get("cancellation_request_id", "")),
                "cancellation_reason": row.get("cancellation_reason", info.get("cancellation_reason", "")),
                "cancel_requested_at": row.get("cancel_requested_at", info.get("cancel_requested_at", "")),
            })
            break
    return info


def _write_work_status(work_dir: Path, update: dict) -> dict:
    current = _read_work_status(work_dir)
    status = {**current, **update, "updated_at": now_iso()}
    if "schema" not in status:
        status["schema"] = "WorkStatus.v1"
    _write_json(work_dir / "work_status.json", status)
    return status


def _next_steering_seq(work_dir: Path) -> int:
    steering_dir = work_dir / "steering"
    steering_dir.mkdir(parents=True, exist_ok=True)
    highest = 0
    for path in steering_dir.glob("*.json"):
        try:
            highest = max(highest, int(path.name.split("_", 1)[0]))
        except Exception:
            continue
    return highest + 1


def _write_steering_event(work_dir: Path, event: dict) -> dict:
    steering_dir = work_dir / "steering"
    steering_dir.mkdir(parents=True, exist_ok=True)
    for path in steering_dir.glob("*.json"):
        data = _read_json(path, {})
        if data.get("steering_event_id") == event.get("steering_event_id"):
            return {**data, "duplicate": True}
    seq = _next_steering_seq(work_dir)
    row = {**event, "steering_seq": seq}
    path = steering_dir / f"{seq:06d}_{event.get('action', 'event')}.json"
    _write_json(path, row)
    return row


def request_cancel(
    space: str,
    task_id: str,
    *,
    actor: str = "대표",
    reason: str = "",
    room_generation_at_request=None,
    source_event_seq=None,
) -> dict:
    reason = str(reason or "")[:500]

    def mutate():
        rows, error = _rows_with_error(_registry_path(space))
        if error:
            raise TaskRegistryError(error)
        latest = {}
        for row in reversed(rows):
            if row.get("task_id") == task_id:
                latest = row
                break
        if not latest:
            raise TaskRegistryError(f"task not found: {task_id}")
        if latest.get("state") in {"done", "error", "blocked", "partial_ready", "cancelled"}:
            raise TaskRegistryError("task is already closed")
        cancellation_request_id = _stable_id(
            "task_cancel",
            space,
            task_id,
            latest.get("task_pack_id", ""),
        )
        if latest.get("cancel_requested") or latest.get("state") == "cancel_requested":
            return {
                "event": latest,
                "duplicate": True,
                "cancellation_request_id": latest.get("cancellation_request_id", cancellation_request_id),
            }
        policy_fields = _work_policy_fields(_work_policy_from_row(latest))
        work_dir = ROOT / str(latest.get("work_dir", ""))
        if not latest.get("work_dir") or not work_dir.exists():
            raise TaskRegistryError("task work_dir missing")
        policy_fields = _work_policy_fields(_work_policy_from_row(latest))
        requested_at = now_iso()
        cancel_request = {
            "schema": "TaskCancelRequest.v1",
            "cancellation_request_id": cancellation_request_id,
            "space_id": space,
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "worker_agent": latest.get("worker_agent", ""),
            "requested_by": actor,
            "reason": reason,
            "requested_at": requested_at,
            "policy": "cooperative_first",
            "room_generation_at_request": room_generation_at_request,
            "source_event_seq": source_event_seq,
            "control_request_room_generation": room_generation_at_request,
            "control_request_source_event_seq": source_event_seq,
            **_context_fields(latest),
        }
        _write_json(work_dir / "취소요청.json", cancel_request)
        steering = _write_steering_event(work_dir, {
            "schema": "TaskSteeringEvent.v1",
            "steering_event_id": cancellation_request_id,
            "action": "cancel_requested",
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "requested_by": actor,
            "reason": reason,
            "created_at": requested_at,
        })
        _write_work_status(work_dir, {
            "state": "cancel_requested",
            "cancel_requested": True,
            "cancellation_request_id": cancellation_request_id,
            "cancellation_reason": reason,
            "cancel_requested_at": requested_at,
            "last_seen_steering_seq": steering.get("steering_seq", 0),
            "control_request_room_generation": room_generation_at_request,
            "control_request_source_event_seq": source_event_seq,
            **policy_fields,
        })
        event = {
            "schema": "TaskRegistryEvent.v1",
            "event_id": _stable_id("task_event", space, task_id, "task_cancel_requested", latest.get("task_pack_id", "")),
            "event": "task_cancel_requested",
            "state": "cancel_requested",
            "space_id": space,
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "task_pack_checksum": latest.get("task_pack_checksum", ""),
            "worker_agent": latest.get("worker_agent", ""),
            "work_dir": latest.get("work_dir", ""),
            "cancel_requested": True,
            "cancellation_request_id": cancellation_request_id,
            "cancellation_reason": reason,
            "cancel_requested_by": actor,
            "cancel_requested_at": requested_at,
            "steering_seq": steering.get("steering_seq", 0),
            "control_request_room_generation": room_generation_at_request,
            "control_request_source_event_seq": source_event_seq,
            "control_request_actor": actor,
            **policy_fields,
            **_context_fields(latest),
        }
        result = _append_unique(_registry_path(space), event, "event_id")
        return {
            "event": result.get("record") or event,
            "duplicate": bool(result.get("duplicate")),
            "cancellation_request": cancel_request,
            "steering": steering,
            "cancellation_request_id": cancellation_request_id,
        }

    return _with_lock(space, mutate)


def request_steering(
    space: str,
    task_id: str,
    *,
    action: str,
    instruction: str = "",
    actor: str = "대표",
    room_generation_at_request=None,
    source_event_seq=None,
    reason_code: str = "",
    dedupe_key: str = "",
    only_if_progress_due: bool = False,
) -> dict:
    action = str(action or "").strip()
    if action not in {"request_progress", "revise_task"}:
        raise TaskRegistryError(f"unsupported steering action: {action}")
    instruction = str(instruction or "")[:1000]
    reason_code = str(reason_code or "")[:120]
    dedupe_key = str(dedupe_key or "")[:240]

    def mutate():
        rows, error = _rows_with_error(_registry_path(space))
        if error:
            raise TaskRegistryError(error)
        latest = {}
        for row in reversed(rows):
            if row.get("task_id") == task_id:
                latest = row
                break
        if not latest:
            raise TaskRegistryError(f"task not found: {task_id}")
        if latest.get("state") in {"done", "error", "blocked", "partial_ready", "cancelled"}:
            if only_if_progress_due:
                return {"event": latest, "duplicate": True, "skipped": True, "skip_reason": "task_closed"}
            raise TaskRegistryError("task is already closed")
        if latest.get("state") == "cancel_requested" or latest.get("cancel_requested"):
            if only_if_progress_due:
                return {"event": latest, "duplicate": True, "skipped": True, "skip_reason": "task_cancellation_requested"}
            raise TaskRegistryError("task cancellation is already requested")
        if only_if_progress_due:
            due_status = _progress_report_due_status(latest)
            if not due_status.get("progress_report_due"):
                skip_reason = "progress_already_requested" if due_status.get("progress_report_requested_since_heartbeat") else "progress_not_due"
                if latest.get("pending_steering_ack"):
                    skip_reason = "pending_steering_ack"
                return {"event": latest, "duplicate": True, "skipped": True, "skip_reason": skip_reason}
        work_dir = ROOT / str(latest.get("work_dir", ""))
        if not latest.get("work_dir") or not work_dir.exists():
            raise TaskRegistryError("task work_dir missing")
        policy_fields = _work_policy_fields(_work_policy_from_row(latest))
        requested_at = now_iso()
        unique_progress_part = dedupe_key or (uuid4().hex if action == "request_progress" else "")
        steering_event_id = _stable_id(
            "task_steering",
            space,
            task_id,
            latest.get("task_pack_id", ""),
            action,
            actor,
            instruction,
            reason_code,
            unique_progress_part,
        )
        requires_ack = action == "revise_task"
        steering = _write_steering_event(work_dir, {
            "schema": "TaskSteeringEvent.v1",
            "steering_event_id": steering_event_id,
            "action": action,
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "requested_by": actor,
            "instruction": instruction,
            "reason": instruction,
            "reason_code": reason_code,
            "dedupe_key": dedupe_key,
            "requires_worker_ack": requires_ack,
            "created_at": requested_at,
            "room_generation_at_request": room_generation_at_request,
            "source_event_seq": source_event_seq,
            "control_request_room_generation": room_generation_at_request,
            "control_request_source_event_seq": source_event_seq,
        })
        steering_seq = int(steering.get("steering_seq") or 0)
        actual_requested_at = steering.get("created_at", requested_at)
        current_status = _read_work_status(work_dir)
        last_seen_steering_seq = int(current_status.get("last_seen_steering_seq") or 0)
        existing_pending_ack = _pending_ack_steering_from_rows(rows, task_id, current_status)
        current_pending_ack = {
            "pending": True,
            "steering_seq": steering_seq,
            "steering_action": action,
            "steering_instruction": instruction,
            "steering_event_id": steering_event_id,
        } if requires_ack and steering_seq > last_seen_steering_seq else {}
        effective_pending_ack = current_pending_ack if current_pending_ack else existing_pending_ack
        pending_ack = bool(current_pending_ack or existing_pending_ack.get("pending"))
        if not steering.get("duplicate"):
            _write_work_status(work_dir, {
                "state": current_status.get("state") or latest.get("state") or "running",
                "latest_steering_seq": steering_seq,
                "latest_steering_action": action,
                "latest_steering_instruction": instruction,
                "latest_steering_requested_at": actual_requested_at,
                "latest_steering_requested_by": actor,
                "latest_steering_reason_code": reason_code,
                "latest_steering_dedupe_key": dedupe_key,
                "latest_steering_control_request_source_event_seq": source_event_seq,
                "latest_steering_control_request_room_generation": room_generation_at_request,
                "latest_steering_requires_ack": requires_ack,
                "pending_steering_ack": pending_ack,
                **_pending_ack_fields(effective_pending_ack),
                **policy_fields,
            })
        event = {
            "schema": "TaskRegistryEvent.v1",
            "event_id": _stable_id("task_event", space, task_id, "task_steering_requested", steering_event_id),
            "event": "task_steering_requested",
            "state": latest.get("state") or "running",
            "space_id": space,
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "task_pack_checksum": latest.get("task_pack_checksum", ""),
            "worker_agent": latest.get("worker_agent", ""),
            "work_dir": latest.get("work_dir", ""),
            "cancel_requested": False,
            "steering_event_id": steering_event_id,
            "steering_seq": steering_seq,
            "steering_action": action,
            "steering_instruction": instruction,
            "steering_requested_by": actor,
            "steering_requested_at": actual_requested_at,
            "steering_reason_code": reason_code,
            "steering_dedupe_key": dedupe_key,
            "latest_steering_seq": steering_seq,
            "latest_steering_action": action,
            "latest_steering_instruction": instruction,
            "latest_steering_requested_at": actual_requested_at,
            "latest_steering_requested_by": actor,
            "latest_steering_reason_code": reason_code,
            "latest_steering_dedupe_key": dedupe_key,
            "latest_steering_control_request_source_event_seq": source_event_seq,
            "latest_steering_control_request_room_generation": room_generation_at_request,
            "requires_worker_ack": requires_ack,
            "last_seen_steering_seq": last_seen_steering_seq,
            "pending_steering_ack": pending_ack,
            **_pending_ack_fields(effective_pending_ack),
            "control_request_room_generation": room_generation_at_request,
            "control_request_source_event_seq": source_event_seq,
            "control_request_actor": actor,
            "last_heartbeat_at": latest.get("last_heartbeat_at", ""),
            "heartbeat_phase": latest.get("heartbeat_phase", ""),
            "heartbeat_note": latest.get("heartbeat_note", ""),
            **policy_fields,
            **_context_fields(latest),
        }
        result = _append_unique(_registry_path(space), event, "event_id")
        return {
            "event": result.get("record") or event,
            "duplicate": bool(result.get("duplicate") or steering.get("duplicate")),
            "steering": steering,
            "steering_event_id": steering_event_id,
            "steering_seq": steering_seq,
            "steering_reason_code": reason_code,
            "steering_dedupe_key": dedupe_key,
            "requires_worker_ack": requires_ack,
            "pending_steering_ack": pending_ack,
        }

    return _with_lock(space, mutate)


def update_task_work_settings(
    space: str,
    task_id: str,
    settings: dict | None = None,
    *,
    actor: str = "대표",
) -> dict:
    def mutate():
        rows, error = _rows_with_error(_registry_path(space))
        if error:
            raise TaskRegistryError(error)
        latest = {}
        for row in reversed(rows):
            if row.get("task_id") == task_id:
                latest = row
                break
        if not latest:
            raise TaskRegistryError(f"task not found: {task_id}")
        if latest.get("state") in {"done", "error", "blocked", "partial_ready", "cancelled"}:
            raise TaskRegistryError("task is already closed")
        work_dir = ROOT / str(latest.get("work_dir", ""))
        if not latest.get("work_dir") or not work_dir.exists():
            raise TaskRegistryError("task work_dir missing")
        current_policy = _work_policy_from_row(latest)
        merged_policy = work_settings.normalize_settings({**current_policy, **(settings or {})})
        merged_policy["settings_source"] = "task_update"
        merged_policy["source_chain"] = [
            *(latest.get("work_settings_source_chain") or []),
            f"task:{task_id}",
        ]
        policy_fields = _work_policy_fields(merged_policy)
        updated_at = now_iso()
        work_settings.write_folder_settings(
            work_dir,
            merged_policy,
            source=f"task-work-settings:{space}:{task_id}",
        )
        status = _write_work_status(work_dir, {
            **policy_fields,
            "work_settings_updated_by": actor,
            "work_settings_updated_at": updated_at,
        })
        event = {
            "schema": "TaskRegistryEvent.v1",
            "event_id": _stable_id("task_event", space, task_id, "task_work_settings_updated", updated_at, uuid4().hex),
            "event": "task_work_settings_updated",
            "state": latest.get("state") or status.get("state") or "running",
            "space_id": space,
            "task_id": task_id,
            "task_pack_id": latest.get("task_pack_id", ""),
            "task_pack_checksum": latest.get("task_pack_checksum", ""),
            "worker_agent": latest.get("worker_agent", ""),
            "work_dir": latest.get("work_dir", ""),
            "cancel_requested": bool(latest.get("cancel_requested")),
            "cancellation_request_id": latest.get("cancellation_request_id", ""),
            "cancellation_reason": latest.get("cancellation_reason", ""),
            "last_heartbeat_at": latest.get("last_heartbeat_at", ""),
            "heartbeat_phase": latest.get("heartbeat_phase", ""),
            "heartbeat_note": latest.get("heartbeat_note", ""),
            "latest_steering_seq": latest.get("latest_steering_seq", latest.get("steering_seq", 0)),
            "latest_steering_action": latest.get("latest_steering_action", latest.get("steering_action", "")),
            "latest_steering_instruction": latest.get("latest_steering_instruction", latest.get("steering_instruction", "")),
            "latest_steering_requested_at": latest.get("latest_steering_requested_at", latest.get("steering_requested_at", "")),
            "latest_steering_requested_by": latest.get("latest_steering_requested_by", latest.get("steering_requested_by", "")),
            "latest_steering_reason_code": latest.get("latest_steering_reason_code", latest.get("steering_reason_code", "")),
            "pending_steering_ack": bool(latest.get("pending_steering_ack")),
            "work_settings_updated_by": actor,
            "work_settings_updated_at": updated_at,
            **policy_fields,
            **_context_fields(latest),
        }
        _append_jsonl(_registry_path(space), event)
        return {"event": event, "work_status": status, "work_settings": merged_policy}

    return _with_lock(space, mutate)


def request_due_progress_reports(
    space: str,
    *,
    actor: str = "공간관리",
    instruction: str = "",
    room_generation_at_request=None,
    source_event_seq=None,
) -> dict:
    instruction = (instruction or "").strip() or (
        "작업 heartbeat가 기준 시간을 넘었습니다. 현재 진행 상황, 막힌 점, 다음 단계, "
        "부분 결과를 work_status/상태에 남기고 가능한 한 빨리 heartbeat를 갱신해줘."
    )
    snap = snapshot(space)
    requested = []
    duplicates = []
    skipped = []
    errors = []
    for item in snap.get("progress_report_due_items") or []:
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            continue
        try:
            result = request_steering(
                space,
                task_id,
                action="request_progress",
                instruction=instruction,
                actor=actor,
                room_generation_at_request=(
                    room_generation_at_request
                    if room_generation_at_request is not None
                    else item.get("room_generation")
                ),
                source_event_seq=(
                    source_event_seq
                    if source_event_seq is not None
                    else item.get("source_event_seq")
                ),
                reason_code=TASK_PROGRESS_REPORT_DUE_REASON_CODE,
                dedupe_key=item.get("progress_report_due_key") or _progress_report_due_key(item),
                only_if_progress_due=True,
            )
        except Exception as exc:
            errors.append({
                "task_id": task_id,
                "worker_agent": item.get("worker_agent", ""),
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
            })
            continue
        if result.get("skipped"):
            skipped.append({
                "task_id": task_id,
                "worker_agent": item.get("worker_agent", ""),
                "skip_reason": result.get("skip_reason", ""),
                "event": result.get("event") or {},
            })
            continue
        target = duplicates if result.get("duplicate") else requested
        target.append({
            "task_id": task_id,
            "worker_agent": item.get("worker_agent", ""),
            "heartbeat_age_ms": item.get("heartbeat_age_ms"),
            "heartbeat_phase": item.get("heartbeat_phase", ""),
            "steering_seq": result.get("steering_seq", 0),
            "steering_event_id": result.get("steering_event_id", ""),
            "duplicate": bool(result.get("duplicate")),
            "event": result.get("event") or {},
        })
    return {
        "ok": not errors,
        "requested_count": len(requested),
        "duplicate_count": len(duplicates),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "requested": requested,
        "duplicates": duplicates,
        "skipped": skipped,
        "errors": errors,
        "threshold_ms": TASK_PROGRESS_REPORT_DUE_MS,
    }


def _pending_ack_steering(space: str, task_id: str, work_status: dict) -> dict:
    rows, error = _rows_with_error(_registry_path(space))
    if error:
        raise TaskRegistryError(error)
    return _pending_ack_steering_from_rows(rows, task_id, work_status)


def record_heartbeat(
    space: str,
    *,
    task_id: str,
    worker: str,
    work_dir: Path,
    task_pack: dict,
    phase: str,
    note: str = "",
) -> dict:
    def mutate():
        rows, error = _rows_with_error(_registry_path(space))
        if error:
            raise TaskRegistryError(error)
        latest = {}
        for row in reversed(rows):
            if row.get("task_id") == task_id:
                latest = row
                break
        current_status = _read_work_status(work_dir)
        closed_states = {"done", "error", "blocked", "partial_ready", "cancelled"}
        if latest.get("state") in closed_states:
            return {
                "event": latest,
                "work_status": current_status,
                "skipped": True,
                "reason": "task_closed",
            }
        ts = now_iso()
        cancel_requested = bool(
            current_status.get("cancel_requested")
            or latest.get("cancel_requested")
            or latest.get("state") == "cancel_requested"
        )
        next_state = "cancel_requested" if cancel_requested else "running"
        try:
            latest_steering_seq = int(current_status.get("latest_steering_seq") or latest.get("latest_steering_seq") or latest.get("steering_seq") or 0)
        except Exception:
            latest_steering_seq = 0
        try:
            last_seen_steering_seq = int(current_status.get("last_seen_steering_seq") or 0)
        except Exception:
            last_seen_steering_seq = 0
        pending_ack_info = _pending_ack_steering_from_rows(rows, task_id, current_status)
        pending_steering_ack = bool(pending_ack_info.get("pending"))
        latest_steering_requires_ack = bool(
            current_status.get("latest_steering_requires_ack")
            or latest.get("latest_steering_requires_ack")
            or latest.get("requires_worker_ack")
            or pending_steering_ack
        )
        policy_seed = {
            **(task_pack.get("work_runtime_policy") or {}),
            **_work_policy_overrides_from_row(current_status),
            **_work_policy_overrides_from_row(latest),
        }
        policy_fields = _work_policy_fields(policy_seed)
        status = {
            **current_status,
            "schema": "WorkStatus.v1",
            "task_id": task_id,
            "task_pack_id": task_pack.get("task_pack_id", ""),
            "state": next_state,
            "worker_agent": worker,
            "last_heartbeat_at": ts,
            "heartbeat_phase": str(phase or "")[:120],
            "heartbeat_note": str(note or "")[:500],
            "heartbeat_count": int((current_status.get("heartbeat_count") or 0)) + 1,
            "cancel_requested": cancel_requested,
            "cancellation_request_id": current_status.get("cancellation_request_id", latest.get("cancellation_request_id", "")),
            "cancellation_reason": current_status.get("cancellation_reason", latest.get("cancellation_reason", "")),
            "latest_steering_seq": latest_steering_seq,
            "latest_steering_action": current_status.get("latest_steering_action", latest.get("latest_steering_action", latest.get("steering_action", ""))),
            "latest_steering_instruction": current_status.get("latest_steering_instruction", latest.get("latest_steering_instruction", latest.get("steering_instruction", ""))),
            "latest_steering_requested_at": current_status.get("latest_steering_requested_at", latest.get("latest_steering_requested_at", latest.get("steering_requested_at", ""))),
            "latest_steering_requested_by": current_status.get("latest_steering_requested_by", latest.get("latest_steering_requested_by", latest.get("steering_requested_by", ""))),
            "latest_steering_reason_code": current_status.get("latest_steering_reason_code", latest.get("latest_steering_reason_code", latest.get("steering_reason_code", ""))),
            "latest_steering_dedupe_key": current_status.get("latest_steering_dedupe_key", latest.get("latest_steering_dedupe_key", latest.get("steering_dedupe_key", ""))),
            "latest_steering_control_request_source_event_seq": current_status.get("latest_steering_control_request_source_event_seq", latest.get("latest_steering_control_request_source_event_seq", latest.get("control_request_source_event_seq"))),
            "latest_steering_control_request_room_generation": current_status.get("latest_steering_control_request_room_generation", latest.get("latest_steering_control_request_room_generation", latest.get("control_request_room_generation"))),
            "latest_steering_requires_ack": latest_steering_requires_ack,
            "pending_steering_ack": pending_steering_ack,
            **_pending_ack_fields(pending_ack_info),
            "updated_at": ts,
            **policy_fields,
            **_context_fields(task_pack),
        }
        _write_json(work_dir / "work_status.json", status)
        event = {
            "schema": "TaskRegistryEvent.v1",
            "event_id": _stable_id("task_event", space, task_id, "task_heartbeat", phase, ts),
            "event": "task_heartbeat",
            "state": next_state,
            "space_id": space,
            "task_id": task_id,
            "task_pack_id": task_pack.get("task_pack_id", ""),
            "task_pack_checksum": task_pack.get("task_pack_checksum", ""),
            "worker_agent": worker,
            "work_dir": _rel(work_dir),
            "last_heartbeat_at": ts,
            "heartbeat_phase": str(phase or "")[:120],
            "heartbeat_note": str(note or "")[:500],
            "cancel_requested": cancel_requested,
            "cancellation_request_id": status.get("cancellation_request_id", ""),
            "cancellation_reason": status.get("cancellation_reason", ""),
            "latest_steering_seq": status.get("latest_steering_seq", 0),
            "latest_steering_action": status.get("latest_steering_action", ""),
            "latest_steering_instruction": status.get("latest_steering_instruction", ""),
            "latest_steering_requested_at": status.get("latest_steering_requested_at", ""),
            "latest_steering_requested_by": status.get("latest_steering_requested_by", ""),
            "latest_steering_reason_code": status.get("latest_steering_reason_code", ""),
            "latest_steering_dedupe_key": status.get("latest_steering_dedupe_key", ""),
            "latest_steering_control_request_source_event_seq": status.get("latest_steering_control_request_source_event_seq"),
            "latest_steering_control_request_room_generation": status.get("latest_steering_control_request_room_generation"),
            "latest_steering_requires_ack": bool(status.get("latest_steering_requires_ack")),
            "pending_steering_ack": bool(status.get("pending_steering_ack")),
            **_pending_ack_fields(pending_ack_info),
            **policy_fields,
            **_context_fields(task_pack),
        }
        _append_jsonl(_registry_path(space), event)
        return {"event": event, "work_status": status, "skipped": False}

    return _with_lock(space, mutate)


def _read_json(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return data if isinstance(data, dict) else fallback


def _lesson_report_content(work_dir: Path) -> str:
    for name in ("레슨적용보고.json", "lesson_application_report.json"):
        path = work_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def _release_request(task_pack: dict, result: str, raw_status: dict, state: str) -> dict:
    context = _context_fields(task_pack)
    # error(엔진 오류/타임아웃 포함)도 blocked처럼 '보고'로 surface한다 — 조용히 사라지지 않게(완료했는데
    # 마지막 공개 단계만 타임아웃돼 결과가 갇히던 문제 해결). 대표가 산출을 보고 재개/재지시할 수 있다.
    release_kind = "final" if state == "done" else "blocked_report" if state in ("blocked", "error") else "partial"
    verification = raw_status.get("verification") if isinstance(raw_status.get("verification"), dict) else {}
    if not verification:
        verification = {"status": "not_run", "not_run_reason": "legacy adapter did not receive verification report"}
    reason = str(raw_status.get("사유") or raw_status.get("reason") or "").strip()
    summary_body = result[:6000]
    if state == "error":
        # 결과는 완료(결과.md=✅)인데 마지막 엔진 호출만 타임아웃된 경우도 있고, 진짜 중단도 있다.
        # 어느 쪽이든 '자동 보고가 끊겼다'는 사실 + 그때까지 산출을 방에 올려 대표가 판단하게 한다.
        banner = (
            f"⚠️ 작업 중 엔진 오류/타임아웃으로 자동 보고가 끊겼습니다(사유: {reason or '엔진 타임아웃'}). "
            "아래는 그때까지의 산출·진행입니다 — 확인 후 재개/재지시해 주세요.\n\n"
        )
        summary_body = (banner + (result or "(산출물 없음 — 작업 초기에 중단됨)"))[:6000]
    return {
        "schema": "ReleaseRequest.v1",
        "release_id": _stable_id("rel", task_pack.get("task_id", ""), task_pack.get("task_pack_id", ""), state),
        "release_state": "draft",
        "queue_state": "not_enqueued",
        "source_task_id": task_pack.get("task_id", ""),
        "worker_agent": task_pack.get("worker_agent", ""),   # 완료 보고를 워커 명의로 게시하기 위함
        "task_pack_id": task_pack.get("task_pack_id", ""),
        "task_pack_checksum_seen": task_pack.get("task_pack_checksum", ""),
        "task_pack_manifest_hash_seen": "",
        "release_kind": release_kind,
        "room_generation": task_pack.get("room_generation"),
        "source_event_seq": task_pack.get("source_event_seq"),
        "answers_intent_id": task_pack.get("intent_id", ""),
        "public_summary": summary_body,   # 미리보기 경로(결과.md 후반)까지 살리려 1000→6000
        "internal_notes": reason[:1000],
        "completeness": "complete" if state == "done" else "partial",
        "continue_after_release": False,
        "verification": verification,
        "risk_level": "medium",
        "approval_required": True,
        "approval_actor": "space_manager",
        "approval_state": "pending",
        "publish_blocked_until_approval": True,
        "draft_only": True,
        "not_publishable_reason": "not enqueued yet",
        "created_at": now_iso(),
        **context,
    }


def finalize_task(space: str, *, task_id: str, worker: str, work_dir: Path, task_pack: dict, objective: str) -> dict:
    raw_status = _read_json(work_dir / "상태.json", {})
    previous_work_status = _read_work_status(work_dir)
    try:
        latest = _with_lock(space, lambda: _latest_task(space, task_id))
    except TaskRegistryError:
        latest = {}
    result = (work_dir / "결과.md").read_text(encoding="utf-8") if (work_dir / "결과.md").exists() else ""
    state = str(raw_status.get("상태") or raw_status.get("state") or "partial")
    lesson_audit = {}
    hold_reason = ""
    report_content = _lesson_report_content(work_dir)
    if state in {"done", "partial_ready"} or report_content:
        try:
            lesson_audit = lesson_ledger.audit_reply_lesson_applications(
                space,
                content=report_content,
                context_pack={
                    "context_pack_id": task_pack.get("task_pack_id", ""),
                    "context_pack_checksum": task_pack.get("task_pack_checksum", ""),
                    "lesson_pack": task_pack.get("lesson_pack") or {},
                },
                agent=worker,
                mode="work",
            )
        except lesson_ledger.LessonLedgerError as exc:
            hold_reason = str(exc)
            state = "blocked"
            raw_status = {
                **raw_status,
                "상태": "blocked",
                "사유": hold_reason,
                "lesson_application_hold": True,
            }
            _write_json(work_dir / "상태.json", raw_status)

    verification = raw_status.get("verification") if isinstance(raw_status.get("verification"), dict) else {}
    if not verification:
        verification = {"status": "not_run", "not_run_reason": "legacy adapter did not receive verification report"}
    release_request = {}
    release_enqueue = {}
    # error(엔진 타임아웃 등)도 포함 — 완료/부분 산출이 조용히 사라지지 않고 방에 surface되게 한다.
    if state in {"done", "blocked", "partial_ready", "error"}:
        release_request = _release_request(task_pack, result, raw_status, state)
    policy_fields = _work_policy_fields({
        **(task_pack.get("work_runtime_policy") or {}),
        **_work_policy_overrides_from_row(previous_work_status),
        **_work_policy_overrides_from_row(latest),
    })
    work_status = {
        "schema": "WorkStatus.v1",
        "task_id": task_id,
        "task_pack_id": task_pack.get("task_pack_id", ""),
        "state": state,
        "worker_agent": worker,
        "updated_at": now_iso(),
        "result_path": "결과.md",
        "status_path": "상태.json",
        "release_request_path": "release_request.json" if release_request else "",
        "release_queue_id": release_request.get("release_queue_id", "") if release_request else "",
        "release_queue_state": release_request.get("queue_state", "") if release_request else "",
        "release_enqueue_error": "",
        "lesson_application_hold": bool(hold_reason),
        "lesson_application_error": hold_reason,
        "lesson_applications": lesson_audit.get("applications", []),
        "cancel_requested": bool(previous_work_status.get("cancel_requested")),
        "cancellation_request_id": previous_work_status.get("cancellation_request_id", ""),
        "cancellation_reason": previous_work_status.get("cancellation_reason", ""),
        "cancel_requested_at": previous_work_status.get("cancel_requested_at", ""),
        "last_seen_steering_seq": previous_work_status.get("last_seen_steering_seq", 0),
        "last_heartbeat_at": previous_work_status.get("last_heartbeat_at", ""),
        "heartbeat_phase": previous_work_status.get("heartbeat_phase", ""),
        "heartbeat_note": previous_work_status.get("heartbeat_note", ""),
        "verification": verification,
        **policy_fields,
        **_context_fields(task_pack),
    }
    registry_event = {
        "schema": "TaskRegistryEvent.v1",
        "event_id": _stable_id("task_event", space, task_id, "task_finalized", task_pack.get("task_pack_id", ""), state),
        "event": "task_finalized",
        "state": state,
        "space_id": space,
        "task_id": task_id,
        "task_pack_id": task_pack.get("task_pack_id", ""),
        "task_pack_checksum": task_pack.get("task_pack_checksum", ""),
        "worker_agent": worker,
        "work_dir": _rel(work_dir),
        "release_request_path": "release_request.json" if release_request else "",
        "release_queue_id": release_request.get("release_queue_id", "") if release_request else "",
        "release_queue_state": release_request.get("queue_state", "") if release_request else "",
        "release_enqueue_error": "",
        "lesson_pack_status": (task_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
        "included_lessons": (task_pack.get("lesson_pack") or {}).get("included_lessons", []),
        "must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in (task_pack.get("lesson_pack") or {}).get("must_apply", [])
            if lesson.get("lesson_id")
        ],
        "lesson_application_hold": bool(hold_reason),
        "lesson_application_error": hold_reason,
        "cancel_requested": bool(previous_work_status.get("cancel_requested")),
        "cancellation_request_id": previous_work_status.get("cancellation_request_id", ""),
        "cancellation_reason": previous_work_status.get("cancellation_reason", ""),
        "last_heartbeat_at": previous_work_status.get("last_heartbeat_at", ""),
        "heartbeat_phase": previous_work_status.get("heartbeat_phase", ""),
        "heartbeat_note": previous_work_status.get("heartbeat_note", ""),
        "finalized_at": now_iso(),
        **policy_fields,
        **_context_fields(task_pack),
    }

    def mutate():
        _append_unique(_registry_path(space), registry_event, "event_id")
        return registry_event

    _with_lock(space, mutate)

    def finalize_release_decision():
        local_cancel_info = _cancel_info(space, task_id, work_dir)
        local_pending_steering = _pending_ack_steering(space, task_id, previous_work_status)
        local_release_request = release_request
        local_release_enqueue = release_enqueue
        # done뿐 아니라 error(엔진 타임아웃 등)도 enqueue→공개한다 — 완료/부분 산출이 조용히 사라지지 않게.
        # blocked/partial_ready는 무언가(레슨·스티어링) 대기 중이라 draft로 보류(아래 특수처리), error는 surface.
        if not (state in ("done", "error") and local_release_request and not hold_reason):
            return {
                "cancel_info": local_cancel_info,
                "pending_steering": local_pending_steering,
                "release_request": local_release_request,
                "release_enqueue": local_release_enqueue,
            }

        try:
            task_generation = int(task_pack.get("room_generation") or orchestration.DEFAULT_ROOM_GENERATION)
            current_generation = int(orchestration.current_generation(space))
        except Exception:
            task_generation = None
            current_generation = None

        if local_cancel_info.get("cancel_requested"):
            release_error = "cancel_requested: task result kept as draft after cancellation request"
            local_release_request = {
                **local_release_request,
                "release_state": "cancel_requested",
                "queue_state": "not_enqueued",
                "draft_only": True,
                "release_queue_id": "",
                "not_publishable_reason": release_error,
                "release_enqueue_error": release_error,
            }
            followup_event = {
                **registry_event,
                "event_id": _stable_id(
                    "task_event",
                    space,
                    task_id,
                    "task_release_cancel_requested",
                    task_pack.get("task_pack_id", ""),
                    release_error,
                ),
                "event": "task_release_cancel_requested",
                "release_queue_id": "",
                "release_queue_state": "not_enqueued",
                "release_enqueue_error": release_error,
                "cancel_requested": True,
                "cancellation_request_id": local_cancel_info.get("cancellation_request_id", ""),
                "cancellation_reason": local_cancel_info.get("cancellation_reason", ""),
                "finalized_at": now_iso(),
            }
            _append_unique(_registry_path(space), followup_event, "event_id")
        elif local_pending_steering.get("pending"):
            release_error = (
                "steering_unacknowledged: latest revise_task steering_seq "
                f"{local_pending_steering.get('steering_seq')} not seen by worker "
                f"(last_seen={local_pending_steering.get('last_seen_steering_seq')})"
            )
            local_release_request = {
                **local_release_request,
                "release_state": "steering_unacknowledged",
                "queue_state": "not_enqueued",
                "draft_only": True,
                "release_queue_id": "",
                "not_publishable_reason": release_error,
                "release_enqueue_error": release_error,
            }
            followup_event = {
                **registry_event,
                "event_id": _stable_id(
                    "task_event",
                    space,
                    task_id,
                    "task_release_steering_unacknowledged",
                    task_pack.get("task_pack_id", ""),
                    release_error,
                ),
                "event": "task_release_steering_unacknowledged",
                "release_queue_id": "",
                "release_queue_state": "not_enqueued",
                "release_enqueue_error": release_error,
                "steering_seq": local_pending_steering.get("steering_seq", 0),
                "steering_action": local_pending_steering.get("steering_action", ""),
                "steering_event_id": local_pending_steering.get("steering_event_id", ""),
                "finalized_at": now_iso(),
            }
            _append_unique(_registry_path(space), followup_event, "event_id")
        elif task_generation is not None and current_generation is not None and task_generation != current_generation:
            release_error = f"stale_generation: task room_generation {task_generation} != current {current_generation}"
            local_release_request = {
                **local_release_request,
                "release_state": "stale_generation",
                "queue_state": "not_enqueued",
                "draft_only": True,
                "release_queue_id": "",
                "not_publishable_reason": release_error,
                "release_enqueue_error": release_error,
            }
            followup_event = {
                **registry_event,
                "event_id": _stable_id(
                    "task_event",
                    space,
                    task_id,
                    "task_release_stale_generation",
                    task_pack.get("task_pack_id", ""),
                    release_error,
                ),
                "event": "task_release_stale_generation",
                "release_queue_id": "",
                "release_queue_state": "not_enqueued",
                "release_enqueue_error": release_error,
                "finalized_at": now_iso(),
            }
            _append_unique(_registry_path(space), followup_event, "event_id")
        else:
            try:
                local_release_enqueue = release_queue.enqueue_release(
                    space,
                    release_request=local_release_request,
                    work_dir=work_dir,
                    task_pack=task_pack,
                )
                local_release_request = local_release_enqueue.get("release_request") or local_release_request
                followup_event = {
                    **registry_event,
                    "event_id": _stable_id(
                        "task_event",
                        space,
                        task_id,
                        "task_release_enqueued",
                        task_pack.get("task_pack_id", ""),
                        local_release_request.get("release_queue_id", ""),
                    ),
                    "event": "task_release_enqueued",
                    "release_queue_id": local_release_request.get("release_queue_id", ""),
                    "release_queue_state": local_release_request.get("queue_state", ""),
                    "release_enqueue_error": "",
                    "finalized_at": now_iso(),
                }
                _append_unique(_registry_path(space), followup_event, "event_id")
            except release_queue.ReleaseQueueError as exc:
                release_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                local_release_request = {
                    **local_release_request,
                    "release_state": "enqueue_failed",
                    "queue_state": "enqueue_failed",
                    "draft_only": True,
                    "release_queue_id": "",
                    "not_publishable_reason": release_error,
                    "release_enqueue_error": release_error,
                }
                followup_event = {
                    **registry_event,
                    "event_id": _stable_id(
                        "task_event",
                        space,
                        task_id,
                        "task_release_enqueue_failed",
                        task_pack.get("task_pack_id", ""),
                        release_error,
                    ),
                    "event": "task_release_enqueue_failed",
                    "release_queue_id": "",
                    "release_queue_state": "enqueue_failed",
                    "release_enqueue_error": release_error,
                    "finalized_at": now_iso(),
                }
                _append_unique(_registry_path(space), followup_event, "event_id")
        return {
            "cancel_info": local_cancel_info,
            "release_request": local_release_request,
            "release_enqueue": local_release_enqueue,
        }

    release_decision = _with_lock(space, finalize_release_decision)
    cancel_info = release_decision["cancel_info"]
    pending_steering = release_decision.get("pending_steering") or {}
    release_request = release_decision["release_request"]
    release_enqueue = release_decision["release_enqueue"]
    if cancel_info.get("cancel_requested"):
        work_status.update({
            "cancel_requested": True,
            "cancellation_request_id": cancel_info.get("cancellation_request_id", ""),
            "cancellation_reason": cancel_info.get("cancellation_reason", ""),
            "cancel_requested_at": cancel_info.get("cancel_requested_at", ""),
        })
    if release_request:
        _write_json(work_dir / "release_request.json", release_request)
    work_status = {
        **work_status,
        "release_queue_id": release_request.get("release_queue_id", "") if release_request else "",
        "release_queue_state": release_request.get("queue_state", "") if release_request else "",
        "release_enqueue_error": release_request.get("release_enqueue_error", "") if release_request else "",
        "pending_steering_ack": bool(
            pending_steering.get("pending")
            or (release_request and release_request.get("release_state") == "steering_unacknowledged")
        ),
        **_pending_ack_fields(pending_steering),
    }
    _write_json(work_dir / "work_status.json", work_status)
    if state == "done" and release_request:
        if release_request.get("queue_state") == "enqueued":
            outcome = "success"
        elif release_request.get("release_state") in {"stale_generation", "cancel_requested", "steering_unacknowledged"}:
            outcome = "superseded"
        elif release_request.get("queue_state") == "enqueue_failed":
            outcome = "failed"
        else:
            outcome = "partial"
    else:
        outcome = "rejected" if state in {"blocked", "cancelled"} else "failed" if state == "error" else "partial"
    try:
        lesson_ledger.record_post_task_evaluation(
            space,
            task_id=task_id,
            outcome=outcome,
            context=task_pack,
            actor=worker,
            task_title=objective[:160],
            result_summary=result[:1000],
            what_worked=["TaskPack v0 adapter completed and enqueued release request for approval"] if outcome == "success" and release_request.get("queue_state") == "enqueued" else [],
            what_failed=[hold_reason or str(raw_status.get("사유") or state or "unknown work status")] if outcome != "success" else [],
            lesson_candidate_needed=outcome != "success",
            no_lesson_reason=(
                "no_failure_or_correction"
                if outcome == "success"
                else "taskpack_v0_completion_hold_or_failure_requires_review"
            ),
        )
    except Exception:
        pass
    return {
        "state": state,
        "result": result,
        "work_status": work_status,
        "release_request": release_request,
        "release_enqueue": release_enqueue,
        "lesson_application_hold": bool(hold_reason),
        "lesson_application_error": hold_reason,
    }


def snapshot(space: str) -> dict:
    registry, registry_error = _rows_with_error(_registry_path(space))
    manifests, manifest_error = _rows_with_error(_manifest_path(space))
    latest_by_task = _latest_tasks(registry)
    now = datetime.now()
    state_counts = {}
    for row in latest_by_task.values():
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    latest = registry[-1] if registry else {}
    held = [
        row for row in latest_by_task.values()
        if row.get("lesson_application_hold") or row.get("state") == "blocked"
    ]
    latest_hold = held[-1] if held else {}
    release_followup_missing_items = _release_followup_missing_items(registry)
    latest_release_followup_missing = release_followup_missing_items[-1] if release_followup_missing_items else {}
    enqueue_failed = [
        row for row in latest_by_task.values()
        if row.get("release_queue_state") == "enqueue_failed"
    ]
    latest_enqueue_failed = enqueue_failed[-1] if enqueue_failed else {}
    closed_states = {"done", "error", "blocked", "partial_ready", "cancelled"}
    running_items = [
        _compact_task(row, now=now)
        for row in sorted(latest_by_task.values(), key=lambda item: item.get("_row_index", 0))
        if row.get("state") == "running" and not row.get("cancel_requested")
    ]
    cancel_requested_items = [
        _compact_task(row, now=now)
        for row in sorted(latest_by_task.values(), key=lambda item: item.get("_row_index", 0))
        if (row.get("state") == "cancel_requested" or row.get("cancel_requested"))
        and row.get("state") not in closed_states
    ]
    active_items = prioritized_active_items(running_items + cancel_requested_items)
    stale_items = [row for row in active_items if row.get("heartbeat_stale")]
    pending_steering_items = [row for row in active_items if row.get("pending_steering_ack")]
    progress_report_due_items = [row for row in active_items if row.get("progress_report_due")]
    progress_report_requested_items = [
        row for row in active_items
        if row.get("progress_report_requested_since_heartbeat")
    ]
    steering_runtime_items = [row for row in active_items if row.get("steering_runtime_state")]
    runtime_activity_items = _task_runtime_activity_rows(registry, limit=12)
    steering_runtime_counts = {}
    for row in steering_runtime_items:
        state = row.get("steering_runtime_state") or "unknown"
        steering_runtime_counts[state] = steering_runtime_counts.get(state, 0) + 1
    latest_cancel_requested = cancel_requested_items[-1] if cancel_requested_items else {}
    latest_steering = {}
    for row in reversed(list(latest_by_task.values())):
        if row.get("latest_steering_action") or row.get("steering_action"):
            latest_steering = row
            break
    latest_heartbeat = _heartbeat_status(latest, now=now) if latest else {
        "heartbeat_age_ms": None,
        "heartbeat_missing": False,
        "heartbeat_stale": False,
        "heartbeat_stale_threshold_ms": TASK_HEARTBEAT_STALE_MS,
    }
    errors = [err for err in (registry_error, manifest_error) if err]
    return {
        "task_count": len(latest_by_task),
        "task_event_count": len(registry),
        "task_pack_manifest_count": len(manifests),
        "heartbeat_stale_threshold_ms": TASK_HEARTBEAT_STALE_MS,
        "progress_report_due_threshold_ms": TASK_PROGRESS_REPORT_DUE_MS,
        "state_counts": state_counts,
        "latest_task_id": latest.get("task_id", ""),
        "latest_state": latest.get("state", ""),
        "latest_worker": latest.get("worker_agent", ""),
        "latest_task_pack_id": latest.get("task_pack_id", ""),
        "latest_lesson_pack_status": latest.get("lesson_pack_status", ""),
        "latest_must_apply_lessons": latest.get("must_apply_lessons", []),
        "latest_lesson_application_hold": bool(latest.get("lesson_application_hold")),
        "latest_release_queue_id": latest.get("release_queue_id", ""),
        "latest_release_queue_state": latest.get("release_queue_state", ""),
        "latest_release_enqueue_error": latest.get("release_enqueue_error", ""),
        "latest_last_heartbeat_at": latest.get("last_heartbeat_at", ""),
        "latest_heartbeat_phase": latest.get("heartbeat_phase", ""),
        "latest_heartbeat_note": latest.get("heartbeat_note", ""),
        "latest_heartbeat_age_ms": latest_heartbeat.get("heartbeat_age_ms"),
        "latest_heartbeat_missing": latest_heartbeat.get("heartbeat_missing"),
        "latest_heartbeat_stale": latest_heartbeat.get("heartbeat_stale"),
        "latest_cancel_requested": bool(latest.get("cancel_requested")),
        "running_items": running_items,
        "cancel_requested_items": cancel_requested_items,
        "active_items": active_items,
        "stale_items": stale_items,
        "pending_steering_items": pending_steering_items,
        "progress_report_due_items": progress_report_due_items,
        "progress_report_requested_items": progress_report_requested_items,
        "steering_runtime_items": steering_runtime_items,
        "steering_runtime_counts": steering_runtime_counts,
        "runtime_activity_items": runtime_activity_items,
        "running_count": len(running_items),
        "cancel_requested_count": len(cancel_requested_items),
        "active_count": len(active_items),
        "stale_task_count": len(stale_items),
        "pending_steering_count": len(pending_steering_items),
        "progress_report_due_count": len(progress_report_due_items),
        "progress_report_requested_count": len(progress_report_requested_items),
        "steering_runtime_count": len(steering_runtime_items),
        "runtime_activity_count": len(runtime_activity_items),
        "heartbeat_missing_count": len([row for row in active_items if row.get("heartbeat_missing")]),
        "latest_steering_action": latest_steering.get("latest_steering_action", latest_steering.get("steering_action", "")),
        "latest_steering_task_id": latest_steering.get("task_id", ""),
        "latest_steering_seq": latest_steering.get("latest_steering_seq", latest_steering.get("steering_seq", 0)),
        "latest_steering_instruction": str(latest_steering.get("latest_steering_instruction", latest_steering.get("steering_instruction", "")))[:240],
        "latest_cancel_requested_task_id": latest_cancel_requested.get("task_id", ""),
        "latest_cancel_requested_reason": latest_cancel_requested.get("cancellation_reason", ""),
        "release_followup_missing_count": len(release_followup_missing_items),
        "release_followup_missing_items": release_followup_missing_items,
        "latest_release_followup_missing_task_id": latest_release_followup_missing.get("task_id", ""),
        "latest_release_followup_missing_worker": latest_release_followup_missing.get("worker_agent", ""),
        "release_enqueue_failed_count": len(enqueue_failed),
        "latest_release_enqueue_failed_task_id": latest_enqueue_failed.get("task_id", ""),
        "latest_release_enqueue_failed_error": latest_enqueue_failed.get("release_enqueue_error", ""),
        "hold_task_count": len(held),
        "latest_hold_task_id": latest_hold.get("task_id", ""),
        "latest_hold_worker": latest_hold.get("worker_agent", ""),
        "latest_hold_task_pack_id": latest_hold.get("task_pack_id", ""),
        "latest_hold_error": latest_hold.get("lesson_application_error", ""),
        "ledger_corrupt": bool(errors),
        "ledger_errors": errors,
    }
