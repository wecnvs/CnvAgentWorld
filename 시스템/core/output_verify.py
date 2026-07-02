# -*- coding: utf-8 -*-
"""산출물 객관 검증 + 이종 검증자 선택 — 에이전트 간 견제·보완 (P4).

왜 (전면 분석 2026-07-02):
- finalize의 verification이 항상 not_run이라 "거짓 성공"(산출물 미변경인데 done, 6/30 실증)이 안 걸러졌다.
- 문헌(MAST 2503.13657): 검증 실패의 13.5%는 검증자가 도장만 찍는 것 → **객관 검증 우선**, 그다음 검증자는
  실제 차단권 + 이종 모델. 동종 모델 상호검토는 비싼 self-consistency(2502.08788). 그래서:
  ① 먼저 기계적·결정론 객관 검증(산출물 실재·주장 일치)을 finalize에서 무조건 돌린다.
  ② 의심/고위험이면 **이종 엔진** 검토자(검토가)를 붙이도록 신호(reviewer_engine)를 만든다.

이 모듈은 순수/결정론(엔진 호출 없음)이라 테스트 가능. 실제 검토자 디스패치·차단은 상위(사회자/reflow)가
이 신호를 받아 수행한다.
"""
from __future__ import annotations

import re
from pathlib import Path

# 작업 폴더에 시스템이 깔아두는 '비산출물'(뼈대) — 이걸 뺀 나머지가 있으면 실제 산출물로 본다.
# 정본은 engine._WORK_SCAFFOLD_FILES(진행 감지용과 동일 기준). 여기서 지연 import로 재사용해 목록 드리프트를
# 막고(크로스체크 지적: discovery_manifest.json 누락으로 산출물 검사 무력화), engine 목록에 없는 산출물성 뼈대만 보탠다.
_EXTRA_SCAFFOLD = {"결과.md", "release_request.json", "레슨적용보고.json", "role.md", "취소요청.json"}
# 시스템 부산물(로그·폴링·인코딩 캐시) — 산출물 아님. 확장자/접미 기준.
_BYPRODUCT_SUFFIXES = (".log", ".b64", ".lock")
_BYPRODUCT_NAMES = {"자동재개.json", "미획득목록.json", "download_progress.jsonl", "lock_poll.log"}
_SCAFFOLD_DIRS = {"steering", "__pycache__", ".history", ".preview"}


def _scaffold_files() -> set:
    """뼈대 파일 집합 = engine 정본 ∪ 산출물성 추가 뼈대. engine import 실패 시 정적 폴백."""
    try:
        from . import engine
        base = set(getattr(engine, "_WORK_SCAFFOLD_FILES", set()))
    except Exception:
        base = {"task_pack.json", "task_handoff_pack.json", "runtime_capabilities.json",
                "execution_strategy.json", "발견후보.md", "지시.md", "discovery_manifest.json",
                "CLAUDE.md", "AGENTS.md", "GEMINI.md", "GEMMA.md", "agent_runtime.json",
                "work_status.json", "상태.json", "작업실행설정.json"}
    return base | _EXTRA_SCAFFOLD
# 결과.md가 '뼈대 TODO만'인지 판정 — 완료 마커/체크포인트 실내용이 있으면 실질 있음.
_DONE_MARKERS = ("✅", "[x]", "완료", "성공", "확인", "결과:", "산출")


def _artifacts(work_dir: Path) -> list[str]:
    """작업 폴더의 실제 산출물(뼈대·잠금·로그·숨김 제외)."""
    out = []
    scaffold = _scaffold_files()
    try:
        for p in work_dir.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(work_dir)
            if any(part in _SCAFFOLD_DIRS for part in rel.parts):
                continue
            if (p.name in scaffold or p.name in _BYPRODUCT_NAMES
                    or p.name.startswith(".") or p.suffix in _BYPRODUCT_SUFFIXES):
                continue
            out.append(str(rel))
    except Exception:
        pass
    return out


