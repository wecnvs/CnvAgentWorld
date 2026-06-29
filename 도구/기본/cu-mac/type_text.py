#!/opt/homebrew/bin/python3.13
"""
텍스트 입력 (한국어·유니코드 포함)
ASCII는 직접 입력, 비ASCII는 클립보드(pbcopy) 경유
"""
import sys
import subprocess
import pyautogui
import time
from pathlib import Path

def _hud(msg: str):
    try:
        p = Path("/tmp/cu_overlay.log")
        lines = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        p.write_text("\n".join(lines[-200:]) + "\n", "utf-8")
    except Exception:
        pass

if len(sys.argv) < 2:
    print("사용법: type_text.py '텍스트'")
    sys.exit(1)

text = sys.argv[1]
preview = text[:40] + ("..." if len(text) > 40 else "")
pyautogui.FAILSAFE = True

if text.isascii():
    pyautogui.write(text, interval=0.02)
else:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    time.sleep(0.1)
    pyautogui.hotkey("command", "v")

time.sleep(0.2)
_hud(f'⌨  입력: "{preview}"')
print(f"typed: {preview}")
