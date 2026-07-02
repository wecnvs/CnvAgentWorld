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


# provisional_must(대표발 즉시적용) 재판정 창 — 이만큼 지나면 사용 실적으로 재판정한다.
# 실제 상태전이는 CNV_REVIEW_ACTIVE=1일 때만(섀도 기본). worked≥1이면 active 확정, 노출됐는데 성과 0이면 강등.
REVIEW_WINDOW_DAYS = 7


def _review_active() -> bool:
    return os.environ.get("CNV_REVIEW_ACTIVE", "") == "1"


def review_provisional_must() -> dict:
    """review_due 자동 재판정(P3' 잔여, 섀도 우선).

    provisional_must(대표 즉시적용·잠정)를 created_at+REVIEW_WINDOW 지나면 사용 실적으로 재판정.
    **원칙(v2.1·크로스체크 반영): 대표 지시를 약한 신호로 죽이지 않는다.**
      - worked가 수렴(신뢰 가능한 독립 확인자 ≥ 임계 & harmful 0)하면 → active 확정(승격만).
      - 확인 안 됨(worked 부족)이면 → **강등하지 않고 유지**(대표 지시는 계속 적용). 미확인 사실만 surfacing 로그.
      - 대표 지시를 실제로 죽이는 건 오직 **명시적 부정신호**(harmful/dispute)이지 'worked 보고 없음'이 아니다.
    승격은 promote_case(method="worked_threshold") — 독립확인자·사이코펀시·conflict 오염방지 게이트 통과 필수
    (단일 자기보고로 못 올라감, 대표 승인 위조 안 함). 기본 섀도(would_* 로그만), CNV_REVIEW_ACTIVE=1일 때만 실제 승격.
    """
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    from datetime import datetime, timedelta
    now = datetime.now()
    cutoff = now - timedelta(days=REVIEW_WINDOW_DAYS)
    shadow = not _review_active()
    confirmed = []
    unconfirmed = []
    try:
        skills = skill_smith.list_skills()
    except Exception:
        return {"ok": False, "error": "list_skills"}
    for s in skills:
        name = s.get("name", "")
        sdir = case_ledger.skill_dir(name)
        if not sdir:
            continue
        try:
            cases = case_ledger.read_cases(sdir)
        except Exception:
            continue
        for c in cases:
            if c.get("status") != "provisional_must":
                continue
            try:
                created = case_ledger._parse_iso(c.get("created_at", ""))
                if created is None or created > cutoff:
                    continue                                   # 아직 재판정 창 전(또는 파싱 실패=보수적 skip)
                cid = c.get("case_id", "")
                worked, harmful = case_ledger.worked_harmful_counts(sdir, cid)
            except Exception:
                continue                                       # 개별 케이스 오류가 전체를 안 멈춤(tz 등 방어)
            if worked >= case_ledger.DEFAULT_CONFIRM_THRESHOLD and harmful == 0:
                if _review_active():
                    try:
                        # worked_threshold: 신뢰 가능한 독립 확인자 게이트 통과해야만 승격(오염방지). 미달 시 raise→유지.
                        case_ledger.promote_case(sdir, cid, by="system/review",
                                                 rationale=f"review_due 자동 확정: worked {worked}·harmful 0 실사용 수렴",
                                                 method="worked_threshold")
                        confirmed.append(cid)
                        _log({"kind": "review_confirmed", "skill": name, "case_id": cid, "worked": worked, "shadow": False})
                    except Exception as exc:
                        _log({"kind": "confirm_gate_rejected", "skill": name, "case_id": cid,
                              "reason": str(exc)[:120]})   # 게이트 미달 → 승격 안 하고 유지(정상)
                else:
                    confirmed.append(cid)
                    _log({"kind": "would_confirm", "skill": name, "case_id": cid, "worked": worked,
                          "shadow": True, "note": "CNV_REVIEW_ACTIVE=1이면 worked_threshold 게이트로 active 확정 시도"})
            elif harmful == 0:
                # worked 미수렴 + harmful 없음 → 강등하지 않고 '미확인 대표지시'로 surfacing만(대표 지시는 유지).
                unconfirmed.append(cid)
                _log({"kind": "provisional_must_unconfirmed", "skill": name, "case_id": cid,
                      "worked": worked, "exposed": _exposed_count(name, cid),
                      "note": "재판정 창 지났으나 worked 미수렴 — 유지(약한 신호로 대표지시 강등 안 함). harmful/dispute만 강등 근거."})
            # harmful>0은 review_queue/growth_feedback가 별도 처리(여기서 안 건드림)
    summary = {"ok": True, "shadow": shadow, "confirmed": confirmed, "unconfirmed": unconfirmed}
    if confirmed or unconfirmed:
        _log({"kind": "review_summary", "shadow": shadow,
              "confirmed": len(confirmed), "unconfirmed": len(unconfirmed)})
    return summary


def _exposed_count(skill: str, case_id: str) -> int:
    """이 케이스가 injection_log에 노출된 횟수(전 공간) — surfacing 로그의 참고 지표(판정 근거 아님)."""
    try:
        from . import injection_log
        n = 0
        for space_dir in injection_log.SPACES.glob("*"):
            if not space_dir.is_dir():
                continue
            for rec in injection_log.recent_injections(space_dir.name, limit=500):
                for c in rec.get("cases", []) or []:
                    if c.get("skill") == skill and c.get("case_id") == case_id:
                        n += 1
        return n
    except Exception:
        return 0


def sweep() -> dict:
    """전 스킬에 expire_stale_candidates + dedup_cases + review_due 재판정 1회 적용. 정리 내역 로그·반환."""
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
    # review_due 재판정(섀도 우선) — 같은 스윕에 포함
    try:
        review = review_provisional_must()
    except Exception as exc:
        review = {"ok": False, "error": str(exc)[:120]}
    summary = {"ok": True, "expired": expired_total, "deduped": deduped_total, "skills_touched": touched,
               "review": {k: review.get(k) for k in ("confirmed", "unconfirmed", "shadow") if k in review}}
    _log({"kind": "sweep_summary", **{k: summary[k] for k in ("expired", "deduped")},
          "skills_touched": len(touched), "review": summary["review"]})
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
