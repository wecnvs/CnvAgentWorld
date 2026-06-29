#!/opt/homebrew/bin/python3.13
"""
computer-use-mac HUD 오버레이
- /tmp/cu_active 파일 존재 시에만 표시
- NSWindowSharingNone → AI 스크린샷에 안 찍힘
- 클릭 투과 (마우스 조작 방해 안 함)
- 우상단 ✕ 버튼으로 수동 종료
- /tmp/cu_overlay.log 파일 실시간 표시
실행: nohup python overlay.py > /dev/null 2>&1 &
"""

import os
import objc
from pathlib import Path
from AppKit import (
    NSApplication, NSWindow, NSMakeRect, NSColor, NSFont,
    NSTextView, NSButton, NSFloatingWindowLevel,
    NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
)
from Foundation import NSObject, NSTimer

LOG_FILE    = "/tmp/cu_overlay.log"
ACTIVE_FILE = "/tmp/cu_active"
PID_FILE    = "/tmp/cu_overlay.pid"
MAX_LINES   = 20
WINDOW_W    = 660
WINDOW_H    = 220
WINDOW_X    = 20
WINDOW_Y    = 60

# 닫기 버튼 크기 (별도 작은 창)
CLOSE_W = 22
CLOSE_H = 22


class Terminator(NSObject):
    """닫기 버튼 클릭 → 앱 종료."""

    def closeApp_(self, sender):
        Path(PID_FILE).unlink(missing_ok=True)
        NSApplication.sharedApplication().terminate_(None)


class LogUpdater(NSObject):
    """NSTimer 콜백 핸들러."""

    def init(self):
        self = objc.super(LogUpdater, self).init()
        if self is None:
            return None
        self._tv        = None
        self._window    = None
        self._close_win = None
        self._last      = ""
        self._visible   = False
        return self

    def setTextView_(self, tv):
        self._tv = tv

    def setWindow_(self, window):
        self._window = window

    def setCloseWindow_(self, win):
        self._close_win = win

    def tick_(self, _timer):
        if self._window is None:
            return

        # ── 표시/숨김 ────────────────────────────────────────
        active = Path(ACTIVE_FILE).exists()
        if active and not self._visible:
            self._window.orderFront_(None)
            if self._close_win:
                self._close_win.orderFront_(None)
            self._visible = True
        elif not active and self._visible:
            self._window.orderOut_(None)
            if self._close_win:
                self._close_win.orderOut_(None)
            self._visible = False

        if not active or self._tv is None:
            return

        # ── 로그 갱신 ─────────────────────────────────────────
        try:
            p = Path(LOG_FILE)
            if not p.exists():
                return
            content = p.read_text("utf-8", errors="replace")
            if content == self._last:
                return
            self._last = content
            lines   = content.strip().split("\n")
            display = "\n".join(lines[-MAX_LINES:])
            self._tv.setString_(display)
            self._tv.scrollToEndOfDocument_(None)
        except Exception:
            pass


def _make_window(rect, bg_color, click_through=True):
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect,
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    win.setSharingType_(0)          # AI 스크린샷 제외
    win.setLevel_(NSFloatingWindowLevel)
    win.setBackgroundColor_(bg_color)
    win.setOpaque_(False)
    win.setAlphaValue_(0.93)
    if click_through:
        win.setIgnoresMouseEvents_(True)
    cv = win.contentView()
    cv.setWantsLayer_(True)
    return win, cv


def main():
    Path(PID_FILE).write_text(str(os.getpid()))

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # Accessory: Dock·메뉴바 없음

    # ── 메인 로그 창 (클릭 투과) ──────────────────────────────
    window, cv = _make_window(
        NSMakeRect(WINDOW_X, WINDOW_Y, WINDOW_W, WINDOW_H),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.04, 0.08, 0.04, 0.90),
        click_through=True,
    )
    cv.layer().setCornerRadius_(14.0)
    cv.layer().setMasksToBounds_(True)

    # 타이틀
    title_tv = NSTextView.alloc().initWithFrame_(
        NSMakeRect(14, WINDOW_H - 26, WINDOW_W - 28, 18)
    )
    title_tv.setEditable_(False)
    title_tv.setSelectable_(False)
    title_tv.setBackgroundColor_(NSColor.clearColor())
    title_tv.setDrawsBackground_(False)
    title_tv.setTextColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.3, 0.9, 0.4, 0.65)
    )
    title_tv.setFont_(NSFont.fontWithName_size_("Menlo-Bold", 10.0))
    title_tv.setString_("🤖 Claude Computer Use  —  작업 로그  (이 창은 스크린샷에 찍히지 않음)")
    cv.addSubview_(title_tv)

    # 로그 텍스트
    log_tv = NSTextView.alloc().initWithFrame_(
        NSMakeRect(14, 10, WINDOW_W - 28, WINDOW_H - 38)
    )
    log_tv.setEditable_(False)
    log_tv.setSelectable_(False)
    log_tv.setBackgroundColor_(NSColor.clearColor())
    log_tv.setDrawsBackground_(False)
    log_tv.setTextColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.2, 1.0, 0.45, 1.0)
    )
    log_tv.setFont_(NSFont.fontWithName_size_("Menlo", 11.0))
    log_tv.setString_("")
    cv.addSubview_(log_tv)

    # ── 닫기 버튼 창 (클릭 가능, 별도 창) ────────────────────
    # 메인 창 우상단에 겹쳐 배치
    close_x = WINDOW_X + WINDOW_W - CLOSE_W - 4
    close_y = WINDOW_Y + WINDOW_H - CLOSE_H - 4

    close_win, ccv = _make_window(
        NSMakeRect(close_x, close_y, CLOSE_W, CLOSE_H),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.15, 0.15, 0.15, 0.85),
        click_through=False,   # 클릭 받아야 함
    )
    close_win.setAlphaValue_(0.88)
    ccv.layer().setCornerRadius_(11.0)
    ccv.layer().setMasksToBounds_(True)

    terminator = Terminator.alloc().init()

    close_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(0, 0, CLOSE_W, CLOSE_H)
    )
    close_btn.setTitle_("✕")
    close_btn.setFont_(NSFont.boldSystemFontOfSize_(11.0))
    close_btn.setBordered_(False)
    close_btn.setTarget_(terminator)
    close_btn.setAction_("closeApp:")
    # 글자색: 회백색
    close_btn.setContentTintColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.85, 0.85, 0.85, 1.0)
    )
    ccv.addSubview_(close_btn)

    # ── 타이머 ────────────────────────────────────────────────
    updater = LogUpdater.alloc().init()
    updater.setTextView_(log_tv)
    updater.setWindow_(window)
    updater.setCloseWindow_(close_win)

    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.4, updater, "tick:", None, True
    )

    # 두 창 모두 시작 시 숨김 — cu_active 생기면 tick_()이 표시
    app.run()


if __name__ == "__main__":
    main()
