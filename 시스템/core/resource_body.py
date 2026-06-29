# -*- coding: utf-8 -*-
"""본문(SKILL.md/지식.md) 진화 프리미티브 — 스킬·지식 공용 (P5).

설계 §3(P3 본문 우선권)·§8(본문개정 단일경로)·§10(version CAS·스냅샷). 계약: 설계_P0_확정스키마.md.

원칙:
- 본문 덮어쓰기는 **버전드 개정**으로만: 이전 본문을 .history 스냅샷으로 보존(롤백 가능) → '덮어쓰기'지만 비파괴.
- `version` CAS: 읽은 버전과 쓸 때 버전이 다르면 거부(동시·stale write 차단). 신규 구현(space_memory는 단조카운터라 재사용 불가).
- 스킬 본문 개정은 N개 케이스 종합 + 개정 전 케이스 회귀 점검 결과(에이전트 판단) 필수 — 특수 1건으로 본문 고정 금지.
- non_overridable 섹션은 개정으로 약화 불가(기계적 보존 검사).
- .history/는 로컬 audit(gitignore).
"""
from __future__ import annotations

import json
import re

from . import case_ledger
from .paths import ROOT
from .transcript import now_iso


class ResourceBodyError(RuntimeError):
    """본문 개정 계약 위반."""


def _rel(path) -> str:
    """ROOT 기준 상대경로(불가하면 절대경로). 테스트(ROOT 밖 tmp)에서도 안전."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _read_manifest(path):
    if not path.exists():
        raise ResourceBodyError(f"매니페스트 없음: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ResourceBodyError("frontmatter(---) 없음")
    end = text.find("\n---", 3)
    if end == -1:
        raise ResourceBodyError("frontmatter 닫힘(---) 없음")
    front = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    return front, body, text


def _front_get(front: str, key: str) -> str:
    m = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.*)$", front)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _front_set(front: str, key: str, value: str) -> str:
    line = f"{key}: {value}"
    if re.search(rf"(?m)^\s*{re.escape(key)}\s*:", front):
        return re.sub(rf"(?m)^\s*{re.escape(key)}\s*:.*$", lambda _m: line, front, count=1)
    return front.rstrip("\n") + "\n" + line


def _nonoverridable_lines(body: str) -> list[str]:
    """본문의 '## non_overridable' 섹션의 비어있지 않은 줄들(헤더 제외)."""
    lines = body.splitlines()
    out = []
    capture = False
    for line in lines:
        if re.match(r"^\s*##\s+non_overridable\b", line, re.IGNORECASE):
            capture = True
            continue
        if capture and re.match(r"^\s*##\s+", line):
            break
        if capture and line.strip():
            out.append(line.strip())
    return out


def _next_version(cur: str) -> str:
    return str(int(cur) + 1) if str(cur).isdigit() else f"{cur or '1'}.1"


def _render(front: str, body: str) -> str:
    return f"---\n{front.strip(chr(10))}\n---\n\n{body.strip(chr(10))}\n"


def revise_body(manifest_path, new_body: str, *, expected_version, by: str, rationale: str,
                from_case_ids=None, min_cases: int = 2, regression_attestation: str = "",
                require_cases: bool = True, new_description=None) -> dict:
    """본문을 버전드 개정한다(CAS + 스냅샷 + non_overridable 보존 + (스킬)N케이스 게이트).

    require_cases=False면 지식 등 케이스 없는 자원(N케이스/회귀점검 면제).
    """
    sdir = manifest_path.parent

    def mutate():
        front, old_body, old_text = _read_manifest(manifest_path)
        cur_ver = _front_get(front, "version") or "1"
        if str(expected_version) != str(cur_ver):
            raise ResourceBodyError(f"stale write 거부: expected_version={expected_version} != current={cur_ver}")

        if require_cases:
            ids = list(from_case_ids or [])
            if len(ids) < min_cases:
                raise ResourceBodyError(f"본문 개정은 {min_cases}개 이상 케이스 종합 필요(받음 {len(ids)}) — 특수 1건 고정 금지")
            if not str(regression_attestation).strip():
                raise ResourceBodyError("개정 전 케이스 회귀 점검 결과(regression_attestation) 필수 — 에이전트 판단")

        old_no = _nonoverridable_lines(old_body)
        if old_no:
            missing = [ln for ln in old_no if ln not in (new_body or "")]
            if missing:
                raise ResourceBodyError(f"non_overridable 약화/삭제 금지 — 새 본문에 누락: {missing[:2]}")

        hist = sdir / ".history"
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"v{cur_ver}.md").write_text(old_text, encoding="utf-8")

        new_ver = _next_version(cur_ver)
        nf = _front_set(front, "version", new_ver)
        nf = _front_set(nf, "last_updated", now_iso())
        if new_description is not None:
            nf = _front_set(nf, "description", json.dumps(str(new_description), ensure_ascii=False))
        manifest_path.write_text(_render(nf, new_body), encoding="utf-8")

        case_ledger._append_jsonl(hist / "revisions.jsonl", {
            "schema": "BodyRevision.v1",
            "from_version": cur_ver, "to_version": new_ver,
            "by": by, "rationale": rationale,
            "from_case_ids": list(from_case_ids or []),
            "regression_attestation": str(regression_attestation),
            "at_utc": now_iso(),
        })
        return {"ok": True, "version": new_ver, "snapshot": _rel(hist / f"v{cur_ver}.md")}

    return case_ledger.with_resource_lock(sdir, mutate)


def rollback_body(manifest_path, to_version, *, by: str, rationale: str) -> dict:
    """이전 스냅샷(.history/v{to_version}.md)으로 본문을 되돌린다(버전은 앞으로 증가 — CAS 단조 유지)."""
    sdir = manifest_path.parent

    def mutate():
        snap = sdir / ".history" / f"v{to_version}.md"
        if not snap.exists():
            raise ResourceBodyError(f"스냅샷 없음: v{to_version}")
        front, _old_body, cur_text = _read_manifest(manifest_path)
        cur_ver = _front_get(front, "version") or "1"
        hist = sdir / ".history"
        (hist / f"v{cur_ver}.md").write_text(cur_text, encoding="utf-8")

        sfront, sbody, _ = _read_manifest(snap)
        new_ver = _next_version(cur_ver)
        nf = _front_set(sfront, "version", new_ver)
        nf = _front_set(nf, "last_updated", now_iso())
        manifest_path.write_text(_render(nf, sbody), encoding="utf-8")

        case_ledger._append_jsonl(hist / "revisions.jsonl", {
            "schema": "BodyRevision.v1", "rollback": True,
            "from_version": cur_ver, "restored_from": str(to_version), "to_version": new_ver,
            "by": by, "rationale": rationale, "at_utc": now_iso(),
        })
        return {"ok": True, "version": new_ver, "restored_from": str(to_version)}

    return case_ledger.with_resource_lock(sdir, mutate)


def current_version(manifest_path) -> str:
    front, _b, _t = _read_manifest(manifest_path)
    return _front_get(front, "version") or "1"
