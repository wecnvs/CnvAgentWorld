#!/usr/bin/env python3
"""UI 요소 정확한 좌표 조회 — win32gui 기반
사용법:
  find_element.py windows [앱이름]    # 화면 창 목록 + 위치
  find_element.py addressbar          # 브라우저 주소창 근사 좌표
  find_element.py app <앱이름>        # 특정 앱 창 상세 정보
"""
import sys
import os
import json
import win32gui
import win32con
import win32process
import psutil


def get_windows(app_filter=None):
    results = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        rect = win32gui.GetWindowRect(hwnd)
        x, y, right, bottom = rect
        w = right - x
        h = bottom - y
        if w < 80 or h < 40:
            return
        title = win32gui.GetWindowText(hwnd)
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app = psutil.Process(pid).name().replace('.exe', '')
        except Exception:
            app = '?'
        if app_filter and app_filter.lower() not in app.lower() and app_filter.lower() not in title.lower():
            return
        results.append({'app': app, 'title': title, 'x': x, 'y': y, 'w': w, 'h': h})

    win32gui.EnumWindows(callback, None)
    # 크기 큰 순 정렬 (주요 창 먼저)
    return sorted(results, key=lambda r: r['w'] * r['h'], reverse=True)


def get_addressbar():
    """브라우저 주소창 근사 좌표 — 창 상단 + 주소창 높이 추정"""
    for browser_name in ('chrome', 'msedge', 'firefox', 'whale', 'opera', 'brave'):
        wins = get_windows(browser_name)
        if wins:
            w = wins[0]
            # 브라우저 주소창은 보통 창 상단 60~80px 지점, 가로 중앙
            est_x = w['x'] + w['w'] // 2
            est_y = w['y'] + 68
            return {'x': est_x, 'y': est_y, 'app': w['app'], 'approx': True}
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
