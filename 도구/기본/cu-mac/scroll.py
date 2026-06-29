#!/opt/homebrew/bin/python3.13
"""
마우스 스크롤 — cliclick으로 이동 + Quartz CGEvent로 스크롤
사용법: scroll.py <x> <y> <up|down> [amount]
"""
import sys
import subprocess
import time
from pathlib import Path

CLICLICK = "/opt/homebrew/bin/cliclick"

def _hud(msg: str):
    try:
        p = Path("/tmp/cu_overlay.log")
        lines = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        p.write_text("\n".join(lines[-200:]) + "\n", "utf-8")
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
subprocess.run([CLICLICK, f"m:{x},{y}"])
time.sleep(0.15)

# Quartz 스크롤 이벤트
import Quartz
delta = amount if direction == "up" else -amount
for _ in range(abs(amount)):
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None,
        Quartz.kCGScrollEventUnitLine,
        1,
        1 if delta > 0 else -1,
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.03)

time.sleep(0.2)
arrow = "↑" if direction == "up" else "↓"
_hud(f"🖱  스크롤 {arrow} x{amount} ({x}, {y})")
print(f"scrolled {direction} x{amount} at ({x}, {y})")
