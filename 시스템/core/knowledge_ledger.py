# -*- coding: utf-8 -*-
"""지식 탐지레이어 (P5) — 지식은 케이스가 없다. claim 단위 검증/반박 신호 + 본문개정.

설계 §9-2: 지식=본문개정 메커니즘(resource_body 재사용) + 탐지레이어. law "지식=참고/스킬=사용".

- 본문(지식.md)은 claim 단위로 작성하고(범용/조건부 2섹션), 각 claim에 agent가 claim_id를 부여한다.
- '이 사실이 틀렸다'는 **반박 깃발 채널**(dispute) — 현 설계 최대 결손이던 입구. claim status를 disputed로(본문 불변, 깃발만).
- 검증(verify)되면 active. claim status는 이벤트에서 파생(append-only, claim_events.jsonl=사이드카).
- must_apply 없음 — 최고 강제력은 should_consider(law §6). 본문개정은 resource_body.revise_body(require_cases=False).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import case_ledger, discovery
from .paths import ROOT
from .transcript import now_iso

KNOWLEDGE = ROOT / "지식"
GRADES = case_ledger.GRADES
CLAIM_EVENTS_FILE = "claim_events.jsonl"
VALID_CLAIM_EVENTS = {"verify", "dispute", "resolve"}


# --------------------------------------------------------------------------- 전역 지식 자원 생성/졸업 (찾아서 참고)
def create_knowledge(name: str, *, description: str, claim: str = "", grade: str = "추가") -> dict:
    """전역 지식 자원(지식/{등급}/{name}/지식.md)을 생성한다 — 발견기가 frontmatter(name·description)로 찾는다.

    이미 있으면 claim(사실 한 줄)을 본문 '## 범용 사실'에 누적 append(졸업 멱등). description은 발견용 필수.
    """
    name = str(name or "").strip()
    if not name:
        raise KnowledgeLedgerError("지식 이름 필수")
    if re.search(r"[/\\]", name):
        raise KnowledgeLedgerError("지식 이름에 경로 구분자(/ \\) 금지")
    desc_one = re.sub(r"\s+", " ", str(description or "")).strip()
    if not desc_one:
        raise KnowledgeLedgerError("description 필수(발견용 — 무엇을·언제·표현·핵심용어)")
    if grade not in GRADES:
        raise KnowledgeLedgerError(f"grade는 {list(GRADES)} 중 하나")
    claim_line = re.sub(r"\s+", " ", str(claim or "")).strip()

    existing = knowledge_dir(name)
    if existing is not None:
        manifest = existing / "지식.md"
        if claim_line:
            text = manifest.read_text(encoding="utf-8") if manifest.exists() else ""
            if claim_line not in text:                       # 중복 사실 방지
                with manifest.open("a", encoding="utf-8") as f:
                    f.write(f"- {claim_line}\n")
        return {"name": name, "path": str(manifest.relative_to(ROOT)), "created": False, "appended": bool(claim_line)}

    kdir = KNOWLEDGE / grade / name
    kdir.mkdir(parents=True, exist_ok=True)
    front = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(desc_one, ensure_ascii=False)}\n"
        "---\n\n"
        f"# {name}\n\n## 범용 사실\n"
    )
    body = front + (f"- {claim_line}\n" if claim_line else "")
    (kdir / "지식.md").write_text(body, encoding="utf-8")
    return {"name": name, "path": str((kdir / "지식.md").relative_to(ROOT)), "created": True, "appended": bool(claim_line)}


def find_similar_knowledge(queries, *, exclude: str = "", top: int = 5) -> list[dict]:
    """기존 지식 자원 중 새 지식과 비슷한 후보(어휘+ngram 신호 — 의미 dedup의 후보 게이트용)."""
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in (queries or []) if str(q).strip()]
    scored = []
    for item in discovery.collect(["knowledge"]):
        if item["name"] == exclude:
            continue
        lexical = max((discovery.score(q, item) for q in queries), default=0)
        if lexical <= 0:
            continue
        scored.append({"name": item["name"], "description": item["description"],
                       "lexical": lexical, "signal": float(lexical)})
    scored.sort(key=lambda s: -s["signal"])
    return scored[:top]


def check_knowledge_discoverable(name: str, queries, *, top: int = 3) -> dict:
    """이 지식을 부를 표현들로 검색했을 때 top-N에 뜨는지(발견 형식게이트, 신호)."""
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in (queries or []) if str(q).strip()]
    per_query = []
    for q in queries:
        hits = discovery.find(q, kind="knowledge", top=top)
        names = [item["name"] for _, item in hits]
        per_query.append({"query": q, "in_top": name in names})
    return {"discoverable": any(p["in_top"] for p in per_query), "per_query": per_query}


class KnowledgeLedgerError(RuntimeError):
    """지식 원장 계약 위반."""


def knowledge_dir(name: str) -> Path | None:
    for grade in GRADES:
        candidate = KNOWLEDGE / grade / name
        if candidate.is_dir():
            return candidate
    return None


def _resolve_dir(name_or_dir) -> Path:
    kdir = name_or_dir if isinstance(name_or_dir, Path) else knowledge_dir(str(name_or_dir))
    if not kdir or not kdir.is_dir():
        raise KnowledgeLedgerError(f"지식 없음: {name_or_dir}")
    return kdir


def _events_path(kdir: Path) -> Path:
    return kdir / CLAIM_EVENTS_FILE


def _read_events(kdir: Path) -> list[dict]:
    path = _events_path(kdir)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _record_claim_event(name_or_dir, claim_id: str, event: str, *, by: str, rationale: str) -> dict:
    kdir = _resolve_dir(name_or_dir)
    if not str(claim_id or "").strip():
        raise KnowledgeLedgerError("claim_id 필수")
    if event not in VALID_CLAIM_EVENTS:
        raise KnowledgeLedgerError(f"event는 {sorted(VALID_CLAIM_EVENTS)} 중 하나")
    if not str(rationale or "").strip():
        raise KnowledgeLedgerError("rationale 필수(왜 검증/반박하는지 — 에이전트 판단)")

    def mutate():
        rec = {
            "schema": "ClaimEvent.v1",
            "claim_id": claim_id,
            "event": event,
            "by": by,
            "rationale": rationale,
            "at_utc": now_iso(),
        }
        case_ledger._append_jsonl(_events_path(kdir), rec)
        return rec

    return case_ledger.with_resource_lock(kdir, mutate)


def verify_claim(name_or_dir, claim_id: str, *, by: str, rationale: str) -> dict:
    """claim이 현실과 맞음을 확인(→ active 경로)."""
    return _record_claim_event(name_or_dir, claim_id, "verify", by=by, rationale=rationale)


def dispute_claim(name_or_dir, claim_id: str, *, by: str, rationale: str) -> dict:
    """'이 사실이 틀렸다' 반박 깃발(→ disputed). 본문은 안 바꾸고 깃발만(harmful 패턴의 지식판)."""
    return _record_claim_event(name_or_dir, claim_id, "dispute", by=by, rationale=rationale)


def resolve_claim(name_or_dir, claim_id: str, *, by: str, rationale: str) -> dict:
    """반박을 검토·해소했음(본문 개정 후 등). 이후 status는 재평가된다."""
    return _record_claim_event(name_or_dir, claim_id, "resolve", by=by, rationale=rationale)


def claim_status(name_or_dir, claim_id: str) -> str:
    """이벤트에서 파생. 마지막 이벤트가 dispute면 disputed, verify/resolve면 active, 없으면 provisional."""
    kdir = _resolve_dir(name_or_dir)
    events = [e for e in _read_events(kdir) if e.get("claim_id") == claim_id]
    if not events:
        return "provisional"
    last = events[-1].get("event")
    if last == "dispute":
        return "disputed"
    if last in {"verify", "resolve"}:
        return "active"
    return "provisional"


def claim_review_queue(name_or_dir) -> list[dict]:
    """현재 disputed인 claim 목록(깃발=신호). 에이전트/대표 재판단·본문개정 입력."""
    kdir = _resolve_dir(name_or_dir)
    events = _read_events(kdir)
    claim_ids = []
    for e in events:
        cid = e.get("claim_id")
        if cid and cid not in claim_ids:
            claim_ids.append(cid)
    out = []
    for cid in claim_ids:
        if claim_status(kdir, cid) == "disputed":
            last = [e for e in events if e.get("claim_id") == cid][-1]
            out.append({"claim_id": cid, "status": "disputed", "reason": last.get("rationale", ""), "by": last.get("by", "")})
    return out


# --- P3'/P4 지식 dispute 입구: claim 텍스트에서 안정 id 파생 + '틀렸다' 반박 채널 ---
# create_knowledge는 claim을 '- 텍스트' 줄로만 append(개별 id 없음)라, dispute 함수가 참조할 id가 없었다.
# claim_id = 텍스트 정규화 해시로 파생 → 본문 불변, 이벤트로만 disputed 표시(harmful 패턴의 지식판).
def claim_id_for(text: str) -> str:
    import hashlib
    norm = re.sub(r"\s+", " ", str(text or "")).strip()
    return "claim_" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def list_claims(name_or_dir) -> list[dict]:
    """지식 본문의 claim 줄들을 파생 id·상태와 함께 반환(주입·dispute 참조용).

    **'사실' 섹션(## 범용 사실 / ## 조건부 사실)의 불릿만** claim으로 본다 — '## 참조 방법' 같은 산문
    섹션의 불릿을 사실로 오추출하지 않게(크로스체크 지적). status는 이벤트를 1회만 읽어 파생(claim마다 재파싱 방지).
    """
    kdir = _resolve_dir(name_or_dir)
    manifest = kdir / "지식.md"
    if not manifest.exists():
        return []
    # 이벤트 1회 읽어 claim_id별 최종 status 파생(마지막 이벤트 승 — claim_status와 동일 규칙).
    status_by_id: dict = {}
    for e in _read_events(kdir):
        cid = e.get("claim_id"); ev = e.get("event")
        if not cid:
            continue
        if ev == "dispute":
            status_by_id[cid] = "disputed"
        elif ev in ("verify", "resolve"):
            status_by_id[cid] = "active"
    out = []
    section = ""
    for line in manifest.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*##\s+(.*\S)\s*$", line)
        if m:
            section = m.group(1)
            continue
        if "사실" not in section:                          # 사실 섹션 불릿만 claim(산문 섹션 제외)
            continue
        cm = re.match(r"^\s*-\s+(.*\S)\s*$", line)
        if cm:
            text = cm.group(1)
            cid = claim_id_for(text)
            out.append({"claim_id": cid, "text": text, "section": section,
                        "status": status_by_id.get(cid, "provisional")})
    return out


def dispute(name_or_dir, *, claim_text: str = "", claim_id: str = "", by: str, rationale: str) -> dict:
    """'이 지식 사실이 틀렸다' 반박 채널. claim_text(본문 매칭) 또는 claim_id로 대상 지정.

    본문은 안 바꾸고 이벤트만(→ disputed). claim_text가 본문 claim과 안 맞으면 그 텍스트 해시로라도
    dispute를 남긴다(추적 보존). 매니저/대표 정정·에이전트 보고 공용 입구.
    """
    cid = str(claim_id or "").strip()
    if not cid:
        if not str(claim_text or "").strip():
            raise KnowledgeLedgerError("dispute는 claim_text 또는 claim_id 필요")
        # 본문에서 매칭되는 claim의 파생 id 우선, 없으면 텍스트 해시
        want = claim_id_for(claim_text)
        cid = want
        for c in list_claims(name_or_dir):
            if c["claim_id"] == want or claim_text.strip() in c["text"] or c["text"] in claim_text:
                cid = c["claim_id"]
                break
    return dispute_claim(name_or_dir, cid, by=by, rationale=rationale)
