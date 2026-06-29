# -*- coding: utf-8 -*-
"""PTY 하나를 소유하는 영속 터미널 세션."""
import asyncio
import os
import threading
import time

from config import IS_WIN, SCROLLBACK_LIMIT
from pty_backends import PosixPty, WinPty

MAIN_LOOP = None


def set_main_loop(loop):
    global MAIN_LOOP
    MAIN_LOOP = loop


class Session:
    def __init__(self, sid, title, shell, cwd, cols, rows):
        self.id = sid
        self.title = title
        self.shell = shell
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.created = time.time()
        self.scrollback = bytearray()
        self.subscribers = set()
        self._lock = threading.Lock()
        self._exited = False

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        self.pty = WinPty(shell, cwd, env, cols, rows) if IS_WIN else PosixPty(shell, cwd, env, cols, rows)
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while True:
            data = self.pty.read()
            if data == "" and not self.pty.alive():
                break
            if not data:
                if not self.pty.alive():
                    break
                time.sleep(0.02)
                continue
            chunk = data.encode("utf-8", "replace")
            with self._lock:
                self.scrollback.extend(chunk)
                if len(self.scrollback) > SCROLLBACK_LIMIT:
                    del self.scrollback[: len(self.scrollback) - SCROLLBACK_LIMIT]
                subs = list(self.subscribers)
            for q in subs:
                self._push(q, data)
        self._exited = True
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            self._push(q, {"__exit__": True})

    def _push(self, q, item):
        if MAIN_LOOP is not None:
            try:
                MAIN_LOOP.call_soon_threadsafe(q.put_nowait, item)
            except Exception:
                pass

    def subscribe(self):
        q = asyncio.Queue()
        with self._lock:
            self.subscribers.add(q)
            snapshot = bytes(self.scrollback)
        return q, snapshot

    def unsubscribe(self, q):
        with self._lock:
            self.subscribers.discard(q)

    def write(self, data: str):
        self.pty.write(data)

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.pty.resize(cols, rows)

    def alive(self):
        return not self._exited and self.pty.alive()

    def kill(self):
        self.pty.kill()

    def info(self):
        return {
            "id": self.id,
            "title": self.title,
            "shell": self.shell,
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
            "created": self.created,
            "alive": self.alive(),
        }
