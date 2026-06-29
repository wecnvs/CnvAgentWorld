#!/bin/bash
# computer-use-mac 도구 실행 래퍼
# macOS 26 libexpat 충돌 자동 해결 (DYLD_LIBRARY_PATH)
# 사용법: bash run_tool.sh <script.py> [인수...]

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/homebrew/bin/python3.13"
EXPAT_LIB="/opt/homebrew/opt/expat/lib"

export DYLD_LIBRARY_PATH="$EXPAT_LIB:${DYLD_LIBRARY_PATH:-}"
exec "$PYTHON" "$TOOLS_DIR/$1" "${@:2}"
