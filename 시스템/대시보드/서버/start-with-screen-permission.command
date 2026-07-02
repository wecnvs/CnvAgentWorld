#!/bin/bash
# Start/restart the 8686 dashboard as a child of Terminal.app.
#
# macOS TCC grants Screen Recording / Accessibility to the responsible GUI app.
# Starting uvicorn directly from launchd, a web terminal, or nohup can leave the
# dashboard's local computer-use capture path without permission even when
# Terminal.app itself is allowed. This launcher must be run by Terminal.app
# (double-click or osascript 'tell Terminal to do script ...').
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/../.run"
mkdir -p "$RUN"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8686}"
PY="${PY:-/usr/bin/python3}"
LOG="$RUN/server.log"
PIDFILE="$RUN/server.pid"
PERM_LOG="$RUN/screen-permission-start.log"
LAUNCH_ID="${CNV_DASHBOARD_LAUNCH_ID:-manual-$$}"
LAUNCH_STATUS="${CNV_DASHBOARD_LAUNCH_STATUS:-$RUN/terminal-start-$PORT-$LAUNCH_ID.status}"

write_status() {
  printf '%s %s\n' "$1" "$(date '+%F %T')" > "$LAUNCH_STATUS"
}

exec > >(tee -a "$LOG") 2>&1

echo "================================================"
echo "CnvAgentWorld dashboard start via Terminal.app - $(date '+%F %T')"
echo "  launch_id=$LAUNCH_ID"
echo "  host=$HOST port=$PORT"
echo "  python=$PY"
echo "  cwd=$HERE"
write_status "started"

if [ "${TERM_PROGRAM:-}" != "Apple_Terminal" ]; then
  echo "WARNING: TERM_PROGRAM=${TERM_PROGRAM:-unset}; this launcher is expected to run inside Terminal.app."
  echo "    Continuing, but Screen Recording may fail if Terminal is not the responsible app."
fi

echo "[permission gate] Checking Screen Recording from this Terminal context..."
PRECHECK="/tmp/cnv_dashboard_screen_precheck_$$.png"
if screencapture -x "$PRECHECK" 2>/dev/null && [ -s "$PRECHECK" ]; then
  echo "  OK: Screen Recording precheck passed ($(wc -c < "$PRECHECK" | tr -d ' ') bytes)"
  rm -f "$PRECHECK" 2>/dev/null || true
  write_status "permission_ok"
else
  echo "  ERROR: Screen Recording precheck failed."
  echo "  The existing :$PORT server was not stopped. Enable Terminal.app in:"
  echo "  System Settings > Privacy & Security > Screen & System Audio Recording (or Screen Recording)"
  echo "  Then run: sh \"$HERE/ctl.sh\" restart"
  write_status "permission_failed"
  {
    echo "[$(date '+%F %T')] Screen Recording precheck failed before dashboard restart."
    echo "TERM_PROGRAM=${TERM_PROGRAM:-unset} PY=$PY PORT=$PORT"
  } >> "$PERM_LOG"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
  exit 70
fi

echo "Stopping old dashboard server on :$PORT after permission gate..."
write_status "stopping"
OLD="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$OLD" ]; then
  echo "  old pid(s): $OLD"
  for p in $OLD; do kill -TERM "$p" 2>/dev/null || true; done
  for _ in $(seq 1 10); do
    sleep 1
    STILL="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$STILL" ] && break
  done
  STILL="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$STILL" ]; then
    echo "  SIGTERM did not stop: $STILL; sending SIGKILL"
    for p in $STILL; do kill -KILL "$p" 2>/dev/null || true; done
    sleep 1
  fi
else
  echo "  no old server"
fi

echo "Starting uvicorn in foreground. Keep this Terminal window open."
cd "$HERE" || exit 1
echo $$ > "$PIDFILE"

(
  for _ in $(seq 1 30); do
    sleep 1
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  done
  if curl -fsS "http://127.0.0.1:$PORT/api/cu/view/status?target=local" >/tmp/cnv_dashboard_local_cu_status_$$.json 2>/tmp/cnv_dashboard_local_cu_status_$$.err; then
    echo "OK: [postcheck] local computer-use screen capture passed - $(date '+%F %T')"
  else
    echo "ERROR: [postcheck] local computer-use screen capture failed - $(date '+%F %T')"
    sed -n '1,6p' /tmp/cnv_dashboard_local_cu_status_$$.err 2>/dev/null || true
  fi
  rm -f /tmp/cnv_dashboard_local_cu_status_$$.json /tmp/cnv_dashboard_local_cu_status_$$.err 2>/dev/null || true
) &

echo "================================================"
write_status "execing"
exec caffeinate -dimsu "$PY" -m uvicorn app:app --host "$HOST" --port "$PORT"
