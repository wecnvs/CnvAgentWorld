# -*- coding: utf-8 -*-
"""세션 화면/입력 원격 프록시 — 타깃(별칭) → 화면·상태·입력 중계.

채널 두 가지:
- cu-helper : 원격 윈도우/VM의 cu_helper.ps1 HTTP 데몬에 중계(host/port는 서버 내부 resolve에서만).
- local     : 서버 컴퓨터(이 호스트) 자체 화면 — OS 자동감지로 cu-mac(mac)/cu-win(Windows) 도구를
              subprocess 호출해 캡처·입력한다. 워크스페이스가 다른 컴퓨터(mac/Win)로 옮겨가도
              서버컴퓨터가 그대로 원격제어 대상이 된다(하드코딩 없음 — [[workspace-portability-constraint]]).

브라우저는 **타깃 별칭만** 보내고, 연결정보(host/port)는 서버 내부에서만 쓴다(law §7 — 대외비 비노출).
반환 `view_status`는 hostname/화면크기/커서뿐이라 host/IP를 싣지 않는다.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from . import app_targets
from .apps import _cuhelper_url, _http_json   # 동일 패키지 — cu-helper URL/JSON 헬퍼 재사용
from .paths import ROOT

IS_WIN = os.name == "nt"

_INPUT_PATHS = {"move": "/move", "click": "/click", "scroll": "/scroll",
                "type": "/type", "key": "/key"}

_SUPPORTED_CHANNELS = {"cu-helper", "local"}


def _resolve(target_name: str) -> dict:
    cfg = app_targets.resolve(target_name)
    ch = cfg.get("channel")
    if ch == "unknown":
        raise ValueError(f"타깃 '{target_name}' 미등록(자산/대외비/앱실행대상/targets.json 확인)")
    if ch not in _SUPPORTED_CHANNELS:
        raise ValueError(f"화면 원격제어 미지원 채널: {ch} (cu-helper/local만 — ssh/parallels는 후속)")
    return cfg


def _http_raw(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "application/octet-stream")


# ----------------------------------------------------------- local 채널(서버컴퓨터 자체 화면)
def _cu_local_dir() -> Path:
    """OS에 맞는 로컬 컴퓨터유즈 도구 묶음. mac→cu-mac, Windows→cu-win."""
    return ROOT / "도구" / "기본" / ("cu-win" if IS_WIN else "cu-mac")


def _run_cu_tool(script: str, args: list, timeout: float = 25.0):
    """로컬 cu 도구 실행(mac=run_tool.sh, Windows=run_tool.ps1) → (stdout, returncode, stderr)."""
    d = _cu_local_dir()
    sargs = [str(a) for a in args]
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(d / "run_tool.ps1"), script, *sargs]
    else:
        cmd = ["bash", str(d / "run_tool.sh"), script, *sargs]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (p.stdout or ""), p.returncode, (p.stderr or "")


_SIZE_RE = re.compile(r"\((\d+)\s*x\s*(\d+)\)")
_CUR_RE = re.compile(r"cursor=\(?(-?\d+)\s*,\s*(-?\d+)\)?")


def _local_capture(with_cursor: bool = True):
    """로컬 화면 1회 캡처 → (png_bytes, w, h, cursor). screenshot.py stdout('(WxH) cursor=(x,y)')에서 파싱."""
    out = Path(tempfile.gettempdir()) / "cu_local_screen.png"
    args = [str(out)] + ([] if with_cursor else ["--no-cursor"])
    stdout, rc, err = _run_cu_tool("screenshot.py", args, timeout=25)
    if rc != 0 or not out.exists():
        raise RuntimeError(f"로컬 화면 캡처 실패(rc={rc}): {(err or stdout)[:200]}")
    raw = out.read_bytes()
    w = h = 0
    m = _SIZE_RE.search(stdout)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    cur: dict = {}
    cm = _CUR_RE.search(stdout)
    if cm:
        cur = {"x": int(cm.group(1)), "y": int(cm.group(2))}
    return raw, w, h, cur


def _to_jpeg(raw: bytes, max_width: int, quality: int):
    """PNG bytes → 축소+JPEG. 실패 시 원본 그대로."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGB")
        w, h = im.size
        if max_width and w > max_width:
            im = im.resize((max_width, max(1, round(h * max_width / w))), Image.BILINEAR)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=max(30, min(95, int(quality))))
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, "image/png"


