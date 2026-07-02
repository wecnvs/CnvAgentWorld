#!/usr/bin/env python3
"""마우스 클릭 — pyautogui 기반 (Windows)
사용법: click.py <x> <y> [--double] [--right] [--middle] [--duration <초>]
"""
import sys
import os
import argparse
import subprocess
import time
import pyautogui
from pathlib import Path

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
LOG_FILE = os.path.join(TEMP, 'cu_overlay.log')
PYTHON = sys.executable
TOOLS_DIR = Path(__file__).parent

pyautogui.FAILSAFE = False  # 화면 구석 이동 시 예외 비활성화


def _hud(msg: str):
    try:
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines() if os.path.exists(LOG_FILE) else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        open(LOG_FILE, 'w', encoding='utf-8').write('\n'.join(lines[-200:]) + '\n')
    except Exception:
        pass


parser = argparse.ArgumentParser()
parser.add_argument('x', type=int)
parser.add_argument('y', type=int)
parser.add_argument('--double',   action='store_true')
parser.add_argument('--right',    action='store_true')
parser.add_argument('--middle',   action='store_true')
parser.add_argument('--duration', type=float, default=0.3)
args = parser.parse_args()

# 클릭 위치 시각 플래시 (백그라운드)
try:
    subprocess.Popen(
        [PYTHON, str(TOOLS_DIR / 'click_flash.py'), str(args.x), str(args.y)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
except Exception:
    pass

# 마우스 이동 → 대기 → 클릭
pyautogui.moveTo(args.x, args.y, duration=args.duration)
time.sleep(0.1)

if args.double:
    pyautogui.doubleClick()
    label = f"🖱  더블클릭 ({args.x}, {args.y})"
elif args.right:
    pyautogui.rightClick()
    label = f"🖱  우클릭 ({args.x}, {args.y})"
elif args.middle:
    pyautogui.middleClick()
    label = f"🖱  휠클릭 ({args.x}, {args.y})"
else:
    pyautogui.click()
    label = f"🖱  클릭 ({args.x}, {args.y})"

_hud(label)
print(f"clicked ({args.x}, {args.y})")
