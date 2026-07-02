# -*- coding: utf-8 -*-
"""터미널 서버(8687) 접근 토큰 가드 — 킬스위치식 옵트인. (서버(8686)와 같은 토큰 파일 공유)

- `시스템/대시보드/.auth_token`이 존재하고 비어있지 않을 때만 활성. 없으면 무동작(현행 유지).
- 루프백(에이전트·tailscale serve 프록시)은 항상 통과. 그 외는 `?auth=<토큰>` 1회 → 쿠키.
- 8687은 PTY 셸 생성 서버라서, LAN 노출(HOST=0.0.0.0) 시 이 가드가 무인증 원격 셸을 막는다.
"""
from __future__ import annotations

from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".auth_token"   # 시스템/대시보드/.auth_token
COOKIE_NAME = "cnv_auth"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def load_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def client_host(conn) -> str:
    try:
        return (conn.client.host or "") if conn.client else ""
    except Exception:
        return ""


def check_request(conn) -> tuple[bool, bool]:
    """(허용 여부, 쿠키 심기 필요 여부). Request/WebSocket 공용."""
    token = load_token()
    if not token:
        return True, False
    if client_host(conn) in _LOOPBACK:
        return True, False
    if conn.cookies.get(COOKIE_NAME, "") == token:
        return True, False
    if conn.query_params.get("auth", "") == token:
        return True, True
    return False, False
