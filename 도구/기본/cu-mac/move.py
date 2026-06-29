#!/opt/homebrew/bin/python3.13
"""마우스 포인터 이동(클릭 없음) — cliclick 기반 (macOS)
사용법: move.py <x> <y> [--duration <초>]

★ 안전 클릭 프로토콜의 1단계: 여기로 포인터만 옮긴 뒤 screenshot.py 로 캡처해
   '빨강 커서 마커'가 목표 위에 있는지 눈으로 확인하고, 맞을 때만 click.py 로 클릭한다.
   (macOS는 pyautogui moveTo가 동작하지 않아 cliclick m: 사용)
"""
import argparse
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


parser = argparse.ArgumentParser()
parser.add_argument("x", type=int)
parser.add_argument("y", type=int)
parser.add_argument("--duration", type=float, default=0.4)
args = parser.parse_args()

easing = max(2, int(args.duration * 12))
subprocess.run([CLICLICK, "-e", str(easing), f"m:{args.x},{args.y}"])
_hud(f"➡️  포인터 이동 ({args.x}, {args.y}) — 캡처로 확인 후 클릭")
print(f"moved to ({args.x}, {args.y})")
