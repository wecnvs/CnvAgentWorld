# -*- coding: utf-8 -*-
"""크로스플랫폼 파일 배타락 프리미티브 — 워크스페이스 이식성(mac/Windows) 기반 (분석 H4).

왜:
- 코어 전반이 `fcntl.flock`(POSIX 전용)을 직접 써서, 워크스페이스를 Windows 서버로 옮기면 import 단계부터
  터진다([[workspace-portability-constraint]] 위반). 이 모듈이 OS 자동감지 락을 제공한다.
- **POSIX 경로는 기존 `fcntl.flock(f, LOCK_EX)`/`LOCK_UN`과 바이트 동일 동작** — mac에서 회귀 없음(전 테스트로 검증).
- Windows 경로는 `msvcrt.locking`(best-effort, 여기선 미검증 — 실기기 검증은 이식 시). import는 OS별로만.

사용:
    from .filelock import lock_exclusive, unlock
    with open(path, "a") as f:
        lock_exclusive(f)
        try: ...
        finally: unlock(f)
"""
from __future__ import annotations

import os

_IS_WIN = os.name == "nt"

if _IS_WIN:
    import msvcrt   # noqa
else:
    import fcntl    # noqa


def lock_exclusive(f) -> None:
    """파일 핸들에 배타 잠금(블로킹). POSIX=flock LOCK_EX, Windows=msvcrt LOCK_LK(best-effort)."""
    if _IS_WIN:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)   # 1바이트 영역 락(전체 파일 대용)
        except OSError:
            pass   # best-effort — Windows 락 실패가 진행을 막지 않게(POSIX와 동일한 관용도)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def unlock(f) -> None:
    if _IS_WIN:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
