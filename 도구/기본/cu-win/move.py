#!/usr/bin/env python3
"""마우스 포인터 이동(클릭 없음) — pyautogui 기반 (Windows)
사용법: move.py <x> <y> [--duration <초>]

★ 안전 클릭 프로토콜의 1단계: 여기로 포인터만 옮긴 뒤 screenshot.py 로 캡처해
   '빨강 커서 마커'가 목표 위에 있는지 눈으로 확인하고, 맞을 때만 click.py 로 클릭한다.
"""
import sys
import os
import argparse
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


parser = argparse.ArgumentParser()
parser.add_argument('x', type=int)
parser.add_argument('y', type=int)
parser.add_argument('--duration', type=float, default=0.3)
args = parser.parse_args()

pyautogui.moveTo(args.x, args.y, duration=args.duration)
time.sleep(0.05)
pos = pyautogui.position()
_hud(f"➡️  포인터 이동 ({pos.x}, {pos.y}) — 캡처로 확인 후 클릭")
print(f"moved to ({args.x}, {args.y}); cursor now ({pos.x}, {pos.y})")
