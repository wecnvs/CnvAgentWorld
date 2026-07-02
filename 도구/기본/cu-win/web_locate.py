#!/usr/bin/env python3
"""웹앱 요소 정확 좌표 찾기 — Windows UI Automation 기반 (픽셀 추측 금지용)
사용법: web_locate.py "<버튼/요소 텍스트 일부>" [창제목일부]

브라우저(Chrome/Edge) 화면의 버튼·링크 좌표를 픽셀로 추측하지 말고, 접근성 트리에서
요소를 이름으로 찾아 **정확한 화면 중심 좌표**를 출력한다. 그 좌표로 move.py→캡처확인→click.py.

★ Chrome 웹콘텐츠가 UIA 트리에 나오려면 렌더러 접근성이 켜져야 한다.
  안 보이면 Chrome 을 --force-renderer-accessibility 로 재기동(launch_chrome_a11y 참고).

출력: "x y | ControlType | '실제이름'"  (없으면 NOTFOUND)
"""
import sys
import time
import uiautomation as auto


def _s(fn, default=""):
    """COMError/stale 안전 속성 접근."""
    try:
        return fn()
    except Exception:
        return default


def find_browser_window(win_hint):
    root = auto.GetRootControl()
    cand = None
    for w in _s(lambda: root.GetChildren(), []):
        cls = _s(lambda: w.ClassName or "")
        nm = _s(lambda: w.Name or "")
        if cls.startswith("Chrome_WidgetWin"):
            if win_hint and win_hint in nm:
                return w
            if cand is None and nm:
                cand = w
    return cand


WANT = {"ButtonControl", "HyperlinkControl", "MenuItemControl", "ListItemControl",
        "TabItemControl", "CheckBoxControl", "EditControl", "TextControl"}


def search(ctrl, target, acc, depth=0):
    # 첫 적합 매치를 찾으면 즉시 True 반환(early-exit) — 전체 트리 완주 방지(속도).
    if depth > 32:
        return False
    for c in _s(lambda: ctrl.GetChildren(), []):
        nm = _s(lambda: c.Name or "")
        if target in nm and nm:
            ct = _s(lambda: c.ControlTypeName or "")
            if ct in WANT or target == nm:
                r = _s(lambda: c.BoundingRectangle, None)
                try:
                    if r and r.width() > 0 and r.height() > 0:
                        acc.append((r.width() * r.height(), r, ct, nm))
                        return True
                except Exception:
                    pass
        if search(c, target, acc, depth + 1):
            return True
    return False


def main():
    target = sys.argv[1]
    win_hint = sys.argv[2] if len(sys.argv) > 2 else "CNV Agent Hub"
    auto.SetGlobalSearchTimeout(6)

    for attempt in range(3):
        win = find_browser_window(win_hint)
        if win is None:
            time.sleep(1.0)
            continue
        acc = []
        search(win, target, acc)
        if acc:
            acc.sort(key=lambda t: t[0])      # 가장 작은(=구체적) 매치 우선
            _, r, ct, nm = acc[0]
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            print(f"{cx} {cy} | {ct} | {nm!r}")
            return
        time.sleep(1.0)
    print(f"NOTFOUND ('{target}' — 브라우저 미발견 또는 접근성 트리에 없음. "
          f"Chrome 을 --force-renderer-accessibility 로 재기동 필요할 수 있음)")
    sys.exit(2)


main()
