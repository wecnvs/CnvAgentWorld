#!/bin/sh
# CnvAgentWorld 대시보드 서버 실행 (기본 포트 8686)
# 기본은 로컬 전용(127.0.0.1). 외부는 'tailscale serve'가 HTTPS로 노출한다.
# raw LAN 노출이 필요하면 HOST=0.0.0.0 으로 실행.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8686}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")" || exit 1
exec /usr/bin/python3 -m uvicorn app:app --host "$HOST" --port "$PORT" "$@"
