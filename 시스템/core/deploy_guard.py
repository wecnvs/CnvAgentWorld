# -*- coding: utf-8 -*-
"""배포 전 형식 PII 사전스캔 — 배포되는 '기본' 자원에 식별정보가 새는지 막는 마지막 그물.

P1 정합: 이것은 *형식 신호(깃발/차단)*이지 케이스 의미를 바꾸지 않는다. 등록 시점 게이트
(case_ledger 민감도 자기점검)와 별개의 *배포 시점* 그물이다.

대외비/추가/고급 자원과 cases.local.jsonl 등 사이드카는 배포되지 않으므로 스캔 대상이 아니다.
배포되는 것 = .gitignore 화이트리스트 = '기본' 등급의 SKILL.md/지식.md + cases.jsonl.

사용: PYTHONPATH=시스템 python3 -m core.deploy_guard   (식별정보 발견 시 exit 1)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .paths import ROOT

# (라벨, 정규식) — 한국 맥락의 개인/회사 식별정보 형식.
PATTERNS = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("주민번호", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")),
    ("사업자번호", re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{5}\b")),
    ("카드번호", re.compile(r"\b\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}\b")),
    ("휴대전화", re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b")),
    ("전화번호", re.compile(r"\b0\d{1,2}[-\s]\d{3,4}[-\s]\d{4}\b")),
    ("금액", re.compile(r"\d[\d,]*\s?(?:억\s?원?|만\s?원|원)\b")),
    ("인명+직함", re.compile(r"[가-힣]{1,4}\s?(?:부장|과장|차장|대리|팀장|사장|대표님|이사|상무|전무|회장|부사장|실장|본부장|주임)")),
]

# 배포되는 등급(= .gitignore 화이트리스트). 나머지 등급/사이드카는 배포 안 됨 → 스캔 불필요.
DEPLOYED_GRADE = "기본"
DEPLOYED_FILES = ("SKILL.md", "지식.md", "cases.jsonl")
RESOURCE_ROOTS = ("스킬", "지식")


def _mask(text: str) -> str:
    text = text.strip()
    if len(text) <= 4:
        return text[0] + "***"
    return text[:2] + "***" + text[-1]


def scan_text(text: str) -> list[dict]:
    """텍스트에서 식별정보 형식을 찾는다. 매치는 마스킹해 반환(스캔 결과 자체가 유출되지 않게)."""
    hits = []
    for label, rx in PATTERNS:
        for m in rx.finditer(text or ""):
            hits.append({"label": label, "match": _mask(m.group(0))})
    return hits


def scan_deployable() -> list[dict]:
    """배포되는 '기본' 등급 자원의 본문/공개 케이스를 스캔. 발견 목록(파일별) 반환."""
    findings = []
    for root in RESOURCE_ROOTS:
        base = ROOT / root / DEPLOYED_GRADE
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.name not in DEPLOYED_FILES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            hits = scan_text(text)
            if hits:
                findings.append({"path": str(path.relative_to(ROOT)), "hits": hits})
    return findings


def main(argv=None) -> int:
    findings = scan_deployable()
    if not findings:
        print("OK: 배포 대상('기본')에서 식별정보 형식 미발견")
        return 0
    print("BLOCK: 배포 대상에서 식별정보 형식 발견 — 일반화하거나 cases.local.jsonl/대외비로 옮기세요")
    for f in findings:
        labels = ", ".join(sorted({h["label"] for h in f["hits"]}))
        print(f"  - {f['path']}: {labels} ({len(f['hits'])}건)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
