#!/usr/bin/env python3
"""마우스 스크롤 — pyautogui 기반
사용법: scroll.py <x> <y> <up|down> [amount=3]
"""
import sys
import os
import time
import pyautogui

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
LOG_FILE = os.path.join(TEMP, 'cu_overlay.log')

pyautogui.FAILSAFE = False


def _hud(msg: str):
    try:
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines() if os.path.exists(LOG_FILE) else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        open(LOG_FILE, 'w', encoding='utf-8').write('\n'.join(lines[-200:]) + '\n')
    except Exception:
        pass


if len(sys.argv) < 4:
    print("사용법: scroll.py <x> <y> <up|down> [amount=3]")
    sys.exit(1)

x         = int(sys.argv[1])
y         = int(sys.argv[2])
direction = sys.argv[3].lower()
amount    = int(sys.argv[4]) if len(sys.argv) > 4 else 3

# 커서 이동
pyautogui.moveTo(x, y, duration=0.2)
time.sleep(0.1)

# pyautogui scroll: 양수 = 위, 음수 = 아래
delta = amount if direction == 'up' else -amount
pyautogui.scroll(delta)

time.sleep(0.1)
arrow = '↑' if direction == 'up' else '↓'
_hud(f"🖱  스크롤 {arrow} x{amount} ({x}, {y})")
print(f"scrolled {direction} x{amount} at ({x}, {y})")
