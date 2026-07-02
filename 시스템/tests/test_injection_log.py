# -*- coding: utf-8 -*-
"""주입 로그(P1' 안전판) — 케이스 노출 기록·역추적 회귀 테스트."""
import sys
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.injection_log as injection_log      # noqa: E402
import core.discovery as discovery              # noqa: E402


class InjectedCaseRefsTests(unittest.TestCase):
    def test_extracts_preview_and_avoid(self):
        hits = [
            (5, {"type": "skill", "name": "스킬A",
                 "cases_preview": [{"case_id": "c1", "polarity": "worked"}],
                 "cases_avoid": [{"case_id": "c2", "polarity": "failed"}]}),
            (3, {"type": "knowledge", "name": "지식B"}),   # 스킬 아님 → 무시
        ]
        refs = discovery.injected_case_refs(hits)
        ids = {r["case_id"] for r in refs}
        self.assertEqual(ids, {"c1", "c2"})
        kinds = {r["case_id"]: r["kind"] for r in refs}
        self.assertEqual(kinds["c1"], "preview")
        self.assertEqual(kinds["c2"], "avoid")

    def test_empty_hits(self):
        self.assertEqual(discovery.injected_case_refs([]), [])


class RecordInjectionTests(unittest.TestCase):
    def setUp(self):
        self._orig = injection_log.SPACES
        self.tmp = Path(__file__).parent / ".test_inj_spaces"
        injection_log.SPACES = self.tmp

    def tearDown(self):
        injection_log.SPACES = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_and_readback(self):
        injection_log.record_injection(
            "방1", kind="work", ref="task9",
            injected=[{"skill": "스킬A", "case_id": "c1", "polarity": "worked", "kind": "preview"}],
            context={"intent_id": "i1"})
        recs = injection_log.recent_injections("방1")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["ref"], "task9")
        self.assertEqual(recs[0]["intent_id"], "i1")
        self.assertEqual(recs[0]["cases"][0]["case_id"], "c1")

    def test_empty_injected_not_recorded(self):
        injection_log.record_injection("방1", kind="chat", ref="t1", injected=[])
        self.assertEqual(injection_log.recent_injections("방1"), [])

    def test_lookup_by_ref(self):
        injection_log.record_injection("방1", kind="work", ref="taskA",
                                       injected=[{"skill": "S", "case_id": "cA"}])
        injection_log.record_injection("방1", kind="work", ref="taskB",
                                       injected=[{"skill": "S", "case_id": "cB"}])
        found = injection_log.last_injection_for_ref("방1", "taskA")
        self.assertIsNotNone(found)
        self.assertEqual(found["cases"][0]["case_id"], "cA")
        self.assertIsNone(injection_log.last_injection_for_ref("방1", "nope"))

    def test_record_failure_is_silent(self):
        # SPACES를 못 만드는 경로로 둬도 예외가 새지 않아야 한다(best-effort).
        injection_log.SPACES = Path("/proc/nonexistent_xyz/spaces")
        try:
            injection_log.record_injection("방1", kind="work", ref="t",
                                           injected=[{"skill": "S", "case_id": "c"}])
        except Exception:
            self.fail("record_injection이 예외를 던졌다(best-effort 위반)")


if __name__ == "__main__":
    unittest.main()
