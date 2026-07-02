#!/usr/bin/env python3
"""스크린샷 캡처 — mss 기반 (논리 해상도 매핑)
사용법: screenshot.py [출력경로]
"""
import sys
import os
import time
import mss
import mss.tools
from PIL import Image
import pyautogui

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
DEFAULT_PATH = os.path.join(TEMP, 'cu_screen.png')
LOG_FILE = os.path.join(TEMP, 'cu_overlay.log')

output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH


def _hud(msg: str):
    try:
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines() if os.path.exists(LOG_FILE) else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        open(LOG_FILE, 'w', encoding='utf-8').write('\n'.join(lines[-200:]) + '\n')
    except Exception:
        pass


with mss.mss() as sct:
    # 주 모니터 (monitors[0] = 전체, monitors[1] = 주 모니터)
    monitor = sct.monitors[1]
    raw = sct.grab(monitor)
    img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')

# pyautogui 논리 해상도로 리사이즈 (DPI 스케일링 대응)
logical_w, logical_h = pyautogui.size()
if img.size != (logical_w, logical_h):
    img = img.resize((logical_w, logical_h), Image.LANCZOS)

# ── 마우스 포인터 위치 마커 오버레이 (★ 클릭 전 포인터 위치 시각 확인용) ──
# mss/Quartz 캡처는 하드웨어 커서를 안 찍으므로, 현재 pyautogui 좌표(클릭과 동일계)에
# 빨강 원+십자선+좌표 라벨을 그려 넣는다. --no-cursor 로 끌 수 있다.
cursor_xy = None
if '--no-cursor' not in sys.argv:
    try:
        from PIL import ImageDraw
        cx, cy = pyautogui.position()
        cursor_xy = (cx, cy)
        d = ImageDraw.Draw(img)
        r = 14
        d.ellipse([cx-r-2, cy-r-2, cx+r+2, cy+r+2], outline=(255, 255, 255), width=3)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 0, 0), width=2)
        d.line([cx-r-10, cy, cx+r+10, cy], fill=(255, 0, 0), width=1)
        d.line([cx, cy-r-10, cx, cy+r+10], fill=(255, 0, 0), width=1)
        d.ellipse([cx-2, cy-2, cx+2, cy+2], fill=(255, 0, 0))
        lbl = f"({cx},{cy})"
        lx, ly = cx + r + 6, cy + r + 4
        if lx > logical_w - 70: lx = cx - r - 64
        if ly > logical_h - 18: ly = cy - r - 18
        d.rectangle([lx-2, ly-1, lx+8*len(lbl)+2, ly+13], fill=(0, 0, 0))
        d.text((lx, ly), lbl, fill=(255, 255, 0))
    except Exception:
        pass

img.save(output_path)
_hud(f"📸 스크린샷 ({logical_w}x{logical_h}) 커서={cursor_xy}")
print(f"saved: {output_path} ({logical_w}x{logical_h}) cursor={cursor_xy}")
