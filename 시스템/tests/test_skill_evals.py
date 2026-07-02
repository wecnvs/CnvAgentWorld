# -*- coding: utf-8 -*-
"""스킬 골든셋 골격(P1' item 3) — 로드·추가·커버리지 판정 회귀 테스트."""
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.skill_evals as skill_evals          # noqa: E402


class SkillEvalsTests(unittest.TestCase):
    def test_empty_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(skill_evals.load_evals(Path(tmp)), [])
            cov = skill_evals.coverage(Path(tmp))
            self.assertEqual(cov["count"], 0)
            self.assertFalse(cov["has_golden"])
            self.assertFalse(skill_evals.regression_ready(Path(tmp)))

    def test_add_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = skill_evals.add_eval(Path(tmp), {"scenario": "앱 실행 요청", "expect": "open -a로 실행"})
            self.assertEqual(r["eval_id"], "ev001")
            self.assertEqual(r["kind"], "positive")
            skill_evals.add_eval(Path(tmp), {"scenario": "권한 팝업", "expect": "클릭으로 허용", "kind": "negative"})
            evals = skill_evals.load_evals(Path(tmp))
            self.assertEqual(len(evals), 2)
            cov = skill_evals.coverage(Path(tmp))
            self.assertEqual(cov["count"], 2)
            self.assertEqual(cov["positive"], 1)
            self.assertEqual(cov["negative"], 1)

    def test_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                skill_evals.add_eval(Path(tmp), {"scenario": "", "expect": "x"})
            with self.assertRaises(ValueError):
                skill_evals.add_eval(Path(tmp), {"scenario": "x", "expect": ""})

    def test_regression_ready_needs_count_and_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 긍정만 5건 → regression_ready 아님(부정 케이스 없으면 회귀로 불충분)
            for i in range(5):
                skill_evals.add_eval(Path(tmp), {"scenario": f"s{i}", "expect": "e"})
            self.assertFalse(skill_evals.regression_ready(Path(tmp)))
            # 부정 1건 추가 → ready
            skill_evals.add_eval(Path(tmp), {"scenario": "neg", "expect": "하지마라", "kind": "negative"})
            self.assertTrue(skill_evals.regression_ready(Path(tmp)))

    def test_append_only_dedup_by_eval_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_evals.add_eval(Path(tmp), {"eval_id": "evX", "scenario": "v1", "expect": "e1"})
            skill_evals.add_eval(Path(tmp), {"eval_id": "evX", "scenario": "v2", "expect": "e2"})
            evals = skill_evals.load_evals(Path(tmp))
            self.assertEqual(len(evals), 1)
            self.assertEqual(evals[0]["scenario"], "v2")   # latest-wins


if __name__ == "__main__":
    unittest.main()