# 서버 내부 직접 캡처(고속 경로) 캐시 — 프레임마다 subprocess/PIL-import/PNG-디스크를 없앤다.
# 종전: 프레임당 screenshot.py subprocess + Quartz + PNG저장 + 재읽기 + PNG디코드 + JPEG재인코딩(수백 ms→~2-5fps).
# 개선: 서버(8686, 화면녹화 권한 상속) 프로세스 안에서 Quartz 캡처→리사이즈→JPEG 한 번에(~15-30fps 목표).
_FAST = {"ok": None, "Quartz": None, "Image": None, "ImageDraw": None}


def _fast_mods():
    if _FAST["ok"] is None:
        try:
            import Quartz                                   # noqa
            from PIL import Image, ImageDraw               # noqa
            _FAST.update(ok=True, Quartz=Quartz, Image=Image, ImageDraw=ImageDraw)
        except Exception:
            _FAST["ok"] = False
    return _FAST["ok"]


def _local_screenshot_fast(max_width: int, quality: int):
    """macOS 서버 내부 Quartz 직접 캡처→JPEG(+커서 마커). 실패 시 예외 → 호출부가 subprocess로 폴백."""
    if IS_WIN or not _fast_mods():
        raise RuntimeError("fast capture 미지원(Windows 또는 Quartz/PIL 없음)")
    Q = _FAST["Quartz"]; Image = _FAST["Image"]; ImageDraw = _FAST["ImageDraw"]
    from io import BytesIO
    cgimg = Q.CGWindowListCreateImage(
        Q.CGRectInfinite, Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID, Q.kCGWindowImageDefault)
    if cgimg is None:
        raise RuntimeError("CGWindowListCreateImage None(화면녹화 권한 없음?)")
    pw = Q.CGImageGetWidth(cgimg); ph = Q.CGImageGetHeight(cgimg)
    provider = Q.CGImageGetDataProvider(cgimg)
    raw = Q.CGDataProviderCopyData(provider)
    bpr = Q.CGImageGetBytesPerRow(cgimg)
    im = Image.frombuffer("RGBA", (pw, ph), bytes(raw), "raw", "BGRA", bpr, 1).convert("RGB")
    # 논리 화면 크기(커서 좌표계) — 물리(레티나) 대비 스케일 계산용
    try:
        bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
        lw = int(bounds.size.width); lh = int(bounds.size.height)
    except Exception:
        lw, lh = pw, ph
    # 출력 리사이즈(가로 max_width)
    ow, oh = im.size
    if max_width and ow > max_width:
        oh = max(1, round(oh * max_width / ow)); ow = max_width
        im = im.resize((ow, oh), Image.BILINEAR)
    # 커서 마커(하드웨어 커서는 캡처에 안 찍히므로 직접 그림 — 그리드에서 커서 위치가 보이게)
    try:
        loc = Q.CGEventGetLocation(Q.CGEventCreate(None))
        cx = int(loc.x * ow / max(1, lw)); cy = int(loc.y * oh / max(1, lh))
        d = ImageDraw.Draw(im)
        r = 9
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=3)
        d.line([cx - r - 4, cy, cx + r + 4, cy], fill=(255, 0, 0), width=1)
        d.line([cx, cy - r - 4, cx, cy + r + 4], fill=(255, 0, 0), width=1)
    except Exception:
        pass
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=max(30, min(95, int(quality))))
    return buf.getvalue(), "image/jpeg"


def _local_screenshot(max_width: int, quality: int):
    # 고속 경로 우선, 실패 시 기존 subprocess 경로로 폴백(무회귀).
    try:
        return _local_screenshot_fast(max_width, quality)
    except Exception:
        raw, _w, _h, _cur = _local_capture(with_cursor=True)
        return _to_jpeg(raw, max_width, quality)


def _local_view_status() -> dict:
    import socket
    # with_cursor=True여야 screenshot.py가 'cursor=(x,y)'를 찍어 좌표가 파싱된다(이미지는 버림).
    _raw, w, h, cur = _local_capture(with_cursor=True)
    return {"ok": True, "hostname": socket.gethostname(),
            "screen": {"w": w, "h": h, "x": 0, "y": 0}, "cursor": cur,
            "os": ("windows" if IS_WIN else "macos")}


# 프런트는 키를 cu-helper(Windows SendKeys) 문법으로 보낸다(`{ENTER}`, `^c`, `%{F4}`).
# local(cu-mac/cu-win press_key.py)은 pyautogui 문법(`command+c`, `enter`)을 받으므로 서버에서 변환한다.
_SK_KEYNAME = {
    "ENTER": "enter", "RETURN": "enter",
    "ESC": "esc", "ESCAPE": "esc", "TAB": "tab",
    "BACKSPACE": "backspace", "BS": "backspace", "BKSP": "backspace",
    "DELETE": "delete", "DEL": "delete", "INSERT": "insert", "INS": "insert",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
    "HOME": "home", "END": "end",
    "PGUP": "pageup", "PGDN": "pagedown", "PAGEUP": "pageup", "PAGEDOWN": "pagedown",
    "SPACE": "space",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}


