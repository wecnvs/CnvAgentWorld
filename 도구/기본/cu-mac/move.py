#!/opt/homebrew/bin/python3.13
"""마우스 포인터 이동(클릭 없음) — cliclick 기반 (macOS)
사용법: move.py <x> <y> [--duration <초>]

★ 안전 클릭 프로토콜의 1단계: 여기로 포인터만 옮긴 뒤 screenshot.py 로 캡처해
   '빨강 커서 마커'가 목표 위에 있는지 눈으로 확인하고, 맞을 때만 click.py 로 클릭한다.
   (macOS는 pyautogui moveTo가 동작하지 않아 cliclick m: 사용)
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _glide

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
# 기본 1.0초 — 원격 그리드(≈5fps)에서 사람처럼 스르륵 보이게. 빠른 이동 필요시 --duration 낮춘다.
parser.add_argument("--duration", type=float, default=_glide.DEFAULT_DURATION)
args = parser.parse_args()

# 현재 위치→목표를 명시 보간해 사람처럼 이동(그리드에 보이게). 위치 못 읽으면 내장 easing 폴백.
toks = _glide.glide_tokens(args.x, args.y, args.duration)
if _glide.is_fallback(toks):
    subprocess.run([CLICLICK, "-e", str(_glide.fallback_easing(args.duration))] + toks)
else:
    subprocess.run([CLICLICK] + toks)
_hud(f"➡️  포인터 이동 ({args.x}, {args.y}) — 캡처로 확인 후 클릭")
print(f"moved to ({args.x}, {args.y})")
