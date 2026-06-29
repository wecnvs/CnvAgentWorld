#!/opt/homebrew/bin/python3.13
"""
키보드 단축키/특수키 입력
사용법: press_key.py "command+c"
        press_key.py "Return"
"""
import sys
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

KEY_MAP = {
    "Return":    "enter",
    "BackSpace": "backspace",
    "Delete":    "delete",
    "Escape":    "esc",
    "Tab":       "tab",
    "space":     "space",
    "super":     "command",
    "cmd":       "command",
    "Up":        "up",
    "Down":      "down",
    "Left":      "left",
    "Right":     "right",
    "Home":      "home",
    "End":       "end",
    "Page_Up":   "pageup",
    "Page_Down": "pagedown",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}

if len(sys.argv) < 2:
    print("사용법: press_key.py 'command+c'")
    sys.exit(1)

key_combo = sys.argv[1]
parts  = key_combo.split("+")
mapped = [KEY_MAP.get(k, k.lower()) for k in parts]

pyautogui.FAILSAFE = True

if len(mapped) == 1:
    pyautogui.press(mapped[0])
else:
    pyautogui.hotkey(*mapped)

time.sleep(0.2)
_hud(f"🔑 키: {key_combo}")
print(f"pressed: {key_combo}")