def _sendkeys_to_combo(keys: str) -> str:
    """SendKeys 문법(`^c`,`%{F4}`,`+{TAB}`,`{ENTER}`,`~`) → press_key.py용 'mod+...+key'(pyautogui 키명).

    ^=주 modifier(mac:command / Windows:ctrl — 복사/붙여넣기 등이 OS에 맞게 동작), %=alt, +=shift.
    """
    s = (keys or "").strip()
    if not s:
        return ""
    primary = "ctrl" if IS_WIN else "command"
    modmap = {"^": primary, "%": "alt", "+": "shift"}
    mods: list = []
    i = 0
    while i < len(s) and s[i] in modmap:
        mods.append(modmap[s[i]])
        i += 1
    rest = s[i:]
    if rest == "~":
        key = "enter"
    elif rest.startswith("{") and rest.endswith("}"):
        key = _SK_KEYNAME.get(rest[1:-1].strip().upper(), rest[1:-1].strip().lower())
    elif len(rest) == 1:
        key = rest.lower()
    elif rest:
        key = _SK_KEYNAME.get(rest.upper(), rest.lower())
    else:
        key = ""
    return "+".join(mods + ([key] if key else []))


def _local_send_input(action: str, params: dict) -> dict:
    a = (action or "").lower()
    p = params or {}
    if a == "move":
        _run_cu_tool("move.py", [int(p.get("x", 0)), int(p.get("y", 0))])
    elif a == "click":
        args = [int(p.get("x", 0)), int(p.get("y", 0))]
        btn = (p.get("button") or "left").lower()
        if p.get("double"):
            args.append("--double")
        if btn == "right":
            args.append("--right")
        elif btn == "middle":
            args.append("--middle")
        _run_cu_tool("click.py", args)
    elif a == "scroll":
        amt = int(p.get("amount", 3) or 3)
        direction = "up" if amt >= 0 else "down"
        _run_cu_tool("scroll.py", [int(p.get("x", 0)), int(p.get("y", 0)), direction, abs(amt) or 3])
    elif a == "type":
        _run_cu_tool("type_text.py", [p.get("text", "")])
    elif a == "key":
        combo = _sendkeys_to_combo(p.get("keys", ""))
        if combo:
            _run_cu_tool("press_key.py", [combo])
    else:
        raise ValueError(f"알 수 없는 입력 action: {action}")
    return {"ok": True}


# ----------------------------------------------------------- 공개 API (채널 분기)
def screenshot(target_name: str, max_width: int = 1280, quality: int = 70):
    """타깃 화면 (bytes, content_type). 보기는 락 불필요.

    local=서버컴퓨터 자체 화면(cu-mac/cu-win 캡처→축소JPEG). cu-helper=원격 데몬에서 JPEG 직접 요청
    (5MB PNG 대신 ~150KB)·구버전이면 서버 PIL 변환 폴백."""
    cfg = _resolve(target_name)
    if cfg.get("channel") == "local":
        return _local_screenshot(max_width, quality)
    raw, ctype = _http_raw(_cuhelper_url(cfg, f"/screenshot?fmt=jpg&w={int(max_width)}&q={int(quality)}"))
    if (ctype and "jpeg" in ctype.lower()) or raw[:2] == b"\xff\xd8":
        return raw, "image/jpeg"           # 이미 JPEG(신 헬퍼) → 패스스루
    return _to_jpeg(raw, max_width, quality)


def view_status(target_name: str) -> dict:
    """타깃 /status — hostname·화면크기(좌표 매핑용)·커서. host/IP 없음(브라우저 안전)."""
    cfg = _resolve(target_name)
    if cfg.get("channel") == "local":
        return _local_view_status()
    return _http_json(_cuhelper_url(cfg, "/status"), timeout=4.0)


def send_input(target_name: str, action: str, params: dict) -> dict:
    """입력 중계(move/click/scroll/type/key). 락 검증은 라우터에서 선행."""
    cfg = _resolve(target_name)
    if cfg.get("channel") == "local":
        return _local_send_input(action, params)
    path = _INPUT_PATHS.get((action or "").lower())
    if not path:
        raise ValueError(f"알 수 없는 입력 action: {action}")
    return _http_json(_cuhelper_url(cfg, path), params or {})
