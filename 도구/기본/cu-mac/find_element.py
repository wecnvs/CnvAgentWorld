#!/opt/homebrew/bin/python3.13
"""
UI 요소 정확한 좌표 조회 — 추측 없이 실제 픽셀 좌표 반환

사용법:
  find_element.py windows [앱이름]     # 화면 창 목록 + 위치
  find_element.py addressbar           # 브라우저 주소창 정확한 좌표
  find_element.py app <앱이름>         # 특정 앱 창 상세 정보
"""
import sys
import json
import subprocess
import Quartz
from AppKit import NSWorkspace


# ── CGWindow: 창 목록 ────────────────────────────────────────────
def get_windows(app_filter=None):
    wins = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly |
        Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    results = []
    for w in wins:
        layer = w.get('kCGWindowLayer', 99)
        if layer > 3 or layer < 0:
            continue
        app   = w.get('kCGWindowOwnerName', '')
        title = w.get('kCGWindowName', '')
        if app_filter and app_filter.lower() not in app.lower():
            continue
        b = dict(w.get('kCGWindowBounds', {}))
        width  = int(b.get('Width', 0))
        height = int(b.get('Height', 0))
        if width < 80 or height < 40:
            continue
        results.append({
            'app': app, 'title': title,
            'x': int(b.get('X', 0)), 'y': int(b.get('Y', 0)),
            'w': width, 'h': height,
        })
    return results


# ── osascript: UI 요소 정확한 좌표 ───────────────────────────────
def osascript(code: str):
    r = subprocess.run(['osascript', '-e', code],
                       capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


def get_addressbar_safari():
    """Safari 주소창 중앙 좌표를 Accessibility 트리에서 직접 읽음"""
    code = '''
tell application "System Events"
    tell process "Safari"
        set tb to first UI element of window 1 whose role is "AXToolbar"
        set els to UI elements of tb
        set bestW to 0
        set bestEl to missing value
        repeat with el in els
            if role of el is "AXGroup" then
                set sz to size of el
                set w to item 1 of sz
                if w > bestW then
                    set bestW to w
                    set bestEl to el
                end if
            end if
        end repeat
        if bestEl is missing value then return "ERROR"
        set p to position of bestEl
        set s to size of bestEl
        set cx to (item 1 of p) + ((item 1 of s) / 2)
        set cy to (item 2 of p) + ((item 2 of s) / 2)
        return (round cx) & "," & (round cy)
    end tell
end tell'''
    out, code = osascript(code)
    if code != 0 or 'ERROR' in out or not out:
        return None
    # 출력: "1314, ,, 236" 같은 형태 처리
    parts = [p.strip() for p in out.split(',') if p.strip().lstrip('-').isdigit()]
    if len(parts) >= 2:
        return {'x': int(parts[0]), 'y': int(parts[1])}
    return None


def get_addressbar_chrome():
    """Chrome 주소창 좌표"""
    code = '''
tell application "System Events"
    tell process "Google Chrome"
        set tb to first UI element of window 1 whose role is "AXToolbar"
        set tf to first UI element of tb whose role is "AXTextField"
        set p to position of tf
        set s to size of tf
        set cx to (item 1 of p) + ((item 1 of s) / 2)
        set cy to (item 2 of p) + ((item 2 of s) / 2)
        return (round cx) & "," & (round cy)
    end tell
end tell'''
    out, rc = osascript(code)
    if rc != 0 or not out:
        return None
    parts = [p.strip() for p in out.split(',') if p.strip().lstrip('-').isdigit()]
    if len(parts) >= 2:
        return {'x': int(parts[0]), 'y': int(parts[1])}
    return None


def get_addressbar():
    # Safari 먼저
    result = get_addressbar_safari()
    if result:
        result['app'] = 'Safari'
        return result
    # Chrome
    result = get_addressbar_chrome()
    if result:
        result['app'] = 'Chrome'
        return result
    # fallback: CGWindow 근사
    for browser in ('Safari', 'Google Chrome', 'Firefox', 'Arc', 'Whale'):
        wins = get_windows(browser)
        if wins:
            w = wins[0]
            return {
                'x': w['x'] + w['w'] // 2,
                'y': w['y'] + 36,
                'app': browser,
                'approx': True,
            }
    return None


# ── main ─────────────────────────────────────────────────────────
cmd = sys.argv[1] if len(sys.argv) > 1 else 'windows'

if cmd == 'windows':
    app_filter = sys.argv[2] if len(sys.argv) > 2 else None
    wins = get_windows(app_filter)
    if not wins:
        print("창 없음")
    for w in wins:
        cx = w['x'] + w['w'] // 2
        cy = w['y'] + w['h'] // 2
        print(f"[{w['app']}] \"{w['title']}\"")
        print(f"  위치: ({w['x']}, {w['y']})  크기: {w['w']}x{w['h']}")
        print(f"  창 중앙 클릭: ({cx}, {cy})")
        print(f"  창 상단 10px: ({cx}, {w['y'] + 10})")
        print()

elif cmd in ('addressbar', 'addr'):
    r = get_addressbar()
    if r:
        approx = ' (근사)' if r.get('approx') else ' ✅ 정확'
        print(f"[{r['app']}] 주소창 중앙{approx}: ({r['x']}, {r['y']})")
        print(json.dumps({'x': r['x'], 'y': r['y']}))
    else:
        print("브라우저 창 없음")

elif cmd == 'app':
    app_name = sys.argv[2] if len(sys.argv) > 2 else ''
    wins = get_windows(app_name)
    for w in wins:
        print(json.dumps(w))
