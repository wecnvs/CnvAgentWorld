#!/bin/bash
# 오버레이 HUD 시작/중지
# 사용법: bash overlay_start.sh [start|stop|status]

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/homebrew/bin/python3.13"
EXPAT_LIB="/opt/homebrew/opt/expat/lib"
PID_FILE="/tmp/cu_overlay.pid"
LOG_FILE="/tmp/cu_overlay.log"

cmd="${1:-start}"

case "$cmd" in
  start)
    # 이미 실행 중이면 중지
    if [ -f "$PID_FILE" ]; then
      old_pid=$(cat "$PID_FILE")
      if kill -0 "$old_pid" 2>/dev/null; then
        echo "오버레이 재시작 (이전 PID $old_pid 종료)"
        kill "$old_pid" 2>/dev/null
        sleep 0.3
      fi
    fi
    # 로그 초기화
    echo "[$(date '+%H:%M:%S')] 오버레이 시작됨" > "$LOG_FILE"
    # 백그라운드 실행
    DYLD_LIBRARY_PATH="$EXPAT_LIB:${DYLD_LIBRARY_PATH:-}" \
      nohup "$PYTHON" "$TOOLS_DIR/overlay.py" > /tmp/cu_overlay_err.log 2>&1 &
    sleep 0.5
    if [ -f "$PID_FILE" ]; then
      echo "오버레이 시작됨 (PID $(cat $PID_FILE))"
    else
      echo "오버레이 시작 실패 — /tmp/cu_overlay_err.log 확인"
    fi
    ;;

  stop)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      kill "$pid" 2>/dev/null && echo "오버레이 종료됨 (PID $pid)" || echo "이미 종료됨"
      rm -f "$PID_FILE"
    else
      echo "실행 중인 오버레이 없음"
    fi
    ;;

  status)
    if [ -f "$PID_FILE" ]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "오버레이 실행 중 (PID $pid)"
      else
        echo "오버레이 PID 파일 있으나 프로세스 없음"
      fi
    else
      echo "오버레이 실행 중 아님"
    fi
    ;;

  *)
    echo "사용법: $0 [start|stop|status]"
    ;;
esac
