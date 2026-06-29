#!/opt/homebrew/bin/python3.13
"""
마우스 클릭 — cliclick 기반 (pyautogui moveTo가 macOS에서 동작 안 함)
사용법: click.py <x> <y> [--double] [--right] [--middle] [--duration <초>]
"""
import sys
import argparse
import subprocess
import time
from pathlib import Path

CLICLICK = "/opt/homebrew/bin/cliclick"
PYTHON    = "/opt/homebrew/bin/python3.13"
TOOLS_DIR = Path(__file__).parent

def _hud(msg: str):
    try:
        p = Path("/tmp/cu_overlay.log")
        lines = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        p.write_text("\n".join(lines[-200:]) + "\n", "utf-8")
    except Exception:
        pass

parser = argparse.ArgumentParser()
parser.add_argument("x", type=int)
parser.add_argument("y", type=int)
parser.add_argument("--double",   action="store_true")
parser.add_argument("--right",    action="store_true")
parser.add_argument("--middle",   action="store_true")
parser.add_argument("--duration", type=float, default=0.4)
args = parser.parse_args()

# easing: duration 0.4 → easing 5 (자연스러운 이동)
easing = max(2, int(args.duration * 12))

if args.double:
    click_cmd = f"dc:{args.x},{args.y}"
    label = f"🖱  더블클릭 ({args.x}, {args.y})"
elif args.right:
    click_cmd = f"rc:{args.x},{args.y}"
    label = f"🖱  우클릭 ({args.x}, {args.y})"
elif args.middle:
    # cliclick에 중간버튼 없음 — pyautogui 폴백
    import pyautogui
    pyautogui.middleClick(args.x, args.y)
    _hud(f"🖱  휠클릭 ({args.x}, {args.y})")
    print(f"clicked ({args.x}, {args.y})")
    sys.exit(0)
else:
    click_cmd = f"c:{args.x},{args.y}"
    label = f"🖱  클릭 ({args.x}, {args.y})"

# 클릭 위치 시각 플래시 (백그라운드 — 사용자에게 빨간 링 표시)
subprocess.Popen(
    [PYTHON, str(TOOLS_DIR / "click_flash.py"), str(args.x), str(args.y)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

# 이동 → 잠깐 대기 → 클릭
subprocess.run([
    CLICLICK,
    "-e", str(easing),
    f"m:{args.x},{args.y}",
    "w:120",
    click_cmd,
])

_hud(label)
print(f"clicked ({args.x}, {args.y})")
