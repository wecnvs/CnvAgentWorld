# -*- coding: utf-8 -*-
"""자원 발견: frontmatter description을 스캔해 후보를 만든다.

CLI 발견기와 엔진 훅이 같은 로직을 공유한다.
"""
import re
from pathlib import Path

from .paths import ROOT

SOURCES = {
    "skill": (ROOT / "스킬", "SKILL.md"),
    "knowledge": (ROOT / "지식", "지식.md"),
    "tool": (ROOT / "도구", "도구.md"),
    "asset": (ROOT / "자산", "자산.md"),
}


def parse_front(path: Path) -> dict:
    """매니페스트 맨 앞 --- ... --- frontmatter에서 주요 필드를 추출한다."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    front = {}
    for line in text[3:end].splitlines():
        match = re.match(r"\s*([A-Za-z_]+)\s*:\s*(.*)", line)
        if match:
            front[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return front


def types_for(kind: str = "all") -> list[str]:
    if kind == "all":
        return list(SOURCES)
    return [name for name in SOURCES if name == kind]


def collect(kinds: list[str]) -> list[dict]:
    items = []
    for kind in kinds:
        base, filename = SOURCES[kind]
        if not base.exists():
            continue
        for manifest in base.rglob(filename):
            front = parse_front(manifest)
            if not front.get("description"):
                continue
            item = {
                "type": kind,
                "name": front.get("name", manifest.parent.name),
                "path": str(manifest.relative_to(ROOT)),
                "description": front.get("description", ""),
                "entry": front.get("entry", ""),
                "runtime": front.get("runtime", ""),
            }
            if kind == "skill":
                _attach_skill_growth(item, manifest.parent)
            items.append(item)
    return items


def _attach_skill_growth(item: dict, sdir) -> None:
    """스킬 후보에 파생 성숙도 + 케이스 미리보기를 붙인다(표시만, 점수에는 영향 없음).

    케이스 시스템은 점진 도입이라, 케이스가 없거나 모듈 문제가 있어도 발견은 깨지지 않는다.
    """
    try:
        from . import case_ledger
        item["maturity"] = case_ledger.maturity(sdir)
        item["cases_preview"] = case_ledger.case_preview(sdir, limit=3)
        item["cases_avoid"] = case_ledger.case_negatives(sdir, limit=3)   # 이중 메모리: 하지 마라
    except Exception:
        pass


def score(query: str, item: dict) -> int:
    """겹치는 검색어 수로 점수화한다. 이름에 들어가면 가중한다."""
    terms = [w for w in re.split(r"[^0-9A-Za-z가-힣]+", query.lower()) if len(w) >= 2]
    if not terms:
        return 0
    name = item["name"].lower()
    text = (item["name"] + " " + item["description"]).lower()
    total = 0
    for term in terms:
        if term in name:
            total += 2
        elif term in text:
            total += 1
    return total


def find(query: str, kind: str = "all", top: int = 5) -> list[tuple[int, dict]]:
    kinds = types_for(kind)
    if not kinds:
        raise ValueError(f"알 수 없는 --type: {kind} (skill|knowledge|tool|asset|all)")
    ranked = sorted(((score(query, item), item) for item in collect(kinds)), key=lambda row: -row[0])
    return [(sc, item) for sc, item in ranked if sc > 0][:top]


def _maturity_summary(item: dict) -> str:
    """성숙도 한 줄 요약(파생 지표). 합산수치 대신 분해해 보여준다."""
    m = item.get("maturity")
    if not m:
        return ""
    if m.get("is_new"):
        return "성숙도: 신규(케이스 0 — 미검증)"
    parts = [f"케이스 {m.get('cases', 0)}"]
    if m.get("worked_ratio") is not None:
        parts.append(f"worked비율 {m['worked_ratio']}")
    if m.get("warn_harmful"):
        parts.append(f"⚠harmful {m.get('harmful', 0)}")
    if m.get("version"):
        parts.append(f"v{m['version']}")
    return "성숙도: " + ", ".join(parts)


def render_cli(query: str, hits: list[tuple[int, dict]]) -> str:
    if not hits:
        return f'후보 없음 — "{query}". 차선: 다른 표현으로 재검색하거나, 발견기 없이 직접 판단(law.md §6).'
    lines = [f'■ "{query}" 후보 {len(hits)}개 (점수순 — 이 중에서 골라 활용):']
    for score_value, item in hits:
        text = (
            f'  [{item["type"]}] {item["name"]}  (점수 {score_value})\n'
            f'      {item["description"]}\n'
            f'      경로: {item["path"]}'
        )
        if item["entry"]:
            text += f'\n      호출: {item["entry"]}  ({item["runtime"] or "?"})'
        summary = _maturity_summary(item)
        if summary:
            text += f'\n      {summary}'
        for case in item.get("cases_preview", []):
            text += f'\n      · ({case.get("polarity","")}) {case.get("condition","")} → {case.get("instruction","")}'
        for a in item.get("cases_avoid", []):
            text += f'\n      ⛔ 하지마라({a.get("strength","")}): {a.get("condition","")} → {a.get("instruction","")}'
        lines.append(text)
    return "\n".join(lines)


def render_context(query: str, hits: list[tuple[int, dict]]) -> str:
    lines = [
        "# 발견 후보 컨텍스트",
        "",
        f"- 요청 요지: {query}",
        "- 이 후보들은 시스템이 미리 찾은 참고 후보이다. 반드시 하나를 써야 하는 것은 아니다.",
        "- 후보들의 description을 비교해 필요한 스킬·지식·도구·자산만 선택한다.",
        "- 부족하면 직접 발견기를 다시 실행해도 된다.",
        "",
    ]
    if not hits:
        lines.append("(관련 후보 없음)")
        return "\n".join(lines)
    for idx, (score_value, item) in enumerate(hits, 1):
        lines.extend([
            f"## {idx}. [{item['type']}] {item['name']} (점수 {score_value})",
            f"- 경로: `{item['path']}`",
            f"- 설명: {item['description']}",
        ])
        if item["entry"]:
            lines.append(f"- 호출: `{item['entry']}` ({item['runtime'] or '?'})")
        summary = _maturity_summary(item)
        if summary:
            lines.append(f"- {summary}")
        preview = item.get("cases_preview", [])
        if preview:
            lines.append("- 케이스(경우의 수, 적용 여부는 condition 보고 판단):")
            for case in preview:
                lines.append(f"    · [{case.get('case_id','')}] ({case.get('polarity','')}) {case.get('condition','')} → {case.get('instruction','')}")
            lines.append("    (이 스킬의 케이스를 적용했다면 답변 끝 JSON에 case_applications:[{skill, case_id, applied, outcome:\"worked|harmful\"}]로 보고하라.)")
        avoid = item.get("cases_avoid", [])
        if avoid:
            lines.append("- ⛔ 하지 마라(부정 교훈 — 행동 전 반드시 확인, 이중 메모리):")
            for a in avoid:
                tag = {"must_avoid": "반드시 피하라", "avoid": "피하라", "caution": "주의"}.get(a.get("strength", ""), "주의")
                why = "격리된 모순" if a.get("kind") == "conflict" else "실패 사례"
                lines.append(f"    · [{tag}/{why}] {a.get('condition','')} → {a.get('instruction','')}")
        lines.append("")
    return "\n".join(lines).rstrip()
