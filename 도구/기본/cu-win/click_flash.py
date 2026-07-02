#!/usr/bin/env python3
"""클릭 위치 시각 표시 — 빨간 링 플래시 (Windows, tkinter)
0.7초 후 자동 종료. SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) → AI 스크린샷 제외.
사용법: click_flash.py <x> <y>
"""
import sys
import ctypes
import tkinter as tk

x = int(sys.argv[1])
y = int(sys.argv[2])
R = 24  # 링 반지름

root = tk.Tk()
root.title("CU_CLICK_FLASH")
root.overrideredirect(True)
root.attributes('-topmost', True)
# 검정(#000001)을 투명색으로 지정
root.attributes('-transparentcolor', '#000001')
root.configure(bg='#000001')
root.geometry(f"{R*2}x{R*2}+{x - R}+{y - R}")

canvas = tk.Canvas(root, width=R*2, height=R*2, bg='#000001', highlightthickness=0)
canvas.pack()
canvas.create_oval(3, 3, R*2-3, R*2-3, outline='#ff3311', width=4)
canvas.create_oval(R-5, R-5, R+5, R+5, fill='#ffee00', outline='')

root.update()

# Win32: click-through + 스크린샷 제외
try:
    GWL_EXSTYLE          = -20
    WS_EX_TRANSPARENT    = 0x00000020
    WS_EX_LAYERED        = 0x00080000
    WDA_EXCLUDEFROMCAPTURE = 0x00000011

    hwnd = ctypes.windll.user32.FindWindowW(None, "CU_CLICK_FLASH")
    if hwnd:
        cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                            cur | WS_EX_TRANSPARENT | WS_EX_LAYERED)
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
except Exception:
    pass

root.after(700, root.destroy)
root.mainloop()
