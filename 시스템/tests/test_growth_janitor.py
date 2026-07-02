# -*- coding: utf-8 -*-
"""케이스 janitor 전역 스윕(P3') — expire/dedup 배선·레이트리밋·킬스위치 회귀 테스트."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.growth_janitor as gj                # noqa: E402
import core.case_ledger as case_ledger          # noqa: E402
import core.skill_smith as skill_smith          # noqa: E402


class GrowthJanitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # janitor 로그/스탬프를 tmp로
        self._run = gj._RUN_DIR
        self._log = gj._LOG
        self._stamp = gj._STAMP
        gj._RUN_DIR = self.tmp / ".run"
        gj._LOG = gj._RUN_DIR / "growth_janitor.jsonl"
        gj._STAMP = gj._RUN_DIR / "growth_janitor.stamp"
        # 가짜 스킬 1개
        self.sdir = self.tmp / "skills" / "테스트스킬"
        self.sdir.mkdir(parents=True)
        (self.sdir / "cases.jsonl").write_text("", encoding="utf-8")
        self._orig_list = skill_smith.list_skills
        self._orig_sdir = case_ledger.skill_dir
        skill_smith.list_skills = lambda: [{"name": "테스트스킬"}]
        case_ledger.skill_dir = lambda name: self.sdir if name == "테스트스킬" else None
        os.environ.pop("CNV_JANITOR_DISABLE", None)

    def tearDown(self):
        gj._RUN_DIR, gj._LOG, gj._STAMP = self._run, self._log, self._stamp
        skill_smith.list_skills = self._orig_list
        case_ledger.skill_dir = self._orig_sdir
        os.environ.pop("CNV_JANITOR_DISABLE", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_case(self, cid, *, status="candidate", created, condition="c", instruction="i"):
        rec = {"case_id": cid, "skill_id": "s", "status": status, "polarity": "worked",
               "condition": condition, "instruction": instruction, "created_at": created}
        with (self.sdir / "cases.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_expires_stale_candidate(self):
        old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
        fresh = datetime.now().isoformat(timespec="seconds")
        self._write_case("cOld", created=old)
        self._write_case("cFresh", created=fresh)
        res = gj.sweep()
        self.assertTrue(res["ok"])
        self.assertEqual(res["expired"], 1)              # 20일 지난 것만 만료
        cases = {c["case_id"]: c for c in case_ledger.read_cases(self.sdir)}
        self.assertEqual(cases["cOld"]["status"], "expired")
        self.assertEqual(cases["cFresh"]["status"], "candidate")

    def test_dedups_exact_duplicates(self):
        t = datetime.now().isoformat(timespec="seconds")
        self._write_case("cA", status="active", created=t, condition="같음", instruction="같음")
        self._write_case("cB", status="active", created=t, condition="같음", instruction="같음")
        res = gj.sweep()
        self.assertEqual(res["deduped"], 1)              # 완전동일 1건 정리
        cases = {c["case_id"]: c for c in case_ledger.read_cases(self.sdir)}
        # 하나는 살고 하나는 retired
        statuses = sorted([cases["cA"]["status"], cases["cB"]["status"]])
        self.assertIn("retired", statuses)

    def test_kill_switch(self):
        os.environ["CNV_JANITOR_DISABLE"] = "1"
        old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
        self._write_case("cOld", created=old)
        res = gj.sweep()
        self.assertEqual(res.get("skipped"), "disabled")
        cases = {c["case_id"]: c for c in case_ledger.read_cases(self.sdir)}
        self.assertEqual(cases["cOld"]["status"], "candidate")   # 킬스위치: 건드리지 않음

    def test_rate_limit(self):
        old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
        self._write_case("cOld", created=old)
        r1 = gj.sweep_if_due(min_interval_sec=3600)      # 첫 실행: due
        self.assertTrue(r1["ok"])
        self.assertNotEqual(r1.get("skipped"), "not_due")
        r2 = gj.sweep_if_due(min_interval_sec=3600)      # 바로 재호출: not_due
        self.assertEqual(r2.get("skipped"), "not_due")

    def test_logs_actions(self):
        old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
        self._write_case("cOld", created=old)
        gj.sweep()
        self.assertTrue(gj._LOG.exists())
        kinds = {json.loads(l)["kind"] for l in gj._LOG.read_text(encoding="utf-8").splitlines() if l.strip()}
        self.assertIn("swept", kinds)
        self.assertIn("sweep_summary", kinds)


if __name__ == "__main__":
    unittest.main()
