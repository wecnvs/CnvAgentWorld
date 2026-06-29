# -*- coding: utf-8 -*-
"""터미널 세션 저장소."""
import threading
import uuid

from terminal_session import Session

SESSIONS = {}
_LOCK = threading.Lock()


def count():
    with _LOCK:
        return len(SESSIONS)


def list_sessions():
    with _LOCK:
        items = [s.info() for s in SESSIONS.values()]
    items.sort(key=lambda x: x["created"])
    return items


def get_session(sid):
    return SESSIONS.get(sid)


def add_session(shell, cwd, cols, rows, title):
    sid = uuid.uuid4().hex[:12]
    if not title:
        title = f"터미널 {count() + 1}"
    sess = Session(sid, title, shell, cwd, cols, rows)
    with _LOCK:
        SESSIONS[sid] = sess
    return sess


def pop_session(sid):
    with _LOCK:
        return SESSIONS.pop(sid, None)
