#!/opt/homebrew/bin/python3.13
"""
스크린샷 캡처 → pyautogui 논리 해상도로 리사이즈 저장
CGWindowListCreateImage 사용: NSWindowSharingNone 창(오버레이) 자동 제외
"""
import sys
import time
from pathlib import Path
import pyautogui
from PIL import Image
import Quartz

def _hud(msg: str):
    try:
        p = Path("/tmp/cu_overlay.log")
        lines = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
        lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        p.write_text("\n".join(lines[-200:]) + "\n", "utf-8")
    except Exception:
        pass

args = sys.argv[1:]
request_permission = "--request-permission" in args
no_cursor = "--no-cursor" in args
pos_args = [a for a in args if not a.startswith("--")]
save_path = pos_args[0] if pos_args else "/tmp/cu_screen.png"


def _screen_capture_allowed() -> bool:
    fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    if fn is None:
        return True
    try:
        return bool(fn())
    except Exception:
        return True


if not _screen_capture_allowed():
    msg = (
        "macOS 화면 녹화 권한 없음: 현재 실행 주체에 Screen Recording 권한이 없어 "
        "창 내용이 빠진 바탕화면 캡처가 될 수 있습니다. 시스템 설정 > 개인정보 보호 및 보안 > "
        "화면 및 시스템 오디오 녹화(또는 화면 기록)에서 대시보드/터미널/Python 실행 주체를 허용한 뒤 "
        "대시보드 서버를 재시작하세요. 오인 방지를 위해 캡처를 중단합니다."
    )
    _hud("❌ " + msg)
    print(msg, file=sys.stderr)
    if request_permission and hasattr(Quartz, "CGRequestScreenCaptureAccess"):
        try:
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass
    sys.exit(2)

cgimg = Quartz.CGWindowListCreateImage(
    Quartz.CGRectInfinite,
    Quartz.kCGWindowListOptionOnScreenOnly,
    Quartz.kCGNullWindowID,
    Quartz.kCGWindowImageDefault,
)

w = Quartz.CGImageGetWidth(cgimg)
h = Quartz.CGImageGetHeight(cgimg)
data_provider = Quartz.CGImageGetDataProvider(cgimg)
raw_data = Quartz.CGDataProviderCopyData(data_provider)
bytes_per_row = Quartz.CGImageGetBytesPerRow(cgimg)

import ctypes
buf = (ctypes.c_uint8 * len(raw_data)).from_buffer_copy(bytes(raw_data))
img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", bytes_per_row, 1)
img = img.convert("RGB")

logical_w, logical_h = pyautogui.size()
if (img.width, img.height) != (logical_w, logical_h):
    img = img.resize((logical_w, logical_h), Image.LANCZOS)

# ── 마우스 포인터 위치 마커 오버레이 (★ 클릭 전 포인터 위치 시각 확인용) ──
# Quartz 캡처는 하드웨어 커서를 안 찍으므로, 현재 pyautogui 좌표(클릭과 동일계)에
# 빨강 원+십자선+좌표 라벨을 그려 넣는다. --no-cursor 로 끌 수 있다.
cursor_xy = None
if not no_cursor:
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

img.save(save_path)
_hud(f"📸 스크린샷 ({logical_w}x{logical_h}) 커서={cursor_xy}")
print(f"saved: {save_path} ({logical_w}x{logical_h}) cursor={cursor_xy}")
