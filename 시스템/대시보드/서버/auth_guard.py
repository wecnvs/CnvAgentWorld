# -*- coding: utf-8 -*-
"""대시보드(8686) 접근 토큰 가드 — 킬스위치식 옵트인.

- 토큰 파일(`시스템/대시보드/.auth_token`)이 **존재하고 비어있지 않을 때만** 활성화된다.
  파일이 없으면 완전히 무동작(현행 유지) → 무중단 배포, 문제 시 파일 삭제가 곧 킬스위치.
- 루프백(127.0.0.1/::1) 클라이언트는 항상 통과 — 같은 머신의 에이전트·도구·tailscale serve
  프록시 경유 요청을 막지 않는다(테일넷 자체가 기기 인증을 담당).
  ⚠️ **`tailscale funnel`(공개 인터넷 노출) 금지**: funnel도 요청을 루프백으로 프록시하므로
  이 토큰 가드가 무력화된다(누구나 루프백으로 보임 = 무인증 통과). 외부 노출은 반드시
  기기 인증이 붙는 `serve`(테일넷 내부)로만 한다. LAN 직접 노출(HOST=0.0.0.0)일 때만 토큰이 실효.
- 그 외(예: HOST=0.0.0.0 LAN 노출) 클라이언트는 `?auth=<토큰>` 1회 접속으로 쿠키를 심고,
  이후 쿠키로 통과한다. 기기당 1회면 된다.

활성화: `python3 -c "import secrets; print(secrets.token_urlsafe(32))" > 시스템/대시보드/.auth_token`
       후 서버 재시작. (파일은 .gitignore 처리됨)
"""
from __future__ import annotations

from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".auth_token"   # 시스템/대시보드/.auth_token
COOKIE_NAME = "cnv_auth"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def load_token() -> str:
    """활성 토큰. 파일이 없거나 비어 있으면 ''(가드 비활성)."""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def client_host(request) -> str:
    try:
        return (request.client.host or "") if request.client else ""
    except Exception:
        return ""


def check_request(request) -> tuple[bool, bool]:
    """(허용 여부, 쿠키를 새로 심어야 하는지)를 반환한다.

    starlette Request/WebSocket 공용 — 둘 다 .client / .cookies / .query_params를 가진다.
    """
    token = load_token()
    if not token:
        return True, False
    if client_host(request) in _LOOPBACK:
        return True, False
    if request.cookies.get(COOKIE_NAME, "") == token:
        return True, False
    if request.query_params.get("auth", "") == token:
        return True, True
    return False, False
