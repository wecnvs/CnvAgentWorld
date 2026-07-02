#!/usr/bin/env python3
"""HUD 오버레이 — tkinter 기반 (Windows)
- /tmp_equivalent/cu_active 마커 존재 시 표시, 없으면 숨김
- click-through (WS_EX_TRANSPARENT)
- 스크린샷 제외 (SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+)
- 종료 버튼 포함
"""
import os
import sys
import time
import ctypes
import tkinter as tk
from tkinter import font as tkfont

TEMP = os.environ.get('TEMP', os.environ.get('TMP', 'C:\\Windows\\Temp'))
LOG_FILE    = os.path.join(TEMP, 'cu_overlay.log')
ACTIVE_FILE = os.path.join(TEMP, 'cu_active')
PID_FILE    = os.path.join(TEMP, 'cu_overlay.pid')

GWL_EXSTYLE            = -20
WS_EX_TRANSPARENT      = 0x00000020
WS_EX_NOACTIVATE       = 0x08000000
WS_EX_LAYERED          = 0x00080000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def set_win32_flags(hwnd, click_through: bool):
    cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if click_through:
        new = cur | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_LAYERED
    else:
        # 종료 버튼 창은 click-through 해제
        new = (cur | WS_EX_LAYERED | WS_EX_NOACTIVATE) & ~WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)


class HUDApp:
    W, H = 430, 310
    CLOSE_SIZE = 22

    def __init__(self):
        # ── 메인 HUD 창 ──────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("CU_HUD_MAIN")
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', 0.88)
        self.root.configure(bg='#111111')

        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{self.W}x{self.H}+{sw - self.W - 20}+20")
        self.root.withdraw()

        # 텍스트 위젯
        mono = tkfont.Font(family='Consolas', size=10)
        self.text = tk.Text(self.root, bg='#111111', fg='#00ff88',
                            font=mono, wrap=tk.WORD,
                            borderwidth=0, highlightthickness=0,
                            state=tk.DISABLED)
        self.text.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # ── 닫기 버튼 창 (별도 창, 클릭 가능) ────────────────
        self.close_win = tk.Toplevel(self.root)
        self.close_win.title("CU_HUD_CLOSE")
        self.close_win.overrideredirect(True)
        self.close_win.attributes('-topmost', True)
        self.close_win.configure(bg='#333333')
        cs = self.CLOSE_SIZE
        self.close_win.geometry(f"{cs}x{cs}+{sw - cs - 20}+20")
        self.close_win.withdraw()

        btn = tk.Button(self.close_win, text='✕', bg='#555555', fg='white',
                        relief='flat', font=tkfont.Font(size=9, weight='bold'),
                        command=self._quit)
        btn.pack(fill=tk.BOTH, expand=True)

        # win32 플래그 적용
        self.root.update()
        self.close_win.update()

        try:
            hwnd_main  = ctypes.windll.user32.FindWindowW(None, "CU_HUD_MAIN")
            hwnd_close = ctypes.windll.user32.FindWindowW(None, "CU_HUD_CLOSE")
            if hwnd_main:
                set_win32_flags(hwnd_main, click_through=True)
            if hwnd_close:
                set_win32_flags(hwnd_close, click_through=False)
        except Exception:
            pass

        self.root.after(400, self._tick)

    def _tick(self):
        active = os.path.exists(ACTIVE_FILE)
        if active:
            self.root.deiconify()
            self.close_win.deiconify()
            self._update_log()
        else:
            self.root.withdraw()
            self.close_win.withdraw()
        self.root.after(400, self._tick)

    def _update_log(self):
        if not os.path.exists(LOG_FILE):
            return
        lines = open(LOG_FILE, encoding='utf-8', errors='replace').read().splitlines()
        self.text.config(state=tk.NORMAL)
        self.text.delete('1.0', tk.END)
        self.text.insert(tk.END, '\n'.join(lines[-20:]))
        self.text.see(tk.END)
        self.text.config(state=tk.DISABLED)

    def _quit(self):
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        self.root.destroy()

    def run(self):
        # PID 파일 기록
        open(PID_FILE, 'w').write(str(os.getpid()))
        self.root.mainloop()


HUDApp().run()
