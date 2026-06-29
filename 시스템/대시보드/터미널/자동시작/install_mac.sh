#!/bin/bash
# CnvAgentWorld 터미널 서버(:8687) macOS 자동시작 설치 (LaunchAgent)
#  - ~/Library/LaunchAgents/com.cnvagentworld.terminal.plist 생성 후 load
#  - RunAtLoad=true  : 로그인/부팅 시 자동 기동
#  - KeepAlive=true  : 서버가 죽으면 자동 재시작
#  - 경로는 이 스크립트 위치 기준으로 자동 계산(시스템/ 구조에 맞춰짐)
#  - 제거:  ./install_mac.sh --uninstall
set -e

AUTO="$(cd "$(dirname "$0")" && pwd)"        # .../터미널/자동시작
TS="$(dirname "$AUTO")"                        # .../터미널
LABEL="com.cnvagentworld.terminal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${PORT:-8687}"

if [ "$1" = "--uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "[제거] 완료: $PLIST (자동시작 해제)"
  exit 0
fi

# 1) 의존성 있는 python3 확인 (그 절대경로를 plist에 박는다)
PY="$(command -v python3)"
if [ -z "$PY" ] || ! "$PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "[오류] python3에 fastapi/uvicorn이 없습니다."
  echo "       먼저:  $PY -m pip install fastapi 'uvicorn[standard]'"
  exit 1
fi

# 2) 이미 수동으로 떠 있는 8687 정리 (포트 충돌 방지 → launchd가 소유하게)
pid="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
[ -n "$pid" ] && { kill $pid 2>/dev/null || true; echo "[정리] 기존 :$PORT (PID $pid) 종료"; sleep 1; }

# 3) LaunchAgent plist 생성
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>server:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$TS</string>
  <key>RunAtLoad</key>      <true/>
  <key>KeepAlive</key>      <true/>
  <key>StandardOutPath</key><string>$AUTO/autostart.log</string>
  <key>StandardErrorPath</key><string>$AUTO/autostart.err</string>
</dict>
</plist>
PLISTEOF

# 4) (재)로드
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "[설치 완료] $PLIST"
echo "  python : $PY"
echo "  workdir: $TS  (포트 $PORT)"
echo "  → 로그인/부팅 시 자동 기동 + 죽으면 자동 재시작."
echo "  확인:  curl -s http://127.0.0.1:$PORT/api/sessions"
echo "  제거:  $0 --uninstall"
