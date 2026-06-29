# -*- coding: utf-8 -*-
"""공간 wake에 전달할 최소 ContextPack/TurnHandoffPack."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import lesson_ledger, space_memory
from .paths import SPACES
from .transcript import now_iso, read


MAX_GUIDE_CHARS = 4000
MAX_SUMMARY_CHARS = 4000
MAX_MESSAGE_CHARS = 1000
MAX_HANDOFF_PREVIEW_CHARS = 1400
MAX_HANDOFF_BRIEF_PREVIEW_CHARS = 1800


def _space_dir(space: str) -> Path:
    return SPACES / space


def _north_star_path(space: str) -> Path:
    return _space_dir(space) / "north_star_goal_ledger.jsonl"


def _context_pack_path(space: str) -> Path:
    return _space_dir(space) / "context_packs.jsonl"


def _wake_manifest_path(space: str) -> Path:
    return _space_dir(space) / "wake_pack_manifest.jsonl"


def _read_text(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return text[:limit]


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _rows_with_error(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], ""
    out = []
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
            out.append(row)
        else:
            bad_lines += 1
    if bad_lines:
        return out, f"{path.name}: invalid_json_lines={bad_lines}"
    return out, ""


def _rows(path: Path) -> list[dict]:
    return _rows_with_error(path)[0]


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _checksum(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _preview(value, limit: int = MAX_HANDOFF_PREVIEW_CHARS) -> str:
    text = str(value or "").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def append_north_star_goal(
    space: str,
    text: str,
    *,
    source: str = "manual",
    source_message_id: str = "",
    priority: str = "normal",
) -> dict:
    clean = (text or "").strip()
    if not clean:
        raise ValueError("north star goal text required")
    row = {
        "schema": "NorthStarGoalLedger.v1",
        "goal_id": _stable_id("goal", space, clean, source_message_id),
        "state": "active",
        "text": clean,
        "source": source,
        "source_message_id": source_message_id,
        "priority": priority,
        "created_at": now_iso(),
    }
    _append_jsonl(_north_star_path(space), row)
    return row


def _active_goals_from_rows(rows: list[dict], limit: int = 8) -> list[dict]:
    by_id = {}
    for row in rows:
        goal_id = row.get("goal_id")
        if goal_id:
            by_id[goal_id] = row
    active = [row for row in by_id.values() if row.get("state", "active") == "active"]
    return active[-limit:]


def active_north_star_goals(space: str, limit: int = 8) -> list[dict]:
    return _active_goals_from_rows(_rows(_north_star_path(space)), limit)


def _recent_messages(space: str, limit: int = 32) -> list[dict]:
    out = []
    for row in read(space, limit):
        out.append({
            "event_seq": row.get("event_seq"),
            "message_id": row.get("message_id", ""),
            "speaker": row.get("화자", ""),
            "speaker_code": row.get("코드", ""),
            "role": row.get("역할", ""),
            "content": str(row.get("내용", ""))[:MAX_MESSAGE_CHARS],
            "intent_id": row.get("intent_id", ""),
            "conversation_thread_id": row.get("conversation_thread_id", ""),
            "room_generation": row.get("room_generation"),
        })
    return out


def _source_message(space: str, context: dict | None) -> dict:
    context = context or {}
    source_message_id = str(context.get("source_message_id") or "")
    source_event_seq = context.get("source_event_seq")
    for row in reversed(read(space, None)):
        if source_message_id and row.get("message_id") == source_message_id:
            return row
        try:
            if source_event_seq is not None and int(row.get("event_seq") or 0) == int(source_event_seq):
                return row
        except Exception:
            continue
    return {}


def _memory_snapshot(space: str) -> dict:
    try:
        return space_memory.ensure_projection(space)
    except Exception as exc:
        legacy = _read_text(_space_dir(space) / "요약.md", MAX_SUMMARY_CHARS)
        return {
            "schema": "SpaceMemorySnapshot.v1",
            "space_id": space,
            "memory_source": "legacy_summary",
            "projection_available": False,
            "projection_corrupt": True,
            "projection_errors": [f"{type(exc).__name__}: {str(exc)[:180]}"],
            "projection_id": "",
            "projection_version": 0,
            "projection_checksum": "",
            "projection_state": "failed",
            "source": "",
            "projection_method": {},
            "applied_event_seq": 0,
            "latest_event_seq": 0,
            "projection_lag": 0,
            "active_context_summary": legacy[:MAX_MESSAGE_CHARS],
            "active_context": [],
            "representative_requests": [],
            "user_directive_items": [],
            "relevant_past": [],
            "topic_threads": [],
            "active_topic_threads": [],
            "dormant_topic_threads": [],
            "precedence_policy": {},
            "conflict_hints": {},
            "source_refs": [],
            "legacy_summary_hint": {
                "status": "fallback",
                "char_count": len(legacy),
                "summary_hash": _stable_id("summary", legacy),
                "text": legacy[:1200],
            },
        }


def build_context_pack(
    space: str,
    *,
    mode: str,
    event: str,
    context: dict | None,
    target_agent: str = "",
    recent_limit: int = 32,
) -> dict:
    sdir = _space_dir(space)
    context = context or {}
    guide = _read_text(sdir / "공간지침.md", MAX_GUIDE_CHARS)
    summary = _read_text(sdir / "요약.md", MAX_SUMMARY_CHARS)
    memory = _memory_snapshot(space)
    recent = _recent_messages(space, recent_limit)
    source = _source_message(space, context)
    goals = active_north_star_goals(space)
    lesson_pack = lesson_ledger.build_lesson_pack(
        space,
        mode=mode,
        context=context,
        event=event,
        target_agent=target_agent,
    )
    latest_event_seq = recent[-1].get("event_seq") if recent else None
    identity = {
        "space_id": space,
        "mode": mode,
        "target_agent": target_agent,
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "latest_event_seq": latest_event_seq,
        "guide_hash": _stable_id("guide", guide),
        "summary_hash": _stable_id("summary", summary),
        "memory_source": memory.get("memory_source", "legacy_summary"),
        "memory_projection_id": memory.get("projection_id", ""),
        "memory_projection_version": memory.get("projection_version", 0),
        "memory_projection_checksum": memory.get("projection_checksum", ""),
        "memory_applied_event_seq": memory.get("applied_event_seq", 0),
        "memory_projection_lag": memory.get("projection_lag", 0),
        "goal_ids": [g.get("goal_id", "") for g in goals],
        "lesson_ids": lesson_pack.get("included_lessons", []),
        "lesson_pack_hash": _checksum(lesson_pack),
        "lesson_pack_status": lesson_pack.get("lesson_pack_status", ""),
    }
    pack = {
        "schema": "ContextPack.compat_minimal.v1",
        "context_pack_id": _stable_id("ctx", identity),
        "created_at": now_iso(),
        **identity,
        "event": event,
        "north_star_goals": goals,
        "lesson_pack": lesson_pack,
        "space_guide_excerpt": guide,
        "space_summary_excerpt": summary,
        "space_memory_projection": memory,
        "active_context": memory.get("active_context", []),
        "active_context_summary": memory.get("active_context_summary", ""),
        "representative_requests": memory.get("representative_requests", []),
        "user_directive_items": memory.get("user_directive_items", []),
        "relevant_past": memory.get("relevant_past", []),
        "topic_threads": memory.get("topic_threads", []),
        "active_topic_threads": memory.get("active_topic_threads", []),
        "dormant_topic_threads": memory.get("dormant_topic_threads", []),
        "precedence_policy": memory.get("precedence_policy", {}),
        "conflict_hints": memory.get("conflict_hints", {}),
        "source_refs": memory.get("source_refs", []),
        "current_user_request": {
            "event_seq": source.get("event_seq"),
            "message_id": source.get("message_id", ""),
            "speaker": source.get("화자", ""),
            "content": str(source.get("내용", ""))[:MAX_MESSAGE_CHARS],
            "ingress_type": source.get("ingress_type", ""),
            "cancel_replan_fence": bool(source.get("cancel_replan_fence")),
        },
        "recent_messages": recent,
        "fallback_policy": {
            "legacy_message_fallback_allowed": True,
            "managed_v2_side_effects_allowed_without_pack": False,
            "if_pack_missing_or_mismatch": "stop_or_return_to_manager",
        },
    }
    pack["context_pack_checksum"] = _checksum(pack)
    return pack


def turn_handoff_brief(pack: dict, target_agent: str, manager_message: str, reason: str) -> str:
    req = pack.get("current_user_request") or {}
    goals = pack.get("north_star_goals") or []
    goal_lines = "\n".join(f"- {g.get('text', '')}" for g in goals[:5]) or "- 등록된 장기 목표 없음"
    lesson_pack = pack.get("lesson_pack") or {}
    must_lessons = lesson_pack.get("must_apply") or []
    may_lessons = lesson_pack.get("may_apply") or []
    ref_lessons = lesson_pack.get("reference_only") or []
    memory = pack.get("space_memory_projection") or {}
    active_context_summary = str(memory.get("active_context_summary") or pack.get("active_context_summary") or "").strip()

    def context_lines(items: list[dict], empty: str, limit: int = 32) -> str:
        lines = []
        for item in items[:limit]:
            seq = item.get("event_seq", "")
            speaker = item.get("speaker") or item.get("role") or "?"
            preview = str(item.get("content_preview") or item.get("content") or "").replace("\n", " ").strip()
            lines.append(f"- event #{seq} {speaker}: {preview[:220]}")
        return "\n".join(lines) if lines else empty

    def lesson_lines(items: list[dict], empty: str) -> str:
        lines = []
        for lesson in items[:5]:
            lesson_id = lesson.get("lesson_id", "")
            instruction = str(lesson.get("instruction") or "").replace("\n", " ").strip()
            level = lesson.get("application_level", "")
            lines.append(f"- {lesson_id} [{level}] {instruction}")
        return "\n".join(lines) if lines else empty

    def directive_lines(items: list[dict], empty: str, limit: int = 40) -> str:
        # 대표 지시는 시간순 전부 전달한다(누락 금지). 나중 지시가 이전과 충돌하면 나중 것 우선,
        # 충돌하지 않는 이전 지시는 계속 유효 — 에이전트가 현재 방향을 종합해 판단하도록 한다.
        lines = []
        for item in items[-limit:]:
            seq = item.get("event_seq", "")
            rank = item.get("precedence_rank", "")
            preview = str(item.get("content_preview") or "").replace("\n", " ").strip()
            lines.append(f"- event #{seq} rank {rank}: {preview[:260]}")
        return "\n".join(lines) if lines else empty

    def topic_lines(items: list[dict], empty: str, limit: int = 8) -> str:
        lines = []
        for item in items[:limit]:
            latest = item.get("latest_event_seq", "")
            thread_id = item.get("thread_id", "")
            status = item.get("status", "")
            preview = str(item.get("latest_user_request") or item.get("latest_assistant_reply") or "").replace("\n", " ").strip()
            lines.append(f"- {status} {thread_id} @event #{latest}: {preview[:180]}")
        return "\n".join(lines) if lines else empty

    must_ids = [lesson.get("lesson_id", "") for lesson in must_lessons if lesson.get("lesson_id")]
    precedence = memory.get("precedence_policy") or pack.get("precedence_policy") or {}
    conflict_hints = memory.get("conflict_hints") or pack.get("conflict_hints") or {}
    report_template = ""
    if must_ids:
        applications = [{
            "lesson_id": lesson_id,
            "applied": True,
            "not_applicable_reason": "",
            "how": "이번 답변에 어떻게 반영했는지 한 줄로 기록",
            "outcome": "success",
            "needs_lesson_update": False,
        } for lesson_id in must_ids]
        report_template = (
            "\n## LessonApplicationReport\n"
            "아래 must_apply 레슨은 답변 마지막에 시스템용 JSON으로 적용 여부를 보고한다. "
            "이 JSON은 공개 전 제거된다.\n"
            "```json\n"
            f"{json.dumps({'schema': 'LessonApplicationReport.v1', 'applications': applications}, ensure_ascii=False, indent=2)}\n"
            "```\n"
        )
    return (
        "# TurnHandoffBrief\n\n"
        f"- 너는 이 공간의 채팅에이전트로 턴을 받았다: `{target_agent}`\n"
        f"- space_id: {pack.get('space_id', '')}\n"
        f"- intent_id: {pack.get('intent_id', '')}\n"
        f"- conversation_thread_id: {pack.get('conversation_thread_id', '')}\n"
        f"- room_generation: {pack.get('room_generation')}\n"
        f"- response_target: source message `{req.get('message_id', '')}`에 이어서 방에 답할 후보를 만든다.\n"
        f"- 공간관리 전달 메시지: {manager_message}\n"
        f"- 턴 전달 이유: {reason}\n\n"
        "## 대표/공간 장기 목표\n"
        f"{goal_lines}\n\n"
        "## 현재 맥락 projection\n"
        f"- memory_source: {memory.get('memory_source', pack.get('memory_source', ''))}\n"
        f"- applied_event_seq: {memory.get('applied_event_seq', pack.get('memory_applied_event_seq', 0))}\n"
        f"- active_context_summary: {active_context_summary or '등록된 현재 맥락 없음'}\n"
        "- 최근 핵심 대화\n"
        f"{context_lines(memory.get('active_context') or pack.get('recent_messages') or [], '- 없음')}\n"
        "- 관련 과거/대표 요청\n"
        f"{context_lines((memory.get('representative_requests') or [])[-12:] or memory.get('relevant_past') or [], '- 없음')}\n\n"
        "## 대표 지시 누적(시간순 전부) · 현재 방향 종합\n"
        "- 아래는 대표가 지금까지 남긴 지시를 event 순서대로 누적한 것이다. 빠짐없이 읽는다.\n"
        "- **입장 변경 규칙**: 나중 지시가 이전 지시와 충돌하면 나중 것을 따른다(대표의 입장이 바뀐 것). "
        "충돌하지 않는 이전 지시는 계속 유효하다. 이 규칙으로 **이전 요청과 모순 없이 현재 추구하는 방향**을 스스로 종합해 판단한다.\n"
        f"- precedence_rule: {precedence.get('rule', 'confirmed conflict만 최신 발언 우선')}\n"
        f"- conflict_candidate_count: {conflict_hints.get('candidate_count', 0)}\n"
        f"{directive_lines(memory.get('user_directive_items') or pack.get('user_directive_items') or [], '- 없음')}\n\n"
        "## 주제 상태\n"
        "- active topics\n"
        f"{topic_lines(memory.get('active_topic_threads') or pack.get('active_topic_threads') or [], '- 없음')}\n"
        "- dormant topics\n"
        f"{topic_lines(memory.get('dormant_topic_threads') or pack.get('dormant_topic_threads') or [], '- 없음')}\n\n"
        "## 이번 wake 레슨\n"
        f"- lesson_pack_status: {lesson_pack.get('lesson_pack_status', 'unknown')}\n"
        "- must_apply\n"
        f"{lesson_lines(must_lessons, '- 없음')}\n"
        "- may_apply\n"
        f"{lesson_lines(may_lessons, '- 없음')}\n"
        "- reference_only\n"
        f"{lesson_lines(ref_lessons, '- 없음')}\n\n"
        "## 응답 원칙\n"
        "- 지금 턴의 맥락과 공간지침에 맞춰 한 번 답한다.\n"
        "- 작업이 필요하면 직접 작업 폴더를 만들거나 결과를 공개하지 말고, `ChatAgentResult.v1` JSON으로 `action=request_work`와 `work_request.objective`를 반환한다.\n"
        "- **`public_reply`는 방에 네 말풍선으로 공개된다. 협업이 보이도록 거의 항상 채운다.** 작업을 넘길 때도 방의 동료·대표가 보도록, 무엇을 어떻게 할지(또는 정리한 요지·검수 결과)를 한두 문장으로 `public_reply`에 적는다. 비워 두면 방에서 네 존재가 보이지 않는다.\n"
        "- `request_work`의 작업 상세(objective/constraints)는 TaskRegistry로 가고, `public_reply`는 방에 공개된다 — 둘 다 채우면 협업이 방에서 보이면서 작업도 진행된다.\n"
        "- `suggested_worker`에는 작업을 맡길 방 멤버를 적는다. 표시이름(예: 구현자)이나 토큰(예: 구현자_2a79) 모두 쓸 수 있고, 시스템이 토큰으로 해석한다.\n"
        "- 다른 공간이나 다른 intent의 기억과 섞지 않는다.\n"
        "- ChatAgentResult.v1 예시: "
        '{"schema":"ChatAgentResult.v1","action":"request_work","public_reply":"요구사항을 이렇게 정리했고, 구현은 구현자에게 넘깁니다: add/list/done 3기능, JSON 저장.","work_request":{"objective":"해야 할 작업","constraints":[],"suggested_worker":"'
        f"{target_agent}"
        '"},"manager_requests":[]}\n'
        f"{report_template}"
    )


def build_turn_handoff_pack(
    space: str,
    *,
    target_agent: str,
    manager_message: str,
    reason: str,
    context: dict | None,
    manager_claim_context: dict | None,
    context_pack: dict,
) -> dict:
    context = context or {}
    claim = manager_claim_context or {}
    wake_id = _stable_id(
        "wake",
        space,
        target_agent,
        context.get("intent_id", ""),
        context.get("source_event_seq"),
        context.get("source_message_id", ""),
        claim.get("claim_token", ""),
    )
    pack = {
        "schema": "TurnHandoffPack.compat_minimal.v1",
        "wake_id": wake_id,
        "turn_handoff_id": _stable_id("turn", wake_id, context_pack.get("context_pack_id", "")),
        "created_at": now_iso(),
        "space_id": space,
        "target_agent": target_agent,
        "mode": "chat",
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "response_target": {
            "type": "space_thread",
            "reply_to_message_id": context.get("reply_to_message_id") or context.get("source_message_id", ""),
            "source_event_seq": context.get("source_event_seq"),
            "conversation_thread_id": context.get("conversation_thread_id", ""),
            "intent_id": context.get("intent_id", ""),
        },
        "why_you": reason,
        "manager_message": manager_message,
        "allowed_actions": ["reply_candidate_to_manager", "request_work_via_manager"],
        "disallowed_actions": ["direct_transcript_write", "direct_task_wake", "direct_public_publish"],
        "return_contract": {
            "kind": "single_chat_reply_candidate",
            "published_by": "space_manager_publish_ledger",
            "must_echo_space_id": space,
            "must_remain_in_thread": True,
            "structured_request_schema": "ChatAgentResult.v1",
            "request_work_route": "space_manager_task_registry",
            "must_report_lessons": [
                lesson.get("lesson_id", "")
                for lesson in (context_pack.get("lesson_pack") or {}).get("must_apply", [])
                if lesson.get("lesson_id")
            ],
            "lesson_application_report_schema": "LessonApplicationReport.v1",
        },
        "lesson_pack_status": (context_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
        "memory_source": context_pack.get("memory_source", ""),
        "memory_projection_id": context_pack.get("memory_projection_id", ""),
        "memory_projection_version": context_pack.get("memory_projection_version", 0),
        "memory_projection_checksum": context_pack.get("memory_projection_checksum", ""),
        "memory_applied_event_seq": context_pack.get("memory_applied_event_seq", 0),
        "memory_projection_lag": context_pack.get("memory_projection_lag", 0),
        "included_lessons": (context_pack.get("lesson_pack") or {}).get("included_lessons", []),
    }
    pack["turn_handoff_brief"] = turn_handoff_brief(context_pack, target_agent, manager_message, reason)
    pack["turn_handoff_checksum"] = _checksum(pack)
    return pack


def _turn_handoff_observation(
    *,
    space: str,
    manifest_id: str,
    state: str,
    recipient: str,
    delivery_type: str,
    context_pack: dict,
    turn_handoff_pack: dict,
    manager_claim_context: dict | None,
    delivered_at: str,
) -> dict:
    claim = manager_claim_context or {}
    lesson_pack = context_pack.get("lesson_pack") or {}
    must_apply = [
        lesson.get("lesson_id", "")
        for lesson in lesson_pack.get("must_apply", [])
        if lesson.get("lesson_id")
    ]
    recent_messages = context_pack.get("recent_messages") or []
    current_request = context_pack.get("current_user_request") or {}
    return_contract = turn_handoff_pack.get("return_contract") or {}
    response_target = turn_handoff_pack.get("response_target") or {}
    return {
        "schema": "TurnHandoffObservation.v1",
        "manifest_id": manifest_id,
        "state": state,
        "delivered_at": delivered_at,
        "turn_handoff_created_at": turn_handoff_pack.get("created_at", ""),
        "space_id": space,
        "recipient": recipient,
        "target_agent": turn_handoff_pack.get("target_agent") or recipient,
        "delivery_type": delivery_type,
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        "turn_handoff_checksum": turn_handoff_pack.get("turn_handoff_checksum", ""),
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "intent_id": context_pack.get("intent_id", ""),
        "conversation_thread_id": context_pack.get("conversation_thread_id", ""),
        "room_generation": context_pack.get("room_generation"),
        "source_event_seq": context_pack.get("source_event_seq"),
        "source_message_id": context_pack.get("source_message_id", ""),
        "response_target": response_target,
        "why_you": _preview(turn_handoff_pack.get("why_you", ""), 700),
        "manager_message_preview": _preview(turn_handoff_pack.get("manager_message", ""), 900),
        "turn_handoff_brief_preview": _preview(
            turn_handoff_pack.get("turn_handoff_brief", ""),
            MAX_HANDOFF_BRIEF_PREVIEW_CHARS,
        ),
        "current_user_request_preview": _preview(current_request.get("content", ""), 700),
        "recent_message_count": len(recent_messages),
        "allowed_actions": list(turn_handoff_pack.get("allowed_actions") or []),
        "disallowed_actions": list(turn_handoff_pack.get("disallowed_actions") or []),
        "return_contract": return_contract,
        "lesson_pack_status": lesson_pack.get("lesson_pack_status", ""),
        "included_lesson_count": len(lesson_pack.get("included_lessons") or []),
        "must_apply_lesson_count": len(must_apply),
        "must_apply_lessons": must_apply,
    }


def record_pack_delivery(
    space: str,
    *,
    recipient: str,
    delivery_type: str,
    context_pack: dict,
    turn_handoff_pack: dict | None = None,
    manager_claim_context: dict | None = None,
) -> dict:
    claim = manager_claim_context or {}
    manifest_id = f"manifest_{uuid4().hex[:12]}"
    state = "context_delivered"
    delivered_at = now_iso()
    turn_handoff_observation = {}
    if turn_handoff_pack:
        turn_handoff_observation = _turn_handoff_observation(
            space=space,
            manifest_id=manifest_id,
            state=state,
            recipient=recipient,
            delivery_type=delivery_type,
            context_pack=context_pack,
            turn_handoff_pack=turn_handoff_pack,
            manager_claim_context=manager_claim_context,
            delivered_at=delivered_at,
        )
    row = {
        "schema": "WakePackManifest.v1",
        "manifest_id": manifest_id,
        "state": state,
        "delivered_at": delivered_at,
        "space_id": space,
        "recipient": recipient,
        "delivery_type": delivery_type,
        "mode": context_pack.get("mode", ""),
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "wake_id": (turn_handoff_pack or {}).get("wake_id", ""),
        "turn_handoff_id": (turn_handoff_pack or {}).get("turn_handoff_id", ""),
        "turn_handoff_checksum": (turn_handoff_pack or {}).get("turn_handoff_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "intent_id": context_pack.get("intent_id", ""),
        "conversation_thread_id": context_pack.get("conversation_thread_id", ""),
        "room_generation": context_pack.get("room_generation"),
        "source_event_seq": context_pack.get("source_event_seq"),
        "source_message_id": context_pack.get("source_message_id", ""),
        "lesson_pack_status": (context_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
        "included_lessons": (context_pack.get("lesson_pack") or {}).get("included_lessons", []),
        "must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in (context_pack.get("lesson_pack") or {}).get("must_apply", [])
            if lesson.get("lesson_id")
        ],
    }
    if turn_handoff_observation:
        row["turn_handoff_observation"] = turn_handoff_observation
    _append_jsonl(_context_pack_path(space), {
        "schema": context_pack.get("schema", "ContextPack.compat_minimal.v1"),
        "recorded_at": row["delivered_at"],
        "recipient": recipient,
        "delivery_type": delivery_type,
        **context_pack,
    })
    _append_jsonl(_wake_manifest_path(space), row)
    return row


def render_manager_context_prompt(context_pack: dict) -> str:
    return (
        "## ContextPack.compat_minimal.v1\n"
        "아래 JSON은 이번 판단에 사용할 정본 맥락이다. pack이 현재 이벤트와 맞지 않는다고 판단되면 pass하지 말고 stop하라.\n"
        "```json\n"
        f"{json.dumps(context_pack, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def render_turn_handoff_prompt(context_pack: dict, turn_handoff_pack: dict) -> str:
    return (
        f"{turn_handoff_pack.get('turn_handoff_brief', '')}\n"
        "## TurnHandoffPack.compat_minimal.v1\n"
        "```json\n"
        f"{json.dumps(turn_handoff_pack, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "## ContextPack.compat_minimal.v1\n"
        "```json\n"
        f"{json.dumps(context_pack, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "# 실제 요청\n\n"
        f"{turn_handoff_pack.get('manager_message', '')}"
    )


def snapshot(space: str) -> dict:
    packs, packs_error = _rows_with_error(_context_pack_path(space))
    manifests, manifests_error = _rows_with_error(_wake_manifest_path(space))
    goal_rows, goals_error = _rows_with_error(_north_star_path(space))
    counts = {}
    for row in manifests:
        key = row.get("delivery_type", "unknown")
        counts[key] = counts.get(key, 0) + 1
    latest_manifest = manifests[-1] if manifests else {}
    latest_pack = packs[-1] if packs else {}
    latest_lesson_pack = latest_pack.get("lesson_pack") or {}
    handoff_manifests = [row for row in manifests if row.get("turn_handoff_id")]
    latest_handoff_manifest = handoff_manifests[-1] if handoff_manifests else {}
    latest_handoff = latest_handoff_manifest.get("turn_handoff_observation") or {}
    if latest_handoff_manifest and not latest_handoff:
        latest_handoff = {
            "schema": "TurnHandoffObservation.legacy_manifest.v1",
            "delivered_at": latest_handoff_manifest.get("delivered_at", ""),
            "space_id": latest_handoff_manifest.get("space_id", space),
            "recipient": latest_handoff_manifest.get("recipient", ""),
            "target_agent": latest_handoff_manifest.get("recipient", ""),
            "delivery_type": latest_handoff_manifest.get("delivery_type", ""),
            "wake_id": latest_handoff_manifest.get("wake_id", ""),
            "turn_handoff_id": latest_handoff_manifest.get("turn_handoff_id", ""),
            "turn_handoff_checksum": latest_handoff_manifest.get("turn_handoff_checksum", ""),
            "context_pack_id": latest_handoff_manifest.get("context_pack_id", ""),
            "context_pack_checksum": latest_handoff_manifest.get("context_pack_checksum", ""),
            "intent_id": latest_handoff_manifest.get("intent_id", ""),
            "conversation_thread_id": latest_handoff_manifest.get("conversation_thread_id", ""),
            "room_generation": latest_handoff_manifest.get("room_generation"),
            "source_event_seq": latest_handoff_manifest.get("source_event_seq"),
            "source_message_id": latest_handoff_manifest.get("source_message_id", ""),
            "lesson_pack_status": latest_handoff_manifest.get("lesson_pack_status", ""),
            "included_lesson_count": len(latest_handoff_manifest.get("included_lessons") or []),
            "must_apply_lesson_count": len(latest_handoff_manifest.get("must_apply_lessons") or []),
            "must_apply_lessons": latest_handoff_manifest.get("must_apply_lessons") or [],
        }
    ledger_errors = [err for err in (packs_error, manifests_error, goals_error) if err]
    return {
        "context_pack_count": len(packs),
        "wake_manifest_count": len(manifests),
        "turn_handoff_count": len(handoff_manifests),
        "delivery_counts": counts,
        "latest_manifest_id": latest_manifest.get("manifest_id", ""),
        "latest_manifest_state": latest_manifest.get("state", ""),
        "latest_delivered_at": latest_manifest.get("delivered_at", ""),
        "latest_context_pack_id": latest_pack.get("context_pack_id", ""),
        "latest_wake_id": latest_manifest.get("wake_id", ""),
        "latest_turn_handoff_id": latest_handoff_manifest.get("turn_handoff_id", ""),
        "latest_turn_handoff_checksum": latest_handoff_manifest.get("turn_handoff_checksum", ""),
        "latest_turn_handoff": latest_handoff,
        "latest_recipient": latest_manifest.get("recipient", ""),
        "latest_delivery_type": latest_manifest.get("delivery_type", ""),
        "latest_lesson_pack_status": latest_lesson_pack.get("lesson_pack_status", ""),
        "latest_memory_source": latest_pack.get("memory_source", ""),
        "latest_memory_projection_id": latest_pack.get("memory_projection_id", ""),
        "latest_memory_projection_version": latest_pack.get("memory_projection_version", 0),
        "latest_memory_applied_event_seq": latest_pack.get("memory_applied_event_seq", 0),
        "latest_memory_projection_lag": latest_pack.get("memory_projection_lag", 0),
        "latest_included_lessons": latest_lesson_pack.get("included_lessons", []),
        "latest_must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in latest_lesson_pack.get("must_apply", [])
            if lesson.get("lesson_id")
        ],
        "north_star_goal_count": len(_active_goals_from_rows(goal_rows, limit=1000)),
        "ledger_corrupt": bool(ledger_errors),
        "ledger_errors": ledger_errors,
    }
