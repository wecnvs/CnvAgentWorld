# -*- coding: utf-8 -*-
"""산출물 객관 검증 + 이종 검증자(P4) 회귀 테스트."""
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.output_verify as ov                 # noqa: E402


def _wd(tmp, files: dict):
    d = Path(tmp)
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


SKELETON = "# 결과\n\n## 단계 체크리스트\n- [ ] STEP 1\n- [ ] STEP 2\n\n## 다음 단계\nSTEP1부터."


class VerifyOutputTests(unittest.TestCase):
    def test_suspect_when_done_but_no_artifacts_and_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = _wd(tmp, {"지시.md": "x", "task_pack.json": "{}", "결과.md": SKELETON})
            v = ov.verify_output(wd, {"objective": "뭐 해줘"}, SKELETON, "done")
            self.assertEqual(v["status"], "suspect")
            self.assertTrue(v["review_recommended"])
            self.assertTrue(v["reviewer_engine"])   # 이종 엔진 추천

    def test_passed_when_artifact_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = _wd(tmp, {"지시.md": "x", "결과.md": SKELETON, "output.html": "<html>산출</html>"})
            v = ov.verify_output(wd, {"objective": "html 만들어"}, SKELETON, "done")
            self.assertEqual(v["status"], "passed")
            self.assertEqual(v["artifacts_count"], 1)

    def test_passed_when_result_substantive(self):
        with tempfile.TemporaryDirectory() as tmp:
            substantive = ("# 결과\n\n### STEP 0 완료 ✅\n- 캡처: cap0.png\n- screenshot exit 0 확인\n"
                           "### STEP 1 완료 ✅\n- [x] 계산기 실행\n- [x] 123 입력 확인\n결과: 성공") * 2
            wd = _wd(tmp, {"지시.md": "x", "결과.md": substantive})
            v = ov.verify_output(wd, {}, substantive, "done")
            self.assertEqual(v["status"], "passed")

    def test_not_applicable_for_error_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = _wd(tmp, {"결과.md": SKELETON})
            for st in ("error", "blocked", "cancelled"):
                v = ov.verify_output(wd, {}, SKELETON, st)
                self.assertEqual(v["status"], "not_applicable")
                self.assertFalse(v["review_recommended"])

    def test_high_risk_recommends_review_even_when_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = _wd(tmp, {"결과.md": SKELETON, "patch.diff": "diff"})
            v = ov.verify_output(wd, {"objective": "law_manager.md 지침 수정"}, SKELETON, "done")
            self.assertEqual(v["status"], "passed")
            self.assertTrue(v["review_recommended"])   # 고위험 → 이종 검토 권고

    def test_scaffold_files_not_counted_as_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = _wd(tmp, {"지시.md": "x", "task_pack.json": "{}", "runtime_capabilities.json": "{}",
                           "발견후보.md": "x", "결과.md": SKELETON, "상태.json": "{}"})
            v = ov.verify_output(wd, {}, SKELETON, "done")
            self.assertEqual(v["artifacts_count"], 0)   # 뼈대는 산출물 아님
            self.assertEqual(v["status"], "suspect")


class ReviewerEngineTests(unittest.TestCase):
    def test_heterogeneous_pick(self):
        self.assertEqual(ov.pick_reviewer_engine("claude"), "codex")
        self.assertEqual(ov.pick_reviewer_engine("codex"), "claude")
        self.assertNotEqual(ov.pick_reviewer_engine("claude"), "claude")

    def test_available_constraint(self):
        # codex 없으면 available 안에서 doer와 다른 것
        self.assertEqual(ov.pick_reviewer_engine("claude", available={"claude", "gemini"}), "gemini")
        # 다른 엔진 아예 없으면 빈 문자열(이종 불가)
        self.assertEqual(ov.pick_reviewer_engine("claude", available={"claude"}), "")


if __name__ == "__main__":
    unittest.main()
