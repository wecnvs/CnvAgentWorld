#!/bin/sh
# 대시보드 서버(8686) 수동 제어 — 감독/자동부활 '없음'(개발용).
#
#  설계 의도:
#  - 8687(터미널)은 launchd가 상시 관리한다(부팅 자동기동 + 죽으면 부활). 그게 항상 떠 있는 제어 거점.
#  - 8686(대시보드)은 '개발 대상'이라 자동부활을 일부러 두지 않는다.
#    수정 → stop → 코드 고침 → start. 감독이 옛 코드로 되살리거나 크래시루프로 싸우지 않는다.
#  - 8686에서 띄우고 내리는 건 보통 8687 터미널에서 이 스크립트를 호출해서 한다.
#
#  용법: ctl.sh {start|stop|restart|status}
set -u

HERE=$(cd "$(dirname "$0")" && pwd)        # 시스템/대시보드/서버
RUN="$HERE/../.run"; mkdir -p "$RUN"
PF="$RUN/server.pid"; LOG="$RUN/server.log"
PORT="${PORT:-8686}"

port_pids() { lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null; }
is_up()     { [ -n "$(port_pids)" ]; }

start() {
  if is_up; then echo "[8686] 이미 실행 중 (pid $(port_pids | tr '\n' ' '))"; return 0; fi
  nohup sh "$HERE/run.sh" >> "$LOG" 2>&1 &
  echo $! > "$PF"
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

restart() { stop; sleep 1; start; }

status() {
  if is_up; then echo "[8686] up    pid $(port_pids | tr '\n' ' ')  :$PORT"
  else echo "[8686] down   :$PORT"; fi
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  *) echo "용법: ctl.sh {start|stop|restart|status}"; exit 1 ;;
esac
