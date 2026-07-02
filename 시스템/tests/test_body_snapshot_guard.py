# -*- coding: utf-8 -*-
"""본문 스냅샷 안전판(P1') — ensure_snapshot이 편집 전 원본을 롤백 가능하게 보존하는지."""
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.resource_body as resource_body     # noqa: E402

SKILL = """---
skill_id: skill_test123
name: 테스트스킬
version: 1
---

# 본문 원본

## non_overridable
- 절대 하지 마라: 위험행동
"""


class EnsureSnapshotTests(unittest.TestCase):
    def _mk(self, tmp):
        m = Path(tmp) / "SKILL.md"
        m.write_text(SKILL, encoding="utf-8")
        return m

    def test_snapshot_created_then_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._mk(tmp)
            r1 = resource_body.ensure_snapshot(m, by="tester", reason="편집 전")
            self.assertTrue(r1["ok"])
            hist = Path(tmp) / ".history"
            snaps = list(hist.glob("pre_*.md"))
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0].read_text(encoding="utf-8"), SKILL)
            # 다시 호출 — 내용 동일이면 새 스냅샷 안 만든다.
            r2 = resource_body.ensure_snapshot(m, by="tester", reason="또")
            self.assertEqual(r2.get("skipped"), "unchanged")
            self.assertEqual(len(list(hist.glob("pre_*.md"))), 1)

    def test_raw_edit_is_recoverable_via_presnapshot(self):
        # 시나리오: 편집 전 스냅샷 → doer가 버전을 안 올리고 SKILL.md를 직접 덮어씀 → 원본 복구 가능.
        with tempfile.TemporaryDirectory() as tmp:
            m = self._mk(tmp)
            snap_res = resource_body.ensure_snapshot(m, by="공간관리", reason="위임 전")
            m.write_text(SKILL.replace("본문 원본", "doer가 막 고친 본문"), encoding="utf-8")
            # 편집 전 원본이 고유 이름 스냅샷으로 남아있다(v1.md 충돌 없이).
            snap = Path(tmp) / snap_res["snapshot"].split("/")[-1] if False else Path(snap_res["snapshot"])
            # _rel은 ROOT 밖 tmp라 절대경로일 수 있음 — 파일명으로 찾는다.
            snaps = list((Path(tmp) / ".history").glob("pre_*.md"))
            self.assertEqual(len(snaps), 1)
            self.assertIn("본문 원본", snaps[0].read_text(encoding="utf-8"))
            # restore_snapshot으로 원본을 되살린다.
            resource_body.restore_snapshot(m, snaps[0].name, by="관리자", rationale="doer 오편집 복구")
            self.assertIn("본문 원본", m.read_text(encoding="utf-8"))
            self.assertNotIn("doer가 막 고친 본문", m.read_text(encoding="utf-8"))

    def test_changed_content_makes_new_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._mk(tmp)
            resource_body.ensure_snapshot(m, by="t", reason="1차")
            m.write_text(SKILL.replace("본문 원본", "바뀐 본문"), encoding="utf-8")
            resource_body.ensure_snapshot(m, by="t", reason="2차")
            snaps = list((Path(tmp) / ".history").glob("pre_*.md"))
            self.assertGreaterEqual(len(snaps), 2)

    def test_revise_body_still_gates_single_case(self):
        # 단일 케이스로는 본문 개정 거부(설계 P3) — 게이트가 살아있는지 확인.
        with tempfile.TemporaryDirectory() as tmp:
            m = self._mk(tmp)
            with self.assertRaises(resource_body.ResourceBodyError):
                resource_body.revise_body(m, "# 새 본문\n\n## non_overridable\n- 절대 하지 마라: 위험행동\n",
                                          expected_version="1", by="t", rationale="한 건으로 개정 시도",
                                          from_case_ids=["c1"], regression_attestation="ok")

    def test_revise_body_preserves_nonoverridable(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = self._mk(tmp)
            with self.assertRaises(resource_body.ResourceBodyError):
                # non_overridable 줄을 빠뜨린 새 본문 → 거부.
                resource_body.revise_body(m, "# 새 본문(안전지침 삭제됨)\n",
                                          expected_version="1", by="t", rationale="2건 종합",
                                          from_case_ids=["c1", "c2"], regression_attestation="회귀 점검 완료")


if __name__ == "__main__":
    unittest.main()
