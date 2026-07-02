# -*- coding: utf-8 -*-
"""신규 스킬 생성 + 중복/발견 점검 (P4).

설계: 루트폴더/설계_자기성장스킬시스템.md §4, 계약: 설계_P0_확정스키마.md §5.

원칙:
- 신규도 처음부터 실 스킬(draft 없음). 성숙도는 case_ledger의 파생 카운터로 드러난다.
- 중복 판정은 **어휘 발견기(discovery.score)에만 의존하지 않는다** — 동의어/패러프레이즈를 못 잡기 때문.
  엔진은 어휘 + 문자 n-gram 유사도를 *recall 신호*로 제공하고, 실제 '같은 스킬인가'의 의미 판단은
  에이전트가 description을 읽고 한다(P1: 의미 판단은 에이전트).
- 등급 = 배포 경계. 신규 기본값 추가, 식별정보 가능성 → 대외비. 기본 승격은 대표 확인(P5/배포).
"""
from __future__ import annotations

import hashlib
import re

from . import case_ledger, discovery
from .paths import ROOT
from .transcript import now_iso

SKILLS = case_ledger.SKILLS
GRADES = case_ledger.GRADES


class SkillSmithError(RuntimeError):
    """신규 스킬 생성/점검 계약 위반."""


# --------------------------------------------------------------------------- 유사도(신호용)
def _normalize(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(text or "").lower())


def _bigrams(text: str) -> set[str]:
    norm = _normalize(text)
    return {norm[i:i + 2] for i in range(len(norm) - 1)} if len(norm) >= 2 else {norm} if norm else set()


def _jaccard(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


def _grade_of(path: str) -> str:
    parts = str(path).split("/")
    return parts[1] if len(parts) > 1 else ""


# --------------------------------------------------------------------------- 점검
def list_skills() -> list[dict]:
    """모든 스킬의 name/description/grade/성숙도 간략 목록(에이전트가 의미 중복 판단할 입력)."""
    out = []
    for item in discovery.collect(["skill"]):
        out.append({
            "name": item["name"],
            "description": item["description"],
            "grade": _grade_of(item["path"]),
            "path": item["path"],
            "maturity": item.get("maturity", {}),
        })
    return out


def skill_detail(name: str) -> dict:
    """대시보드/검토용 스킬 상세. SKILL.md 원문과 frontmatter를 함께 반환한다."""
    name = str(name or "").strip()
    sdir = case_ledger.skill_dir(name)
    if not sdir:
        raise SkillSmithError(f"스킬 없음: {name}")
    manifest = sdir / "SKILL.md"
    try:
        content = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillSmithError(f"SKILL.md 읽기 실패: {type(exc).__name__}") from exc
    front = discovery.parse_front(manifest)
    return {
        "name": front.get("name", sdir.name),
        "description": front.get("description", ""),
        "grade": sdir.parent.name,
        "path": str(manifest.relative_to(ROOT)),
        "frontmatter": front,
        "maturity": case_ledger.maturity(sdir),
        "content": content,
    }


def find_similar_skills(queries, *, exclude: str = "", top: int = 5) -> list[dict]:
    """기존 스킬 중 새 작업과 비슷한 것을 recall 신호로 추린다(어휘 + 문자 n-gram).

    **이것은 신호다. '같은 스킬이다'의 확정은 에이전트가 description을 읽고 판단한다.**
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in (queries or []) if str(q).strip()]
    scored = []
    for item in discovery.collect(["skill"]):
        if item["name"] == exclude:
            continue
        text = item["name"] + " " + item["description"]
        lexical = max((discovery.score(q, item) for q in queries), default=0)
        ngram = max((_jaccard(q, text) for q in queries), default=0.0)
        combined = lexical + ngram * 5  # n-gram을 어휘보다 가중(패러프레이즈 recall)
        if combined <= 0:
            continue
        scored.append({
            "name": item["name"],
            "description": item["description"],
            "grade": _grade_of(item["path"]),
            "lexical": lexical,
            "ngram": round(ngram, 2),
            "signal": round(combined, 2),
        })
    scored.sort(key=lambda r: -r["signal"])
    return scored[:top]


def check_discoverable(name: str, queries, *, top: int = 3) -> dict:
    """발견 형식게이트: 이 스킬을 부를 표현들로 검색했을 때 top-N에 뜨는지(신호, 차단 아님).

    안 뜨면 description을 보강하라는 신호(P1 비위반 — 형식 점검).
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in (queries or []) if str(q).strip()]
    per_query = []
    for q in queries:
        hits = discovery.find(q, kind="skill", top=top)
        names = [item["name"] for _, item in hits]
        per_query.append({"query": q, "in_top": name in names, "top_names": names})
    return {
        "discoverable": bool(per_query) and all(r["in_top"] for r in per_query),
        "per_query": per_query,
    }


# --------------------------------------------------------------------------- 생성
def _skill_id(name: str) -> str:
    return "skill_" + hashlib.sha256(_normalize(name).encode("utf-8")).hexdigest()[:12]


def create_skill(name: str, *, description: str, body: str = "", grade: str = "추가",
                 skill_id: str = "", non_overridable: str = "", overwrite: bool = False) -> dict:
    """스킬 폴더 + SKILL.md를 생성한다(P0 frontmatter: skill_id·version·last_updated).

    - 신규 기본 등급 = 추가(배포 안 됨, 내부 사용). 식별정보면 호출자가 grade='대외비' 지정.
    - description은 발견용 — 반드시 채운다(없으면 거부). 발견 형식게이트는 호출측에서 check_discoverable로.
    """
    name = str(name or "").strip()
    if not name:
        raise SkillSmithError("스킬 이름 필수")
    if re.search(r"[/\\]", name):
        raise SkillSmithError("스킬 이름에 경로 구분자 금지")
    if not str(description or "").strip():
        raise SkillSmithError("description 필수(발견용 — 무엇을·언제·표현·핵심용어)")
    if grade not in GRADES:
        raise SkillSmithError(f"grade는 {list(GRADES)} 중 하나")

    sdir = SKILLS / grade / name
    manifest = sdir / "SKILL.md"
    if manifest.exists() and not overwrite:
        raise SkillSmithError(f"이미 존재: {manifest.relative_to(ROOT)} (overwrite=True로 덮어쓰기)")

    sid = skill_id or _skill_id(name)
    desc_one = re.sub(r"\s+", " ", str(description)).strip()

    front = (
        "---\n"
        f"skill_id: {sid}\n"
        f"name: {name}\n"
        f"description: {json_escape(desc_one)}\n"
        "version: 1\n"
        f"last_updated: {now_iso()}\n"
        "---\n"
    )
    scaffold = body.strip() or (
        f"# {name}\n\n"
        "## 언제 쓰나\n"
        f"- {desc_one}\n\n"
        "## 절차\n"
        "1. (범용 절차를 적는다 — 특정 인물·숫자·1회성은 본문에 박지 말고 케이스로)\n"
    )
    text = front + "\n" + scaffold + "\n"
    if non_overridable.strip():
        text += "\n## non_overridable\n" + non_overridable.strip() + "\n"

    sdir.mkdir(parents=True, exist_ok=True)
    manifest.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "skill_id": sid,
        "name": name,
        "grade": grade,
        "path": str(manifest.relative_to(ROOT)),
    }


def json_escape(value: str) -> str:
    import json
    return json.dumps(str(value), ensure_ascii=False)
