# -*- coding: utf-8 -*-
"""크로스플랫폼 파일락 프리미티브(H4) — POSIX 경로 동작·직렬화 회귀 테스트."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.filelock as filelock                # noqa: E402


class FileLockTests(unittest.TestCase):
    def test_lock_unlock_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lock"
            p.touch()
            with p.open("r+", encoding="utf-8") as f:
                filelock.lock_exclusive(f)
                filelock.unlock(f)   # 예외 없이 락/해제

    def test_serializes_concurrent_writers(self):
        # 두 스레드가 같은 락파일로 직렬화되면 카운터가 정확히 증가(경합 손실 없음).
        with tempfile.TemporaryDirectory() as tmp:
            lockp = Path(tmp) / "lock"
            lockp.touch()
            datap = Path(tmp) / "n"
            datap.write_text("0", encoding="utf-8")
            errors = []

            def worker():
                for _ in range(50):
                    try:
                        with lockp.open("r+", encoding="utf-8") as lf:
                            filelock.lock_exclusive(lf)
                            try:
                                n = int(datap.read_text(encoding="utf-8"))
                                time.sleep(0.0001)          # 경합 창 확대
                                datap.write_text(str(n + 1), encoding="utf-8")
                            finally:
                                filelock.unlock(lf)
                    except Exception as e:
                        errors.append(e)

            ts = [threading.Thread(target=worker) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(int(datap.read_text(encoding="utf-8")), 100)   # 직렬화로 손실 0


if __name__ == "__main__":
    unittest.main()
