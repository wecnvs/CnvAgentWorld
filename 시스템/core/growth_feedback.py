# -*- coding: utf-8 -*-
"""부정 피드백 루프 — "아니야 다시해"가 실제로 스킬을 고치게 (성장루프 P2', 섀도 우선).

왜 (전면 분석 2026-07-02):
- 지금 시스템은 worked만 받고 harmful은 0건. 대표 교정("아니야 다시해")이 *직전에 쓰인 케이스*에
  부정 신호를 보내는 경로가 없었다 → 잘못 배운 케이스가 안 죽는다. 이 모듈이 그 입구다.
- 데이터 기반은 injection_log(P1'): "어느 턴/작업에 어떤 케이스가 노출됐나"를 조회해 원인을 좁힌다.

안전 (v2.1 원칙 — "대표 피드백도 틀릴 수 있다 / 개선이 새 문제를 만들면 안 된다"):
- **이건 시스템이 원인을 *추측*하는 부분이다.** 그래서 기본은 **섀도 모드**: "이 케이스에 harmful을 찍고/강등하겠다"를
  판단·기록만 하고 **실제로 실행하지 않는다.** 관측 기간에 오귀속률을 보고, 신뢰되면 플래그로 실행을 켠다.
- 실행 스위치(둘 다 기본 꺼짐): 환경변수
    CNV_GROWTH_HARMFUL_ACTIVE=1  → harmful 이벤트를 실제 기록
    CNV_GROWTH_DEMOTE_ACTIVE=1   → harmful 누적 임계 도달 시 실제 강등(demote)
- harmful은 '깃발(신호)'이지 즉시 강등이 아니다(설계 P1). 강등은 worked 대비 비율·누적 임계로만.
- 기계 살포 금지: 재작업이 났다고 직전 케이스 전부에 뿌리지 않는다 — **교정된 그 스킬의**, 최근 실제
  노출된(preview) 케이스로 좁힌다(avoid로 노출된 '하지마라' 케이스는 원인이 아니므로 제외).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import case_ledger, injection_log
from .paths import ROOT
from .transcript import now_iso

SPACES = ROOT / "공간"
_SHADOW_NAME = "growth_shadow.jsonl"

# harmful 누적 강등 임계(운영 데이터로 P3에서 튜닝). worked보다 harmful이 많고 최소 2건 이상일 때만.
DEMOTE_MIN_HARMFUL = 2
RECENT_INJECTION_LOOKBACK = 15


def _harmful_active() -> bool:
    return os.environ.get("CNV_GROWTH_HARMFUL_ACTIVE", "") == "1"


def _demote_active() -> bool:
    return os.environ.get("CNV_GROWTH_DEMOTE_ACTIVE", "") == "1"


def _shadow_log(space: str, rec: dict) -> None:
    """섀도(또는 실행) 판단을 공간 로그에 남긴다(관측·감사용, best-effort)."""
    try:
        rec = {"schema": "GrowthShadow.v1", "at_utc": now_iso(), **rec}
        path = SPACES / space / _SHADOW_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_shadow(space: str, *, limit: int = 50) -> list[dict]:
    path = SPACES / space / _SHADOW_NAME
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-limit:][::-1]


def _recent_suspects(space: str, skill: str, *, exclude_case_id: str = "") -> list[str]:
    """교정된 스킬의, 최근 실제 노출된(preview) 케이스 case_id — 부정 신호의 원인 후보."""
    suspects: list[str] = []
    seen = set()
    for rec in injection_log.recent_injections(space, limit=RECENT_INJECTION_LOOKBACK):
        for c in rec.get("cases", []) or []:
            if c.get("skill") != skill:
                continue
            if c.get("kind") != "preview":       # avoid('하지마라')는 원인이 아님
                continue
            cid = c.get("case_id", "")
            if not cid or cid == exclude_case_id or cid in seen:
                continue
            seen.add(cid)
            suspects.append(cid)
    return suspects


def on_skill_correction(space: str, skill: str, *, corrected_by: str = "대표",
                        new_case_id: str = "", rationale: str = "") -> dict:
    """대표가 어떤 스킬의 결과를 교정했을 때 호출 — 그 스킬의 최근 노출 케이스에 harmful을 (섀도)귀속.

    반환: {"suspects":[...], "harmful_applied":[...], "shadow":bool, "demotions":[...]}.
    실행 여부는 CNV_GROWTH_* 플래그. 기본(섀도)은 판단만 로그.
    """
    sdir = case_ledger.skill_dir(skill)
    result = {"skill": skill, "suspects": [], "harmful_applied": [], "demotions": [],
              "shadow": not _harmful_active()}
    if not sdir:
        return result
    suspects = _recent_suspects(space, skill, exclude_case_id=new_case_id)
    result["suspects"] = suspects
    if not suspects:
        _shadow_log(space, {"kind": "correction_no_suspect", "skill": skill,
                            "note": "교정 발생했으나 최근 노출된 이 스킬 케이스 없음", "rationale": rationale})
        return result
    for cid in suspects:
        if _harmful_active():
            try:
                case_ledger.record_case_event(sdir, cid, "harmful", by=corrected_by,
                                              rationale=f"대표 교정 역추적: {rationale}"[:240])
                result["harmful_applied"].append(cid)
                _shadow_log(space, {"kind": "harmful_recorded", "skill": skill, "case_id": cid,
                                    "rationale": rationale, "shadow": False})
            except Exception as exc:
                _shadow_log(space, {"kind": "harmful_error", "skill": skill, "case_id": cid,
                                    "error": str(exc)[:160]})
        else:
            _shadow_log(space, {"kind": "would_flag_harmful", "skill": skill, "case_id": cid,
                                "rationale": rationale, "shadow": True,
                                "note": "섀도: CNV_GROWTH_HARMFUL_ACTIVE=1이면 실제 harmful 기록"})
        demo = maybe_auto_demote(space, skill, cid, corrected_by=corrected_by)
        if demo.get("acted") or demo.get("would_demote"):
            result["demotions"].append(demo)
    return result


def maybe_auto_demote(space: str, skill: str, case_id: str, *, corrected_by: str = "시스템") -> dict:
    """harmful 누적이 임계를 넘고 worked보다 많으면 (섀도)강등 판단. active일 때만 실제 강등 대상.

    섀도(기본)면 would_demote만 로그. CNV_GROWTH_DEMOTE_ACTIVE=1이면 실제 demote_case.
    """
    sdir = case_ledger.skill_dir(skill)
    out = {"skill": skill, "case_id": case_id, "would_demote": False, "acted": False}
    if not sdir:
        return out
    try:
        worked, harmful = case_ledger.worked_harmful_counts(sdir, case_id)
    except Exception:
        return out
    # harmful이 임계 이상이고 worked보다 많을 때만(단발 harmful로 성급히 죽이지 않는다).
    if harmful < DEMOTE_MIN_HARMFUL or harmful <= worked:
        return out
    out["worked"] = worked
    out["harmful"] = harmful
    if _demote_active():
        try:
            case_ledger.demote_case(sdir, case_id, by=corrected_by,
                                    rationale=f"harmful 누적 {harmful} > worked {worked} (자동 강등)")
            out["acted"] = True
            _shadow_log(space, {"kind": "demoted", "skill": skill, "case_id": case_id,
                                "worked": worked, "harmful": harmful, "shadow": False})
        except Exception as exc:
            _shadow_log(space, {"kind": "demote_error", "skill": skill, "case_id": case_id,
                                "error": str(exc)[:160]})
    else:
        out["would_demote"] = True
        _shadow_log(space, {"kind": "would_demote", "skill": skill, "case_id": case_id,
                            "worked": worked, "harmful": harmful, "shadow": True,
                            "note": "섀도: CNV_GROWTH_DEMOTE_ACTIVE=1이면 실제 강등"})
    return out
