# -*- coding: utf-8 -*-
"""공간 성장 루프 v0: LessonLedger와 사후 평가 기록."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from .paths import ROOT, SPACES
from .transcript import now_iso


class LessonLedgerError(RuntimeError):
    """레슨/평가 기록 계약을 만족하지 못했다."""


LEARNING_DIRNAME = "learning"
ATTENTION_OUTCOMES = {"failed", "rejected", "corrected", "superseded"}
MAX_LESSON_INSTRUCTION_CHARS = 800
MAX_PROMOTION_DRAFT_CHARS = 4000
MAX_PROMOTION_SNAPSHOT_ITEMS = 12


def _space_dir(space: str) -> Path:
    return SPACES / space


def _learning_dir(space: str) -> Path:
    return _space_dir(space) / LEARNING_DIRNAME


def _lessons_path(space: str) -> Path:
    return _learning_dir(space) / "lessons.jsonl"


def _applications_path(space: str) -> Path:
    return _learning_dir(space) / "lesson_applications.jsonl"


def _post_interaction_path(space: str) -> Path:
    return _learning_dir(space) / "post_interaction_evaluations.jsonl"


def _post_task_path(space: str) -> Path:
    return _learning_dir(space) / "post_task_evaluations.jsonl"


def _promotion_candidates_path(space: str) -> Path:
    return _learning_dir(space) / "promotion_candidates.jsonl"


def _growth_gaps_path(space: str) -> Path:
    return _learning_dir(space) / "growth_gaps.jsonl"


def _resource_applications_path(space: str) -> Path:
    return _learning_dir(space) / "resource_applications.jsonl"


def _lock_path(space: str) -> Path:
    return _learning_dir(space) / ".lesson_ledger.lock"


def _ensure(space: str):
    _learning_dir(space).mkdir(parents=True, exist_ok=True)


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


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


def _rows(path: Path) -> list[dict]:
    rows, error = _rows_with_error(path)
    if error:
        raise LessonLedgerError(error)
    return rows


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _append_unique(path: Path, data: dict, id_field: str) -> dict:
    rows, error = _rows_with_error(path)
    if error:
        raise LessonLedgerError(error)
    wanted = data.get(id_field)
    if wanted:
        for row in reversed(rows):
            if row.get(id_field) == wanted:
                return {"record": row, "duplicate": True}
    _append_jsonl(path, data)
    return {"record": data, "duplicate": False}


def _latest_lessons(space: str) -> tuple[list[dict], str]:
    rows, error = _rows_with_error(_lessons_path(space))
    if error:
        return [], error
    by_id = {}
    for row in rows:
        lesson_id = row.get("lesson_id")
        if lesson_id:
            by_id[lesson_id] = row
    return list(by_id.values()), ""


def _get_lesson(space: str, lesson_id: str) -> dict | None:
    lessons, error = _latest_lessons(space)
    if error:
        raise LessonLedgerError(error)
    for lesson in lessons:
        if lesson.get("lesson_id") == lesson_id:
            return lesson
    return None


def graduate_lesson(space: str, lesson_id: str, *, by: str, reason: str, graduated_to: str = "") -> dict:
    """레슨을 졸업시킨다(영구 저장소 역할 종료). status=graduated → build_lesson_pack 주입에서 자동 제외.

    P6: 레슨=임시 버퍼. 영구 학습은 스킬 케이스(절차)·지식 claim(사실)으로 졸업한다. append-only(되돌림 가능).
    """
    def mutate():
        lesson = _get_lesson(space, lesson_id)
        if not lesson:
            raise LessonLedgerError(f"lesson 없음: {lesson_id}")
        updated = dict(lesson)
        updated["status"] = "graduated"
        updated["graduated_by"] = by
        updated["graduate_reason"] = reason
        updated["graduated_to"] = graduated_to
        updated["graduated_at"] = now_iso()
        _append_jsonl(_lessons_path(space), updated)
        return updated

    return _with_lock(space, mutate)


def migrate_lesson_to_case(space: str, lesson_id: str, skill, candidate: dict, *, by: str, from_daepyo: bool = False) -> dict:
    """레슨 → 스킬 케이스 다리. 에이전트가 절차적(procedural)이라 판단한 레슨을 스킬 케이스로 졸업시킨다.

    candidate는 case_ledger 판단 계약(polarity·action·routing_kind·judgment_rationale·source_quote)을 채워야 한다.
    두 원장(케이스/레슨)은 락이 분리돼 완전 원자적이진 않지만, 양쪽 멱등이라 재실행 안전하다.
    """
    from . import case_ledger
    lesson = _get_lesson(space, lesson_id)
    if not lesson:
        raise LessonLedgerError(f"lesson 없음: {lesson_id}")
    case = case_ledger.propose_case(skill, candidate, proposed_by=by, from_daepyo=from_daepyo)
    graduate_lesson(space, lesson_id, by=by,
                    reason="migrated to skill case (P6)", graduated_to=case.get("case_id", ""))
    return {"case": case, "lesson_id": lesson_id, "graduated_to": case.get("case_id", "")}


def _latest_by_id(rows: list[dict], id_field: str) -> dict:
    by_id = {}
    for idx, row in enumerate(rows):
        row_id = row.get(id_field)
        if not row_id:
            continue
        copy = dict(row)
        copy["_row_index"] = idx
        by_id[row_id] = copy
    return by_id


def _promotion_target_kind(lesson: dict) -> str:
    target = str(lesson.get("promotion_target") or "").strip().lower()
    return {
        "knowledge": "knowledge",
        "지식": "knowledge",
        "skill": "skill",
        "스킬": "skill",
    }.get(target, "")


def _promotion_title(lesson: dict) -> str:
    instruction = str(lesson.get("instruction") or "").strip()
    title = re.split(r"[.!?\n]", instruction, maxsplit=1)[0].strip()
    if not title:
        title = str(lesson.get("kind") or "lesson").strip() or "lesson"
    return title[:80]


def _promotion_path_suggestion(space: str, lesson: dict, target_kind: str) -> str:
    lesson_id = str(lesson.get("lesson_id") or "lesson")
    suffix = hashlib.sha256(lesson_id.encode("utf-8")).hexdigest()[:10]
    if target_kind == "skill":
        return f"스킬/후보/{space}-{suffix}/SKILL.md"
    return f"지식/후보/{space}-{suffix}.md"


def _promotion_draft_markdown(space: str, lesson: dict, target_kind: str) -> str:
    title = _promotion_title(lesson)
    instruction = str(lesson.get("instruction") or "").strip()
    applies_when = lesson.get("applies_when") or {}
    evidence = lesson.get("evidence") or []
    if target_kind == "skill":
        body = f"""# {title}

## Purpose
이 스킬 후보는 검증된 레슨을 반복 가능한 작업 절차로 승격하기 위한 초안이다.

## When To Use
- 공간: {space}
- 레슨 종류: {lesson.get("kind", "lesson")}
- 적용 조건: {json.dumps(applies_when, ensure_ascii=False, sort_keys=True)}

## Instructions
{instruction}

## Evidence
{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}
"""
    else:
        body = f"""# {title}

## Summary
{instruction}

## Scope
- 공간: {space}
- 레슨 종류: {lesson.get("kind", "lesson")}
- 적용 조건: {json.dumps(applies_when, ensure_ascii=False, sort_keys=True)}

