# -*- coding: utf-8 -*-
"""케이스 janitor 전역 스윕 — 대표 무개입 수렴의 청소부 (성장루프 P3').

왜 (전면 분석 2026-07-02):
- 대표는 승격·검토·정리를 하지 않는다. 그러면 traction 없는 candidate가 영원히 쌓인다(감사 시점 candidate 80건).
- `expire_stale_candidates`·`dedup_cases`는 설계(§6·§8)가 **자동/결정론 허용**으로 명시한 안전 청소다:
  · expire: status==candidate + worked 0 + 14일 초과인 것만 만료(append-only=복원가능, 대표발 provisional_must 제외).
  · dedup: condition+instruction+polarity가 **완전히 동일**한 케이스만 정리('비슷한' 통합은 안 함 — 그건 에이전트 판단).
- 종전엔 이 둘이 어디에도 배선되지 않아 안 돌았다(크로스체크 확인). 이 모듈이 전역 저빈도 스윕으로 돌린다.

안전:
- expire/dedup은 의미반전이 아니라 청소라 실제 실행하되 **모든 동작을 로그**(관측·감사). 위험한 자동 판단
  (승격·강등·supersede·conflict 해소)은 여기 넣지 않는다 — 그건 섀도 게이트(growth_feedback 등).
- 킬스위치: CNV_JANITOR_DISABLE=1이면 스윕을 건너뛴다. 저빈도(기본 1시간) 레이트리밋(스탬프 파일).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import case_ledger, skill_smith
from .paths import ROOT
from .transcript import now_iso

_RUN_DIR = ROOT / "시스템" / "대시보드" / ".run"
_LOG = _RUN_DIR / "growth_janitor.jsonl"
_STAMP = _RUN_DIR / "growth_janitor.stamp"
DEFAULT_MIN_INTERVAL_SEC = 3600


def _disabled() -> bool:
    return os.environ.get("CNV_JANITOR_DISABLE", "") == "1"


def _log(rec: dict) -> None:
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at_utc": now_iso(), **rec}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def sweep() -> dict:
    """전 스킬에 expire_stale_candidates + dedup_cases 1회 적용. 정리 내역 로그·반환."""
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    expired_total = 0
    deduped_total = 0
    touched = []
    try:
        skills = skill_smith.list_skills()
    except Exception as exc:
        _log({"kind": "sweep_error", "error": str(exc)[:160]})
        return {"ok": False, "error": str(exc)[:160]}
    for s in skills:
        name = s.get("name", "")
        sdir = case_ledger.skill_dir(name)
        if not sdir:
            continue
        try:
            expired = case_ledger.expire_stale_candidates(sdir)
        except Exception:
            expired = []
        try:
            deduped = case_ledger.dedup_cases(sdir)
        except Exception:
            deduped = []
        if expired or deduped:
            expired_total += len(expired)
            deduped_total += len(deduped)
            touched.append({"skill": name, "expired": len(expired), "deduped": len(deduped)})
            _log({"kind": "swept", "skill": name,
                  "expired": [e.get("case_id") for e in expired],
                  "deduped": [d.get("case_id") for d in deduped]})
    summary = {"ok": True, "expired": expired_total, "deduped": deduped_total, "skills_touched": touched}
    _log({"kind": "sweep_summary", **{k: summary[k] for k in ("expired", "deduped")},
          "skills_touched": len(touched)})
    return summary


def _due(min_interval_sec: int) -> bool:
    try:
        if not _STAMP.exists():
            return True
        from datetime import datetime
        last = _STAMP.read_text(encoding="utf-8").strip()
        last_dt = datetime.fromisoformat(last)
        now_dt = datetime.fromisoformat(now_iso())
        return (now_dt - last_dt).total_seconds() >= min_interval_sec
    except Exception:
        return True


def _stamp() -> None:
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        _STAMP.write_text(now_iso(), encoding="utf-8")
    except Exception:
        pass


def sweep_if_due(*, min_interval_sec: int = DEFAULT_MIN_INTERVAL_SEC) -> dict:
    """레이트리밋된 스윕 — 백스톱 루프가 매 주기 불러도 저빈도로만 실제 실행(값싼 no-op)."""
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    if not _due(min_interval_sec):
        return {"ok": True, "skipped": "not_due"}
    _stamp()
    return sweep()
