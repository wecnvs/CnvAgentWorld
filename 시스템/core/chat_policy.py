# -*- coding: utf-8 -*-
"""공간 채팅 입력의 공간관리 실행 정책."""
from __future__ import annotations


INTERNAL_NO_MANAGER_REQUESTERS = frozenset({"관리자에이전트", "공간관리", "시스템", "system"})


def normalize_requester(requester: str | None) -> str:
    value = str(requester or "대표").strip()
    return value or "대표"


def should_run_space_manager(
    requester: str | None,
    requested_run_manager: bool,
    *,
    trusted_internal: bool = False,
) -> bool:
    """공개 입력면은 항상 공간관리를 실행하고, 내부 코드만 명시적으로 skip할 수 있다."""
    if not trusted_internal:
        return True
    normalized = normalize_requester(requester)
    return bool(requested_run_manager) or normalized not in INTERNAL_NO_MANAGER_REQUESTERS
