# -*- coding: utf-8 -*-
"""부정 피드백 루프(P2') — harmful 역추적/자동강등의 섀도·실행 모드 회귀 테스트."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.growth_feedback as gf               # noqa: E402
import core.injection_log as injection_log      # noqa: E402
import core.case_ledger as case_ledger          # noqa: E402


class _Env:
    """환경변수 임시 설정 컨텍스트."""
    def __init__(self, **kv):
        self.kv = kv
        self.old = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class GrowthFeedbackTests(unittest.TestCase):
    def setUp(self):
        self._sp = gf.SPACES
        self._ilsp = injection_log.SPACES
        self.tmp = Path(tempfile.mkdtemp())
        gf.SPACES = self.tmp
        injection_log.SPACES = self.tmp
        # 가짜 스킬 폴더
        self.skill_dir = self.tmp / "_skills" / "테스트스킬"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "cases.jsonl").write_text("", encoding="utf-8")
        # case_ledger.skill_dir를 우리 폴더로 몽키패치
        self._orig_skill_dir = case_ledger.skill_dir
        case_ledger.skill_dir = lambda name: self.skill_dir if name == "테스트스킬" else None

    def tearDown(self):
        gf.SPACES = self._sp
        injection_log.SPACES = self._ilsp
        case_ledger.skill_dir = self._orig_skill_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _inject(self, cid, kind="preview"):
        injection_log.record_injection(
            "방1", kind="work", ref="task1",
            injected=[{"skill": "테스트스킬", "case_id": cid, "polarity": "worked", "kind": kind}])

    def test_shadow_default_no_harmful_recorded(self):
        self._inject("cX")
        with _Env(CNV_GROWTH_HARMFUL_ACTIVE=None, CNV_GROWTH_DEMOTE_ACTIVE=None):
            r = gf.on_skill_correction("방1", "테스트스킬", rationale="틀렸음")
        self.assertEqual(r["suspects"], ["cX"])
        self.assertTrue(r["shadow"])
        self.assertEqual(r["harmful_applied"], [])       # 섀도: 실제 기록 안 함
        # 섀도 로그엔 would_flag_harmful가 남는다
        shadow = gf.read_shadow("방1")
        kinds = {s["kind"] for s in shadow}
        self.assertIn("would_flag_harmful", kinds)
        # 실제 harmful 이벤트는 0
        w, h = case_ledger.worked_harmful_counts(self.skill_dir, "cX")
        self.assertEqual(h, 0)

    def test_active_records_harmful(self):
        self._inject("cY")
        with _Env(CNV_GROWTH_HARMFUL_ACTIVE="1", CNV_GROWTH_DEMOTE_ACTIVE=None):
            r = gf.on_skill_correction("방1", "테스트스킬", rationale="틀렸음")
        self.assertIn("cY", r["harmful_applied"])
        w, h = case_ledger.worked_harmful_counts(self.skill_dir, "cY")
        self.assertEqual(h, 1)

    def test_avoid_cases_not_attributed(self):
        # avoid('하지마라')로 노출된 케이스는 원인이 아니므로 제외.
        self._inject("cAvoid", kind="avoid")
        with _Env(CNV_GROWTH_HARMFUL_ACTIVE="1"):
            r = gf.on_skill_correction("방1", "테스트스킬")
        self.assertNotIn("cAvoid", r["suspects"])

    def test_new_case_excluded(self):
        self._inject("cOld")
        self._inject("cNew")
        with _Env(CNV_GROWTH_HARMFUL_ACTIVE=None):
            r = gf.on_skill_correction("방1", "테스트스킬", new_case_id="cNew")
        self.assertIn("cOld", r["suspects"])
        self.assertNotIn("cNew", r["suspects"])          # 방금 추가한 교정 케이스는 원인 아님

    def test_demote_shadow_vs_active(self):
        # 실제 케이스를 provisional_must로 만든 뒤(대표 발의), harmful 2건 누적 → 강등 조건 충족.
        rec = case_ledger.propose_case(self.skill_dir, {
            "condition": "어떤 상황", "instruction": "이렇게", "polarity": "worked",
            "action": "add_case", "routing_kind": "procedural",
            "judgment_rationale": "테스트", "source_quote": "테스트"}, proposed_by="대표", from_daepyo=True)
        cid = rec["case_id"]
        case_ledger.record_case_event(self.skill_dir, cid, "harmful", by="t", rationale="1")
        case_ledger.record_case_event(self.skill_dir, cid, "harmful", by="t", rationale="2")
        demo = gf.maybe_auto_demote("방1", "테스트스킬", cid)
        self.assertTrue(demo["would_demote"])            # 섀도: 판단만
        self.assertFalse(demo["acted"])
        with _Env(CNV_GROWTH_DEMOTE_ACTIVE="1"):
            demo2 = gf.maybe_auto_demote("방1", "테스트스킬", cid)
        self.assertTrue(demo2["acted"])                  # 실행 플래그 시 실제 강등
        # 강등 결과: provisional_must → candidate
        cases = {c["case_id"]: c for c in case_ledger.read_cases(self.skill_dir)}
        self.assertEqual(cases[cid]["status"], "candidate")

    def test_no_suspects_safe(self):
        with _Env():
            r = gf.on_skill_correction("방없음", "테스트스킬")
        self.assertEqual(r["suspects"], [])              # 주입 이력 없으면 조용히 no-op


if __name__ == "__main__":
    unittest.main()
