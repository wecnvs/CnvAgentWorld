# -*- coding: utf-8 -*-
"""스킬 골든셋(평가셋) 골격 — 자동 승격·본문개정의 회귀 안전판 (성장루프 P1' item 3).

왜 (전면 분석 2026-07-02 + 벤치마킹):
- 대표는 승격·검토를 하지 않는다. 그러면 "이 케이스/본문을 반영해도 기존이 안 깨지나"를 누가 보증하나?
  → **골든셋**: 스킬마다 대표적 시나리오 10~20개를 두고, 자동 승격/본문개정 전에 이걸 회귀로 통과해야 한다.
  Anthropic 공식 절차("문서 쓰기 전에 평가부터 만들어라") + 메모리 오염 방어(회귀 게이트)의 실무 정본.
- 이 모듈은 **골격**이다: 평가셋의 스키마·위치·로드·커버리지 판정까지. 실제 LLM 채점 실행은 자동 승격을
  켜는 단계(P3)에서 이 골격 위에 붙인다. 지금은 "골든셋이 있는가/몇 개인가"를 노출해 승격 안전도를 가늠한다.

파일: 스킬/{등급}/{이름}/evals.jsonl (append-only, 배포 대상 아님이면 사이드카처럼 다룬다).
레코드(EvalCase.v1):
  {"eval_id","scenario"(입력/상황),"expect"(기대 행동/판정 기준),"kind":"positive|negative",
   "source":"seed|from_case:<case_id>|daepyo_feedback","created_at"}
"""
from __future__ import annotations

import json
from pathlib import Path

from . import case_ledger
from .transcript import now_iso

_EVALS_NAME = "evals.jsonl"
MIN_GOLDEN = 3       # 최소 권장(신규 스킬), 이상적 10~20 (Anthropic best-practice)
REGRESSION_READY = 5  # 자동 본문개정/승격 회귀를 신뢰할 최소 커버리지(운영 데이터로 P3에서 튜닝)


def _evals_path(sdir: Path) -> Path:
    return sdir / _EVALS_NAME


def load_evals(sdir: Path) -> list[dict]:
    """스킬 골든셋 로드(최신 eval_id 기준 dedup, append-only)."""
    path = _evals_path(Path(sdir))
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    latest = {}
    order = []
    for r in rows:
        k = str(r.get("eval_id") or f"__anon_{len(order)}")
        if k not in latest:
            order.append(k)
        latest[k] = r
    return [latest[k] for k in order]


def add_eval(sdir: Path, evalcase: dict, *, by: str = "system") -> dict:
    """골든셋 케이스 1건 추가(append-only, 자원락). eval_id 없으면 부여."""
    sdir = Path(sdir)

    def mutate():
        rec = dict(evalcase or {})
        rec.setdefault("schema", "EvalCase.v1")
        if not rec.get("eval_id"):
            existing = len(load_evals(sdir))
            rec["eval_id"] = f"ev{existing + 1:03d}"
        rec.setdefault("kind", "positive")
        rec.setdefault("created_at", now_iso())
        rec["by"] = by
        if not str(rec.get("scenario") or "").strip():
            raise ValueError("eval 'scenario'(상황/입력)는 필수")
        if not str(rec.get("expect") or "").strip():
            raise ValueError("eval 'expect'(기대 판정 기준)는 필수")
        case_ledger._append_jsonl(_evals_path(sdir), rec)
        return rec

    return case_ledger.with_resource_lock(sdir, mutate)


def coverage(sdir: Path) -> dict:
    """골든셋 커버리지 요약(발견·승격 안전도 가늠용, 파생 지표). 차단 아님 — 신호."""
    evals = load_evals(Path(sdir))
    n = len(evals)
    pos = sum(1 for e in evals if e.get("kind") != "negative")
    neg = n - pos
    return {
        "count": n,
        "positive": pos,
        "negative": neg,
        "has_golden": n > 0,
        "meets_min": n >= MIN_GOLDEN,
        "regression_ready": n >= REGRESSION_READY and neg > 0,   # 긍정만 있으면 회귀로 불충분
    }


def regression_ready(sdir: Path) -> bool:
    """자동 본문개정/승격의 회귀 게이트를 신뢰할 만큼 골든셋이 갖춰졌는가(P3에서 실채점 게이트가 참조)."""
    return coverage(sdir)["regression_ready"]
