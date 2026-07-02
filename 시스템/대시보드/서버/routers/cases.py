# -*- coding: utf-8 -*-
"""스킬 케이스 API — core.case_ledger 위의 얇은 HTTP 껍데기 (P-wire-C1').

대표가 대시보드에서 직접(진짜 대표 세션) candidate 케이스를 검토·승인·강등·retire 한다.
매니저 tick은 candidate만 발의하고, active/must 확정은 여기(실제 대표 인증)에서만 일어난다.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import case_ledger, skill_smith

router = APIRouter(prefix="/api/skills", tags=["skills"])


class CaseAction(BaseModel):
    by: str = "대표"
    rationale: str = ""
    method: str | None = None   # promote 전용: 기본 owner_approval(대표 직접 승인)


class CaseOutcome(BaseModel):
    event: str                  # worked | harmful | applied
    by: str = "대표"
    rationale: str = ""


class CaseBranch(BaseModel):
    """모순 격리(conflict)를 조건 좁혀 분기 해소. applies_when/does_not_apply_when 중 최소 1개 필요."""
    applies_when: dict | None = None
    does_not_apply_when: list | None = None
    restore_to: str | None = None     # 미지정 시 격리 전 상태(pre_conflict_status)
    by: str = "대표"
    rationale: str = ""


def _sdir(skill: str):
    sdir = case_ledger.skill_dir(skill)
    if not sdir:
        raise HTTPException(status_code=404, detail=f"스킬 없음: {skill}")
    return sdir


@router.get("")
def list_skills():
    """전 스킬 + 파생 성숙도(대표가 어떤 스킬에 케이스가 쌓였는지 본다)."""
    return skill_smith.list_skills()


@router.get("/review")
def review_all():
    """전 스킬 통합 검토큐 — 격리된 모순(conflict)·harmful·검토기한을 대표가 한눈에 본다.

    §9.1 자동 격리가 만든 conflict는 사람이 해소해야 비로소 안전 루프가 닫힌다.
    이 엔드포인트가 그 '보이게 함'을 담당한다(스킬별로 흩어진 review_queue를 한 곳에 모음).
    """
    skills_out = []
    total_conflicts = 0
    total_review = 0
    for s in skill_smith.list_skills():
        name = s.get("name", "")
        sdir = case_ledger.skill_dir(name)
        if not sdir:
            continue
        try:
            rq = case_ledger.review_queue(sdir)
        except case_ledger.CaseLedgerError:
            continue
        if not rq:
            continue
        conflicts = [x for x in rq if x.get("status") == "conflict"]
        total_conflicts += len(conflicts)
        total_review += len(rq)
        skills_out.append({
            "skill": name,
            "conflicts": len(conflicts),
            "review": len(rq),
            "items": rq,
        })
    skills_out.sort(key=lambda x: (x["conflicts"], x["review"]), reverse=True)
    return {"total_conflicts": total_conflicts, "total_review": total_review, "skills": skills_out}


@router.get("/{skill}")
def skill_detail(skill: str):
    """선택한 스킬의 frontmatter 설명과 SKILL.md 원문을 대시보드에 제공한다."""
    try:
        return skill_smith.skill_detail(skill)
    except skill_smith.SkillSmithError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{skill}/cases")
def list_cases(skill: str):
    sdir = _sdir(skill)
    cases = [c for c in case_ledger.read_cases(sdir) if c.get("status") not in case_ledger.DEAD_STATUSES]
    return {
        "skill": skill,
        "maturity": case_ledger.maturity(sdir),
        "review_queue": case_ledger.review_queue(sdir),
        "convergence": case_ledger.case_convergence(sdir),
        "evaluator_reliability": case_ledger.evaluator_reliability(sdir),
        "cases": cases,
    }


@router.post("/{skill}/cases/{case_id}/event")
def case_event(skill: str, case_id: str, body: CaseOutcome):
    """케이스 적용 결과 기록(대표가 '이거 잘됐다/안됐다' 마킹). worked/harmful → 수렴·검토큐에 반영.

    P1: 이벤트 기록은 신호일 뿐, 상태 전이(active 확정/강등)는 promote/demote(판단·승인)로만 일어난다.
    """
    _sdir(skill)
    try:
        case_ledger.record_case_event(skill, case_id, body.event, by=body.by, rationale=body.rationale)
        return {"ok": True}
    except case_ledger.CaseLedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{skill}/cases/{case_id}/promote")
def promote(skill: str, case_id: str, body: CaseAction):
    _sdir(skill)
    try:
        return case_ledger.promote_case(
            skill, case_id, by=body.by,
            rationale=body.rationale or "대표 승인",
            method=body.method or "owner_approval",
        )
    except case_ledger.CaseLedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{skill}/cases/{case_id}/demote")
def demote(skill: str, case_id: str, body: CaseAction):
    _sdir(skill)
    try:
        return case_ledger.demote_case(skill, case_id, by=body.by, rationale=body.rationale or "대표 강등")
    except case_ledger.CaseLedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{skill}/cases/{case_id}/retire")
def retire(skill: str, case_id: str, body: CaseAction):
    _sdir(skill)
    try:
        return case_ledger.retire_case(skill, case_id, by=body.by, rationale=body.rationale or "대표 폐기")
    except case_ledger.CaseLedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{skill}/cases/{case_id}/branch")
def branch(skill: str, case_id: str, body: CaseBranch):
    """conflict 케이스를 조건 좁혀 분기 해소(more-specific-wins). 병합·삭제 아님(설계 §3)."""
    _sdir(skill)
    resolution = {"case_id": case_id}
    if body.applies_when is not None:
        resolution["applies_when"] = body.applies_when
    if body.does_not_apply_when is not None:
        resolution["does_not_apply_when"] = body.does_not_apply_when
    if body.restore_to:
        resolution["restore_to"] = body.restore_to
    try:
        out = case_ledger.branch_conflict(skill, [resolution], by=body.by, rationale=body.rationale or "대표 분기 해소")
        return out[0] if out else {"ok": True}
    except case_ledger.CaseLedgerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
