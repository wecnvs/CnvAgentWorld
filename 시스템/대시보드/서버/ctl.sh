#!/bin/sh
# 대시보드 서버(8686) 수동 제어 — 감독/자동부활 '없음'(개발용).
#
#  설계 의도:
#  - 8687(터미널)은 launchd가 상시 관리한다(부팅 자동기동 + 죽으면 부활). 그게 항상 떠 있는 제어 거점.
#  - 8686(대시보드)은 '개발 대상'이라 자동부활을 일부러 두지 않는다.
#    수정 → stop → 코드 고침 → start. 감독이 옛 코드로 되살리거나 크래시루프로 싸우지 않는다.
#  - macOS에서는 8686을 반드시 Terminal.app 자식으로 띄운다. 그래야 Terminal의
#    화면녹화/손쉬운사용 권한을 서버 로컬 computer-use 경로가 상속한다.
#    8687 웹터미널/launchd/nohup/python 직행으로 띄우면 권한 없는 바탕화면 캡처가 된다.
#
#  용법: ctl.sh {start|stop|restart|status}
set -u

HERE=$(cd "$(dirname "$0")" && pwd)        # 시스템/대시보드/서버
RUN="$HERE/../.run"; mkdir -p "$RUN"
PF="$RUN/server.pid"; LOG="$RUN/server.log"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8686}"
PY="${PY:-/usr/bin/python3}"
IS_DARWIN=0
[ "$(uname -s 2>/dev/null || true)" = "Darwin" ] && IS_DARWIN=1
LAUNCH_STATUS=""

port_pids() { lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null; }
is_up()     { [ -n "$(port_pids)" ]; }

sq() {
  # POSIX-safe single-quote for shell command text passed to Terminal.app.
  printf "'%s'" "$(printf "%s" "$1" | sed "s/'/'\\\\''/g")"
}

terminal_start() {
  LAUNCH_ID="$(date '+%Y%m%d%H%M%S')-$$"
  LAUNCH_STATUS="$RUN/terminal-start-$PORT-$LAUNCH_ID.status"
  WRAPPER="$RUN/terminal-start-$PORT-$LAUNCH_ID.command"
  rm -f "$LAUNCH_STATUS" "$WRAPPER"
  {
    printf '#!/bin/bash\n'
    printf 'rm -f "$0"\n'
    printf 'export HOST=%s\n' "$(sq "$HOST")"
    printf 'export PORT=%s\n' "$(sq "$PORT")"
    printf 'export PY=%s\n' "$(sq "$PY")"
    printf 'export CNV_DASHBOARD_LAUNCH_ID=%s\n' "$(sq "$LAUNCH_ID")"
    printf 'export CNV_DASHBOARD_LAUNCH_STATUS=%s\n' "$(sq "$LAUNCH_STATUS")"
    printf 'exec bash %s\n' "$(sq "$HERE/start-with-screen-permission.command")"
  } > "$WRAPPER"
  chmod +x "$WRAPPER"
  open -a Terminal "$WRAPPER"
}

wait_terminal_launch() {
  BEFORE_PIDS="${1:-}"
  LIMIT="${2:-30}"
  i=0
  while [ $i -lt "$LIMIT" ]; do
    if [ -n "$LAUNCH_STATUS" ] && [ -f "$LAUNCH_STATUS" ]; then
      STATE="$(sed -n '1p' "$LAUNCH_STATUS" 2>/dev/null || true)"
      case "$STATE" in
        permission_failed*)
          echo "[8686] Terminal 권한 게이트 실패 — 기존 서버 유지됨 (pid $(port_pids | tr '\n' ' '))"
          echo "[8686] System Settings > Privacy & Security > Screen & System Audio Recording에서 Terminal.app 허용 후 다시 restart"
          return 70
          ;;
        execing*)
          j=0
          while [ $j -lt 20 ]; do
            if is_up; then
              NOW_PIDS="$(port_pids | tr '\n' ' ')"
              if [ -z "$BEFORE_PIDS" ] || [ "$NOW_PIDS" != "$BEFORE_PIDS" ]; then
                echo "[8686] Terminal 권한 컨텍스트로 실행됨 (pid $NOW_PIDS, :$PORT)"
                return 0
              fi
            fi
            sleep 1; j=$((j+1))
          done
          echo "[8686] Terminal 실행자는 uvicorn 진입 직전까지 도달했지만 포트 교체 확인이 지연됨 — $LOG 확인"
          return 1
          ;;
        started*|permission_ok*|stopping*)
          ;;
        *)
          ;;
      esac
    fi
    sleep 1; i=$((i+1))
  done
  echo "[8686] Terminal 실행 요청 확인 실패 — Terminal 창, 권한 프롬프트, $LOG 확인"
  return 1
}

start() {
  if is_up; then echo "[8686] 이미 실행 중 (pid $(port_pids | tr '\n' ' '))"; return 0; fi
  if [ "$IS_DARWIN" -eq 1 ]; then
    terminal_start || return $?
    wait_terminal_launch "" 30
    return $?
  else
    nohup sh "$HERE/run.sh" >> "$LOG" 2>&1 &
    echo $! > "$PF"
  fi
  i=0; while [ $i -lt 8 ]; do is_up && break; sleep 1; i=$((i+1)); done
  if is_up; then echo "[8686] 시작됨 (pid $(port_pids | tr '\n' ' '), :$PORT)"
  else echo "[8686] 시작 실패 — $LOG 확인"; fi
}

stop() {
  if ! is_up; then echo "[8686] 실행 중 아님"; rm -f "$PF"; return 0; fi
  for p in $(port_pids); do kill -TERM "$p" 2>/dev/null; done
  i=0; while [ $i -lt 5 ]; do is_up || break; sleep 1; i=$((i+1)); done
  for p in $(port_pids); do kill -KILL "$p" 2>/dev/null; done   # 안 죽으면 강제
  rm -f "$PF"
  echo "[8686] 중지됨"
}

restart() {
  if [ "$IS_DARWIN" -eq 1 ]; then
    # The Terminal launcher performs the permission gate before stopping the old server.
    BEFORE="$(port_pids | tr '\n' ' ')"
    terminal_start || return $?
    wait_terminal_launch "$BEFORE" 30
  else
    stop; sleep 1; start
  fi
}

status() {
  if is_up; then
    echo "[8686] up    pid $(port_pids | tr '\n' ' ')  :$PORT"
    for p in $(port_pids); do
      ps -p "$p" -o pid,ppid,user,stat,comm,args 2>/dev/null || true
    done
    if [ "$IS_DARWIN" -eq 1 ]; then
      echo "[8686] macOS 권한 경로: Terminal.app 자식으로 실행되어야 local computer-use가 창을 볼 수 있음"
    fi
  else echo "[8686] down   :$PORT"; fi
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  *) echo "용법: ctl.sh {start|stop|restart|status}"; exit 1 ;;
esac
