# -*- coding: utf-8 -*-
"""CaseLedger v1 — 스킬별 케이스(경우의 수) 읽기 + 파생 성숙도 + 자원 락.

설계: 루트폴더/설계_자기성장스킬시스템.md(v0.6), 계약: 루트폴더/설계_P0_확정스키마.md.
P1 범위 = 읽기 표면만. 케이스 등록/판단(쓰기)은 P2에서 추가한다.

스킬 폴더 레이아웃:
  스킬/{등급}/{이름}/
    SKILL.md          ← frontmatter(skill_id, version, last_updated ...)
    cases.jsonl       ← 공개안전 케이스(append-only)
    cases.local.jsonl ← 식별정보 포함 케이스 + raw audit (gitignore)
    case_events.jsonl ← 적용 결과/카운터 이벤트(gitignore)
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .paths import ROOT
from .transcript import now_iso

SKILLS = ROOT / "스킬"
GRADES = ("기본", "추가", "고급", "대외비")

CASES_FILE = "cases.jsonl"
CASES_LOCAL_FILE = "cases.local.jsonl"
CASE_EVENTS_FILE = "case_events.jsonl"
LOCK_FILE = ".case_ledger.lock"

# 케이스가 "죽은" 상태 — 주입·성숙도 집계에서 제외한다.
DEAD_STATUSES = {"superseded", "retired", "expired", "graduated"}
# 주입 대상 상태.
INJECTABLE_STATUSES = {"active", "provisional_must", "candidate"}

MAX_INSTRUCTION_CHARS = 800
MAX_CONDITION_CHARS = 300


class CaseLedgerError(RuntimeError):
    """케이스 원장 계약을 만족하지 못했다."""


# --------------------------------------------------------------------------- 경로
def skill_dir(name: str) -> Path | None:
    """등급 폴더들을 뒤져 이름이 일치하는 스킬 폴더를 찾는다(없으면 None)."""
    for grade in GRADES:
        candidate = SKILLS / grade / name
        if candidate.is_dir():
            return candidate
    return None


def _cases_path(sdir: Path) -> Path:
    return sdir / CASES_FILE


def _cases_local_path(sdir: Path) -> Path:
    return sdir / CASES_LOCAL_FILE


def _events_path(sdir: Path) -> Path:
    return sdir / CASE_EVENTS_FILE


def _lock_path(sdir: Path) -> Path:
    return sdir / LOCK_FILE


# --------------------------------------------------------------------------- 락
def with_resource_lock(sdir: Path, fn):
    """스킬 폴더 단위 직렬화. 전역 자원(스킬)을 여러 주체가 동시 쓰는 것을 막는다."""
    sdir.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(sdir)
    lock.touch(exist_ok=True)
    with lock.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- 공용 헬퍼
def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _append_jsonl(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 읽기
def _read_jsonl(path: Path) -> tuple[list[dict], str]:
    if not path.exists():
        return [], ""
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], f"{path.name}: {type(exc).__name__}"
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, (f"{path.name}: invalid_json_lines={bad}" if bad else "")


def _latest_by_id(rows: list[dict], id_field: str) -> list[dict]:
    """같은 id의 마지막 줄만 남긴다(append-only + supersede 멱등)."""
    latest: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = str(row.get(id_field) or "")
        if not key:
            key = f"__anon_{len(order)}"
        if key not in latest:
            order.append(key)
        latest[key] = row
    return [latest[k] for k in order]


def read_cases(sdir: Path, *, include_local: bool = True) -> list[dict]:
    """cases.jsonl(+ cases.local.jsonl)의 최신 케이스. 죽은 상태는 제외하지 않는다(호출측 판단)."""
    rows, err = _read_jsonl(_cases_path(sdir))
    if err:
        raise CaseLedgerError(err)
    if include_local:
        local_rows, local_err = _read_jsonl(_cases_local_path(sdir))
        if local_err:
            raise CaseLedgerError(local_err)
        rows = rows + local_rows
    return _latest_by_id(rows, "case_id")


def read_events(sdir: Path) -> list[dict]:
    rows, err = _read_jsonl(_events_path(sdir))
    if err:
        raise CaseLedgerError(err)
    return rows


def _live_cases(cases: list[dict]) -> list[dict]:
    return [c for c in cases if c.get("status") not in DEAD_STATUSES]


# --------------------------------------------------------------------------- 파생 성숙도
def _parse_front(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    front: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = re.match(r"\s*([A-Za-z_]+)\s*:\s*(.*)", line)
        if match:
            front[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return front


def maturity(sdir: Path) -> dict:
    """발견/선택 시 신뢰도를 가늠하는 파생 지표. 저장하지 않고 매번 계산한다.

    합산 단일수치(update_count) 금지 — 방향(worked_ratio)·경고(harmful)·신선도로 분해 노출.
    """
    try:
        cases = _live_cases(read_cases(sdir))
    except CaseLedgerError:
        cases = []
    try:
        events = read_events(sdir)
    except CaseLedgerError:
        events = []
    worked = sum(1 for e in events if e.get("event") == "worked")
    harmful = sum(1 for e in events if e.get("event") == "harmful")
    front = _parse_front(sdir / "SKILL.md")
    decided = worked + harmful
    return {
        "cases": len(cases),
        "worked": worked,
        "harmful": harmful,
        "worked_ratio": (round(worked / decided, 2) if decided else None),
        "is_new": len(cases) == 0,
        "warn_harmful": harmful > 0,
        "version": front.get("version", ""),
        "last_updated": front.get("last_updated", ""),
    }


def case_preview(sdir: Path, *, limit: int = 3) -> list[dict]:
    """발견 시 함께 보여줄 **긍정** 케이스 미리보기(worked, 표시용 — 주입 게이트 아님).

    부정 교훈(failed/conflict)은 섞지 않고 case_negatives()로 따로 노출한다(이중 메모리, §9.1).
    """
    try:
        cases = _live_cases(read_cases(sdir))
    except CaseLedgerError:
        return []
    try:
        cmap = confidence_map(sdir)
    except CaseLedgerError:
        cmap = {}
    cases = [c for c in cases if c.get("polarity") == "worked" and c.get("status") != "conflict"]
    cases.sort(key=lambda c: _case_priority(c, cmap.get(c.get("case_id", ""))), reverse=True)
    out = []
    for case in cases[:limit]:
        out.append({
            "case_id": case.get("case_id", ""),
            "condition": str(case.get("condition") or "")[:MAX_CONDITION_CHARS],
            "instruction": str(case.get("instruction") or "")[:160],
            "polarity": case.get("polarity", ""),
            "status": case.get("status", ""),
            "confidence": cmap.get(case.get("case_id", "")),
        })
    return out


# --------------------------------------------------------------------------- 부정 교훈 / 이중 메모리 (§9.1)
# 나쁜 케이스는 삭제·병합 말고 '이렇게 하지 마라' 참조로 노출해 행동 전 읽힌다(A-MemGuard, Zombie Agents).
AVOID_STRENGTH_RANK = {"must_avoid": 3, "avoid": 2, "caution": 1}


def _avoid_strength(case: dict, kind: str) -> str:
    """부정 교훈의 강도. failed+확정+must=반드시피하라 / failed+확정=피하라 / 그 외(미검증·격리)=주의."""
    if kind == "conflict":
        return "caution"                                    # 격리=모순중 — 단정 금지, 주의만
    if case.get("status") in {"active", "provisional_must"}:
        return "must_avoid" if _is_must(case) else "avoid"
    return "caution"                                        # candidate failed = 미검증


def _avoid_view(case: dict, kind: str) -> dict:
    return {
        "case_id": case.get("case_id", ""),
        "condition": str(case.get("condition") or "")[:MAX_CONDITION_CHARS],
        "instruction": str(case.get("instruction") or "")[:160],
        "polarity": case.get("polarity", ""),
        "status": case.get("status", ""),
        "kind": kind,                                       # failed | conflict
        "strength": _avoid_strength(case, kind),
    }


def _collect_negatives(cases: list[dict]) -> list[tuple[dict, str]]:
    """부정 교훈 후보: failed(live·주입가능) + conflict(격리). DEAD는 제외."""
    negs = []
    for c in cases:
        status = c.get("status", "")
        if status in DEAD_STATUSES:
            continue
        if status == "conflict":
            negs.append((c, "conflict"))
        elif c.get("polarity") == "failed" and status in INJECTABLE_STATUSES:
            negs.append((c, "failed"))
    return negs


def case_negatives(sdir: Path, *, limit: int = 3) -> list[dict]:
    """'하지 마라' 부정 교훈(이중 메모리) — failed + conflict 케이스를 강도순으로. 행동 전 참조용."""
    try:
        cases = read_cases(sdir)
    except CaseLedgerError:
        return []
    try:
        cmap = confidence_map(sdir)
    except CaseLedgerError:
        cmap = {}
    negs = _collect_negatives(cases)
    negs.sort(key=lambda pair: (AVOID_STRENGTH_RANK.get(_avoid_strength(*pair), 0),
                                cmap.get(pair[0].get("case_id", ""), 0.0)), reverse=True)
    return [_avoid_view(c, kind) for c, kind in negs[:limit]]


# --------------------------------------------------------------------------- 우선순위/매칭 (키워드 게이트 없음)
def _is_must(case: dict) -> bool:
    return bool(
        case.get("must_apply")
        or case.get("application_level") == "must_apply"
        or case.get("enforcement") == "must_apply"
    )


def _specificity(case: dict) -> int:
    """applies_when/does_not_apply_when에 걸린 조건 수 = 특수도. more-specific-wins 정렬 힌트(P1: 정렬은 신호).

    더 특수한(조건을 더 좁힌) 케이스가 일반 케이스보다 먼저 오게 한다 — 일반은 더 특수한 게 없을 때 적용된다는
    원칙(Delgrande-Schaub 1997)을 *선별 순서*로 근사. 적용 여부 자체는 여전히 에이전트가 condition 읽고 판단.
    """
    aw = case.get("applies_when") or {}
    score = 0
    for key in ("task_types", "agent_modes", "resource_paths", "keywords"):
        val = aw.get(key) or []
        score += len(val) if isinstance(val, (list, tuple)) else (1 if val else 0)
    if aw.get("space_id"):
        score += 1
    dna = case.get("does_not_apply_when") or []
    score += len(dna) if isinstance(dna, (list, tuple)) else (1 if dna else 0)
    return score


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _case_priority(case: dict, confidence: float | None = None) -> tuple:
    status = case.get("status", "")
    status_score = 2 if status in {"active", "provisional_must"} else 1 if status == "candidate" else 0
    must_score = 3 if status in {"active", "provisional_must"} and _is_must(case) else 0
    evidence = case.get("evidence_level", "")
    evidence_score = 1 if evidence in {"user_directive", "verified_result", "reviewer_approval"} else 0
    conf = confidence if confidence is not None else _to_float(case.get("confidence", 0))
    # more-specific-wins: 같은 등급이면 더 특수한 케이스가 먼저(reverse 정렬에서 상위).
    return (must_score + status_score + evidence_score, _specificity(case), conf, str(case.get("created_at", "")))


def _scope_matches(case: dict, space: str) -> bool:
    """scope=space면 origin/space_id가 현재 공간일 때만. global이면 항상.

    주의: 키워드(applies_when.keywords) 부분일치로는 절대 거르지 않는다(P1: 기계적 금지).
    어떤 케이스가 지금 상황에 맞는지는 에이전트가 condition을 읽고 판단한다.
    """
    if case.get("scope") == "space":
        applies = case.get("applies_when") or {}
        origin = case.get("origin_space") or applies.get("space_id") or ""
        return (not space) or (not origin) or origin == space
    return True


def _mode_matches(case: dict, mode: str) -> bool:
    modes = (case.get("applies_when") or {}).get("agent_modes") or []
    return not modes or mode in modes


def _case_view(case: dict, confidence: float | None = None) -> dict:
    return {
        "case_id": case.get("case_id", ""),
        "condition": str(case.get("condition") or "")[:MAX_CONDITION_CHARS],
        "instruction": str(case.get("instruction") or "")[:MAX_INSTRUCTION_CHARS],
        "polarity": case.get("polarity", ""),
        "status": case.get("status", ""),
        "must_apply": _is_must(case),
        "evidence_level": case.get("evidence_level", ""),
        # confidence는 파생값(독립확인자·harmful·충돌이웃·감쇠). 저장값은 초기 prior일 뿐.
        "confidence": confidence if confidence is not None else _to_float(case.get("confidence", 0)),
        "sensitivity": case.get("sensitivity", "public"),
    }


def build_case_pack(
    name_or_dir,
    *,
    space: str = "",
    mode: str = "",
    target_agent: str = "",
    max_cases: int = 8,
    max_must: int = 3,
    max_avoid: int = 4,
) -> dict:
    """스킬 X 사용 시 함께 줄 케이스를 추린다.

    선별 = scope/mode 매칭 + 우선순위 정렬 + 예산.
    **이중 메모리(§9.1):** worked는 긍정 3단(must/may/reference), failed·conflict는 `avoid`(부정 교훈).
    키워드 게이트는 쓰지 않는다(설계 §5, P1). 적용 여부는 에이전트가 condition을 읽고 판단.
    """
    sdir = name_or_dir if isinstance(name_or_dir, Path) else skill_dir(str(name_or_dir))
    base = {
        "schema": "CasePack.v1",
        "skill": (sdir.name if isinstance(sdir, Path) else str(name_or_dir)),
        "case_pack_status": "ok",
        "must_apply": [],
        "may_apply": [],
        "reference_only": [],
        "avoid": [],
        "included_case_ids": [],
        "excluded": [],
        "max_cases": max_cases,
        "max_must": max_must,
        "max_avoid": max_avoid,
        "errors": [],
    }
    if not sdir or not sdir.is_dir():
        base["case_pack_status"] = "skill_not_found"
        return base
    try:
        cases = read_cases(sdir)
    except CaseLedgerError as exc:
        base["case_pack_status"] = "unavailable"
        base["errors"] = [str(exc)]
        return base
    try:
        cmap = confidence_map(sdir)
    except CaseLedgerError:
        cmap = {}

    candidates = []
    negatives = []
    excluded = []
    for case in cases:
        status = case.get("status", "")
        is_neg_conflict = status == "conflict"
        is_neg_failed = case.get("polarity") == "failed" and status in INJECTABLE_STATUSES
        if is_neg_conflict or is_neg_failed:
            if not _scope_matches(case, space) or not _mode_matches(case, mode):
                excluded.append({"case_id": case.get("case_id", ""), "reason": "scope/mode"})
                continue
            negatives.append((case, "conflict" if is_neg_conflict else "failed"))
            continue
        if status not in INJECTABLE_STATUSES:
            excluded.append({"case_id": case.get("case_id", ""), "reason": f"status:{status or 'unknown'}"})
            continue
        if not _scope_matches(case, space):
            excluded.append({"case_id": case.get("case_id", ""), "reason": "scope_mismatch"})
            continue
        if not _mode_matches(case, mode):
            excluded.append({"case_id": case.get("case_id", ""), "reason": "mode_mismatch"})
            continue
        candidates.append(case)
    candidates.sort(key=lambda c: _case_priority(c, cmap.get(c.get("case_id", ""))), reverse=True)
    # 부정 교훈(이중 메모리): 강도순 → confidence순, 예산 컷
    negatives.sort(key=lambda pair: (AVOID_STRENGTH_RANK.get(_avoid_strength(*pair), 0),
                                     cmap.get(pair[0].get("case_id", ""), 0.0)), reverse=True)
    for case, kind in negatives[:max_avoid]:
        base["avoid"].append(_avoid_view(case, kind))
    for case, kind in negatives[max_avoid:]:
        excluded.append({"case_id": case.get("case_id", ""), "reason": "avoid_budget"})

    must = [c for c in candidates if c.get("status") in {"active", "provisional_must"} and _is_must(c)]
    must_kept = must[:max_must]
    must_ids = {c.get("case_id", "") for c in must_kept}
    for c in must[max_must:]:
        excluded.append({"case_id": c.get("case_id", ""), "reason": "must_budget"})

    included = list(must_kept)
    for case in candidates:
        cid = case.get("case_id", "")
        if cid in must_ids or any(cid == c.get("case_id", "") for c in must):
            continue
        if len(included) >= max_cases:
            excluded.append({"case_id": cid, "reason": "case_budget"})
            continue
        included.append(case)

    for case in included:
        view = _case_view(case, cmap.get(case.get("case_id", "")))
        if case.get("status") in {"active", "provisional_must"} and _is_must(case):
            base["must_apply"].append(view)
        elif case.get("status") in {"active", "provisional_must"}:
            base["may_apply"].append(view)
        else:
            base["reference_only"].append(view)
    base["included_case_ids"] = [c.get("case_id", "") for c in included if c.get("case_id")]
    base["excluded"] = excluded[:20]
    return base


# --------------------------------------------------------------------------- 쓰기 (P2: 발의/판단)
REQUIRED_JUDGMENT_FIELDS = ("polarity", "action", "routing_kind", "judgment_rationale", "source_quote")
VALID_POLARITY = {"worked", "failed"}
VALID_ACTION = {"add_case", "supersede", "new_skill", "none"}
VALID_ROUTING = {"procedural", "factual", "preference"}
VALID_SENSITIVITY = {"public", "confidential"}
VALID_EVENTS = {"applied", "worked", "harmful", "flagged", "review_due", "expired"}
WRITE_ACTIONS = {"add_case", "supersede"}   # propose_case가 직접 처리하는 행동


def _target_cases_path(sdir: Path, sensitivity: str) -> Path:
    """민감도로 저장 파일을 가른다. confidential은 항상 사이드카(배포 제외).

    이로써 '기본 등급 cases.jsonl엔 confidential 못 들어감' 불변식이 구조적으로 성립한다.
    """
    return _cases_local_path(sdir) if sensitivity == "confidential" else _cases_path(sdir)


def _read_with_origin(sdir: Path) -> list[tuple[dict, Path]]:
    out: list[tuple[dict, Path]] = []
    for path in (_cases_path(sdir), _cases_local_path(sdir)):
        rows, err = _read_jsonl(path)
        if err:
            raise CaseLedgerError(err)
        for row in rows:
            out.append((row, path))
    return out


def _validate_judgment(candidate: dict) -> None:
    """P1 계약: 의미 변경은 에이전트 판단 산출물이 있을 때만. 4(+1)필드 강제."""
    missing = [f for f in REQUIRED_JUDGMENT_FIELDS if not str(candidate.get(f) or "").strip()]
    if missing:
        raise CaseLedgerError(f"판단 계약 누락: {', '.join(missing)} (키워드 자동등록 금지 — 에이전트 판단 필수)")
    if candidate["polarity"] not in VALID_POLARITY:
        raise CaseLedgerError(f"polarity는 {sorted(VALID_POLARITY)} 중 하나여야 함")
    if candidate["action"] not in VALID_ACTION:
        raise CaseLedgerError(f"action은 {sorted(VALID_ACTION)} 중 하나여야 함")
    if candidate["routing_kind"] not in VALID_ROUTING:
        raise CaseLedgerError(f"routing_kind는 {sorted(VALID_ROUTING)} 중 하나여야 함")
    if not str(candidate.get("instruction") or "").strip():
        raise CaseLedgerError("instruction 필수")


# --------------------------------------------------------------------------- 모순 자동 격리 (§9.1)
# 새 케이스가 기존 *확정* 케이스(active/provisional_must)와 모순되면 자동으로 active로 올리지 않는다.
# 리서치 교훈: 최신우선(recency-wins)은 안티패턴 — 새 것이 자동으로 이기지 않고 conflict로 격리해
# 사람/에이전트가 범인을 고른다. 병합·삭제 금지, 격리(>삭제)로 롤백 여지를 남긴다(설계_케이스오염안전 §4).
_CONFLICT_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
CONFLICT_COMMITTED_STATUSES = {"active", "provisional_must"}


def _condition_tokens(text: str) -> set[str]:
    return {t.lower() for t in _CONFLICT_TOKEN_RE.findall(str(text or "")) if len(t) >= 2}


def _conditions_overlap(a: str, b: str) -> bool:
    """두 조건 자연어가 *신호 수준*에서 겹치는지(완전한 의미판정 아님 — 과검출=안전 방향).

    동의어·패러프레이즈는 못 잡는다(P1 경고): 이건 기계적 안전망(바닥)이고,
    등록 전 에이전트의 모순 read(설계 §3.2)가 1차 의미 방어다. 자카드≥0.5 또는 좁은 조건의 80%+ 포함.
    """
    ta, tb = _condition_tokens(a), _condition_tokens(b)
    if not ta or not tb:
        return False
    inter = ta & tb
    if not inter:
        return False
    if len(inter) / len(ta | tb) >= 0.5:
        return True
    smaller = ta if len(ta) <= len(tb) else tb
    return len(inter) / len(smaller) >= 0.8


def _opposite_polarity(a: str, b: str) -> bool:
    return a in VALID_POLARITY and b in VALID_POLARITY and a != b


def _scopes_can_cooccur(a: dict, b: dict) -> bool:
    """두 케이스가 같은 상황에 함께 적용될 수 있나(둘 다 같은 공간 한정이면 같은 공간일 때만 충돌)."""
    if a.get("scope") == "space" and b.get("scope") == "space":
        oa, ob = a.get("origin_space") or "", b.get("origin_space") or ""
        return (not oa) or (not ob) or oa == ob
    return True


def find_conflicting_cases(pool, *, condition: str, polarity: str,
                           declared=None, exclude=None) -> list[str]:
    """새 케이스와 모순되는 *확정 케이스* id 목록(신호). 두 경로:
    ① 에이전트가 conflicts_with로 명시 선언(상태 무관 존중) ② 반대 polarity + 조건 겹침(기계적 바닥).
    """
    declared = {str(x) for x in (declared or set()) if x}
    exclude = exclude or set()
    out: list[str] = []
    for c in pool:
        cid = str(c.get("case_id") or "")
        if not cid or cid in exclude:
            continue
        if cid in declared:                       # 에이전트 명시 선언 — 상태 무관 존중
            out.append(cid)
            continue
        if c.get("status") not in CONFLICT_COMMITTED_STATUSES:
            continue
        if _opposite_polarity(polarity, str(c.get("polarity") or "")) and \
                _conditions_overlap(condition, str(c.get("condition") or "")):
            out.append(cid)
    return out


def propose_case(name_or_dir, candidate: dict, *, proposed_by: str = "", from_daepyo: bool = False, skill_id: str = "") -> dict:
    """스킬에 케이스를 발의(등록)한다.

    - 누구나 발의 가능(P0). identity는 자동승인 여부만 가른다: 대표 발의 = 즉시 provisional_must(must),
      그 외 = candidate(수렴 대기, 확정은 P3).
    - 게이트: 판단 4필드 계약 + 민감도(confidential→사이드카, 미판단=confidential) + 자원락 직렬화 + supersede 대상 검증.
    - routing_kind=factual|preference는 지식 대상(P5)이라 거부. 스킬 케이스는 procedural만.
    """
    sdir = name_or_dir if isinstance(name_or_dir, Path) else skill_dir(str(name_or_dir))
    if not sdir or not sdir.is_dir():
        raise CaseLedgerError(f"스킬 없음: {name_or_dir}")
    _validate_judgment(candidate)
    action = candidate["action"]
    if action not in WRITE_ACTIONS:
        raise CaseLedgerError(
            f"propose_case는 add_case|supersede만 처리(요청: {action}). none=발의 안함, new_skill=스킬 생성 경로(P4)"
        )
    if candidate["routing_kind"] != "procedural":
        raise CaseLedgerError(f"routing_kind={candidate['routing_kind']}는 지식 대상(P5). 스킬 케이스는 procedural만")

    sensitivity = candidate.get("sensitivity") or "confidential"   # 보수적 기본값
    if sensitivity not in VALID_SENSITIVITY:
        sensitivity = "confidential"

    front = _parse_front(sdir / "SKILL.md")
    resolved_skill_id = skill_id or candidate.get("skill_id") or front.get("skill_id") or sdir.name

    def mutate():
        latest: dict[str, tuple[dict, Path]] = {}
        for row, path in _read_with_origin(sdir):
            cid = row.get("case_id")
            if cid:
                latest[cid] = (row, path)

        supersedes = candidate.get("supersedes") or []
        if isinstance(supersedes, str):
            supersedes = [supersedes]
        if action == "supersede" and not supersedes:
            raise CaseLedgerError("action=supersede인데 supersedes(대상 case_id)가 없음")

        condition = str(candidate.get("condition") or "")
        instruction = str(candidate.get("instruction") or "")
        polarity = candidate["polarity"]
        case_id = candidate.get("case_id") or _stable_id("case", resolved_skill_id, condition, instruction, polarity)

        must = bool(from_daepyo)
        status = "provisional_must" if from_daepyo else "candidate"
        evidence_level = candidate.get("evidence_level") or ("user_directive" if from_daepyo else "agent_observation")

        record = {
            "schema": "CaseLedger.v1",
            "case_id": case_id,
            "skill_id": resolved_skill_id,
            "condition": condition,
            "instruction": instruction,
            "polarity": polarity,
            "applies_when": candidate.get("applies_when") or {
                "space_id": candidate.get("origin_space", ""),
                "task_types": [], "agent_modes": [], "resource_paths": [], "keywords": [],
            },
            "does_not_apply_when": candidate.get("does_not_apply_when") or [],
            "conflicts_with": candidate.get("conflicts_with") or [],
            "supersedes": supersedes,
            "action": action,
            "routing_kind": "procedural",
            "judgment_rationale": str(candidate.get("judgment_rationale") or ""),
            "source_quote": str(candidate.get("source_quote") or "")[:480],
            "sensitivity": sensitivity,
            "proposed_by": proposed_by or candidate.get("proposed_by", ""),
            "approved_by": ("대표" if from_daepyo else ""),
            "origin_space": candidate.get("origin_space", ""),
            "scope": candidate.get("scope") or ("space" if candidate.get("origin_space") else "global"),
            "status": status,
            "application_level": "must_apply" if must else "may_apply",
            "must_apply": must,
            "enforcement": "must_apply" if must else "",
            "evidence_level": evidence_level,
            "confidence": float(candidate.get("confidence", 0.7 if from_daepyo else 0.5)),
            "review_due_at_utc": candidate.get("review_due_at_utc", ""),
            "valid_until_utc": candidate.get("valid_until_utc", ""),
            "stale_after_at_utc": candidate.get("stale_after_at_utc", ""),
            "last_used_at_utc": "",
            "last_verified_at_utc": "",
            "created_at": now_iso(),
        }

        for old_id in supersedes:
            if old_id not in latest:
                raise CaseLedgerError(f"supersede 대상 case_id 없음: {old_id}")
            old_row, old_path = latest[old_id]
            marked = dict(old_row)
            marked["status"] = "superseded"
            marked["superseded_by"] = case_id
            marked["superseded_at_utc"] = now_iso()
            _append_jsonl(old_path, marked)

        # --- 모순 자동 격리(§9.1) — add_case만(supersede는 에이전트가 이미 명시 해소한 경로) ---
        if action == "add_case":
            declared = candidate.get("conflicts_with") or []
            if isinstance(declared, str):
                declared = [declared]
            pool = []
            for cid, (row, _p) in latest.items():
                if cid == case_id or cid in supersedes:
                    continue
                row_skill = row.get("skill_id")
                if row_skill and resolved_skill_id and row_skill != resolved_skill_id:
                    continue
                if not _scopes_can_cooccur(record, row):
                    continue
                pool.append(row)
            conflict_ids = find_conflicting_cases(
                pool, condition=condition, polarity=polarity, declared=declared)
            if conflict_ids:
                record["conflicts_with"] = sorted(set(record.get("conflicts_with") or []) | set(conflict_ids))
                if from_daepyo:
                    # 대표=사람=최종권위(요구5): 새 지시는 적용하되, 모순되는 기존 케이스를 조용히
                    # recency로 덮지 않고 conflict로 격리(삭제 아님·롤백가능)해 review_queue로 노출한다.
                    for old_id in conflict_ids:
                        old_row = latest[old_id][0]
                        if old_row.get("status") in DEAD_STATUSES:
                            continue
                        prev = set(old_row.get("conflicts_with") or [])
                        _set_status(sdir, old_id, "conflict", by=(proposed_by or "대표"),
                                    reason=f"대표 신규지시와 모순 — 격리(해소필요), superseding={case_id}",
                                    extra={"conflicts_with": sorted(prev | {case_id}),
                                           "pre_conflict_status": old_row.get("status", "")})
                else:
                    # 비대표(미검증) 발의: 새 것을 자동으로 살리지 않고 conflict로 격리(주입 제외).
                    record["status"] = "conflict"
                    record["must_apply"] = False
                    record["application_level"] = "may_apply"
                    record["enforcement"] = ""
                    record["auto_quarantined"] = True
                    record["pre_conflict_status"] = "candidate"   # 분기 해소 시 복귀 기본값

        _append_jsonl(_target_cases_path(sdir, sensitivity), record)
        return record

    return with_resource_lock(sdir, mutate)


def record_case_event(name_or_dir, case_id: str, event: str, *, by: str = "", rationale: str = "", skill_id: str = "") -> dict:
    """케이스 적용 결과/카운터 이벤트(applied/worked/harmful/...)를 append. 가변 카운터는 여기 집계."""
    sdir = name_or_dir if isinstance(name_or_dir, Path) else skill_dir(str(name_or_dir))
    if not sdir or not sdir.is_dir():
        raise CaseLedgerError(f"스킬 없음: {name_or_dir}")
    if event not in VALID_EVENTS:
        raise CaseLedgerError(f"event는 {sorted(VALID_EVENTS)} 중 하나여야 함")

    def mutate():
        _append_jsonl(_events_path(sdir), {
            "schema": "CaseEvent.v1",
            "case_id": case_id,
            "skill_id": skill_id,
            "event": event,
            "by": by,
            "rationale": rationale,
            "at_utc": now_iso(),
        })
        return {"ok": True}

    return with_resource_lock(sdir, mutate)


# --------------------------------------------------------------------------- 수렴·보안 (P3)
DEFAULT_CONFIRM_THRESHOLD = 2   # worked_threshold 경로: worked N회 + harmful 0 → active
DEFAULT_CANDIDATE_TTL_DAYS = 14  # 비대표 candidate가 traction 없이 만료되는 기간


def _resolve_dir(name_or_dir) -> Path:
    sdir = name_or_dir if isinstance(name_or_dir, Path) else skill_dir(str(name_or_dir))
    if not sdir or not sdir.is_dir():
        raise CaseLedgerError(f"스킬 없음: {name_or_dir}")
    return sdir


def worked_harmful_counts(name_or_dir, case_id: str) -> tuple[int, int]:
    sdir = _resolve_dir(name_or_dir)
    events = read_events(sdir)
    worked = sum(1 for e in events if e.get("case_id") == case_id and e.get("event") == "worked")
    harmful = sum(1 for e in events if e.get("case_id") == case_id and e.get("event") == "harmful")
    return worked, harmful


def worked_distinct_sources(name_or_dir, case_id: str) -> set[str]:
    """이 케이스를 'worked'로 보고한 서로 다른 출처(by) 집합. 빈 출처는 'unknown'으로 묶는다.

    §9.1 오염 방지: 자동 승격은 '횟수'가 아니라 '서로 다른 확인자 수'로 판단해야 한다 —
    한 에이전트가 자기 케이스를 여러 번 worked 해도 1명이다(자기강화 루프 차단)."""
    sdir = _resolve_dir(name_or_dir)
    srcs = set()
    for e in read_events(sdir):
        if e.get("case_id") == case_id and e.get("event") == "worked":
            srcs.add(str(e.get("by") or "").strip() or "unknown")
    return srcs


# --------------------------------------------------------------------------- 평가자 신뢰성 / 사이코펀시 (§9.1)
# worked/harmful 신호의 출처('by')별 신뢰도. P1: 신호일 뿐 — 자동 blame/삭제 안 함.
# 리서치 함정: 평가자가 노이즈면 다수결도 무력. 그러나 평가자가 멀쩡한데 blame하면 해롭다
#   → 결과 기반으로만 보수적으로 본다: "그가 worked로 확인한 케이스가 나중에 harmful/모순으로 드러난 비율".
RELIABILITY_FLOOR = 0.5        # 이 미만 평가자는 자동승격 독립확인자로 인정하지 않는다(사이코펀시 누적 차단)
SYCOPHANCY_MIN_WORKED = 3      # worked ≥ 이 값 + harmful 0 + 나쁜확인 ≥1 → 사이코펀시 깃발(항상 동의·일부 실패)


def evaluator_reliability(name_or_dir, *, now: str | None = None) -> dict:
    """평가자(by)별 신뢰도 신호. by -> {worked, harmful, worked_cases, bad_confirmations, reliability, sycophancy_flag}.

    reliability = 1 - (그가 worked한 케이스 중 harmful>0 또는 conflict로 드러난 수)/(그가 worked한 케이스 수).
    무근거(worked 0)는 1.0(무죄 추정). 자동 차단이 아니라 confidence 가중·자동승격 인정 여부의 *신호*.
    """
    sdir = _resolve_dir(name_or_dir)
    events = read_events(sdir)
    cases = {c.get("case_id", ""): c for c in read_cases(sdir)}
    harmful_total: dict[str, int] = {}
    for e in events:
        if e.get("event") == "harmful":
            harmful_total[e.get("case_id", "")] = harmful_total.get(e.get("case_id", ""), 0) + 1
    worked_cases: dict[str, set] = {}
    worked_votes: dict[str, int] = {}
    harmful_votes: dict[str, int] = {}
    for e in events:
        by = str(e.get("by") or "").strip() or "unknown"
        cid = e.get("case_id", "")
        if e.get("event") == "worked":
            worked_cases.setdefault(by, set()).add(cid)
            worked_votes[by] = worked_votes.get(by, 0) + 1
        elif e.get("event") == "harmful":
            harmful_votes[by] = harmful_votes.get(by, 0) + 1
    out: dict[str, dict] = {}
    for by, cids in worked_cases.items():
        bad = sum(1 for cid in cids
                  if harmful_total.get(cid, 0) > 0 or cases.get(cid, {}).get("status") == "conflict")
        n = len(cids)
        reliability = 1.0 if n == 0 else round(max(0.0, 1.0 - bad / n), 3)
        syco = worked_votes.get(by, 0) >= SYCOPHANCY_MIN_WORKED and harmful_votes.get(by, 0) == 0 and bad >= 1
        out[by] = {
            "worked": worked_votes.get(by, 0), "harmful": harmful_votes.get(by, 0),
            "worked_cases": n, "bad_confirmations": bad,
            "reliability": reliability, "sycophancy_flag": syco,
        }
    for by, h in harmful_votes.items():                      # harmful만 낸 평가자도 정보용 포함
        out.setdefault(by, {"worked": 0, "harmful": h, "worked_cases": 0,
                            "bad_confirmations": 0, "reliability": 1.0, "sycophancy_flag": False})
    return out


def _reliability_of(rel: dict, source: str) -> float:
    return float(rel.get(source, {}).get("reliability", 1.0))


# --------------------------------------------------------------------------- 신뢰도 비균일 감쇠 (§9.1)
# confidence는 저장된 정적값이 아니라 신호에서 **매번 파생**한다(P1: 정렬·표시 신호일 뿐 자동 전이 트리거 아님).
# 합산 단일카운터(update_count) 금지 — 나쁜 신호엔 내려가는 비균일 점수: 독립확인자↑·harmful↓강·충돌이웃↓·시간감쇠↓.
CONFIDENCE_HALFLIFE_DAYS = 120.0
_EVIDENCE_BASE = {
    "user_directive": 0.70,
    "reviewer_approval": 0.65,
    "verified_result": 0.60,
    "agent_observation": 0.45,
}


def _time_decay(anchor_iso: str, now_dt: datetime) -> float:
    """최근 신선도 기준(생성/검증/worked 중 최신) 이후 경과로 완만 감쇠(반감기 CONFIDENCE_HALFLIFE_DAYS)."""
    anchor = _parse_iso(anchor_iso)
    if anchor is None:
        return 1.0
    age_days = (now_dt - anchor).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / CONFIDENCE_HALFLIFE_DAYS)


def confidence_map(name_or_dir, *, now: str | None = None) -> dict:
    """case_id -> 파생 confidence(0~1). 저장 안 함, 매번 계산.

    분해 신호(베타사후 + 곱 패널티 + 감쇠):
      - 독립 확인자(제안자·unknown 제외 worked 출처 수) → 성공 근거↑
      - harmful → 강한 곱 패널티(1건도 신호)
      - 충돌 이웃(conflicts_with 수, conflict 상태면 +1) → 패널티
      - 시간 감쇠(최근 생성/검증/worked 기준 반감기)
    """
    sdir = _resolve_dir(name_or_dir)
    now_dt = _parse_iso(now or now_iso()) or datetime.now()
    events = read_events(sdir)
    cases = read_cases(sdir)
    rel = evaluator_reliability(sdir)                       # 신뢰 낮은 확인자는 confidence 기여 축소
    worked_src: dict[str, set] = {}
    worked_at: dict[str, str] = {}
    harmful_cnt: dict[str, int] = {}
    for e in events:
        cid = e.get("case_id", "")
        if e.get("event") == "worked":
            worked_src.setdefault(cid, set()).add(str(e.get("by") or "").strip() or "unknown")
            at = str(e.get("at_utc") or "")
            if at > worked_at.get(cid, ""):
                worked_at[cid] = at
        elif e.get("event") == "harmful":
            harmful_cnt[cid] = harmful_cnt.get(cid, 0) + 1
    out: dict[str, float] = {}
    for c in cases:
        cid = c.get("case_id", "")
        proposer = str(c.get("proposed_by") or "").strip()
        srcs = worked_src.get(cid, set())
        indep_srcs = {s for s in srcs if s and s != "unknown" and s != proposer}
        indep_weight = sum(_reliability_of(rel, s) for s in indep_srcs)   # 사이코펀시=가중↓
        harmful = harmful_cnt.get(cid, 0)
        base = _EVIDENCE_BASE.get(c.get("evidence_level", ""), 0.45)
        if c.get("status") == "conflict":
            base = min(base, 0.25)
        successes = indep_weight + (1 if c.get("evidence_level") == "user_directive" else 0)
        alpha, beta = 1.0 + successes, 1.0 + harmful
        evidence_score = alpha / (alpha + beta)
        n = successes + harmful
        w = n / (n + 2.0)                                  # 증거 쌓일수록 evidence_score 비중↑
        score = (1.0 - w) * base + w * evidence_score
        if harmful > 0:
            score *= max(0.3, 1.0 - 0.35 * harmful)        # harmful 강한 곱 패널티
        neigh = len([x for x in (c.get("conflicts_with") or []) if x])
        if c.get("status") == "conflict":
            neigh = max(neigh, 1)
        if neigh > 0:
            score *= max(0.4, 1.0 - 0.2 * neigh)           # 충돌 이웃 패널티
        anchor = max(str(c.get("created_at") or ""), str(c.get("last_verified_at_utc") or ""), worked_at.get(cid, ""))
        score *= _time_decay(anchor, now_dt)
        out[cid] = round(min(1.0, max(0.0, score)), 3)
    return out


def derive_confidence(name_or_dir, case_id: str, *, now: str | None = None) -> float:
    """단일 케이스의 파생 confidence(없으면 0.0)."""
    return confidence_map(name_or_dir, now=now).get(case_id, 0.0)


def _latest_case_with_path(sdir: Path, case_id: str) -> tuple[dict | None, Path | None]:
    found = (None, None)
    for row, path in _read_with_origin(sdir):
        if row.get("case_id") == case_id:
            found = (row, path)
    return found


def _set_status(sdir: Path, case_id: str, new_status: str, *, by: str, reason: str, extra: dict | None = None) -> dict:
    """상태 전이를 append-only로 기록(직전 행 복사 + status/감사필드 갱신). 자원락 안에서 호출.

    P1: 이 함수는 '의미 변경'이라 반드시 by(주체)+reason(근거)을 받는다(에이전트 판단 또는 명시 시스템 사유).
    """
    row, path = _latest_case_with_path(sdir, case_id)
    if row is None:
        raise CaseLedgerError(f"case 없음: {case_id}")
    updated = dict(row)
    updated["status"] = new_status
    updated["status_changed_by"] = by
    updated["status_change_reason"] = reason
    updated["status_changed_at_utc"] = now_iso()
    if extra:
        updated.update(extra)
    _append_jsonl(path, updated)
    return updated


def promote_case(name_or_dir, case_id: str, *, by: str, rationale: str,
                 method: str = "second_judgment", confirm_threshold: int = DEFAULT_CONFIRM_THRESHOLD) -> dict:
    """케이스를 active로 확정한다. 두 경로:

    - method='worked_threshold': worked ≥ confirm_threshold 그리고 harmful == 0 (자동 수렴 근거).
    - method='second_judgment': 에이전트의 두 번째 독립 판단. **미성숙 가드: worked ≥ 1 전제.**
    - method='owner_approval': 대시보드에서 **실제 대표가 직접 승인**(진짜 대표 세션 = 위조 불가). worked 가드 면제,
      must_apply + evidence_level=user_directive로 확정.

    어느 쪽이든 by(주체)+rationale 필수(P1). 카운터/임계치 단독으로 자동 전이하지 않는다 — 호출은 판단의 결과다.
    """
    sdir = _resolve_dir(name_or_dir)

    def mutate():
        worked, harmful = worked_harmful_counts(sdir, case_id)
        if method == "worked_threshold":
            # §9.1 오염 방지: '횟수'가 아니라 '서로 다른 확인자 수'로. 그리고 제안자 본인의 자기confirm만으론
            # 절대 승격 불가(독립 확인자 ≥1 필수) — 자기강화 오염 루프 차단.
            row, _ = _latest_case_with_path(sdir, case_id)
            proposer = str((row or {}).get("proposed_by") or "").strip()
            sources = worked_distinct_sources(sdir, case_id)
            independent = {s for s in sources if s and s != proposer and s != "unknown"}
            # §9.1 사이코펀시: 신뢰도 미달(과거 확인이 harmful/모순으로 드러난) 평가자는 자동승격 확인자로 불인정.
            rel = evaluator_reliability(sdir)
            reliable_sources = {s for s in sources if _reliability_of(rel, s) >= RELIABILITY_FLOOR}
            reliable_independent = {s for s in independent if _reliability_of(rel, s) >= RELIABILITY_FLOOR}
            if (row or {}).get("status") == "conflict":
                # §9.1: 모순 격리는 자동승격으로 못 푼다 — 사람/판단이 범인을 골라 해소해야 한다.
                raise CaseLedgerError("worked_threshold 미충족: conflict 격리 상태 — 자동승격 불가(사람/판단으로 해소)")
            if harmful > 0:
                raise CaseLedgerError(f"worked_threshold 미충족: harmful={harmful}>0")
            if len(reliable_sources) < confirm_threshold:
                raise CaseLedgerError(
                    f"worked_threshold 미충족: 신뢰 가능한 확인자 {len(reliable_sources)}(<{confirm_threshold}) "
                    f"[전체 {len(sources)}, 신뢰도<{RELIABILITY_FLOOR} 제외]")
            if not reliable_independent:
                raise CaseLedgerError("worked_threshold 미충족: 제안자 외 '신뢰 가능한' 독립 확인자가 없음(사이코펀시/자기confirm 차단)")
        elif method == "second_judgment":
            if worked < 1:
                raise CaseLedgerError("미성숙 가드: 2차 독립판단 승격은 worked≥1 전제")
        elif method == "owner_approval":
            pass   # 대표 직접 승인 = 권위 있음, worked 가드 면제(대표가 곧 증거)
        else:
            raise CaseLedgerError("method는 worked_threshold|second_judgment|owner_approval")
        extra = {"last_verified_at_utc": now_iso()}
        if method == "owner_approval":
            extra.update({"must_apply": True, "application_level": "must_apply",
                          "enforcement": "must_apply", "evidence_level": "user_directive",
                          "approved_by": "대표"})
        return _set_status(sdir, case_id, "active", by=by,
                           reason=f"promote/{method}: {rationale}", extra=extra)

    return with_resource_lock(sdir, mutate)


def demote_case(name_or_dir, case_id: str, *, by: str, rationale: str, to: str = "candidate") -> dict:
    """provisional_must/active를 강등(보통 candidate). 에이전트 판단 결과(harmful 검토 후 등)."""
    if to not in {"candidate", "retired"}:
        raise CaseLedgerError("demote 대상은 candidate|retired")
    sdir = _resolve_dir(name_or_dir)
    return with_resource_lock(sdir, lambda: _set_status(
        sdir, case_id, to, by=by, reason=f"demote: {rationale}",
        extra={"must_apply": False, "application_level": "may_apply", "enforcement": ""}))


def retire_case(name_or_dir, case_id: str, *, by: str, rationale: str) -> dict:
    sdir = _resolve_dir(name_or_dir)
    return with_resource_lock(sdir, lambda: _set_status(
        sdir, case_id, "retired", by=by, reason=f"retire: {rationale}"))


def mark_conflict(name_or_dir, case_id: str, *, by: str, rationale: str, conflicts_with=None) -> dict:
    sdir = _resolve_dir(name_or_dir)
    extra = {"conflicts_with": list(conflicts_with)} if conflicts_with else None
    return with_resource_lock(sdir, lambda: _set_status(
        sdir, case_id, "conflict", by=by, reason=f"conflict: {rationale}", extra=extra))


BRANCH_RESTORE_STATUSES = {"active", "provisional_must", "candidate"}


def branch_conflict(name_or_dir, resolutions, *, by: str, rationale: str) -> list[dict]:
    """모순 격리(conflict)를 '병합·삭제'가 아니라 조건을 좁혀 **분기**(more-specific-wins)로 해소한다.

    리서치(Delgrande-Schaub 1997): 모순은 applies_when으로 좁혀 분기, 공격적 병합 금지(minimal change).
    각 resolution(dict): {case_id, applies_when?, does_not_apply_when?, restore_to?}.
    - 좁힌 조건은 **에이전트가 직접 제공**한다(P1: 의미 변경=판단). by·rationale 필수.
    - 배치 안에서 **최소 한 케이스는 조건을 좁혀야** 한다(그냥 격리해제 금지 — 그러면 다시 충돌).
    - status는 conflict → restore_to(미지정 시 격리 전 상태 pre_conflict_status, 그도 없으면 candidate).
      **분기는 '격리 해제'일 뿐 승격이 아니다** — active 확정은 여전히 promote_case(수렴/대표 승인) 경로.
    """
    if not str(by or "").strip() or not str(rationale or "").strip():
        raise CaseLedgerError("branch_conflict: by·rationale 필수(P1 의미변경)")
    sdir = _resolve_dir(name_or_dir)
    res_list = resolutions if isinstance(resolutions, list) else [resolutions]
    if not res_list:
        raise CaseLedgerError("branch_conflict: resolutions 비어있음")

    def mutate():
        prepared = []
        any_narrowed = False
        for res in res_list:
            cid = str(res.get("case_id") or "")
            if not cid:
                raise CaseLedgerError("branch_conflict: case_id 누락")
            row, _ = _latest_case_with_path(sdir, cid)
            if row is None:
                raise CaseLedgerError(f"branch_conflict: case 없음 {cid}")
            if row.get("status") != "conflict":
                raise CaseLedgerError(f"branch_conflict: conflict 상태만 분기 가능(현재 {row.get('status')}) {cid}")
            extra: dict = {}
            if res.get("applies_when") is not None:
                extra["applies_when"] = res["applies_when"]
                any_narrowed = True
            if res.get("does_not_apply_when") is not None:
                extra["does_not_apply_when"] = res["does_not_apply_when"]
                any_narrowed = True
            target = res.get("restore_to") or row.get("pre_conflict_status") or "candidate"
            if target not in BRANCH_RESTORE_STATUSES:
                raise CaseLedgerError(f"restore_to는 {sorted(BRANCH_RESTORE_STATUSES)} 중 하나여야 함")
            prepared.append((cid, target, extra))
        if not any_narrowed:
            raise CaseLedgerError(
                "branch_conflict: 최소 한 케이스의 조건을 좁혀야 분기 해소(applies_when/does_not_apply_when). "
                "조건 변경 없는 단순 격리해제는 금지(다시 충돌)")
        out = []
        for cid, target, extra in prepared:
            extra["branched_at_utc"] = now_iso()
            out.append(_set_status(sdir, cid, target, by=by,
                                   reason=f"branch(applies_when 분기): {rationale}", extra=extra))
        return out

    return with_resource_lock(sdir, mutate)


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def expire_stale_candidates(name_or_dir, *, ttl_days: int = DEFAULT_CANDIDATE_TTL_DAYS, now: str | None = None) -> list[dict]:
    """traction 없는 candidate를 만료(janitor). 의미 반전이 아니라 '확정 못 받은 발의' 청소다.

    조건: status==candidate + worked 이벤트 0 + created_at이 ttl_days 초과. 대표발(provisional_must)은 대상 아님.
    append-only 전이라 되돌릴 수 있다.
    """
    sdir = _resolve_dir(name_or_dir)
    now_dt = _parse_iso(now or now_iso()) or datetime.now()
    cutoff = now_dt - timedelta(days=ttl_days)

    def mutate():
        expired = []
        cases = read_cases(sdir)
        for case in cases:
            if case.get("status") != "candidate":
                continue
            cid = case.get("case_id", "")
            worked, _ = worked_harmful_counts(sdir, cid)
            if worked > 0:
                continue
            created = _parse_iso(case.get("created_at", ""))
            if created is None or created > cutoff:
                continue
            expired.append(_set_status(sdir, cid, "expired", by="system/janitor",
                                       reason=f"stale_no_traction(>{ttl_days}d, worked=0)"))
        return expired

    return with_resource_lock(sdir, mutate)


def review_queue(name_or_dir, *, now: str | None = None) -> list[dict]:
    """주의가 필요한 케이스 목록(깃발=신호, 상태변경 아님). 대시보드/에이전트 재판단 입력.

    포함: status==conflict, harmful>0(검토 필요), provisional_must이고 review_due 지남.
    """
    sdir = _resolve_dir(name_or_dir)
    now_dt = _parse_iso(now or now_iso()) or datetime.now()
    out = []
    for case in read_cases(sdir):
        if case.get("status") in DEAD_STATUSES:
            continue
        cid = case.get("case_id", "")
        reasons = []
        if case.get("status") == "conflict":
            reasons.append("conflict")
        _, harmful = worked_harmful_counts(sdir, cid)
        if harmful > 0:
            reasons.append(f"harmful={harmful}")
        if case.get("status") == "provisional_must":
            due = _parse_iso(case.get("review_due_at_utc", ""))
            if due is not None and due <= now_dt:
                reasons.append("review_due")
        if reasons:
            out.append({"case_id": cid, "status": case.get("status", ""), "reasons": reasons})
    return out


def case_convergence(name_or_dir, *, confirm_threshold: int = DEFAULT_CONFIRM_THRESHOLD,
                     include_local: bool = True) -> list[dict]:
    """케이스별 수렴 상태 요약(P5/C2). 이벤트를 한 번만 읽어 worked/harmful 집계 + readiness 플래그.

    P1: 이건 *신호*다 — ready_to_promote여도 자동 전이하지 않는다. 실제 active 확정은 promote_case(판단/대표 승인).
    include_local=False: 대외비 사이드카 케이스를 제외(HTTP 노출용 — condition 텍스트가 새지 않게, CRITICAL-2).
    """
    sdir = _resolve_dir(name_or_dir)
    events = read_events(sdir)
    worked_by: dict[str, int] = {}
    harmful_by: dict[str, int] = {}
    for e in events:
        cid = e.get("case_id", "")
        if e.get("event") == "worked":
            worked_by[cid] = worked_by.get(cid, 0) + 1
        elif e.get("event") == "harmful":
            harmful_by[cid] = harmful_by.get(cid, 0) + 1
    try:
        cmap = confidence_map(sdir)
    except CaseLedgerError:
        cmap = {}
    out = []
    for case in read_cases(sdir, include_local=include_local):
        status = case.get("status", "")
        if status in DEAD_STATUSES:
            continue
        cid = case.get("case_id", "")
        worked = worked_by.get(cid, 0)
        harmful = harmful_by.get(cid, 0)
        ready = status in {"candidate", "provisional_must"} and worked >= confirm_threshold and harmful == 0
        out.append({
            "case_id": cid, "status": status,
            "condition": str(case.get("condition") or "")[:MAX_CONDITION_CHARS],
            "worked": worked, "harmful": harmful,
            "confidence": cmap.get(cid),
            "ready_to_promote": ready,
            "needs_review": harmful > 0 or status == "conflict",
        })
    return out


# --------------------------------------------------------------------------- Curator (P5)
def _content_key(case: dict) -> tuple:
    return (
        str(case.get("condition") or "").strip(),
        str(case.get("instruction") or "").strip(),
        case.get("polarity", ""),
    )


def dedup_cases(name_or_dir, *, by: str = "system/curator") -> list[dict]:
    """**완전 동일** 내용(condition+instruction+polarity 일치) 케이스만 멱등 정리(오래된 것 retire).

    P1: '비슷한' 통합은 하지 않는다(의미 판단은 에이전트). 여기는 정확히 같은 것만 기계적으로 합친다.
    """
    sdir = _resolve_dir(name_or_dir)

    def mutate():
        live = [c for c in read_cases(sdir) if c.get("status") not in DEAD_STATUSES]
        live.sort(key=lambda c: str(c.get("created_at", "")))
        seen: dict[tuple, str] = {}
        retired = []
        for case in live:
            key = _content_key(case)
            cid = case.get("case_id", "")
            if key in seen:
                retired.append(_set_status(sdir, cid, "retired", by=by,
                                           reason=f"exact_dup_of:{seen[key]}"))
            else:
                seen[key] = cid
        return retired

    return with_resource_lock(sdir, mutate)


def curator_report(name_or_dir, *, body_revision_at: int = 5, split_at: int = 8) -> dict:
    """정리/개정/분리 **제안**(신호만, 자동 commit 없음 — P1·설계 §8). 대시보드/에이전트 판단 입력."""
    sdir = _resolve_dir(name_or_dir)
    live = [c for c in read_cases(sdir) if c.get("status") not in DEAD_STATUSES]
    active = [c for c in live if c.get("status") in {"active", "provisional_must"}]
    conflicts = [c.get("case_id", "") for c in live if c.get("status") == "conflict"]

    # 완전 동일 중복 그룹 탐지(제안만)
    groups: dict[tuple, list[str]] = {}
    for c in live:
        groups.setdefault(_content_key(c), []).append(c.get("case_id", ""))
    exact_dups = [ids for ids in groups.values() if len(ids) > 1]

    suggestions = []
    if exact_dups:
        suggestions.append({"type": "dedup", "groups": exact_dups, "how": "dedup_cases()로 정리 가능"})
    if conflicts:
        suggestions.append({"type": "resolve_conflict", "case_ids": conflicts})
    if len(active) >= body_revision_at:
        suggestions.append({"type": "body_revision",
                            "reason": f"active 케이스 {len(active)}개 — 본문 범용화(N케이스 종합) 검토",
                            "case_ids": [c.get("case_id", "") for c in active]})
    if len(active) >= split_at:
        suggestions.append({"type": "split",
                            "reason": f"케이스 분기 {len(active)}개 — 스킬 분리를 에이전트가 판단"})
    rq = review_queue(sdir)
    if rq:
        suggestions.append({"type": "review", "items": rq})

    return {"skill": sdir.name, "live_cases": len(live), "active": len(active), "suggestions": suggestions}
