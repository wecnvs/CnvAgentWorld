#!/usr/bin/env python3
"""키보드 단축키 / 키 입력 — pyautogui 기반
사용법: press_key.py "Return"
        press_key.py "ctrl+c"
        press_key.py "win+d"

macOS ↔ Windows 키 매핑:
  command → ctrl  (대부분의 단축키)
  command+space → win  (Spotlight → Windows Search)
  command+m → win+d 또는 개별 창 최소화
"""
import sys
import os
import time
import pyautogui

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
LOG_FILE = os.path.join(TEMP, 'cu_overlay.log')

pyautogui.FAILSAFE = False

# macOS → Windows 키 자동 변환 맵
KEY_MAP = {
    'command': 'ctrl',
    'cmd':     'ctrl',
    'option':  'alt',
    'Return':  'enter',
    'Delete':  'backspace',
    'Escape':  'esc',
    'Space':   'space',
}


def _hud(msg: str):
    try:
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines() if os.path.exists(LOG_FILE) else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        open(LOG_FILE, 'w', encoding='utf-8').write('\n'.join(lines[-200:]) + '\n')
    except Exception:
        pass


if len(sys.argv) < 2:
    print("사용법: press_key.py <키 또는 단축키>")
    sys.exit(1)

key_str = sys.argv[1]

# 키 변환 적용
keys = [KEY_MAP.get(k, k) for k in key_str.split('+')]

if len(keys) == 1:
    pyautogui.press(keys[0])
else:
    pyautogui.hotkey(*keys)

time.sleep(0.05)
_hud(f"⌨️  키: {key_str}")
print(f"pressed: {key_str}")
