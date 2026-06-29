# -*- coding: utf-8 -*-
"""작업계획 승인 게이트 WorkPlan v1.

채팅에이전트가 작업을 받으면 곧장 실행하지 않고 '계획'을 먼저 등록한다.
needs_approval(= 에이전트 명시 선언 OR 시스템 휴리스틱 high)이면 대표 결재 말풍선을 거치고,
아니면 공간관리 자동승인(auto_manager)으로 실행 단계로 넘어간다.

상태: pending_approval → approved → executing → done | error
        pending_approval → rejected
        any(미실행) → superseded

설계 계약: 루트폴더/설계_작업계획승인.md
구조는 release_queue.py(승인 게이트)와 동형 — append-only jsonl + fcntl 락 + stable id + 상태전이.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import threading
from pathlib import Path

from .paths import ROOT, SPACES
from .transcript import now_iso


class WorkPlanError(RuntimeError):
    """WorkPlan 계약을 만족하지 못했다."""


# ── 위험 휴리스틱 신호 키워드 (system_level=high 격상) ──────────────────────────
# 시스템은 '격상만' 한다 — 에이전트가 low라 해도 아래 신호가 잡히면 승인행으로 올린다.
RISK_SIGNALS: dict[str, tuple[str, ...]] = {
    # 지침·규칙 변경 — 반드시 승인
    "guide_change": (
        "law.md", "law_", "지침", "규칙 변경", "규칙변경", "role.md", "공간지침",
        "claude.md", "agents.md", "gemini.md", "gemma.md", "정책 변경", "정책변경",
    ),
    # 대외비 공유/반출 — 반드시 승인
    "confidential_share": (
        "대외비", "confidential", "sensitivity", "기밀", "비공개 자료", "내부자료 공유",
    ),
    # 외부 발행/전송
    "external_publish": (
        "메일", "이메일", "email", "발송", "전송", "게시", "배포", "deploy", "publish",
        "push", "푸시", "결제", "송금", "트윗", "tweet", "외부 공개", "외부공개", "업로드",
    ),
    # 대량/비가역 파일 변경
    #  주의: 부분문자열 매칭이라 '포맷' 단독은 '영상 포맷/콘텐츠 포맷' 같은 양성 표현을 오탐한다
    #  (라이브에서 유튜브 '포맷' 조사 작업이 결재게이트에 걸림). 파괴적 의미(디스크/드라이브 포맷)로 좁힌다.
    "bulk_file_change": (
        "전부 삭제", "모두 삭제", "일괄 삭제", "대량 삭제", "rm -rf", "초기화",
        "덮어쓰기", "전체 삭제", "drop table", "디스크 포맷", "드라이브 포맷", "포맷하", "포맷해",
    ),
}

# 고비용 신호 — 계획 단계 수가 이 값을 넘으면 high로 본다.
HIGH_COST_STEP_THRESHOLD = 12

PENDING = "pending_approval"
APPROVED = "approved"
EXECUTING = "executing"
DONE = "done"
ERROR = "error"
REJECTED = "rejected"
SUPERSEDED = "superseded"

_TERMINAL_STATES = {DONE, ERROR, REJECTED, SUPERSEDED}
_UNSTARTED_STATES = {PENDING, APPROVED}

_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


# ── 경로/직렬화 헬퍼 ─────────────────────────────────────────────────────────
def _queue_path(space: str) -> Path:
    return SPACES / space / "work_plans.jsonl"


def _lock_path(space: str) -> Path:
    return SPACES / space / ".work_plans.lock"


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except Exception:
        return str(path)


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _rows_with_error(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], f"{path.name}: {type(exc).__name__}"
    rows: list[dict] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        return rows, f"{path.name}: invalid_json_lines={bad}"
    return rows, ""


def _strip_internal(row: dict) -> dict:
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


def _with_lock(space: str, fn):
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(space, threading.RLock())
    lock = _lock_path(space)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    with local_lock:
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _latest_by_plan(rows: list[dict]) -> dict:
    latest: dict[str, dict] = {}
    for idx, row in enumerate(rows):
        plan_id = row.get("plan_id")
        if plan_id:
            latest[plan_id] = {**row, "_row_index": idx}
    return latest


def _latest_in_rows(rows: list[dict], plan_id: str) -> dict:
    wanted = str(plan_id or "").strip()
    if not wanted:
        raise WorkPlanError("plan_id required")
    for row in reversed(rows):
        if row.get("plan_id") == wanted:
            return row
    raise WorkPlanError(f"work plan not found: {wanted}")


# ── 위험/승인 판정 ───────────────────────────────────────────────────────────
def _norm_steps(plan_steps) -> list[str]:
    if isinstance(plan_steps, str):
        plan_steps = [plan_steps]
    if not isinstance(plan_steps, (list, tuple)):
        return []
    return [str(s).strip() for s in plan_steps if str(s).strip()][:24]


def detect_risk_signals(text: str) -> list[str]:
    """objective+plan+constraints 합본 텍스트에서 high 신호를 찾는다."""
    low = (text or "").lower()
    signals: list[str] = []
    for signal, keywords in RISK_SIGNALS.items():
        if any(kw.lower() in low for kw in keywords):
            signals.append(signal)
    return signals


def assess_approval(
    objective: str,
    plan_steps,
    agent_needs_approval=None,
    *,
    agent_risk_level: str | None = None,
    agent_reason: str = "",
    constraints=None,
) -> dict:
    """승인 필요 판정. 게이트 = (에이전트 명시 선언) OR (시스템 휴리스틱 high).

    - 에이전트가 승인 필요로 '선언'하면 무조건 승인행(에이전트 판단 존중).
    - 에이전트가 선언하지 않으면(None/False) 시스템 위험도로만 판정한다 — 위험도 기반 혼합.
      평범한 작업은 자동 진행(무회귀), 지침변경·대외비·외부발행·대량삭제·고비용은 시스템이
      승인행으로 '격상'한다(격상-only, 절대 낮추지 않음).
    """
    steps = _norm_steps(plan_steps)
    constraints = [str(c).strip() for c in (constraints or []) if str(c).strip()]
    blob = "\n".join([str(objective or ""), *steps, *constraints])

    signals = detect_risk_signals(blob)
    if len(steps) > HIGH_COST_STEP_THRESHOLD:
        signals.append("high_cost")
    system_level = "high" if signals else "low"

    # 에이전트 명시 선언: None(미선언)/False → 시스템 위험도에 위임. True만 즉시 승인행.
    declared = bool(agent_needs_approval)

    needs_approval = bool(declared or system_level == "high")
    approval_mode = "representative" if needs_approval else "auto_manager"

    reasons: list[str] = []
    if declared:
        reasons.append(f"에이전트 승인요청: {agent_reason}" if agent_reason else "에이전트가 승인 필요로 선언")
    if system_level == "high":
        reasons.append("시스템 위험신호: " + ", ".join(signals))
    approval_reason = " / ".join(reasons) if reasons else "승인 불필요(자동 진행 가능)"

    return {
        "needs_approval": needs_approval,
        "approval_mode": approval_mode,
        "agent_needs_approval": declared,
        "agent_level": str(agent_risk_level or "").strip().lower() or ("high" if declared else "low"),
        "agent_reason": str(agent_reason or "")[:500],
        "system_level": system_level,
        "system_signals": signals,
        "approval_reason": approval_reason[:500],
    }


# ── 등록 ─────────────────────────────────────────────────────────────────────
def register(
    space: str,
    *,
    requesting_agent: str,
    worker: str,
    objective: str,
    plan_steps,
    assessment: dict | None = None,
    constraints=None,
    context: dict | None = None,
) -> dict:
    """작업계획을 pending_approval로 등록한다(멱등: plan_id stable_id).

    assessment 미제공 시 assess_approval로 산출한다.
    """
    objective = str(objective or "").strip()
    if not objective:
        raise WorkPlanError("objective required")
    worker = str(worker or "").strip()
    if not worker:
        raise WorkPlanError("worker required")
    steps = _norm_steps(plan_steps)
    if not steps:
        steps = [objective]
    constraints = [str(c).strip() for c in (constraints or []) if str(c).strip()][:12]
    context = context or {}
    if assessment is None:
        assessment = assess_approval(objective, steps, constraints=constraints)

    plan_id = _stable_id(
        "plan", space, requesting_agent, worker, objective,
        context.get("intent_id", ""),
    )
    record = {
        "schema": "WorkPlan.v1",
        "plan_id": plan_id,
        "event": "plan_registered",
        "space_id": space,
        "requesting_agent": requesting_agent,
        "worker": worker,
        "objective": objective,
        "plan_steps": steps,
        "constraints": constraints,
        "needs_approval": bool(assessment["needs_approval"]),
        "approval_reason": assessment.get("approval_reason", ""),
        "risk": {
            "agent_needs_approval": assessment.get("agent_needs_approval"),
            "agent_level": assessment.get("agent_level", ""),
            "agent_reason": assessment.get("agent_reason", ""),
            "system_level": assessment.get("system_level", "low"),
            "system_signals": assessment.get("system_signals", []),
        },
        "state": PENDING,
        "approval_mode": assessment.get("approval_mode", "representative"),
        "approval_message_id": "",
        "approved_by": "",
        "approved_at_utc": "",
        "rejected_by": "",
        "reject_reason": "",
        "task_id": "",
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "manager_claim_token": context.get("manager_claim_token", ""),
        "manager_fencing_token": context.get("manager_fencing_token", ""),
        "created_at_utc": now_iso(),
    }

    def mutate():
        path = _queue_path(space)
        rows, error = _rows_with_error(path)
        if error:
            raise WorkPlanError(error)
        for row in reversed(rows):
            if row.get("plan_id") == plan_id:
                return {"record": _strip_internal(row), "duplicate": True}
        _append_jsonl(path, record)
        return {"record": record, "duplicate": False}

    return _with_lock(space, mutate)


# ── 조회 ─────────────────────────────────────────────────────────────────────
def get(space: str, plan_id: str) -> dict:
    rows, error = _rows_with_error(_queue_path(space))
    if error:
        raise WorkPlanError(error)
    return _strip_internal(_latest_in_rows(rows, plan_id))


def list_plans(space: str, states: set[str] | None = None) -> list[dict]:
    rows, error = _rows_with_error(_queue_path(space))
    if error:
        raise WorkPlanError(error)
    latest = _latest_by_plan(rows)
    items = sorted(latest.values(), key=lambda r: r.get("_row_index", 0))
    if states:
        items = [r for r in items if r.get("state") in states]
    return [_strip_internal(r) for r in items]


# ── 상태 전이 ────────────────────────────────────────────────────────────────
def _append_transition(space: str, plan_id: str, build) -> dict:
    def mutate():
        path = _queue_path(space)
        rows, error = _rows_with_error(path)
        if error:
            raise WorkPlanError(error)
        latest = _latest_in_rows(rows, plan_id)
        built = build(latest)
        if isinstance(built, dict) and built.get("_duplicate_result"):
            return {"record": _strip_internal(built["event"]), "duplicate": True}
        wanted = built.get("event_id")
        if wanted:
            for row in reversed(rows):
                if row.get("event_id") == wanted:
                    return {"record": _strip_internal(row), "duplicate": True}
        _append_jsonl(path, built)
        return {"record": built, "duplicate": False}

    return _with_lock(space, mutate)


def _carry(latest: dict) -> dict:
    """전이 이벤트에 보존할 핵심 필드(최신 줄이 곧 plan 상태가 되도록)."""
    keys = (
        "schema", "plan_id", "space_id", "requesting_agent", "worker", "objective",
        "plan_steps", "constraints", "needs_approval", "approval_reason", "risk",
        "approval_mode", "approval_message_id", "approved_by", "approved_at_utc",
        "rejected_by", "reject_reason", "task_id", "intent_id", "conversation_thread_id",
        "room_generation", "source_event_seq", "source_message_id",
        "manager_claim_token", "manager_fencing_token", "created_at_utc",
    )
    return {k: latest.get(k) for k in keys}


def set_approval_message(space: str, plan_id: str, message_id: str) -> dict:
    """대화창에 올린 결재 말풍선의 message_id를 plan에 기록한다."""
    mid = str(message_id or "").strip()
    if not mid:
        raise WorkPlanError("message_id required")

    def build(latest: dict) -> dict:
        if latest.get("approval_message_id") == mid:
            return {"_duplicate_result": True, "event": latest}
        return {
            **_carry(latest),
            "event": "approval_message_set",
            "state": latest.get("state", PENDING),
            "approval_message_id": mid,
            "event_id": _stable_id("plan_event", space, plan_id, "approval_message", mid),
            "updated_at_utc": now_iso(),
        }

    result = _append_transition(space, plan_id, build)
    return {"record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def approve(space: str, plan_id: str, *, actor: str, mode: str, reason: str = "") -> dict:
    """계획 승인. mode='auto_manager'(자동) | 'representative'(대표).

    불변식 B: needs_approval=true(approval_mode=representative)인 계획은 auto_manager로 승인 불가.
    """
    mode = str(mode or "").strip()
    if mode not in {"auto_manager", "representative"}:
        raise WorkPlanError(f"invalid approve mode: {mode}")

    def build(latest: dict) -> dict:
        state = latest.get("state")
        if state == APPROVED:
            return {"_duplicate_result": True, "event": latest}
        if state in {EXECUTING, DONE}:
            return {"_duplicate_result": True, "event": latest}
        if state != PENDING:
            raise WorkPlanError(f"plan not pending_approval (state={state})")
        if mode == "auto_manager" and latest.get("approval_mode") == "representative":
            # 불변식 B: 승인필요 계획은 자동승인 절대 불가
            raise WorkPlanError("needs_approval plan cannot be auto-approved (requires representative)")
        return {
            **_carry(latest),
            "event": "plan_approved",
            "state": APPROVED,
            "approval_mode": latest.get("approval_mode"),
            "approved_by": actor,
            "approved_via": mode,
            "approve_reason": str(reason or "")[:500],
            "approved_at_utc": now_iso(),
            "event_id": _stable_id("plan_event", space, plan_id, "approved"),
        }

    result = _append_transition(space, plan_id, build)
    return {"record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def reject(space: str, plan_id: str, *, actor: str, reason: str = "") -> dict:
    def build(latest: dict) -> dict:
        state = latest.get("state")
        if state == REJECTED:
            return {"_duplicate_result": True, "event": latest}
        if state in {EXECUTING, DONE}:
            raise WorkPlanError(f"plan already started (state={state})")
        if state not in {PENDING, APPROVED}:
            raise WorkPlanError(f"plan not rejectable (state={state})")
        return {
            **_carry(latest),
            "event": "plan_rejected",
            "state": REJECTED,
            "rejected_by": actor,
            "reject_reason": str(reason or "")[:500],
            "rejected_at_utc": now_iso(),
            "event_id": _stable_id("plan_event", space, plan_id, "rejected"),
        }

    result = _append_transition(space, plan_id, build)
    return {"record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def mark_executing(space: str, plan_id: str, *, task_id: str) -> dict:
    """승인된 계획을 실행 단계로 전이하며 생성된 작업코드를 결속한다."""
    task_id = str(task_id or "").strip()
    if not task_id:
        raise WorkPlanError("task_id required")

    def build(latest: dict) -> dict:
        state = latest.get("state")
        if state == EXECUTING and latest.get("task_id") == task_id:
            return {"_duplicate_result": True, "event": latest}
        if state != APPROVED:
            raise WorkPlanError(f"plan must be approved before executing (state={state})")
        return {
            **_carry(latest),
            "event": "plan_executing",
            "state": EXECUTING,
            "task_id": task_id,
            "executing_at_utc": now_iso(),
            "event_id": _stable_id("plan_event", space, plan_id, "executing", task_id),
        }

    result = _append_transition(space, plan_id, build)
    return {"record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def mark_finished(space: str, plan_id: str, *, state: str, note: str = "") -> dict:
    if state not in {DONE, ERROR}:
        raise WorkPlanError(f"invalid finish state: {state}")

    def build(latest: dict) -> dict:
        if latest.get("state") == state:
            return {"_duplicate_result": True, "event": latest}
        if latest.get("state") not in {EXECUTING, APPROVED}:
            raise WorkPlanError(f"plan not running (state={latest.get('state')})")
        return {
            **_carry(latest),
            "event": f"plan_{state}",
            "state": state,
            "finish_note": str(note or "")[:500],
            "finished_at_utc": now_iso(),
            "event_id": _stable_id("plan_event", space, plan_id, state),
        }

    result = _append_transition(space, plan_id, build)
    return {"record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))}


def supersede(space: str, plan_ids, *, actor: str = "공간관리", reason: str = "") -> list[dict]:
    """미실행(pending/approved) 계획들을 무효화한다(generation 변경 등)."""
    out = []
    for plan_id in plan_ids or []:
        def build(latest: dict, _pid=plan_id) -> dict:
            if latest.get("state") in _TERMINAL_STATES:
                return {"_duplicate_result": True, "event": latest}
            if latest.get("state") not in _UNSTARTED_STATES:
                raise WorkPlanError(f"plan not supersedable (state={latest.get('state')})")
            return {
                **_carry(latest),
                "event": "plan_superseded",
                "state": SUPERSEDED,
                "superseded_by": actor,
                "supersede_reason": str(reason or "")[:500],
                "superseded_at_utc": now_iso(),
                "event_id": _stable_id("plan_event", space, _pid, "superseded", reason),
            }

        try:
            result = _append_transition(space, plan_id, build)
            out.append({"plan_id": plan_id, "record": result.get("record") or {}, "duplicate": bool(result.get("duplicate"))})
        except WorkPlanError as exc:
            out.append({"plan_id": plan_id, "error": str(exc)})
    return out


# ── 스냅샷(공간관리 가시성·자동연속 트리거용) ────────────────────────────────
def snapshot(space: str) -> dict:
    rows, error = _rows_with_error(_queue_path(space))
    latest = _latest_by_plan(rows)
    values = sorted(latest.values(), key=lambda r: r.get("_row_index", 0))
    state_counts: dict[str, int] = {}
    for row in values:
        st = row.get("state", "unknown")
        state_counts[st] = state_counts.get(st, 0) + 1
    pending = [r for r in values if r.get("state") == PENDING]
    auto_pending = [r for r in pending if r.get("approval_mode") == "auto_manager"]
    rep_pending = [r for r in pending if r.get("approval_mode") == "representative"]

    def _item(row: dict) -> dict:
        return {
            "plan_id": row.get("plan_id", ""),
            "requesting_agent": row.get("requesting_agent", ""),
            "worker": row.get("worker", ""),
            "objective": str(row.get("objective", ""))[:300],
            "plan_steps": row.get("plan_steps", [])[:12],
            "state": row.get("state", ""),
            "approval_mode": row.get("approval_mode", ""),
            "needs_approval": bool(row.get("needs_approval")),
            "approval_reason": row.get("approval_reason", ""),
            "approval_message_id": row.get("approval_message_id", ""),
            "task_id": row.get("task_id", ""),
            "intent_id": row.get("intent_id", ""),
            "room_generation": row.get("room_generation"),
        }

    latest_row = values[-1] if values else {}
    return {
        "schema": "WorkPlanSnapshot.v1",
        "plan_count": len(values),
        "event_count": len(rows),
        "pending_count": len(pending),
        "auto_approvable_pending_count": len(auto_pending),
        "representative_pending_count": len(rep_pending),
        "state_counts": state_counts,
        "pending_items": [_item(r) for r in pending[-8:]],
        "auto_pending_items": [_item(r) for r in auto_pending[-8:]],
        "representative_pending_items": [_item(r) for r in rep_pending[-8:]],
        "latest_plan_id": latest_row.get("plan_id", ""),
        "latest_state": latest_row.get("state", ""),
        "ledger_corrupt": bool(error),
        "ledger_errors": [error] if error else [],
    }
