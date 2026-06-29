# -*- coding: utf-8 -*-
"""채팅에이전트의 구조화 반환(ChatAgentResult) 해석."""
from __future__ import annotations

import json
import re


class ChatAgentResultError(ValueError):
    """ChatAgentResult 계약을 만족하지 못했다."""


def extract(text: str) -> dict | None:
    """응답 본문에서 ChatAgentResult.v1 JSON 객체를 찾는다."""
    raw = (text or "").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    found = []
    for start in [m.start() for m in re.finditer(r"\{", raw)]:
        try:
            obj, _ = decoder.raw_decode(raw[start:])
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("schema") == "ChatAgentResult.v1":
            found.append(obj)
    return found[-1] if found else None


def resolve_worker(value: str, member_tokens: set[str], worker_aliases: dict | None = None) -> str | None:
    """worker 지정값을 멤버 토큰으로 해석한다. 토큰/표시이름/코드 모두 허용.

    에이전트는 동료를 자연어 이름("구현자")으로 부르는 게 자연스러운데, 시스템 식별자는
    토큰("구현자_2a79")이다. 이름·코드·토큰을 모두 받아 토큰으로 정규화한다.
    """
    v = (value or "").strip()
    if not v:
        return None
    if v in member_tokens:
        return v
    aliases = worker_aliases or {}
    if v in aliases:
        return aliases[v]
    low = v.lower()
    for alias, token in aliases.items():
        if str(alias).strip().lower() == low:
            return token
    # 토큰이 "이름_코드" 형태일 때 이름만으로도 매칭
    for token in member_tokens:
        if "_" in token and token.rsplit("_", 1)[0].lower() == low:
            return token
    return None


def work_request(
    result: dict,
    *,
    default_worker: str,
    member_tokens: set[str],
    worker_aliases: dict | None = None,
) -> dict | None:
    action = str(result.get("action") or "").strip()
    if action not in {"request_work", "mixed"}:
        return None
    request = result.get("work_request") if isinstance(result.get("work_request"), dict) else {}
    manager_requests = result.get("manager_requests") if isinstance(result.get("manager_requests"), list) else []
    for item in manager_requests:
        if isinstance(item, dict) and item.get("type") == "request_work":
            request = {**item, **request}
            break
    objective = str(
        request.get("objective")
        or request.get("reason")
        or result.get("public_reply")
        or ""
    ).strip()
    if not objective:
        raise ChatAgentResultError("request_work objective required")
    worker_raw = str(
        request.get("suggested_worker")
        or request.get("worker")
        or request.get("target_agent")
        or default_worker
    ).strip()
    worker = resolve_worker(worker_raw, member_tokens, worker_aliases)
    if worker is None:
        raise ChatAgentResultError(
            f"request_work worker '{worker_raw}' is not a room member "
            f"(members: {sorted(member_tokens)})"
        )
    constraints = request.get("constraints") if isinstance(request.get("constraints"), list) else []

    # 계획(plan): 단계 목록. 없으면 [](호출부가 objective 단일 단계로 채움) → 하위호환.
    plan_raw = request.get("plan")
    if isinstance(plan_raw, str):
        plan_raw = [plan_raw]
    plan = [str(item).strip() for item in plan_raw if str(item).strip()][:12] if isinstance(plan_raw, list) else []

    # 승인 필요 명시 선언(needs_approval): 에이전트가 직접 고른다.
    #  - 키가 아예 없으면 None(미선언) → 게이트에서 보수적으로 True 취급.
    #  - 있으면 bool로 정규화.
    if "needs_approval" in request:
        needs_approval = bool(request.get("needs_approval"))
    else:
        needs_approval = None
    approval_reason = str(request.get("approval_reason") or "").strip()

    # 위험 자가평가(risk): {level, reason} 보조 신호.
    risk_raw = request.get("risk") if isinstance(request.get("risk"), dict) else {}
    risk_level = str(risk_raw.get("level") or "").strip().lower()
    if risk_level not in {"low", "high"}:
        risk_level = ""
    risk_reason = str(risk_raw.get("reason") or "").strip()

    return {
        "objective": objective,
        "worker": worker,
        "constraints": [str(item) for item in constraints if str(item).strip()][:12],
        "plan": plan,
        "needs_approval": needs_approval,
        "approval_reason": approval_reason,
        "risk_level": risk_level,
        "risk_reason": risk_reason,
        "public_reply": str(result.get("public_reply") or "").strip(),
        "raw": result,
    }