def _result_substantive(result: str) -> bool:
    """결과.md가 뼈대 TODO를 넘어 실질 체크포인트/완료 내용을 담았나(결정론 휴리스틱)."""
    text = (result or "").strip()
    if len(text) < 200:
        return False
    # 체크된 항목([x]) 또는 완료 마커가 있고, 미체크 TODO만 있는 게 아니면 실질 있음으로 본다.
    checked = text.count("[x]") + text.count("[X]")
    markers = sum(text.count(m) for m in _DONE_MARKERS)
    return checked >= 1 or markers >= 2


def verify_output(work_dir, task_pack: dict, result: str, state: str) -> dict:
    """finalize 시 산출물을 객관(결정론) 검증한다. 엔진 호출 없음.

    반환 verification 계약:
      status: passed | suspect | inconclusive | not_applicable
      checks: [{name, ok, detail}]
      reason, artifacts_count, review_recommended, reviewer_engine
    """
    work_dir = Path(work_dir)
    artifacts = _artifacts(work_dir)
    substantive = _result_substantive(result)
    checks = [
        {"name": "artifacts_present", "ok": len(artifacts) > 0,
         "detail": f"산출 파일 {len(artifacts)}개" + (f": {artifacts[:5]}" if artifacts else " (뼈대 외 없음)")},
        {"name": "result_substantive", "ok": substantive,
         "detail": "결과.md 실질 내용 있음" if substantive else "결과.md가 뼈대 TODO 수준"},
    ]
    doer_engine = str((task_pack.get("runtime") or {}).get("engine") or task_pack.get("engine") or "claude")
    if state not in {"done", "partial_ready"}:
        # 성공 주장이 아님(blocked/error/cancelled) — 산출물 검증 대상 아님(이미 부분보고로 공개됨).
        return {"status": "not_applicable", "checks": checks, "reason": f"state={state}",
                "artifacts_count": len(artifacts), "review_recommended": False, "reviewer_engine": ""}
    # state=done 인데 산출물도 없고 결과.md도 뼈대뿐 → '거짓 성공' 의심.
    if not artifacts and not substantive:
        return {"status": "suspect", "checks": checks,
                "reason": "done 선언이나 산출 파일 없음 + 결과.md 뼈대뿐 — 거짓 완료 의심(근거 없는 done)",
                "artifacts_count": 0, "review_recommended": True,
                "reviewer_engine": pick_reviewer_engine(doer_engine)}
    # 산출물 또는 실질 결과 중 하나라도 있으면 객관 통과. 단 고위험이면 이종 검토 권고.
    high_risk = _is_high_risk(task_pack)
    return {"status": "passed", "checks": checks,
            "reason": "산출물/실질 결과 확인됨" + (" (고위험 — 이종 검토 권고)" if high_risk else ""),
            "artifacts_count": len(artifacts), "review_recommended": high_risk,
            "reviewer_engine": pick_reviewer_engine(doer_engine) if high_risk else ""}


def _is_high_risk(task_pack: dict) -> bool:
    """고위험 산출물(이종 검토 권고 대상): 지침/시스템/대량파일/외부발행 성격."""
    obj = str(task_pack.get("objective") or "").lower()
    scope = task_pack.get("scope") or {}
    if scope.get("external_side_effects") not in (None, "", "forbidden"):
        return True
    risk_terms = ("law", "지침", "시스템/core", "배포", "deploy", "발행", "전송", "대량", "삭제", "마이그레이션")
    return any(t in obj for t in risk_terms)


# 이종 검증자: 동종 모델 상호검토는 비싼 self-consistency(2502.08788) → 검토자는 doer와 다른 엔진을 권한다.
_ENGINE_ALT = {"claude": "codex", "codex": "claude", "gemini": "claude", "gemma": "claude"}


def pick_reviewer_engine(doer_engine: str, available: set | None = None) -> str:
    """doer와 다른 검토 엔진을 고른다(이종성). available 주면 그 안에서만."""
    doer = (doer_engine or "claude").strip().lower()
    alt = _ENGINE_ALT.get(doer, "codex")
    if available is not None:
        if alt in available:
            return alt
        for e in available:
            if e != doer:
                return e
        return ""          # 다른 엔진 없음 → 이종 검토 불가(동종 폴백은 상위 판단)
    return alt


def review_recommended(verification: dict, task_pack: dict) -> bool:
    return bool((verification or {}).get("review_recommended"))
