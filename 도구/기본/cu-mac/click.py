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

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))
import _glide

CLICLICK = "/opt/homebrew/bin/cliclick"
PYTHON    = "/opt/homebrew/bin/python3.13"

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
# 기본 1.0초 — 원격 그리드(≈5fps)에서 커서가 사람처럼 목표까지 스르륵 이동해 클릭하는 게 보이게.
# 빠른 무관전 조작이 필요하면 호출부가 --duration 을 낮춘다.
parser.add_argument("--duration", type=float, default=_glide.DEFAULT_DURATION)
args = parser.parse_args()

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

# 사람처럼 보간 이동 → 잠깐 대기 → 클릭 (한 cliclick 호출로 원자적 — move 후 드리프트 없음)
toks = _glide.glide_tokens(args.x, args.y, args.duration)
if _glide.is_fallback(toks):
    subprocess.run([CLICLICK, "-e", str(_glide.fallback_easing(args.duration)),
                    f"m:{args.x},{args.y}", "w:120", click_cmd])
else:
    subprocess.run([CLICLICK] + toks + ["w:120", click_cmd])

_hud(label)
print(f"clicked ({args.x}, {args.y})")
