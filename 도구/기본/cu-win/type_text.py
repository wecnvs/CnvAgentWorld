#!/usr/bin/env python3
"""텍스트 입력 — 클립보드 붙여넣기 방식 (Unicode/한국어 완벽 지원)
pyautogui.write()는 ASCII만 지원하므로 클립보드 경유 Ctrl+V 방식 사용.
사용법: type_text.py "입력할 텍스트"
"""
import sys
import os
import time
import pyperclip
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


if len(sys.argv) < 2:
    print("사용법: type_text.py <텍스트>")
    sys.exit(1)

text = sys.argv[1]

# 기존 클립보드 백업 후 복원 (사용자 클립보드 보호)
try:
    original = pyperclip.paste()
except Exception:
    original = ''

try:
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.15)
finally:
    # 클립보드 원복
    try:
        pyperclip.copy(original)
    except Exception:
        pass

preview = text[:40] + ('...' if len(text) > 40 else '')
_hud(f"⌨️  입력: {preview}")
print(f"typed: {preview}")
