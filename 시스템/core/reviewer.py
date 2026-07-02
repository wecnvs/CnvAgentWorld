# -*- coding: utf-8 -*-
"""이종 검토자 원장 — 산출물 검증 결과를 독립 에이전트(다른 엔진)가 재검토 (P4-2, 섀도 우선).

왜 (전면 분석 + 문헌):
- P4-1의 output_verify는 '이 결과를 검토해야 한다(review_recommended)'와 '어느 엔진으로(reviewer_engine)'까지만 냈다.
  P4-2는 그 신호를 받아 실제 검토 흐름을 붙인다.
- MAST(2503.13657): 검증 실패의 13.5%는 검증자가 도장만 찍는 것 → 검토자는 **실제 차단권 + 거부율 모니터링**
  (거부율 0%면 도장 찍기 경보). 동종 모델 상호검토는 비싼 self-consistency(2502.08788) → **이종 엔진**.

안전 (D0 + v2.1 섀도 먼저):
- D0: 결과 공개는 항상 자동(위험은 작업 시작 게이트). 그래서 **기본은 검토 의도·verdict를 기록·관측만 하고
  공개를 막지 않는다.** 실제 차단은 CNV_REVIEW_BLOCK_ACTIVE=1일 때만(관측으로 오판율 확인 후 켠다).
- 실제 검토자 디스패치(다른 엔진 에이전트 실행)는 CNV_REVIEW_DISPATCH_ACTIVE=1일 때만. 기본은
  '이 결과를 엔진 X로 검토했어야 함'을 원장에 남기는 섀도.

원장: 공간/{space}/review_ledger.jsonl (append-only, 런타임 — gitignore).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import ROOT
from .transcript import now_iso

SPACES = ROOT / "공간"
_LEDGER = "review_ledger.jsonl"
# 거부율 도장찍기 경보 — 최소 이만큼 검토가 쌓였는데 거부가 0이면 '검토자가 다 통과시킨다'는 신호.
RUBBER_STAMP_MIN_REVIEWS = 8


def _dispatch_active() -> bool:
    return os.environ.get("CNV_REVIEW_DISPATCH_ACTIVE", "") == "1"


def _block_active() -> bool:
    return os.environ.get("CNV_REVIEW_BLOCK_ACTIVE", "") == "1"


def _path(space: str) -> Path:
    return SPACES / space / _LEDGER


def _append(space: str, rec: dict) -> None:
    try:
        rec = {"schema": "ReviewLedger.v1", "at_utc": now_iso(), **rec}
        p = _path(space)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def should_review(verification: dict) -> bool:
    return bool((verification or {}).get("review_recommended"))


def record_review_intent(space: str, task_id: str, verification: dict, *, doer_engine: str = "") -> dict:
    """검토 권고 결과를 원장에 기록(섀도). 실제 디스패치는 플래그일 때만.

    반환: {"logged":bool, "dispatched":bool, "reviewer_engine":..., "blocking":bool}.
    """
    if not should_review(verification):
        return {"logged": False}
    reviewer_engine = (verification or {}).get("reviewer_engine", "")
    blocking = _block_active()
    dispatched = _dispatch_active() and bool(reviewer_engine)
    rec = {
        "kind": "review_intent",
        "task_id": task_id,
        "verify_status": (verification or {}).get("status", ""),
        "reason": str((verification or {}).get("reason") or "")[:200],   # None 방어
        "doer_engine": doer_engine,
        "reviewer_engine": reviewer_engine,
        "dispatched": dispatched,     # 실제 검토자 실행 여부(플래그)
        "blocking": blocking,         # 거부 시 공개 차단 여부(플래그)
        "shadow": not dispatched,
    }
    _append(space, rec)
    return {"logged": True, "dispatched": dispatched, "reviewer_engine": reviewer_engine, "blocking": blocking}


def record_verdict(space: str, task_id: str, *, verdict: str, by_engine: str, by: str = "", reason: str = "") -> dict:
    """검토자 판정(approve|reject) 기록. 거부율 모니터링의 입력.

    verdict는 approve|reject. by_engine=검토에 쓴 엔진(이종성 감사용).
    """
    v = str(verdict or "").strip().lower()
    if v not in {"approve", "reject"}:
        raise ValueError("verdict는 approve|reject")
    rec = {"kind": "review_verdict", "task_id": task_id, "verdict": v,
           "by_engine": by_engine, "by": by, "reason": str(reason)[:240]}
    _append(space, rec)
    return rec


def _read(space: str) -> list[dict]:
    p = _path(space)
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def rejection_stats(space: str) -> dict:
    """검토 verdict 집계 — 거부율 + 도장 찍기 경보(문헌: 검증자 자기 통과가 검증실패의 큰 몫)."""
    verdicts = [r for r in _read(space) if r.get("kind") == "review_verdict"]
    n = len(verdicts)
    rejects = sum(1 for r in verdicts if r.get("verdict") == "reject")
    rate = (rejects / n) if n else None
    # 이종성 점검: 검토 엔진이 다양한가(전부 한 엔진이면 이종 검토 아님)
    engines = {r.get("by_engine", "") for r in verdicts if r.get("by_engine")}
    return {
        "reviews": n, "rejects": rejects, "rejection_rate": rate,
        "engines_used": sorted(engines),
        "rubber_stamp_alarm": bool(n >= RUBBER_STAMP_MIN_REVIEWS and rejects == 0),
        "homogeneous_alarm": bool(n >= RUBBER_STAMP_MIN_REVIEWS and len(engines) <= 1),
    }


def recent_intents(space: str, *, limit: int = 30) -> list[dict]:
    return [r for r in _read(space) if r.get("kind") == "review_intent"][-limit:][::-1]
