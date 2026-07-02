#!/usr/bin/env python3
"""HUD 오버레이 로그 기록
사용법:
  log.py "메시지"           # 일반 상태 메시지
  log.py --think "내용"    # AI 판단/분석 (💭 prefix)
  log.py --done "내용"     # 작업 완료 (✅ + cu_active 마커 제거)
"""
import sys
import os
import time

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
LOG_FILE    = os.path.join(TEMP, 'cu_overlay.log')
ACTIVE_FILE = os.path.join(TEMP, 'cu_active')


def append_log(line: str):
    lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines() if os.path.exists(LOG_FILE) else []
    lines.append(f"[{time.strftime('%H:%M:%S')}] {line}")
    open(LOG_FILE, 'w', encoding='utf-8').write('\n'.join(lines[-200:]) + '\n')


args = sys.argv[1:]

if not args:
    print("사용법: log.py [--think|--done] <메시지>")
    sys.exit(1)

if args[0] == '--think':
    msg = ' '.join(args[1:])
    append_log(f"💭 {msg}")
    # cu_active 마커 생성 (HUD 표시)
    open(ACTIVE_FILE, 'w').write('')
    print(f"[think] {msg}")

elif args[0] == '--done':
    msg = ' '.join(args[1:])
    append_log(f"✅ {msg}")
    # cu_active 마커 제거 (HUD 자동 숨김)
    if os.path.exists(ACTIVE_FILE):
        os.remove(ACTIVE_FILE)
    print(f"[done] {msg}")

else:
    msg = ' '.join(args)
    append_log(msg)
    # cu_active 마커 생성
    open(ACTIVE_FILE, 'w').write('')
    print(f"[log] {msg}")
