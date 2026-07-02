# -*- coding: utf-8 -*-
"""이종 검토자 원장(P4-2) — 검토 의도 기록·verdict·거부율/도장찍기 경보 회귀 테스트."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.reviewer as reviewer                # noqa: E402


class ReviewerLedgerTests(unittest.TestCase):
    def setUp(self):
        self._sp = reviewer.SPACES
        self.tmp = Path(tempfile.mkdtemp())
        reviewer.SPACES = self.tmp
        for k in ("CNV_REVIEW_DISPATCH_ACTIVE", "CNV_REVIEW_BLOCK_ACTIVE"):
            os.environ.pop(k, None)

    def tearDown(self):
        reviewer.SPACES = self._sp
        for k in ("CNV_REVIEW_DISPATCH_ACTIVE", "CNV_REVIEW_BLOCK_ACTIVE"):
            os.environ.pop(k, None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_review_when_not_recommended(self):
        self.assertFalse(reviewer.should_review({"review_recommended": False}))
        r = reviewer.record_review_intent("방1", "t1", {"review_recommended": False})
        self.assertFalse(r["logged"])

    def test_intent_logged_shadow_by_default(self):
        v = {"status": "suspect", "review_recommended": True, "reviewer_engine": "codex", "reason": "근거없음"}
        r = reviewer.record_review_intent("방1", "t1", v, doer_engine="claude")
        self.assertTrue(r["logged"])
        self.assertFalse(r["dispatched"])       # 섀도: 실제 디스패치 안 함
        self.assertEqual(r["reviewer_engine"], "codex")
        intents = reviewer.recent_intents("방1")
        self.assertEqual(len(intents), 1)
        self.assertTrue(intents[0]["shadow"])
        self.assertEqual(intents[0]["doer_engine"], "claude")

    def test_dispatch_flag_flips_shadow(self):
        os.environ["CNV_REVIEW_DISPATCH_ACTIVE"] = "1"
        v = {"review_recommended": True, "reviewer_engine": "codex"}
        r = reviewer.record_review_intent("방1", "t1", v, doer_engine="claude")
        self.assertTrue(r["dispatched"])

    def test_verdict_and_rejection_rate(self):
        for i in range(3):
            reviewer.record_verdict("방1", f"t{i}", verdict="approve", by_engine="codex")
        reviewer.record_verdict("방1", "t9", verdict="reject", by_engine="codex", reason="산출물 불일치")
        st = reviewer.rejection_stats("방1")
        self.assertEqual(st["reviews"], 4)
        self.assertEqual(st["rejects"], 1)
        self.assertAlmostEqual(st["rejection_rate"], 0.25)
        self.assertFalse(st["rubber_stamp_alarm"])   # 거부 있음

    def test_rubber_stamp_alarm(self):
        for i in range(reviewer.RUBBER_STAMP_MIN_REVIEWS):
            reviewer.record_verdict("방1", f"t{i}", verdict="approve", by_engine="codex")
        st = reviewer.rejection_stats("방1")
        self.assertTrue(st["rubber_stamp_alarm"])     # N건 검토에 거부 0 → 도장 찍기 경보

    def test_homogeneous_alarm(self):
        # 검토 엔진이 전부 하나(이종성 없음)면 경보
        for i in range(reviewer.RUBBER_STAMP_MIN_REVIEWS):
            reviewer.record_verdict("방1", f"t{i}", verdict="approve" if i else "reject", by_engine="claude")
        st = reviewer.rejection_stats("방1")
        self.assertTrue(st["homogeneous_alarm"])
        self.assertEqual(st["engines_used"], ["claude"])

    def test_bad_verdict_rejected(self):
        with self.assertRaises(ValueError):
            reviewer.record_verdict("방1", "t1", verdict="maybe", by_engine="codex")


if __name__ == "__main__":
    unittest.main()
