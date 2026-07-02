# -*- coding: utf-8 -*-
"""주입 로그 — 어떤 케이스가 어느 턴/작업에 '주입(노출)'됐는지 기록한다 (성장루프 P1' 안전판).

왜 필요한가 (전면 분석 2026-07-02, 크로스체크):
- 현재 시스템은 worked 자기보고는 받지만, "이 케이스가 노출됐는데 결과가 나빴다"를 역추적할 데이터가 없었다.
  발견기가 case_preview/case_negatives로 케이스를 프롬프트에 실어도 그 사실이 어디에도 안 남았다.
- 이 로그가 두 가지 후속(P2'/P3')의 데이터 기반이다:
  ① harmful 역추적: 대표가 "아니야 다시해"라고 하면, 직전 턴/작업에 무엇이 주입됐는지 조회해 원인 후보를 좁힌다.
  ② 음승률/미사용 감지: 주입(노출) 대비 worked/harmful 비율, 오래 노출됐는데 성과 없는 케이스.

설계 정합: 이것은 *신호 기록*이지 의미 변경이 아니다(P1). append-only, 공간 스코프.
저장: 공간/{space}/injection_log.jsonl (런타임 산출물 — gitignore).
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import ROOT
from .transcript import now_iso

SPACES = ROOT / "공간"
_LOG_NAME = "injection_log.jsonl"


def _log_path(space: str) -> Path:
    return SPACES / space / _LOG_NAME


def record_injection(space: str, *, kind: str, ref: str, injected, context: dict | None = None) -> None:
    """주입 이벤트 1건을 기록한다(best-effort — 실패해도 주입/실행을 끊지 않는다).

    kind: "chat" | "work"  — 어떤 종류의 턴에 주입됐나.
    ref:  턴/작업 식별자(task_id, turn_id, wake_id 등) — 역추적 키.
    injected: [{skill, case_id, polarity, kind}] (discovery.injected_case_refs 산출).
    """
    try:
        refs = [r for r in (injected or []) if r.get("case_id")]
        if not refs:
            return
        rec = {
            "schema": "InjectionLog.v1",
            "at_utc": now_iso(),
            "kind": str(kind or ""),
            "ref": str(ref or ""),
            "cases": refs,
        }
        ctx = context or {}
        for k in ("intent_id", "turn_id", "wake_id", "source_event_seq"):
            if ctx.get(k):
                rec[k] = ctx[k]
        path = _log_path(space)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_injections(space: str, *, limit: int = 20) -> list[dict]:
    """최근 주입 기록(최신순). harmful 역추적·감사용."""
    path = _log_path(space)
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out[-limit:][::-1]


def last_injection_for_ref(space: str, ref: str) -> dict | None:
    """특정 턴/작업(ref)에 주입된 마지막 기록 — 재작업·교정 시 원인 케이스 후보 조회."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    for rec in recent_injections(space, limit=200):
        if rec.get("ref") == ref:
            return rec
    return None
