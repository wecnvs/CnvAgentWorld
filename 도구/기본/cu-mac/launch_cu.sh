#!/bin/bash
# Claude Code 실행 + 터미널 전체 출력을 HUD 오버레이에 스트리밍
#
# 사용법: bash launch_cu.sh [claude 인수...]
#   예시: bash launch_cu.sh
#         bash launch_cu.sh --model claude-opus-4-7
#
# 이 스크립트로 claude를 실행하면:
# - 터미널에 찍히는 모든 내용이 실시간으로 HUD에 미러링됨
# - HUD는 세션 종료 시 자동으로 숨겨짐

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/homebrew/bin/python3.13"
EXPAT_LIB="/opt/homebrew/opt/expat/lib"
RAW_LOG="/tmp/cu_term_raw.log"
CLEAN_LOG="/tmp/cu_overlay.log"
ACTIVE_FILE="/tmp/cu_active"

cleanup() {
    # 마커 제거 → 오버레이 자동 숨김
    rm -f "$ACTIVE_FILE"
    kill "$FILTER_PID" 2>/dev/null
    wait "$FILTER_PID" 2>/dev/null
    echo "" >> "$CLEAN_LOG"
    echo "[$(date '+%H:%M:%S')] === 세션 종료 ===" >> "$CLEAN_LOG"
}
trap cleanup EXIT

# 로그 초기화
> "$RAW_LOG"
> "$CLEAN_LOG"
echo "[$(date '+%H:%M:%S')] Claude Code 세션 시작 — 터미널 출력 스트리밍 중" >> "$CLEAN_LOG"

# 오버레이 시작 (이미 실행 중이면 재사용)
bash "$TOOLS_DIR/overlay_start.sh" start

# 마커 생성 → 오버레이 표시
touch "$ACTIVE_FILE"

# 백그라운드: raw log → ANSI 제거 → clean log
DYLD_LIBRARY_PATH="$EXPAT_LIB:${DYLD_LIBRARY_PATH:-}" \
tail -F "$RAW_LOG" 2>/dev/null \
    | "$PYTHON" "$TOOLS_DIR/ansi_filter.py" >> "$CLEAN_LOG" &
FILTER_PID=$!

# claude 실행 (script로 PTY 캡처 — 화면에도 정상 표시)
DYLD_LIBRARY_PATH="$EXPAT_LIB:${DYLD_LIBRARY_PATH:-}" \
script -q -F "$RAW_LOG" claude "$@"
