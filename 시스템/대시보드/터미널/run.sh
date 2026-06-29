#!/bin/sh
# CnvAgentWorld 터미널 서비스 실행 (기본 포트 8687, 루트폴더 기준 셸)
# 기본은 로컬 전용(127.0.0.1). 외부는 'tailscale serve'가 HTTPS로 노출한다.
# raw LAN 노출이 필요하면 HOST=0.0.0.0 으로 실행.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8687}"
cd "$(dirname "$0")" || exit 1
exec python3 -m uvicorn server:app --host "$HOST" --port "$PORT" "$@"
