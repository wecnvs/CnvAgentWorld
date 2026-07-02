# -*- coding: utf-8 -*-
"""TaskPack v0 adapter와 작업 상태 원장."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
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
# 갓 디스패치된 작업은 서브프로세스 기동·모델 로드로 첫 heartbeat까지 수십 초가 걸린다. 그 사이엔
# heartbeat_missing→stale로 뜨지만 실제로는 '시작 중=살아있음'이므로, created 이후 이 유예창 안이면
# startup_grace로 표시해 소비자(예: 자동연속 억제)가 죽은 작업과 구분한다(콜드스타트 race의 '생각 중' 깜빡임 방지).
TASK_STARTUP_GRACE_MS = 90_000
# heartbeat가 이 시간(5분)을 넘겨 끊긴 stale 작업 중, 상태.json에 완료/취소 근거가 전혀 없는
# '무진행 스트랜드'(워커가 done/error도 못 쓴 채 죽거나 락대기로 멈춰 결과가 영영 안 돌아오는 경우)는
# 조용히 active에 박제하지 않고 error(중단 보고)로 강제 종결해 그때까지의 산출을 방에 surface한다
# (대표 신고: 작업이 돼도 결과·캡처가 대화로 안 돌아온다 → 결과가 반드시 돌아오게).
TASK_STRAND_REPORT_GRACE_MS = 5 * 60_000
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
# 종결 상태 집합 — 원장 압축·heartbeat early-return 등에서 공용(종전엔 리터럴로 5곳 반복).
CLOSED_TASK_STATES = {"done", "error", "blocked", "partial_ready", "cancelled"}
# 원장 압축이 재작성할 가치가 있는 최소 제거량 — 이 미만이면 rewrite 비용이 이득을 압도(테스트·소규모 방 보호).
COMPACT_MIN_REMOVED_EVENTS = 40


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


# ── 원장 압축 (O(n²) 성장 해소) ──────────────────────────────────────────────
# 실측(레빗_bcd7, 1일): 원장 1.8MB·이벤트 1,100개 중 96.5%가 '종결된 task의 평범한 heartbeat'.
# 종결 task엔 새 heartbeat가 구조적으로 안 붙으므로(record_heartbeat가 closed면 early-return)
# 그 heartbeat들은 다시 읽힐 일도 갱신될 일도 없는 죽은 무게인데, 모든 이벤트 기록·snapshot이
# 이 무게까지 매번 전량 재파싱했다(heartbeat 10초 주기 × 폴링 1.5초 주기 → 누적 O(n²)).
# 압축 규칙(소비자 무손실): 종결 task에서 아래만 보존하고 평범한 heartbeat를 제거한다 —
#   · heartbeat 외 모든 이벤트(created/finalized/steering/cancel/release_*/settings)
#   · 그 task의 마지막 heartbeat 1개(latest fold·last_heartbeat_at 보존)
#   · phase가 TASK_RUNTIME_HEARTBEAT_LABELS에 있는 heartbeat(runtime_activity 투영 보존)
# 제거분은 .archive 파일에 append(전체 이력 보존, 코드가 다시 읽지 않음). event_id 불변 → 멱등.


def _archive_path(space: str) -> Path:
    return _space_dir(space) / "task_registry.jsonl.archive"


def _compact_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    latest: dict[str, dict] = {}
    last_heartbeat_index: dict[str, int] = {}
    for idx, row in enumerate(rows):
        task_id = row.get("task_id")
        if not task_id:
            continue
        latest[task_id] = row
        if row.get("event") == "task_heartbeat":
            last_heartbeat_index[task_id] = idx
    closed = {
        task_id for task_id, row in latest.items()
        if str(row.get("state") or "") in CLOSED_TASK_STATES
    }
    if not closed:
        return rows, []
    kept, removed = [], []
    for idx, row in enumerate(rows):
        task_id = row.get("task_id")
        if (
            task_id in closed
            and row.get("event") == "task_heartbeat"
            and idx != last_heartbeat_index.get(task_id)
            and str(row.get("heartbeat_phase") or "") not in TASK_RUNTIME_HEARTBEAT_LABELS
        ):
            removed.append(row)
            continue
        kept.append(row)
    return kept, removed


def compact_closed_task_events(space: str) -> dict:
    """종결 task의 평범한 heartbeat를 원장에서 걷어내 활성 원장을 작게 유지한다(멱등·락 내 원자 교체).

    호출처: finalize_task 말미(종결 직후가 가장 자연스러운 시점) + reap 백스톱(30초 주기, 기존 원장
    마이그레이션 겸). 제거량이 COMPACT_MIN_REMOVED_EVENTS 미만이면 재작성하지 않는다.
    """
    def mutate():
        path = _registry_path(space)
        rows, error = _rows_with_error(path)
        if error:
            # 파싱 못 한 라인이 있는 원장은 압축하지 않는다 — 원본 라인 유실 방지.
            return {"ok": False, "error": error, "removed": 0}
        kept, removed = _compact_rows(rows)
        if len(removed) < COMPACT_MIN_REMOVED_EVENTS:
            return {"ok": True, "removed": 0, "skipped": True}
        with _archive_path(space).open("a", encoding="utf-8") as archive:
            for row in removed:
                archive.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp = path.with_suffix(".jsonl.compact_tmp")
        tmp.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept),
            encoding="utf-8",
        )
        tmp.replace(path)
        return {"ok": True, "removed": len(removed), "kept": len(kept)}

    try:
        return _with_lock(space, mutate)
    except Exception as exc:
        # 압축 실패가 종결/백스톱을 막으면 안 된다 — 다음 기회에 재시도된다.
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "removed": 0}


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
    # 첫 heartbeat 전(missing)이라도 created 이후 유예창 안이면 '시작 중=살아있음'으로 본다.
    startup_grace = False
    if active_state and missing:
        created_age_ms = _heartbeat_age_ms(row.get("created_at", ""), now=now)
        startup_grace = created_age_ms is not None and created_age_ms <= max(threshold_ms, TASK_STARTUP_GRACE_MS)
    return {
        "heartbeat_age_ms": age_ms,
        "heartbeat_missing": missing,
        "heartbeat_stale": stale,
        "heartbeat_startup_grace": startup_grace,
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


# ── 컴퓨터유즈(원격/로컬 화면 조작) 작업 판별·인가 ───────────────────────────────
# 러너는 에이전트를 --dangerously-skip-permissions로 돌려 샌드박스를 하드 강제하지 않는다.
# 따라서 task_pack의 scope/capabilities는 '선언(권고)'이고 정직한 에이전트는 그 선언을 지킨다.
# 원격 Revit 경고창 진단처럼 화면 캡처·클릭이 본질인 작업에 보수 기본(network:none·
# external_side_effects:forbidden·vision:false)을 그대로 주면, 에이전트가 '내 scope 밖'이라
# 정직하게 BLOCKED로 멈춘다(실측: 레빗_bcd7 작업 329e/240a). 그래서 CU 작업만 scope를 작업
# 성격에 맞게 '인가'해, 에이전트가 대시보드 /api/cu(로컬호스트·비밀불필요)로 화면을 보고 조작하게 한다.
# 판별은 '등록된 화면 타깃(app_targets) 지목' + 'CU 동작 마커' 동시충족일 때만 → 오탐/회귀 차단
# (아니면 None → 기존 보수 scope 그대로).
_CU_DASHBOARD_BASE = "http://127.0.0.1:8686"
_CU_ACTION_MARKERS = (
    "computer-use", "computer use", "컴퓨터유즈", "컴퓨터 유즈",
    "화면 캡처", "스크린샷", "screenshot", "화면 조작", "gui 조작", "gui조작",
    "cu-win", "cu-mac", "cu_helper", "cu-helper",
    "클릭", "타이핑", "경고창", "애드인", "리본", "원격 제어", "원격제어",
)
_CU_TOOL_DIRS = ["도구/기본/cu-win", "도구/기본/cu-mac"]


def detect_computer_use_target(objective: str) -> str | None:
    """objective가 (1) 등록된 화면 타깃(app_targets)을 실제로 지목하고 (2) 컴퓨터유즈 동작
    의도가 있으면 그 타깃명을 돌려준다. 둘 중 하나라도 없으면 None(→ 보수 scope 유지·무회귀).
    등록 타깃명에 근거하므로 단순 키워드로는 오탐하지 않는다."""
    text = str(objective or "")
    low = text.lower()
    if not any(m in low for m in _CU_ACTION_MARKERS):
        return None
    try:
        from . import app_targets
        targets = app_targets.list_targets()
    except Exception:
        return None
    # 구체적(긴) 타깃명 우선 매칭. 'local'(서버 자체 화면)은 명시적일 때만.
    named = sorted((str(t.get("name") or "") for t in targets if t.get("name")), key=len, reverse=True)
    for name in named:
        if name and name != "local" and name in text:
            return name
    if "서버 컴퓨터" in text or "이 호스트" in text or " local " in f" {low} ":
        return "local"
    return None


def _computer_use_scope(base_scope: dict, target: str, channel: str) -> dict:
    """CU 작업용으로 scope를 '인가'한다(이 작업 한정). 러너 하드샌드박스가 아니므로 선언을 바꿔
    정직한 에이전트가 대시보드 /api/cu로 화면 캡처·입력을 하게 한다. 조작 안전은 charter로 가둔다."""
    s = dict(base_scope)
    s["read_paths"] = list(base_scope.get("read_paths", [])) + _CU_TOOL_DIRS + [
        "스킬/추가/computer-use-win", "스킬/추가/computer-use-mac", "스킬/추가/computer-use-charter",
    ]
    s["execute_paths"] = list(_CU_TOOL_DIRS)
    s["allowed_tools"] = ["dashboard_cu_api", "cu-win", "cu-mac", "bash(cu 도구·curl 한정)"]
    s["network_policy"] = f"computer_use: 로컬호스트 대시보드 {_CU_DASHBOARD_BASE}/api/cu 만 (target={target})"
    s["external_side_effects"] = (
        f"computer_use_allowed: 타깃 '{target}'({channel}) 화면 GUI 조작 허용 — "
        "computer-use-charter 준수(캡처 없이 클릭 금지), 되돌리기 어려운 변경은 화면 원문 확인·근거기록 후에만"
    )
    return s


def _computer_use_pack_block(target: str, channel: str, worker: str) -> dict:
    b = _CU_DASHBOARD_BASE
    return {
        "schema": "ComputerUseGrant.v1",
        "authorized": True,
        "target": target,
        "channel": channel,
        "dashboard_base": b,
        "how_to": [
            f"1) 화면 캡처(비밀 불필요·누구나): `curl -s '{b}/api/cu/view/screenshot?target={target}&w=1600&q=80' -o 화면.jpg` → 저장한 화면.jpg를 읽어(비전) 실제 화면·경고 원문을 판독한다.",
            f"2) 화면/세션 상태 확인: `curl -s '{b}/api/cu/view/status?target={target}'`.",
            f"3) 입력(클릭/타이핑/키)은 타깃 락 보유자만: 먼저 `curl -s -X POST '{b}/api/cu/acquire' -H 'Content-Type: application/json' -d '{{\"agent_id\":\"{worker}\",\"target\":\"{target}\",\"ttl\":180}}'` 로 락을 잡고, `POST {b}/api/cu/view/input` (body: agent_id,target,action=click|type|key,...) 로 조작, 끝나면 `POST {b}/api/cu/release`.",
            "4) computer-use-charter 준수: '캡처→포인터이동→재캡처확인→클릭' 루프. 캡처 없이/목표 UI 불확실 시 클릭 금지. 조작 후 화면 변화 없으면 같은 조작 반복 금지(창 비활성·frozen 등 다른 가설로 재판정).",
            "5) 되돌리기 어려운 변경(애드인 신뢰 승인·설정 영구변경)은 화면 원문·원인 확정 후에만, 근거를 결과.md에 남기고 한 스텝씩 검증한다.",
            "6) 연결정보/자격증명 등 비밀은 결과·공개경로에 평문 금지(law §7). 화면 캡처 증거도 비밀 미노출 영역만 공개한다.",
        ],
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
    # 컴퓨터유즈 작업이면 scope를 '인가'로 상향(이 작업 한정). 아니면 보수 기본 그대로(무회귀).
    cu_target = detect_computer_use_target(objective)
    cu_channel = ""
    _base_scope = {
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
    }
    if cu_target:
        try:
            from . import app_targets
            cu_channel = str((app_targets.resolve(cu_target) or {}).get("channel") or "")
        except Exception:
            cu_channel = ""
        _scope = _computer_use_scope(_base_scope, cu_target, cu_channel)
    else:
        _scope = _base_scope
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
        "scope": _scope,
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
    if cu_target:
        # 에이전트가 '어떻게' 화면을 보고 조작하는지(로컬호스트 /api/cu·charter·락)를 명시한다.
        pack["computer_use"] = _computer_use_pack_block(cu_target, cu_channel, worker)
    pack["task_pack_checksum"] = _checksum(pack)
    capabilities = runtime_capabilities(runtime_info, work_policy)
    if cu_target:
        # CU 작업만 network·vision·shell을 인가(이 작업 한정). 러너가 샌드박스를 하드강제하지 않으므로
        # 이 선언이 정직한 에이전트에게 '인가'로 읽힌다. 비-CU 작업은 보수 기본 그대로(무회귀).
        capabilities["supports_network"] = True
        capabilities["supports_image_inspection"] = True
        capabilities["supports_shell"] = True
        capabilities["computer_use_target"] = cu_target
        capabilities["computer_use_channel"] = cu_channel
        capabilities["source"] = str(capabilities.get("source", "")) + "+computer_use_elevated"
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


def _is_timeout_reason(reason: str) -> bool:
    """실패 사유가 '엔진 타임아웃류'인지. (보고 턴만 끊긴 경우를 진짜 실패와 구분하기 위함)"""
    r = str(reason or "")
    rl = r.lower()
    return ("타임아웃" in r) or ("timeout" in rl) or ("시간 초과" in r) or ("timeoutexpired" in rl)


# 결과.md에서 작업자가 '명시적으로' 완료를 선언한 라인을 찾는 정규식(상태 선언만, 계획 언급 제외).
# 보수적: `완료`는 (a)뒤에 한글이 붙은 형태(완료되지·완료됨 등)와 (b)부정·유보어(완료 안/못/예정/않 …)를
# 제외한다 — '상태: 완료되지 않음'·'완료 예정' 같은 미완 표현을 done으로 오탐하지 않게(교차검증 지적).
_DONE_DECL_RE = re.compile(
    r"(최종\s*)?상태\s*[:：]\s*(done\b|완료(?![가-힣])(?!\s*(안|못|미|예정|전|않|불가|실패|보류|중)))",
    re.IGNORECASE,
)


def _checkpoint_reports_done(result_md: str) -> bool:
    """결과.md 체크포인트가 작업 완료를 '명시적으로' 선언했는지(보수적 판정).

    엔진 타임아웃은 '보고 턴'만 끊을 뿐 작업 자체는 체크포인트상 완료일 수 있다(large-task-checkpointing).
    오탐(미완을 완료로 승격)을 막기 위해, 미통과 체크리스트 항목(`- [ ]`) 안의 'done' 언급(=계획)은
    제외하고, 상태 선언 라인(`상태: done`/`최종 상태: DONE`/`상태: 완료`) 또는 `완주(done)`만 완료로 본다.
    """
    if not result_md:
        return False
    for raw in result_md.splitlines():
        if "- [ ]" in raw:          # 미통과(계획) 단계 — 완료 선언이 아니다
            continue
        line = raw.strip().lstrip(">").replace("#", "").replace("*", "").strip()
        if _DONE_DECL_RE.search(line):
            return True
        low = line.lower()
        if ("완주(done)" in low) or ("완주 (done)" in low):
            return True
    return False


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
    if not summary_body.strip():
        # 결과.md 없이 done/blocked 선언된 작업 — 빈 public_summary는 publish_release의
        # "publish content required"에 걸려, 승인 후 영구 미공개 고아가 된다(감사 확정 경로).
        # 조용히 갇히는 대신 '근거 없는 완료 선언' 사실 자체를 방에 배너로 surface해 대표가 판단하게 한다.
        summary_body = (
            f"⚠️ 작업자가 상태를 '{state}'로 선언했지만 결과.md에 산출 기록이 없습니다"
            f"(사유: {reason or '기록 없음'}). 근거 없는 완료 선언일 수 있으니 작업 폴더를 확인하거나 재지시해 주세요."
        )
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
    # C(타임아웃이 완료를 가리는 문제): 엔진이 '최종 보고 턴'에서 타임아웃나면 상태.json을 error로 덮어써,
    # 작업이 체크포인트(결과.md)상 완료(예: 4/4 DONE)인데도 방엔 '⚠️ 타임아웃 에러'로만 떠 사회자·대표가
    # 실패로 오인하고 정체된다(실증 2026-06-29 c6b6 seq45). 보고 턴 타임아웃은 작업 실패가 아니므로,
    # state=="error"(타임아웃 사유) + 결과.md가 '명시적 완료'를 선언하면 done으로 화해해 정상 완료로 흘린다.
    # 보수적: 타임아웃류 사유 + 명시 완료선언일 때만(미완을 done으로 오승격하지 않게). 그 외 error는 종전대로.
    if state == "error":
        _err_reason = str(raw_status.get("사유") or raw_status.get("reason") or "")
        if _is_timeout_reason(_err_reason) and _checkpoint_reports_done(result):
            state = "done"
            raw_status = {
                **raw_status,
                "상태": "done",
                "사유": f"체크포인트 완료 화해(보고 턴 타임아웃): {_err_reason[:160]}",
                "completed_via_checkpoint": True,
                "engine_timeout_reconciled": True,
            }
            _write_json(work_dir / "상태.json", raw_status)
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
            # law_work.md 정본: 재지시(revise) 반영 전 결과는 공개 대기열에 올라가지 않는다 — enqueue 금지 유지.
            # 사회자에게는 RoomStatusSnapshot.tasks.pending_steering_count 로 노출되고, 대표 입력의
            # 응답의무는 sweep(고아 assigned 재개)이 되살린다. 결과 본문을 공개하는 유일한 길은
            # 워커가 재지시를 실제 반영(ack)한 뒤 다시 finalize 되는 것.
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
    # 작업 실패 사유 문자열(타임아웃 판정·케이스화 근거). hold_reason > raw_status.사유 > state 순.
    _fail_reason = str(hold_reason or raw_status.get("사유") or state or "unknown work status")
    _fail_l = _fail_reason.lower()
    _is_timeout_failure = ("타임아웃" in _fail_reason) or ("timeout" in _fail_l) or ("시간 초과" in _fail_reason)
    # 타임아웃류 작업 실패는 자기성장 루프로 케이스화한다(체크포인트 재개·중복기동 방지 교훈 → 스킬 승격 후보).
    # 종전엔 no_lesson_reason="...requires_review"로 punt돼, 이 방의 지배적 실패(엔진 타임아웃)가 5회+
    # 반복돼도 케이스가 0이었다. wake_failed 핸들러(room_manager)와 같은 패턴으로 work-task 경로도 잇는다.
    # instruction은 안정 문자열 → lesson_id가 내용기반 dedup되어 같은 타임아웃이 반복돼도 lesson이 폭증하지 않는다.
    # 그 외 실패(blocked/cancelled/스키마 등)는 종전대로 수동검토 보류 — 회귀 최소화.
    _task_lesson_candidate = None
    if outcome != "success" and _is_timeout_failure:
        _task_lesson_candidate = {
            "kind": "lesson",
            "scope": "space",
            "promotion_target": "skill",
            "instruction": (
                "작업이 엔진 타임아웃으로 끊기면 같은 목표로 새 작업을 또 띄우지 말고, 착수 즉시 결과.md에 "
                "단계 체크리스트를 박아 두고 미통과 단계부터 이어서 재개한다(통과 단계 재생성 금지). "
                "한 번에 너무 큰 작업은 자원 단위로 쪼개 한 번에 하나씩 체크포인트를 남기며 진행한다"
                "(large-task-checkpointing)."
            ),
            "applies_when": {
                "keywords": ["타임아웃", "timeout", "체크포인트", "재개", "결과.md", "large-task-checkpointing"],
            },
            "evidence_type": "agent_observation",
            "source_quote": _fail_reason[:240],
        }
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
            what_failed=[_fail_reason] if outcome != "success" else [],
            lesson_candidate_needed=outcome != "success",
            lesson_candidate=_task_lesson_candidate,
            no_lesson_reason=(
                "no_failure_or_correction"
                if outcome == "success"
                else ""
                if _task_lesson_candidate
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
        # 종결 직후가 압축 적기 — 이 task의 heartbeat 무게가 방금 '죽은 무게'가 됐다.
        # (제거량이 임계 미만이면 no-op — 테스트·소규모 방은 재작성 안 함.)
        "ledger_compaction": compact_closed_task_events(space),
    }


def recent_closed_items(space: str, *, worker: str = "", limit: int = 20) -> list[dict]:
    """최근 종료된 작업(done/error/blocked/partial_ready/cancelled)을 작업별 최신상태 기준 최신순 반환.

    snapshot()은 active(running/cancel_requested)만 노출한다. 에이전트가 '내가 무엇을 어디까지
    했는지'(완료 이력·진척)를 자기 wake에서 보게 하려면 종료된 작업도 필요하다 — 그 재료를 여기서
    제공한다. worker를 주면 그 에이전트가 맡았던 작업만 거른다. 예외안전·limit으로 hot-path를 가둔다.
    """
    try:
        registry, _ = _rows_with_error(_registry_path(space))
    except Exception:
        return []
    latest_by_task = _latest_tasks(registry)
    closed_states = {"done", "error", "blocked", "partial_ready", "cancelled"}
    now = datetime.now()
    rows = [
        _compact_task(row, now=now)
        for row in sorted(latest_by_task.values(), key=lambda item: item.get("_row_index", 0))
        if row.get("state") in closed_states and (not worker or row.get("worker_agent") == worker)
    ]
    return rows[::-1][:limit]  # 최신 종료 먼저


def reap_stale_tasks(space: str) -> list[dict]:
    """heartbeat가 끊긴 비종결 작업을 work_dir 상태.json/체크포인트 근거로 강제 finalize한다(자동복구 reaper).

    [메우는 빈틈] 엔진이 '최종 보고/release 턴'에서 타임아웃나거나 워커 프로세스가 하드킬되면, 작업은
    산출물상 완료(상태.json=done, 결과.md 체크포인트 DONE)인데도 registry엔 running/cancel_requested로
    남아 active로 '박제'된다. 그러면 (1)사회자가 '작업 중'으로 오인해 다음 작업을 안 띄우고(체인 정지),
    (2)완료 산출물이 release/배포로 안 흘러간다(실증 2026-06-30 dc1f: 8파일 완료·done인데 registry running
    → 다음 종 미발행·미배포; 좀비 2a22: 죽은 워커의 cancel_requested 영구 active로 체인 차단).
    recover_space는 방이 idle이면 no-op이고(박제는 방 idle+작업 running), reflow_all_spaces는 이미
    enqueue된 release만 발행하므로(박제 작업은 release 자체가 없음) 둘 다 이 케이스를 못 푼다.

    [동작] 신선 heartbeat 작업은 절대 건드리지 않는다(살아 일하는 중일 수 있음 — snapshot의 heartbeat_stale
    판정만 신뢰). stale 작업에 대해 work_dir 상태.json을 보고:
      · 상태=done                         → finalize_task(done)  → release→reflow 공개·자동배포
      · 상태=error + 타임아웃 사유 + 체크포인트 완료 → finalize_task(동일 reconciliation으로 done 화해)
      · cancel_requested(죽은 워커)         → 상태.json=cancelled 대필 후 finalize_task(cancelled)로 종결
    그 외(근거 없는 미완 error 등)는 보존한다(미완을 done으로 오승격하지 않게). 예외안전.
    """
    results: list[dict] = []
    try:
        snap = snapshot(space)
    except Exception:
        return results
    stale = [a for a in (snap.get("active_items") or []) if a.get("heartbeat_stale")]
    for item in stale:
        task_id = str(item.get("task_id") or "")
        worker = str(item.get("worker_agent") or "")
        rel = str(item.get("work_dir") or "")
        if not task_id or not rel:
            continue
        work_dir = ROOT / rel
        if not work_dir.exists():
            continue
        try:
            raw_status = _read_json(work_dir / "상태.json", {})
            state = str(raw_status.get("상태") or raw_status.get("state") or "")
            reason = str(raw_status.get("사유") or raw_status.get("reason") or "")
            result_md = (work_dir / "결과.md").read_text(encoding="utf-8") if (work_dir / "결과.md").exists() else ""
            cancel_req = bool(item.get("cancel_requested")) or state == "cancel_requested" or item.get("state") == "cancel_requested"
            finalize_as = ""
            if state == "done":
                finalize_as = "done"
            elif state == "error" and _is_timeout_reason(reason) and _checkpoint_reports_done(result_md):
                finalize_as = "done"  # finalize_task가 동일 reconciliation 수행
            elif cancel_req:
                finalize_as = "cancelled"
            if not finalize_as:
                # 완료/취소 근거는 없지만 heartbeat가 오래(기본 5분+) 끊긴 '무진행 스트랜드' — 워커가
                # done/error도 못 쓴 채 죽거나 락대기로 멈춰, 그대로 두면 결과가 영영 대화로 안 돌아온다
                # (대표 신고). 조용히 active에 박제하지 않고 error(중단 보고)로 종결해 그때까지의 산출
                # (결과.md·캡처)을 방에 surface한다 — 대표가 진행을 보고 재개/재지시할 수 있게. (finalize_task가
                # 상태.json=error를 blocked_report 경로로 공개; 완료로 오승격하지 않음.) 살아 heartbeat하는
                # 작업은 애초에 stale이 아니라 여기 오지 않으므로 라이브 작업을 오종결할 위험은 없다.
                age_ms = item.get("heartbeat_age_ms")
                if age_ms is None or age_ms < TASK_STRAND_REPORT_GRACE_MS:
                    continue  # 잠깐 stale일 수 있음(막 끊김) → 보수적으로 더 기다린다
                raw_status = {
                    **raw_status,
                    "상태": "error",
                    "사유": (reason + " | " if reason else "")
                            + "reaper: heartbeat 장기 끊김 무진행 스트랜드 강제 중단 보고(워커 종료/락대기 추정)",
                }
                _write_json(work_dir / "상태.json", raw_status)
                finalize_as = "error"  # finalize_task가 error를 blocked_report로 산출 surface
            tp_path = work_dir / "task_pack.json"
            task_pack = _read_json(tp_path, {}) if tp_path.exists() else {}
            if finalize_as == "cancelled" and state != "cancelled":
                # 죽은 워커가 못 쓴 종결 상태를 시스템이 대신 기록(finalize_task가 상태.json을 읽으므로)
                raw_status = {
                    **raw_status,
                    "상태": "cancelled",
                    "사유": (reason + " | " if reason else "") + "reaper: 죽은 워커 취소요청 강제 종결(heartbeat stale)",
                }
                _write_json(work_dir / "상태.json", raw_status)
            res = finalize_task(
                space, task_id=task_id, worker=worker, work_dir=work_dir,
                task_pack=task_pack, objective=task_pack.get("objective", ""),
            )
            results.append({
                "space": space, "task_id": task_id, "worker": worker,
                "reaped_as": (res.get("state") if isinstance(res, dict) else finalize_as),
            })
        except Exception as exc:
            results.append({"space": space, "task_id": task_id, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    return results


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
