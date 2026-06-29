#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""채팅/작업 모델 분리(resolve_work_runtime) — 빠른 티키타카 + 강한 작업."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import runtime  # noqa: E402


class ResolveWorkRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="tmp_wrt_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_falls_back_to_chat_runtime_when_no_work_model(self):
        # work_model 미설정 → 작업도 채팅 모델 그대로(현행 동작·무회귀)
        runtime.write_runtime(self.dir, "claude", "haiku")
        wr = runtime.resolve_work_runtime(self.dir)
        self.assertEqual(wr["engine"], "claude")
        self.assertEqual(wr["model"], "haiku")

    def test_work_model_overrides_chat_model(self):
        # 채팅 haiku(빠름) + 작업 opus(강함) 분리
        runtime.write_runtime(self.dir, "claude", "haiku", work_model="claude-opus-4-8")
        chat = runtime.read_runtime(self.dir)
        self.assertEqual(chat["model"], "haiku")           # 채팅은 haiku
        self.assertEqual(chat["work_model"], "claude-opus-4-8")
        wr = runtime.resolve_work_runtime(self.dir)
        self.assertEqual(wr["model"], "claude-opus-4-8")   # 작업은 opus

    def test_explicit_override_wins(self):
        runtime.write_runtime(self.dir, "claude", "haiku", work_model="claude-opus-4-8")
        wr = runtime.resolve_work_runtime(self.dir, model="sonnet")
        self.assertEqual(wr["model"], "sonnet")

    def test_work_engine_and_model(self):
        runtime.write_runtime(self.dir, "claude", "haiku", work_engine="claude", work_model="sonnet")
        wr = runtime.resolve_work_runtime(self.dir)
        self.assertEqual(wr["engine"], "claude")
        self.assertEqual(wr["model"], "sonnet")


if __name__ == "__main__":
    unittest.main()