## Evidence
{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}
"""
    return body[:MAX_PROMOTION_DRAFT_CHARS]


def _promotion_candidate_from_lesson(space: str, lesson: dict, *, actor: str) -> dict:
    target_kind = _promotion_target_kind(lesson)
    if not target_kind:
        raise LessonLedgerError("promotion target must be knowledge or skill")
    lesson_id = str(lesson.get("lesson_id") or "").strip()
    if not lesson_id:
        raise LessonLedgerError("lesson_id required")
    instruction = str(lesson.get("instruction") or "").strip()
    if not instruction:
        raise LessonLedgerError("lesson instruction required")
    promotion_id = _stable_id(
        "promotion",
        space,
        lesson_id,
        target_kind,
        instruction,
        lesson.get("created_at", ""),
    )
    return {
        "schema": "LessonPromotionCandidate.v1",
        "promotion_event_id": _stable_id("promotion_event", promotion_id, "created"),
        "promotion_id": promotion_id,
        "event": "candidate_created",
        "space_id": space,
        "lesson_id": lesson_id,
        "target_kind": target_kind,
        "state": "pending_review",
        "title": _promotion_title(lesson),
        "instruction_preview": instruction[:360],
        "target_path_suggestion": _promotion_path_suggestion(space, lesson, target_kind),
        "draft_markdown": _promotion_draft_markdown(space, lesson, target_kind),
        "source_lesson": {
            "kind": lesson.get("kind", "lesson"),
            "scope": lesson.get("scope", "space"),
            "status": lesson.get("status", ""),
            "evidence_level": lesson.get("evidence_level", ""),
            "confidence": lesson.get("confidence", 0),
            "application_level": lesson.get("application_level", ""),
            "must_apply": bool(lesson.get("must_apply")),
            "applies_when": lesson.get("applies_when") or {},
            "source_evaluation_id": lesson.get("source_evaluation_id", ""),
        },
        "reason": "lesson explicitly requested promotion_target",
        "risk_notes": [
            "전역 지식/스킬 파일은 자동 수정하지 않는다.",
            "승인 이후 별도 적용 단계에서 기존 리소스와 충돌을 검토해야 한다.",
        ],
        "created_by": actor,
        "created_at": now_iso(),
        "reviewed_by": "",
        "reviewed_at": "",
        "review_reason": "",
    }


def _promotion_view(row: dict) -> dict:
    return {
        "promotion_id": row.get("promotion_id", ""),
        "promotion_event_id": row.get("promotion_event_id", ""),
        "lesson_id": row.get("lesson_id", ""),
        "target_kind": row.get("target_kind", ""),
        "state": row.get("state", ""),
        "title": row.get("title", ""),
        "instruction_preview": row.get("instruction_preview", ""),
        "target_path_suggestion": row.get("target_path_suggestion", ""),
        "draft_markdown": row.get("draft_markdown", ""),
        "reason": row.get("reason", ""),
        "risk_notes": row.get("risk_notes") or [],
        "created_by": row.get("created_by", ""),
        "created_at": row.get("created_at", ""),
        "reviewed_by": row.get("reviewed_by", ""),
        "reviewed_at": row.get("reviewed_at", ""),
        "review_reason": row.get("review_reason", ""),
        "_row_index": row.get("_row_index", 0),
    }


GROWTH_GAP_OPEN_STATES = {
    "needs_review",
    "resource_gap_needs_triage",
    "resource_proposal_ready",
    "promotion_candidate_ready",
    "promotion_candidate_created",
    "resource_apply_blocked",
}

# 이 outcome 들은 punt 사유(no_lesson_reason)가 있어도 '검토 없이 닫으면 안 되는' 주의 결과다.
# 실증(레빗_bcd7): failed 5·rejected 2·partial 1 전부 "…requires_review" 류 사유 문자열과 함께
# no_change 로 태어나며 닫혀 성장루프가 공회전했다(94건 중 needs_review 0 — 도달 불가 상태였음).
# superseded 는 세대펜스가 설계대로 작동한 것(by-design)이라 제외한다.
GROWTH_REVIEW_OUTCOMES = {"failed", "rejected", "corrected", "partial"}


def _growth_gap_id(space: str, evaluation_id: str) -> str:
    return _stable_id("growth_gap", space, evaluation_id)


def _growth_gap_view(row: dict) -> dict:
    return {
        "gap_id": row.get("gap_id", ""),
        "gap_event_id": row.get("gap_event_id", ""),
        "event": row.get("event", ""),
        "state": row.get("state", ""),
        "evaluation_id": row.get("evaluation_id", ""),
        "outcome": row.get("outcome", ""),
        "source_event": row.get("source_event", ""),
        "target_kind": row.get("target_kind", ""),
        "promotion_id": row.get("promotion_id", ""),
        "lesson_ids": row.get("lesson_ids") or [],
        "recommended_next_action": row.get("recommended_next_action", ""),
        "reason": row.get("reason", ""),
        "goal_fit": row.get("goal_fit", ""),
        "created_at": row.get("created_at", ""),
        "_row_index": row.get("_row_index", 0),
    }


def _growth_gap_from_evaluation(space: str, evaluation: dict, lesson_candidate: dict | None) -> dict:
    lesson_candidate = lesson_candidate or {}
    evaluation_id = evaluation.get("evaluation_id", "")
    lesson_ids = [lesson_id for lesson_id in (evaluation.get("created_lesson_ids") or []) if lesson_id]
    target_kind = _promotion_target_kind(lesson_candidate) or "none"
    resource_change_needed = bool(evaluation.get("resource_change_needed"))
    lesson_needed = bool(evaluation.get("lesson_candidate_needed"))
    no_lesson_reason = str(evaluation.get("no_lesson_reason") or "")
    if lesson_ids and target_kind != "none":
        state = "promotion_candidate_ready"
        next_action = "scan_promotions"
        reason = "lesson_created_with_promotion_target"
    elif lesson_ids:
        state = "lesson_created"
        next_action = "use_lesson_in_next_context_pack"
        reason = "lesson_created"
    elif resource_change_needed and target_kind != "none":
        state = "resource_proposal_ready"
        next_action = "create_resource_proposal"
        reason = "resource_change_needed_with_target_kind"
    elif resource_change_needed:
        state = "resource_gap_needs_triage"
        next_action = "decide_skill_or_knowledge_or_no_change"
        reason = "resource_change_needed_without_target_kind"
    elif (
        str(evaluation.get("outcome") or "").strip().lower() in GROWTH_REVIEW_OUTCOMES
        and (not no_lesson_reason or "review" in no_lesson_reason.lower())
    ):
        # 종전엔 no_lesson_reason 분기가 먼저라, "…requires_review"라고 스스로 검토를 요구하는
        # punt 사유조차 no_change 로 태어나며 닫혔다(needs_review 도달 불가 — 성장루프 공회전).
        # 주의 outcome 에서 사유가 없거나 사유가 검토를 요구하면 열린 상태로 남겨 사회자/승격 경로가
        # 실제 성장(케이스·레슨·승격후보)으로 잇게 한다. 명시적 종결 사유(already_covered·fence_worked
        # 등)는 아래 no_change 분기로 정상 종결된다.
        state = "needs_review"
        next_action = "record_lesson_or_no_lesson_reason"
        reason = no_lesson_reason or "attention_outcome_requires_review"
    elif no_lesson_reason:
        state = "no_change"
        next_action = "none"
        reason = no_lesson_reason
    elif lesson_needed:
        state = "needs_review"
        next_action = "record_lesson_or_no_lesson_reason"
        reason = "attention_outcome_without_closed_disposition"
    else:
        state = "no_change"
        next_action = "none"
        reason = "no_failure_or_growth_gap"
    return {
        "schema": "GrowthGapDisposition.v1",
        "gap_id": _growth_gap_id(space, evaluation_id),
        "gap_event_id": _stable_id("growth_gap_event", space, evaluation_id, state, reason),
        "event": "evaluation_disposition",
        "space_id": space,
        "evaluation_id": evaluation_id,
        "outcome": evaluation.get("outcome", ""),
        "source_event": evaluation.get("source_event", ""),
        "state": state,
        "target_kind": target_kind,
        "promotion_id": "",
        "lesson_ids": lesson_ids,
        "lesson_candidate_needed": lesson_needed,
        "resource_change_needed": resource_change_needed,
        "recommended_next_action": next_action,
        "reason": reason,
        "goal_fit": "supports_learning_loop",
        "created_at": now_iso(),
        **_context_fields(evaluation),
    }


def _growth_gap_transition(
    space: str,
    *,
    evaluation_id: str,
    state: str,
    event: str,
    reason: str,
    promotion_id: str = "",
    target_kind: str = "",
    lesson_ids: list[str] | None = None,
) -> dict:
    return {
        "schema": "GrowthGapDisposition.v1",
        "gap_id": _growth_gap_id(space, evaluation_id),
        "gap_event_id": _stable_id("growth_gap_event", space, evaluation_id, state, event, promotion_id, reason),
        "event": event,
        "space_id": space,
        "evaluation_id": evaluation_id,
        "outcome": "",
        "source_event": "",
        "state": state,
        "target_kind": target_kind or "none",
        "promotion_id": promotion_id,
        "lesson_ids": lesson_ids or [],
        "lesson_candidate_needed": False,
        "resource_change_needed": False,
        "recommended_next_action": "none" if state in {"promotion_approved", "promotion_rejected", "promotion_applied"} else "review_promotion_candidate",
        "reason": reason,
        "goal_fit": "supports_learning_loop",
        "created_at": now_iso(),
    }


def _growth_gap_snapshot_from_rows(rows: list[dict]) -> dict:
    latest = list(_latest_by_id(rows, "gap_id").values())
    latest.sort(
        key=lambda row: (
            str(row.get("created_at") or ""),
            int(row.get("_row_index", 0)),
        ),
        reverse=True,
    )
    state_counts = {}
    target_counts = {}
    for row in latest:
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        target = row.get("target_kind", "unknown")
        target_counts[target] = target_counts.get(target, 0) + 1
    open_items = [row for row in latest if row.get("state") in GROWTH_GAP_OPEN_STATES]
    return {
        "growth_gap_event_count": len(rows),
        "growth_gap_count": len(latest),
        "growth_gap_open_count": len(open_items),
        "growth_gap_state_counts": state_counts,
        "growth_gap_target_counts": target_counts,
        "growth_gap_review_required": bool(open_items),
        "growth_gap_items": [_growth_gap_view(row) for row in latest[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
        "growth_gap_open_items": [_growth_gap_view(row) for row in open_items[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
    }


def _resource_name(value: str, fallback: str) -> str:
    base = str(value or fallback or "resource").strip()
    slug = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", base).strip("-_")
    return (slug or fallback or "resource")[:80]


def _frontmatter_value(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return json.dumps(text, ensure_ascii=False)


def _resource_description(promotion: dict) -> str:
    title = str(promotion.get("title") or "").strip()
    instruction = str(promotion.get("instruction_preview") or "").strip()
    target = str(promotion.get("target_kind") or "resource")
    desc = (
        f"{title or instruction[:60]} 관련 {target} 승격 리소스. "
        f"공간 레슨을 반복 작업에 반영하거나 참고할 때 사용한다. "
        f"'이 레슨 적용해', '관련 지식 찾아줘', '반복 실수 방지 스킬 찾아줘'. "
        f"핵심 용어: 성장, 레슨, 승격, {target}, {title}, {instruction[:120]}"
    )
    return re.sub(r"\s+", " ", desc).strip()[:500]


RESOURCE_GRADES = {"기본", "추가", "고급", "대외비"}


def _resolve_grade(promotion: dict) -> str:
    """승격물의 등급(=배포 경계)을 정한다. 명시 grade 우선, 없으면 민감도 기반 보수적 기본값.

    식별정보(개인/회사) 가능성 → 대외비(배포 제외). 그 외 → 추가(기존 동작 하위호환).
    """
    grade = str(promotion.get("grade") or "").strip()
    if not grade:
        sensitivity = str(promotion.get("sensitivity") or "").strip()
        grade = "대외비" if sensitivity == "confidential" else "추가"
    if grade not in RESOURCE_GRADES:
        raise LessonLedgerError(f"unknown grade: {grade} (기본|추가|고급|대외비)")
    return grade


def _target_resource_path(space: str, promotion: dict) -> Path:
    target_kind = str(promotion.get("target_kind") or "").strip()
    grade = _resolve_grade(promotion)
    suffix = hashlib.sha256(str(promotion.get("promotion_id") or "").encode("utf-8")).hexdigest()[:10]
    title = _resource_name(promotion.get("title", ""), promotion.get("lesson_id", "lesson"))
    folder = _resource_name(f"{space}-{suffix}-{title}", f"{space}-{suffix}")
    if target_kind == "skill":
        return ROOT / "스킬" / grade / folder / "SKILL.md"
    if target_kind == "knowledge":
        return ROOT / "지식" / grade / folder / "지식.md"
    raise LessonLedgerError("target_kind must be knowledge or skill")


def _resource_body(space: str, promotion: dict, target_path: Path, *, actor: str, reason: str) -> str:
    name = _resource_name(promotion.get("title", ""), promotion.get("lesson_id", "resource"))
    description = _resource_description(promotion)
    title = str(promotion.get("title") or name)
    instruction = str(promotion.get("instruction_preview") or "").strip()
    draft = str(promotion.get("draft_markdown") or "").strip()
    source = promotion.get("source_lesson") or {}
    front = (
        "---\n"
        f"name: {_frontmatter_value(name)}\n"
        f"description: {_frontmatter_value(description)}\n"
        "---\n"
    )
    audit = (
        f"\n## 승격 메타\n"
        f"- source_space: `{space}`\n"
        f"- source_promotion_id: `{promotion.get('promotion_id', '')}`\n"
        f"- source_lesson_id: `{promotion.get('lesson_id', '')}`\n"
        f"- applied_by: `{actor}`\n"
        f"- apply_reason: {reason or '승인된 성장 후보 적용'}\n"
        f"- source_evidence_level: `{source.get('evidence_level', '')}`\n"
        f"- target_path: `{target_path.relative_to(ROOT)}`\n"
    )
    if promotion.get("target_kind") == "skill":
        return (
            front
            + f"# {title}\n\n"
            + "## 언제 쓰나\n"
            + f"- {description}\n\n"
            + "## 절차\n"
            + f"1. 현재 작업/공간이 source lesson 조건과 맞는지 확인한다.\n"
            + f"2. 다음 지침을 작업 전 체크리스트로 적용한다: {instruction}\n"
            + "3. 적용 결과가 실패하거나 맞지 않으면 레슨 적용 보고에 not_applicable_reason 또는 개선 필요를 남긴다.\n"
            + ("\n## 원본 초안\n" + draft + "\n" if draft else "")
            + audit
        )
    return (
        front
        + f"# {title}\n\n"
        + "## 요약\n"
        + f"{instruction}\n\n"
        + "## 참고 기준\n"
        + "- 이 문서는 승인된 공간 레슨을 전역 지식 후보로 승격 적용한 것이다.\n"
        + "- 현재 상황과 맞지 않으면 무조건 적용하지 말고 source lesson 조건과 공간 지침을 함께 확인한다.\n"
        + ("\n## 원본 초안\n" + draft + "\n" if draft else "")
        + audit
    )


def _resource_apply_view(row: dict) -> dict:
    return {
        "apply_id": row.get("apply_id", ""),
        "apply_event_id": row.get("apply_event_id", ""),
        "promotion_id": row.get("promotion_id", ""),
        "lesson_id": row.get("lesson_id", ""),
        "target_kind": row.get("target_kind", ""),
        "state": row.get("state", ""),
        "target_path": row.get("target_path", ""),
        "actor": row.get("actor", ""),
        "reason": row.get("reason", ""),
        "created_at": row.get("created_at", ""),
        "detail": row.get("detail", ""),
        "_row_index": row.get("_row_index", 0),
    }


def _resource_apply_snapshot_from_rows(rows: list[dict]) -> dict:
    latest = list(_latest_by_id(rows, "apply_id").values())
    latest.sort(
        key=lambda row: (
            str(row.get("created_at") or ""),
            int(row.get("_row_index", 0)),
        ),
        reverse=True,
    )
    state_counts = {}
    by_promotion = {}
    for row in latest:
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        promotion_id = row.get("promotion_id", "")
        if promotion_id and promotion_id not in by_promotion:
            by_promotion[promotion_id] = _resource_apply_view(row)
    applied = [row for row in latest if row.get("state") in {"applied", "applied_existing"}]
    blocked = [row for row in latest if str(row.get("state") or "").startswith("blocked")]
    return {
        "resource_apply_count": len(latest),
        "resource_apply_applied_count": len(applied),
        "resource_apply_blocked_count": len(blocked),
        "resource_apply_state_counts": state_counts,
        "resource_apply_items": [_resource_apply_view(row) for row in latest[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
        "resource_apply_latest_by_promotion": by_promotion,
    }


def _attach_apply_info(promotion_snapshot: dict, apply_snapshot: dict) -> dict:
    by_promotion = apply_snapshot.get("resource_apply_latest_by_promotion") or {}

    def attach(item: dict) -> dict:
        out = dict(item)
        app = by_promotion.get(out.get("promotion_id", "")) or {}
        if app:
            out.update({
                "apply_state": app.get("state", ""),
                "applied_path": app.get("target_path", ""),
                "applied_at": app.get("created_at", ""),
                "applied_by": app.get("actor", ""),
                "apply_detail": app.get("detail", ""),
                "apply_id": app.get("apply_id", ""),
            })
        elif out.get("state") == "approved":
            out.update({
                "apply_state": "not_started",
                "applied_path": "",
                "applied_at": "",
                "applied_by": "",
                "apply_detail": "",
                "apply_id": "",
            })
        else:
            out.setdefault("apply_state", "")
        return out

    for key in ("promotion_items", "promotion_candidate_items", "promotion_pending_items"):
        promotion_snapshot[key] = [attach(item) for item in promotion_snapshot.get(key, [])]
    approved_items = [item for item in promotion_snapshot.get("promotion_items", []) if item.get("state") == "approved"]
    pending_apply = [
        item for item in approved_items
        if item.get("apply_state") in {"", "not_started"}
    ]
    blocked_apply = [
        item for item in promotion_snapshot.get("promotion_items", [])
        if str(item.get("apply_state") or "").startswith("blocked")
    ]
    promotion_snapshot["promotion_apply_pending_count"] = len(pending_apply)
    promotion_snapshot["promotion_apply_blocked_count"] = len(blocked_apply)
    promotion_snapshot["promotion_apply_pending_items"] = pending_apply[:MAX_PROMOTION_SNAPSHOT_ITEMS]
    promotion_snapshot["promotion_apply_blocked_items"] = blocked_apply[:MAX_PROMOTION_SNAPSHOT_ITEMS]
    return promotion_snapshot


def _promotion_snapshot_from_rows(rows: list[dict]) -> dict:
    latest = list(_latest_by_id(rows, "promotion_id").values())
    latest.sort(
        key=lambda row: (
            str(row.get("reviewed_at") or row.get("created_at") or ""),
            int(row.get("_row_index", 0)),
        ),
        reverse=True,
    )
    state_counts = {}
    target_counts = {}
    for row in latest:
        state = row.get("state", "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
        target = row.get("target_kind", "unknown")
        target_counts[target] = target_counts.get(target, 0) + 1
    pending = [row for row in latest if row.get("state") == "pending_review"]
    approved = [row for row in latest if row.get("state") == "approved"]
    rejected = [row for row in latest if row.get("state") == "rejected"]
    latest_row = latest[0] if latest else {}
    return {
        "promotion_candidate_event_count": len(rows),
        "promotion_candidate_count": len(latest),
        "promotion_state_counts": state_counts,
        "promotion_target_counts": target_counts,
        "promotion_pending_count": len(pending),
        "promotion_approved_count": len(approved),
        "promotion_rejected_count": len(rejected),
        "promotion_candidate_pending_count": len(pending),
        "promotion_candidate_state_counts": state_counts,
        "promotion_candidate_target_counts": target_counts,
        "promotion_review_required": bool(pending),
        "latest_promotion_id": latest_row.get("promotion_id", ""),
        "latest_promotion_candidate_id": latest_row.get("promotion_id", ""),
        "latest_promotion_state": latest_row.get("state", ""),
        "latest_promotion_target_kind": latest_row.get("target_kind", ""),
        "promotion_items": [_promotion_view(row) for row in latest[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
        "promotion_candidate_items": [_promotion_view(row) for row in latest[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
        "promotion_pending_items": [_promotion_view(row) for row in pending[:MAX_PROMOTION_SNAPSHOT_ITEMS]],
    }


def _search_text(event: str, context: dict | None) -> str:
    context = context or {}
    return " ".join([
        str(event or ""),
        str(context.get("source_message_id") or ""),
        str(context.get("intent_id") or ""),
        str(context.get("conversation_thread_id") or ""),
    ]).lower()


def _scope_matches(lesson: dict, space: str, target_agent: str) -> bool:
    scope = lesson.get("scope", "space")
    applies = lesson.get("applies_when") or {}
    lesson_space = applies.get("space_id") or ""
    if lesson_space and lesson_space != space:
        return False
    if scope in {"space", "intent", "task_type", "resource"}:
        return True
    if scope == "agent":
        agent_ids = applies.get("agents") or applies.get("agent_ids") or []
        return not agent_ids or target_agent in agent_ids
    return scope == "global"


def _mode_matches(lesson: dict, mode: str) -> bool:
    modes = (lesson.get("applies_when") or {}).get("agent_modes") or []
    return not modes or mode in modes or ("manager" in modes and mode == "manager")


def _keyword_matches(lesson: dict, text: str) -> bool:
    keywords = (lesson.get("applies_when") or {}).get("keywords") or []
    if not keywords:
        return True
    lowered = text.lower()
    return any(str(keyword).lower() in lowered for keyword in keywords)


def _lesson_priority(lesson: dict) -> tuple[int, float, str]:
    status = lesson.get("status", "")
    evidence = lesson.get("evidence_level", "")
    status_score = 2 if status == "active" else 1 if status == "candidate" else 0
    must_score = 3 if status == "active" and _is_must_apply(lesson) else 0
    evidence_score = 1 if evidence in {"user_directive", "verified_result", "reviewer_approval"} else 0
    try:
        confidence = float(lesson.get("confidence", 0))
    except Exception:
        confidence = 0.0
    return (must_score + status_score + evidence_score, confidence, str(lesson.get("created_at", "")))


def _lesson_view(lesson: dict, application_level: str) -> dict:
    return {
        "lesson_id": lesson.get("lesson_id", ""),
        "kind": lesson.get("kind", "lesson"),
        "scope": lesson.get("scope", "space"),
        "status": lesson.get("status", ""),
        "application_level": application_level,
        "must_apply": application_level == "must_apply" or _is_must_apply(lesson),
        "enforcement": lesson.get("enforcement", ""),
        "instruction": str(lesson.get("instruction") or "")[:MAX_LESSON_INSTRUCTION_CHARS],
        "applies_when": lesson.get("applies_when") or {},
        "evidence_level": lesson.get("evidence_level", ""),
        "confidence": lesson.get("confidence", 0),
        "registry_status": lesson.get("registry_status", "not_applicable_v0"),
    }


def _is_must_apply(lesson: dict) -> bool:
    applies = lesson.get("applies_when") or {}
    return bool(
        lesson.get("must_apply")
        or lesson.get("application_level") == "must_apply"
        or lesson.get("enforcement") == "must_apply"
        or applies.get("must_apply")
        or applies.get("application_level") == "must_apply"
    )


def build_lesson_pack(
    space: str,
    *,
    mode: str,
    context: dict | None,
    event: str = "",
    target_agent: str = "",
    max_lessons: int = 8,
    max_must_apply: int = 3,
) -> dict:
    lessons, error = _latest_lessons(space)
    if error:
        return {
            "schema": "LessonPack.v1",
            "lesson_pack_status": "unavailable",
            "active_space_lessons_checked": False,
            "agent_lessons_checked": False,
            "included_lessons": [],
            "active_space_lessons": [],
            "recent_correction_lessons": [],
            "must_apply": [],
            "may_apply": [],
            "reference_only": [],
            "excluded_lessons": [],
            "max_total_lessons": max_lessons,
            "max_must_apply": max_must_apply,
            "registry_status": "not_applicable_v0",
            "errors": [error],
        }
    text = _search_text(event, context)
    candidates = []
    excluded = []
    for lesson in lessons:
        lesson_id = lesson.get("lesson_id", "")
        status = lesson.get("status", "")
        if status not in {"active", "candidate"}:
            excluded.append({"lesson_id": lesson_id, "reason": f"status:{status or 'unknown'}"})
            continue
        if not _scope_matches(lesson, space, target_agent):
            excluded.append({"lesson_id": lesson_id, "reason": "scope_mismatch"})
            continue
        if not _mode_matches(lesson, mode):
            excluded.append({"lesson_id": lesson_id, "reason": "mode_mismatch"})
            continue
        if not _keyword_matches(lesson, text):
            excluded.append({"lesson_id": lesson_id, "reason": "keyword_mismatch"})
            continue
        candidates.append(lesson)
    candidates.sort(key=_lesson_priority, reverse=True)
    must_candidates = [lesson for lesson in candidates if lesson.get("status") == "active" and _is_must_apply(lesson)]
    must_raw = must_candidates[:max_must_apply]
    must_raw_ids = {lesson.get("lesson_id", "") for lesson in must_raw}
    must_overflow_ids = {lesson.get("lesson_id", "") for lesson in must_candidates[max_must_apply:]}
    for lesson in must_candidates[max_must_apply:]:
        excluded.append({"lesson_id": lesson.get("lesson_id", ""), "reason": "must_apply_budget"})
    included_raw = list(must_raw)
    for lesson in candidates:
        lesson_id = lesson.get("lesson_id", "")
        if lesson_id in must_raw_ids or lesson_id in must_overflow_ids:
            continue
        if len(included_raw) >= max_lessons:
            excluded.append({"lesson_id": lesson_id, "reason": "lesson_budget"})
            continue
        included_raw.append(lesson)
    must_apply = []
    may_apply = []
    reference_only = []
    active_space_lessons = []
    recent_correction_lessons = []
    for lesson in included_raw:
        if lesson.get("status") == "active":
            active_space_lessons.append(_lesson_view(lesson, "must_apply" if _is_must_apply(lesson) else "may_apply"))
        else:
            recent_correction_lessons.append(_lesson_view(lesson, "reference_only"))
        if lesson.get("status") == "active" and _is_must_apply(lesson):
            if len(must_apply) < max_must_apply:
                must_apply.append(_lesson_view(lesson, "must_apply"))
            else:
                excluded.append({"lesson_id": lesson.get("lesson_id", ""), "reason": "must_apply_budget"})
        elif lesson.get("status") == "active":
            may_apply.append(_lesson_view(lesson, "may_apply"))
        else:
            reference_only.append(_lesson_view(lesson, "reference_only"))
    included = [l.get("lesson_id", "") for l in [*must_apply, *may_apply, *reference_only] if l.get("lesson_id")]
    return {
        "schema": "LessonPack.v1",
        "lesson_pack_status": "ok",
        "active_space_lessons_checked": True,
        "agent_lessons_checked": True,
        "included_lessons": included,
        "active_space_lessons": active_space_lessons,
        "recent_correction_lessons": recent_correction_lessons,
        "must_apply": must_apply,
        "may_apply": may_apply,
        "reference_only": reference_only,
        "excluded_lessons": excluded[:20],
        "max_total_lessons": max_lessons,
        "max_must_apply": max_must_apply,
        "registry_status": "not_applicable_v0",
        "errors": [],
    }


def _with_lock(space: str, fn):
    _ensure(space)
    lock = _lock_path(space)
    lock.touch(exist_ok=True)
    with lock.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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


def _lesson_from_candidate(space: str, candidate: dict, evaluation: dict) -> dict:
    instruction = str(candidate.get("instruction") or "").strip()
    if not instruction:
        raise LessonLedgerError("lesson candidate instruction required")
    applies_when = {
        "space_id": space,
        "task_types": [],
        "agent_modes": [],
        "keywords": [],
        "resource_paths": [],
        **(candidate.get("applies_when") or {}),
    }
    evidence = candidate.get("evidence") or [{
        "type": candidate.get("evidence_type", "agent_observation"),
        "source_message_id": evaluation.get("source_message_id", ""),
        "source_event_seq": evaluation.get("source_event_seq"),
        "source_task_id": evaluation.get("task_id", ""),
        "source_release_id": evaluation.get("release_id", ""),
        "outcome": evaluation.get("outcome", ""),
        "source_quote": str(candidate.get("source_quote") or "")[:240],
    }]
    lesson_id = candidate.get("lesson_id") or _stable_id(
        "lesson",
        space,
        candidate.get("scope", "space"),
        candidate.get("kind", "lesson"),
        instruction,
    )
    return {
        "schema": "LessonLedger.v1",
        "lesson_id": lesson_id,
        "kind": candidate.get("kind", "lesson"),
        "scope": candidate.get("scope", "space"),
        "status": candidate.get("status", "candidate"),
        "promotion_target": candidate.get("promotion_target", "none"),
        "applies_when": applies_when,
        "does_not_apply_when": candidate.get("does_not_apply_when") or [],
        "instruction": instruction,
        "evidence": evidence,
        "evidence_level": candidate.get("evidence_level", "agent_observation"),
        "confidence": float(candidate.get("confidence", 0.5)),
        "conflicts_with": candidate.get("conflicts_with") or [],
        "supersedes": candidate.get("supersedes") or [],
        "review_due_at_utc": candidate.get("review_due_at_utc", ""),
        "stale_after_at_utc": candidate.get("stale_after_at_utc", ""),
        "valid_until_utc": candidate.get("valid_until_utc", ""),
        "last_used_at_utc": "",
        "last_verified_at_utc": "",
        "application_level": candidate.get(
            "application_level",
            "must_apply"
            if bool(candidate.get("must_apply") or candidate.get("enforcement") == "must_apply")
            else "may_apply",
        ),
        "must_apply": bool(
            candidate.get("must_apply")
            or candidate.get("application_level") == "must_apply"
            or candidate.get("enforcement") == "must_apply"
        ),
        "enforcement": candidate.get("enforcement", ""),
        "registry_status": "not_applicable_v0",
        "created_at": now_iso(),
        "source_evaluation_id": evaluation.get("evaluation_id", ""),
    }


def _validate_evaluation_contract(evaluation: dict, lesson_candidate: dict | None, no_lesson_reason: str):
    outcome = evaluation.get("outcome", "")
    lesson_needed = bool(evaluation.get("lesson_candidate_needed"))
    if outcome in ATTENTION_OUTCOMES and lesson_needed and not lesson_candidate and not no_lesson_reason:
        raise LessonLedgerError("attention outcome requires lesson_candidate or no_lesson_reason")


def record_post_interaction_evaluation(
    space: str,
    *,
    outcome: str,
    context: dict | None = None,
    source_event: str = "",
    actor: str = "",
    target: str = "",
    publish_effect_id: str = "",
    published_message_id: str = "",
    what_worked: list[str] | None = None,
    what_failed: list[str] | None = None,
    user_feedback_refs: list[str] | None = None,
    verification_refs: list[str] | None = None,
    lesson_candidate_needed: bool = False,
    resource_change_needed: bool = False,
    lesson_candidate: dict | None = None,
    no_lesson_reason: str = "",
) -> dict:
    context_fields = _context_fields(context)
    evaluation = {
        "schema": "PostInteractionEvaluation.v1",
        "evaluation_id": _stable_id(
            "eval_interaction",
            space,
            outcome,
            source_event,
            context_fields.get("intent_id", ""),
            context_fields.get("source_event_seq"),
            context_fields.get("source_message_id", ""),
            actor,
            target,
            publish_effect_id,
        ),
        "space_id": space,
        "task_id": "",
        "release_id": "",
        "outcome": outcome,
        "source_event": source_event,
        "actor": actor,
        "target": target,
        "publish_effect_id": publish_effect_id,
        "published_message_id": published_message_id,
        "what_worked": what_worked or [],
        "what_failed": what_failed or [],
        "user_feedback_refs": user_feedback_refs or [],
        "verification_refs": verification_refs or [],
        "lesson_candidate_needed": bool(lesson_candidate_needed),
        "resource_change_needed": bool(resource_change_needed),
        "created_lesson_ids": [],
        "no_lesson_reason": no_lesson_reason,
        "registry_status": "not_applicable_v0",
        "created_at": now_iso(),
        **context_fields,
    }
    _validate_evaluation_contract(evaluation, lesson_candidate, no_lesson_reason)

    def mutate():
        created_lessons = []
        if lesson_candidate:
            lesson = _lesson_from_candidate(space, lesson_candidate, evaluation)
            result = _append_unique(_lessons_path(space), lesson, "lesson_id")
            created_lessons.append(result["record"].get("lesson_id", ""))
        evaluation["created_lesson_ids"] = [lesson_id for lesson_id in created_lessons if lesson_id]
        result = _append_unique(_post_interaction_path(space), evaluation, "evaluation_id")
        gap = _growth_gap_from_evaluation(space, result.get("record") or evaluation, lesson_candidate)
        _append_unique(_growth_gaps_path(space), gap, "gap_id")
        return result

    return _with_lock(space, mutate)


def record_post_task_evaluation(
    space: str,
    *,
    task_id: str,
    outcome: str,
    context: dict | None = None,
    release_id: str = "",
    actor: str = "",
    task_title: str = "",
    result_summary: str = "",
    what_worked: list[str] | None = None,
    what_failed: list[str] | None = None,
    user_feedback_refs: list[str] | None = None,
    verification_refs: list[str] | None = None,
    lesson_candidate_needed: bool = False,
    resource_change_needed: bool = False,
    lesson_candidate: dict | None = None,
    no_lesson_reason: str = "",
) -> dict:
    if not task_id:
        raise LessonLedgerError("task_id required")
    context_fields = _context_fields(context)
    evaluation = {
        "schema": "PostTaskEvaluation.v1",
        "evaluation_id": _stable_id("eval_task", space, task_id, outcome, release_id),
        "space_id": space,
        "task_id": task_id,
        "release_id": release_id,
        "outcome": outcome,
        "actor": actor,
        "task_title": task_title,
        "result_summary": result_summary[:1000],
        "what_worked": what_worked or [],
        "what_failed": what_failed or [],
        "user_feedback_refs": user_feedback_refs or [],
        "verification_refs": verification_refs or [],
        "lesson_candidate_needed": bool(lesson_candidate_needed),
        "resource_change_needed": bool(resource_change_needed),
        "created_lesson_ids": [],
        "no_lesson_reason": no_lesson_reason,
        "registry_status": "not_applicable_v0",
        "created_at": now_iso(),
        **context_fields,
    }
    _validate_evaluation_contract(evaluation, lesson_candidate, no_lesson_reason)

    def mutate():
        created_lessons = []
        if lesson_candidate:
            lesson = _lesson_from_candidate(space, lesson_candidate, evaluation)
            result = _append_unique(_lessons_path(space), lesson, "lesson_id")
            created_lessons.append(result["record"].get("lesson_id", ""))
        evaluation["created_lesson_ids"] = [lesson_id for lesson_id in created_lessons if lesson_id]
        result = _append_unique(_post_task_path(space), evaluation, "evaluation_id")
        gap = _growth_gap_from_evaluation(space, result.get("record") or evaluation, lesson_candidate)
        _append_unique(_growth_gaps_path(space), gap, "gap_id")
        return result

    return _with_lock(space, mutate)


def record_lesson_application(
    space: str,
    *,
    lesson_id: str,
    pack_id: str,
    manifest_hash_seen: str,
    agent: str,
    mode: str,
    applied: bool,
    not_applicable_reason: str = "",
    how: str = "",
    outcome: str = "unclear",
    needs_lesson_update: bool = False,
) -> dict:
    if not lesson_id:
        raise LessonLedgerError("lesson_id required")
    if not pack_id:
        raise LessonLedgerError("pack_id required")
    row = {
        "schema": "LessonApplication.v1",
        "application_id": _stable_id(
            "lesson_app",
            space,
            lesson_id,
            pack_id,
            manifest_hash_seen,
            agent,
            mode,
            bool(applied),
            not_applicable_reason,
            how,
            outcome,
            bool(needs_lesson_update),
        ),
        "lesson_id": lesson_id,
        "pack_id": pack_id,
        "manifest_hash_seen": manifest_hash_seen,
        "agent": agent,
        "mode": mode,
        "applied": bool(applied),
        "not_applicable_reason": not_applicable_reason,
        "how": how,
        "outcome": outcome,
        "needs_lesson_update": bool(needs_lesson_update),
        "created_at": now_iso(),
    }

    def mutate():
        return _append_unique(_applications_path(space), row, "application_id")

    return _with_lock(space, mutate)


# needs_review gap 이 이 나이(일)를 넘도록 아무 성장으로 이어지지 않으면 자동 종결한다 —
# 열린 gap 이 무한 적체되면 사회자 프롬프트가 낡은 신호로 오염된다(최근 실패만 살아있게 유지).
GROWTH_GAP_STALE_DAYS = 7


def expire_stale_growth_gaps(space: str, *, max_age_days: int = GROWTH_GAP_STALE_DAYS) -> dict:
    """오래된 열린 growth gap 을 reviewed_stale_no_change 로 자동 종결한다(멱등·백스톱용)."""
    from datetime import datetime, timedelta

    def mutate():
        rows, error = _rows_with_error(_growth_gaps_path(space))
        if error:
            return {"ok": False, "error": error, "expired": 0}
        latest = _latest_by_id(rows, "gap_id")
        cutoff = datetime.now() - timedelta(days=max_age_days)
        expired = 0
        for gap in latest.values():
            if gap.get("state") not in GROWTH_GAP_OPEN_STATES:
                continue
            try:
                created = datetime.fromisoformat(str(gap.get("created_at") or ""))
            except Exception:
                continue
            if created.tzinfo is not None:
                created = created.replace(tzinfo=None)
            if created > cutoff:
                continue
            _append_unique(
                _growth_gaps_path(space),
                _growth_gap_transition(
                    space,
                    evaluation_id=str(gap.get("evaluation_id") or ""),
                    state="reviewed_stale_no_change",
                    event="growth_gap_expired",
                    reason=f"{max_age_days}일 초과 미처리 — 자동 종결(최근 실패 신호만 유지)",
                    promotion_id=str(gap.get("promotion_id") or ""),
                    target_kind=str(gap.get("target_kind") or "none"),
                    lesson_ids=gap.get("lesson_ids") or [],
                ),
                "gap_event_id",
            )
            expired += 1
        return {"ok": True, "expired": expired}

    try:
        return _with_lock(space, mutate)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "expired": 0}


def generate_promotion_candidates(space: str, *, actor: str = "공간관리", limit: int = 20) -> dict:
    """명시적으로 승격 대상이 된 레슨을 검토 후보 ledger에 올린다."""
    safe_limit = min(max(int(limit or 20), 1), 50)

    def mutate():
        lesson_rows, lessons_error = _rows_with_error(_lessons_path(space))
        promotion_rows, promotions_error = _rows_with_error(_promotion_candidates_path(space))
        errors = [error for error in (lessons_error, promotions_error) if error]
        if errors:
            raise LessonLedgerError("; ".join(errors))
        latest_lessons = list(_latest_by_id(lesson_rows, "lesson_id").values())
        latest_promotions = _latest_by_id(promotion_rows, "promotion_id")
        created = []
        skipped = []
        for lesson in latest_lessons:
            if len(created) >= safe_limit:
                break
            lesson_id = str(lesson.get("lesson_id") or "")
            status = lesson.get("status", "")
            target_kind = _promotion_target_kind(lesson)
            if status not in {"active", "candidate"} or not target_kind:
                continue
            candidate = _promotion_candidate_from_lesson(space, lesson, actor=actor)
            promotion_id = candidate["promotion_id"]
            if promotion_id in latest_promotions:
                skipped.append({"lesson_id": lesson_id, "promotion_id": promotion_id, "reason": "already_exists"})
                continue
            _append_jsonl(_promotion_candidates_path(space), candidate)
            source_evaluation_id = ((candidate.get("source_lesson") or {}).get("source_evaluation_id") or "")
            if source_evaluation_id:
                _append_unique(
                    _growth_gaps_path(space),
                    _growth_gap_transition(
                        space,
                        evaluation_id=source_evaluation_id,
                        state="promotion_candidate_created",
                        event="promotion_candidate_created",
                        reason="promotion candidate created from lesson",
                        promotion_id=promotion_id,
                        target_kind=target_kind,
                        lesson_ids=[lesson_id],
                    ),
                    "gap_event_id",
                )
            latest_promotions[promotion_id] = candidate
            created.append(candidate)
        return {
            "ok": True,
            "created_count": len(created),
            "duplicate_count": len(skipped),
            "candidates": [_promotion_view(row) for row in created],
            "skipped": skipped,
            "snapshot": _promotion_snapshot_from_rows(list(latest_promotions.values())),
        }

    return _with_lock(space, mutate)


def review_promotion_candidate(
    space: str,
    promotion_id: str,
    *,
    decision: str,
    actor: str = "대표",
    reason: str = "",
) -> dict:
    promotion_id = str(promotion_id or "").strip()
    if not promotion_id:
        raise LessonLedgerError("promotion_id required")
    state = {
        "approve": "approved",
        "approved": "approved",
        "reject": "rejected",
        "rejected": "rejected",
    }.get(str(decision or "").strip().lower())
    if not state:
        raise LessonLedgerError("decision must be approve or reject")

    def mutate():
        rows, error = _rows_with_error(_promotion_candidates_path(space))
        if error:
            raise LessonLedgerError(error)
        latest = _latest_by_id(rows, "promotion_id")
        current = latest.get(promotion_id)
        if not current:
            raise LessonLedgerError("promotion candidate not found")
        review_row = dict(current)
        review_row.update({
            "schema": "LessonPromotionCandidate.v1",
            "promotion_event_id": _stable_id(
                "promotion_review",
                space,
                promotion_id,
                current.get("state", ""),
                state,
                actor,
                reason,
            ),
            "event": "reviewed",
            "previous_state": current.get("state", ""),
            "state": state,
            "reviewed_by": actor,
            "reviewed_at": now_iso(),
            "review_reason": reason,
        })
        result = _append_unique(_promotion_candidates_path(space), review_row, "promotion_event_id")
        source_evaluation_id = ((current.get("source_lesson") or {}).get("source_evaluation_id") or "")
        if source_evaluation_id:
            _append_unique(
                _growth_gaps_path(space),
                _growth_gap_transition(
                    space,
                    evaluation_id=source_evaluation_id,
                    state=f"promotion_{state}",
                    event="promotion_reviewed",
                    reason=reason or f"promotion {state}",
                    promotion_id=promotion_id,
                    target_kind=current.get("target_kind", ""),
                    lesson_ids=[current.get("lesson_id", "")],
                ),
                "gap_event_id",
            )
        return {
            "ok": True,
            "duplicate": bool(result.get("duplicate")),
            "candidate": _promotion_view(result.get("record") or review_row),
        }

    return _with_lock(space, mutate)


def apply_promotion_candidate(
    space: str,
    promotion_id: str,
    *,
    actor: str = "대표",
    reason: str = "",
) -> dict:
    """승인된 promotion 후보를 실제 지식/스킬 리소스 파일로 적용한다.

    기존 파일은 덮어쓰지 않는다. 충돌하면 blocked row를 남기고 사용자가
    target path를 정리한 뒤 다시 판단하도록 한다.
    """
    promotion_id = str(promotion_id or "").strip()
    if not promotion_id:
        raise LessonLedgerError("promotion_id required")

    def mutate():
        promotion_rows, promotions_error = _rows_with_error(_promotion_candidates_path(space))
        apply_rows, apply_error = _rows_with_error(_resource_applications_path(space))
        errors = [error for error in (promotions_error, apply_error) if error]
        if errors:
            raise LessonLedgerError("; ".join(errors))
        promotions = _latest_by_id(promotion_rows, "promotion_id")
        promotion = promotions.get(promotion_id)
        if not promotion:
            raise LessonLedgerError("promotion candidate not found")
        if promotion.get("state") != "approved":
            raise LessonLedgerError("promotion candidate must be approved before apply")
        target_path = _target_resource_path(space, promotion)
        rel_target = str(target_path.relative_to(ROOT))
        apply_id = _stable_id("resource_apply", space, promotion_id, rel_target)
        existing_apply = _latest_by_id(apply_rows, "apply_id").get(apply_id)
        if existing_apply and existing_apply.get("state") in {"applied", "applied_existing"}:
            return {"ok": True, "duplicate": True, "application": _resource_apply_view(existing_apply)}
        content = _resource_body(space, promotion, target_path, actor=actor, reason=reason)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        state = "applied"
        detail = ""
        if target_path.exists():
            try:
                existing_content = target_path.read_text(encoding="utf-8")
            except Exception as exc:
                state = "blocked_path_read_error"
                detail = f"{type(exc).__name__}: {str(exc)[:180]}"
            else:
                if hashlib.sha256(existing_content.encode("utf-8")).hexdigest() == content_hash:
                    state = "applied_existing"
                    detail = "target file already contains identical content"
                else:
                    state = "blocked_path_exists"
                    detail = "target file already exists with different content"
        if state == "applied":
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        row = {
            "schema": "ResourceApplyEvent.v1",
            "apply_id": apply_id,
            "apply_event_id": _stable_id("resource_apply_event", apply_id, state, actor, reason, content_hash),
            "event": "resource_apply",
            "space_id": space,
            "promotion_id": promotion_id,
            "lesson_id": promotion.get("lesson_id", ""),
            "target_kind": promotion.get("target_kind", ""),
            "state": state,
            "target_path": rel_target,
            "content_hash": content_hash,
            "actor": actor,
            "reason": reason,
            "detail": detail,
            "created_at": now_iso(),
        }
        result = _append_unique(_resource_applications_path(space), row, "apply_event_id")
        source_evaluation_id = ((promotion.get("source_lesson") or {}).get("source_evaluation_id") or "")
        if source_evaluation_id:
            transition_state = "promotion_applied" if state in {"applied", "applied_existing"} else "resource_apply_blocked"
            _append_unique(
                _growth_gaps_path(space),
                _growth_gap_transition(
                    space,
                    evaluation_id=source_evaluation_id,
                    state=transition_state,
                    event="resource_apply",
                    reason=detail or reason or state,
                    promotion_id=promotion_id,
                    target_kind=promotion.get("target_kind", ""),
                    lesson_ids=[promotion.get("lesson_id", "")],
                ),
                "gap_event_id",
            )
        return {
            "ok": state in {"applied", "applied_existing"},
            "duplicate": bool(result.get("duplicate")),
            "application": _resource_apply_view(result.get("record") or row),
        }

    return _with_lock(space, mutate)


def _trailing_json_object(text: str) -> tuple[dict, str, bool]:
    raw = str(text or "")
    stripped = raw.rstrip()
    if not stripped:
        return {}, raw, False
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```\s*$", stripped, re.DOTALL | re.IGNORECASE)
    decoder = json.JSONDecoder()
    if fence:
        payload = fence.group(1).strip()
        try:
            obj, end = decoder.raw_decode(payload)
        except Exception:
            obj, end = {}, -1
        if isinstance(obj, dict) and end == len(payload):
            return obj, stripped[:fence.start()].rstrip(), True
    starts = [idx for idx, ch in enumerate(stripped) if ch == "{"]
    for start in reversed(starts):
        try:
            obj, end = decoder.raw_decode(stripped[start:])
        except Exception:
            continue
        if isinstance(obj, dict) and start + end == len(stripped):
            return obj, stripped[:start].rstrip(), True
    return {}, raw, False


def _lesson_ids(items) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            lesson_id = str(item.get("lesson_id") or "").strip()
        else:
            lesson_id = str(item or "").strip()
        if lesson_id:
            out.append(lesson_id)
    return out


def audit_reply_lesson_applications(
    space: str,
    *,
    content: str,
    context_pack: dict | None,
    agent: str,
    mode: str,
) -> dict:
    pack = context_pack or {}
    lesson_pack = pack.get("lesson_pack") or {}
    if lesson_pack.get("lesson_pack_status") == "unavailable":
        errors = lesson_pack.get("errors") or []
        detail = "; ".join(str(error) for error in errors)[:240]
        raise LessonLedgerError(f"lesson_pack_unavailable_hold: {detail or 'lesson pack unavailable'}")
    must_ids = _lesson_ids(lesson_pack.get("must_apply"))
    obj, clean_content, stripped = _trailing_json_object(content)
    report_found = obj.get("schema") == "LessonApplicationReport.v1"
    applications = obj.get("applications") if report_found else []
    if report_found and not isinstance(applications, list):
        raise LessonLedgerError("lesson_application_report_invalid: applications must be list")
    application_rows = []
    seen = {}
    parsed_apps = []
    for item in applications or []:
        if not isinstance(item, dict):
            continue
        lesson_id = str(item.get("lesson_id") or "").strip()
        if not lesson_id:
            continue
        applied = bool(item.get("applied"))
        app = {
            "lesson_id": lesson_id,
            "applied": applied,
            "not_applicable_reason": str(item.get("not_applicable_reason") or "").strip(),
            "how": str(item.get("how") or "")[:1000],
            "outcome": str(item.get("outcome") or "unclear"),
            "needs_lesson_update": bool(item.get("needs_lesson_update")),
        }
        parsed_apps.append(app)
        seen[lesson_id] = app

    missing = [lesson_id for lesson_id in must_ids if lesson_id not in seen]
    no_disposition = [
        lesson_id for lesson_id in must_ids
        if lesson_id in seen
        and not seen[lesson_id].get("applied")
        and not seen[lesson_id].get("not_applicable_reason")
    ]
    if missing or no_disposition:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if no_disposition:
            detail.append("not_applicable_reason_required=" + ",".join(no_disposition))
        raise LessonLedgerError("lesson_must_apply_without_application: " + "; ".join(detail))
    for app in parsed_apps:
        try:
            result = record_lesson_application(
                space,
                lesson_id=app["lesson_id"],
                pack_id=pack.get("context_pack_id", ""),
                manifest_hash_seen=pack.get("context_pack_checksum", ""),
                agent=agent,
                mode=mode,
                applied=app["applied"],
                not_applicable_reason=app["not_applicable_reason"],
                how=app["how"],
                outcome=app["outcome"],
                needs_lesson_update=app["needs_lesson_update"],
            )
        except Exception as exc:
            raise LessonLedgerError(f"lesson_application_record_failed: {type(exc).__name__}: {exc}") from exc
        application_rows.append(result.get("record") or {})

    # 스킬 케이스 자기보고 → 이벤트 기록(C3). **block 아님 record만**(크로스체크 권고, P1):
    # 에이전트가 적용한 케이스의 worked/harmful를 모아 수렴(C2)에 반영. 실패해도 발행을 막지 않는다.
    # 자원락(record_case_event)은 공간락(record_lesson_application)을 요청하지 않으므로 락 순환 없음.
    case_application_rows = []
    raw_case_apps = obj.get("case_applications")
    if isinstance(raw_case_apps, list):
        from . import case_ledger
        for item in raw_case_apps:
            if not isinstance(item, dict):
                continue
            skill = str(item.get("skill") or "").strip()
            case_id = str(item.get("case_id") or "").strip()
            if not skill or not case_id:
                continue
            outcome = str(item.get("outcome") or "").strip().lower()
            if outcome in ("harmful", "failed", "bad"):
                event = "harmful"
            elif outcome in ("worked", "good", "success"):
                event = "worked"
            else:
                event = "applied"
            try:
                case_ledger.record_case_event(skill, case_id, event, by=agent, skill_id=skill, rationale=str(item.get("how") or "")[:300])
                case_application_rows.append({"skill": skill, "case_id": case_id, "event": event})
                # 자동 승격(대표 손 없이): worked가 누적돼 §9.1 게이트(신뢰 가능한 '독립' 확인자 ≥ 임계,
                # harmful 0, conflict 아님)를 충족하면 candidate→active로 자동 수렴. 미충족이면 조용히 통과.
                # 후보 케이스는 어차피 기본 사용되므로, 이 승격은 '우선순위 상향'일 뿐 — 안전(사이코펀시/자기confirm 차단).
                if event == "worked":
                    try:
                        case_ledger.promote_case(skill, case_id, by="system_auto",
                                                 rationale="worked 수렴 자동 승격(독립확인자 게이트)",
                                                 method="worked_threshold")
                    except Exception:
                        pass   # 아직 수렴 안 됨/모순 격리 등 — 정상(다음 worked에서 재시도)
            except Exception:
                continue   # 점진 도입 fail-safe: 케이스 기록 실패가 발행을 막지 않는다

    return {
        "content": clean_content if report_found and stripped else str(content or ""),
        "report_found": report_found,
        "must_apply": must_ids,
        "applications": application_rows,
        "case_applications": case_application_rows,
    }


def snapshot(space: str) -> dict:
    lessons, lessons_error = _rows_with_error(_lessons_path(space))
    applications, applications_error = _rows_with_error(_applications_path(space))
    interactions, interactions_error = _rows_with_error(_post_interaction_path(space))
    tasks, tasks_error = _rows_with_error(_post_task_path(space))
    promotions, promotions_error = _rows_with_error(_promotion_candidates_path(space))
    growth_gaps, growth_gaps_error = _rows_with_error(_growth_gaps_path(space))
    resource_applications, resource_applications_error = _rows_with_error(_resource_applications_path(space))
    lesson_status_counts = {}
    lesson_kind_counts = {}
    for row in lessons:
        lesson_status_counts[row.get("status", "unknown")] = lesson_status_counts.get(row.get("status", "unknown"), 0) + 1
        lesson_kind_counts[row.get("kind", "unknown")] = lesson_kind_counts.get(row.get("kind", "unknown"), 0) + 1
    evaluation_outcomes = {}
    for row in [*interactions, *tasks]:
        outcome = row.get("outcome", "unknown")
        evaluation_outcomes[outcome] = evaluation_outcomes.get(outcome, 0) + 1
    promotion_snapshot = _promotion_snapshot_from_rows(promotions)
    growth_gap_snapshot = _growth_gap_snapshot_from_rows(growth_gaps)
    resource_apply_snapshot = _resource_apply_snapshot_from_rows(resource_applications)
    promotion_snapshot = _attach_apply_info(promotion_snapshot, resource_apply_snapshot)
    errors = [
        e for e in (
            lessons_error,
            applications_error,
            interactions_error,
            tasks_error,
            promotions_error,
            growth_gaps_error,
            resource_applications_error,
        ) if e
    ]
    latest_eval = ([*interactions, *tasks] or [{}])[-1]
    return {
        "lesson_count": len(lessons),
        "lesson_status_counts": lesson_status_counts,
        "lesson_kind_counts": lesson_kind_counts,
        "lesson_application_count": len(applications),
        "post_interaction_evaluation_count": len(interactions),
        "post_task_evaluation_count": len(tasks),
        "evaluation_outcomes": evaluation_outcomes,
        "latest_evaluation_id": latest_eval.get("evaluation_id", ""),
        "latest_outcome": latest_eval.get("outcome", ""),
        **promotion_snapshot,
        **growth_gap_snapshot,
        **resource_apply_snapshot,
        "ledger_corrupt": bool(errors),
        "ledger_errors": errors,
    }
