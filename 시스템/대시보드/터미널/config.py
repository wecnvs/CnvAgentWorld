# -*- coding: utf-8 -*-
"""터미널 서버 공통 설정."""
import os
import platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(BASE_DIR)))

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

SCROLLBACK_LIMIT = 256 * 1024
UPLOAD_DIR = os.path.join(WORKSPACE_ROOT, "temp", "terminal_uploads")
MAX_UPLOAD = 30 * 1024 * 1024
