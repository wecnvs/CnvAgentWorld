#!/opt/homebrew/bin/python3.13
"""
클릭 위치 시각 표시 — 빨간 링 플래시
사용법: click_flash.py <x> <y>  (pyautogui/cliclick 좌표계, 좌상단 원점)
0.7초 후 자동 종료. NSWindowSharingNone → AI 스크린샷엔 안 찍힘.
"""
import sys
import objc
from AppKit import (
    NSApplication, NSWindow, NSMakeRect, NSColor, NSView,
    NSFloatingWindowLevel, NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered, NSBezierPath, NSScreen,
)
from Foundation import NSObject, NSTimer

x = int(sys.argv[1])
y = int(sys.argv[2])
R = 24  # 링 반지름

class RingView(NSView):
    def drawRect_(self, rect):
        # 바깥 링 (빨간/주황)
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.2, 0.05, 0.95).set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(3, 3, R * 2 - 6, R * 2 - 6)
        )
        ring.setLineWidth_(4.0)
        ring.stroke()
        # 안쪽 점 (노란색)
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.9, 0.0, 0.9).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(R - 5, R - 5, 10, 10)
        ).fill()

class AutoClose(NSObject):
    def close_(self, _timer):
        NSApplication.sharedApplication().terminate_(None)

app = NSApplication.sharedApplication()
app.setActivationPolicy_(1)

# 좌표 변환: pyautogui (좌상단 원점) → AppKit (좌하단 원점)
screen_h = NSScreen.mainScreen().frame().size.height
win_x = x - R
win_y = screen_h - y - R   # 링 중심이 (x, y)가 되도록

win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(win_x, win_y, R * 2, R * 2),
    NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered,
    False,
)
win.setSharingType_(0)                  # NSWindowSharingNone — AI 스크린샷 제외
win.setLevel_(NSFloatingWindowLevel + 2)
win.setBackgroundColor_(NSColor.clearColor())
win.setOpaque_(False)
win.setAlphaValue_(1.0)
win.setIgnoresMouseEvents_(True)

view = RingView.alloc().initWithFrame_(NSMakeRect(0, 0, R * 2, R * 2))
win.setContentView_(view)
win.makeKeyAndOrderFront_(None)

closer = AutoClose.alloc().init()
NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    0.7, closer, "close:", None, False
)

app.run()
