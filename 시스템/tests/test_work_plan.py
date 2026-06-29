#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkPlan v1 (작업계획 승인 게이트) 단위 테스트 — 설계_작업계획승인.md P1."""
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import work_plan  # noqa: E402
from core.paths import SPACES  # noqa: E402

PREFIX = "tmp_workplan_"


def _mkspace(name: str) -> Path:
    sdir = SPACES / name
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _cleanup():
    for path in SPACES.glob(PREFIX + "*"):
        shutil.rmtree(path, ignore_errors=True)


class AssessApprovalTest(unittest.TestCase):
    def test_agent_declares_needs_approval_goes_representative(self):
        a = work_plan.assess_approval("간단한 메모 정리", ["1. 정리"], True, agent_reason="확인받고 싶음")
        self.assertTrue(a["needs_approval"])
        self.assertEqual(a["approval_mode"], "representative")
        self.assertIn("에이전트", a["approval_reason"])

    def test_agent_says_no_and_no_signal_goes_auto(self):
        a = work_plan.assess_approval("슬라이드 본문 5장 작성", ["1. 표지", "2. 본문"], False)
        self.assertFalse(a["needs_approval"])
        self.assertEqual(a["approval_mode"], "auto_manager")
        self.assertEqual(a["system_level"], "low")

    def test_missing_declaration_defers_to_system_risk(self):
        # 미선언(None) + 위험신호 없음 → 자동 진행(위험도 기반 혼합). 무회귀.
        a = work_plan.assess_approval("평범한 메모 정리", ["1. 정리"], None)
        self.assertFalse(a["needs_approval"])
        self.assertEqual(a["approval_mode"], "auto_manager")

    def test_missing_declaration_still_escalated_by_system(self):
        # 미선언이어도 지침 변경 등 위험신호가 있으면 시스템이 승인행으로 격상
        a = work_plan.assess_approval("law.md 지침 수정", ["1. 편집"], None)
        self.assertTrue(a["needs_approval"])
        self.assertIn("guide_change", a["system_signals"])

    def test_system_escalates_guide_change_even_if_agent_says_no(self):
        # 에이전트가 불필요라 해도 지침 변경이면 시스템이 승인행으로 격상
        a = work_plan.assess_approval("law.md 지침을 수정한다", ["1. law.md 편집"], False)
        self.assertTrue(a["needs_approval"])
        self.assertEqual(a["approval_mode"], "representative")
        self.assertIn("guide_change", a["system_signals"])

    def test_system_escalates_confidential_share(self):
        a = work_plan.assess_approval("대외비 문서를 다른 공간에 공유한다", ["1. 공유"], False)
        self.assertTrue(a["needs_approval"])
        self.assertIn("confidential_share", a["system_signals"])

    def test_system_escalates_external_publish(self):
        a = work_plan.assess_approval("결과를 메일로 발송한다", ["1. 발송"], False)
        self.assertTrue(a["needs_approval"])
        self.assertIn("external_publish", a["system_signals"])

    def test_video_format_keyword_not_false_positive(self):
        # 회귀(라이브): '영상/콘텐츠 포맷'은 양성 표현인데 bulk_file_change('포맷')에 오탐돼
        # 조사 작업이 결재게이트에 걸렸다. 파괴적 의미(디스크/드라이브 포맷, 포맷해/하)만 잡는다.
        for benign in ("2026년 인기 유튜브 포맷·영상 길이 트렌드 조사", "숏폼 콘텐츠 포맷 정리", "파일 포맷 확인"):
            self.assertEqual(work_plan.detect_risk_signals(benign), [], benign)
            self.assertFalse(work_plan.assess_approval(benign, [benign], None)["needs_approval"], benign)
        for dangerous in ("디스크 포맷 후 재설치", "드라이브 포맷해줘", "디스크 포맷하고 재설치"):
            self.assertIn("bulk_file_change", work_plan.detect_risk_signals(dangerous), dangerous)

    def test_following_guidance_not_false_positive_guide_change(self):
        # 회귀(라이브): '지침' 단독이 guide_change를 오탐해 '미리보기 지침에 맞춰 제출' 같은
        # 지침을 '따르는' 작업까지 결재게이트에 걸렸다. 변경 의도/지침 파일일 때만 잡는다.
        for benign in ("미리보기 지침에 맞춰 제출", "스킬 지침 참고해 작성", "공간지침 준수"):
            self.assertEqual(work_plan.detect_risk_signals(benign), [], benign)
        for danger in ("law.md 지침 수정", "공간지침을 바꾼다", "지침 변경"):
            self.assertIn("guide_change", work_plan.detect_risk_signals(danger), danger)

    def test_high_cost_many_steps_escalates(self):
        steps = [f"{i}. 단계" for i in range(20)]
        a = work_plan.assess_approval("아주 큰 작업", steps, False)
        self.assertTrue(a["needs_approval"])
        self.assertIn("high_cost", a["system_signals"])

    def test_system_never_downgrades(self):
        # 에이전트가 True인데 신호가 없어도 절대 auto로 낮추지 않는다
        a = work_plan.assess_approval("안전한 일", ["1. 함"], True)
        self.assertTrue(a["needs_approval"])


class RegisterTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.space = PREFIX + "reg"
        _mkspace(self.space)

    def tearDown(self):
        _cleanup()

    def test_register_creates_pending_and_is_idempotent(self):
        ctx = {"intent_id": "intent_1"}
        r1 = work_plan.register(
            self.space, requesting_agent="기획자_aaaa", worker="구현자_bbbb",
            objective="기능 X 구현", plan_steps=["1. 설계", "2. 구현"],
            context=ctx,
        )
        self.assertFalse(r1["duplicate"])
        self.assertEqual(r1["record"]["state"], work_plan.PENDING)
        # 동일 (agent,worker,objective,intent) → 같은 plan_id, 멱등
        r2 = work_plan.register(
            self.space, requesting_agent="기획자_aaaa", worker="구현자_bbbb",
            objective="기능 X 구현", plan_steps=["1. 설계", "2. 구현"],
            context=ctx,
        )
        self.assertTrue(r2["duplicate"])
        self.assertEqual(r1["record"]["plan_id"], r2["record"]["plan_id"])
        self.assertEqual(len(work_plan.list_plans(self.space)), 1)

    def test_register_defaults_plan_steps_to_objective(self):
        r = work_plan.register(
            self.space, requesting_agent="a_aaaa", worker="a_aaaa",
            objective="목표만 있음", plan_steps=[],
        )
        self.assertEqual(r["record"]["plan_steps"], ["목표만 있음"])

    def test_register_requires_objective_and_worker(self):
        with self.assertRaises(work_plan.WorkPlanError):
            work_plan.register(self.space, requesting_agent="a", worker="w", objective="", plan_steps=[])
        with self.assertRaises(work_plan.WorkPlanError):
            work_plan.register(self.space, requesting_agent="a", worker="", objective="x", plan_steps=[])


class TransitionTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.space = PREFIX + "trans"
        _mkspace(self.space)

    def tearDown(self):
        _cleanup()

    def _register(self, *, needs_approval):
        assessment = work_plan.assess_approval("obj", ["1. step"], needs_approval)
        return work_plan.register(
            self.space, requesting_agent="ag_0001", worker="wk_0002",
            objective="obj " + ("R" if needs_approval else "A"),
            plan_steps=["1. step"], assessment=assessment,
        )["record"]

    def test_auto_approve_then_execute(self):
        plan = self._register(needs_approval=False)
        self.assertEqual(plan["approval_mode"], "auto_manager")
        ap = work_plan.approve(self.space, plan["plan_id"], actor="공간관리", mode="auto_manager")
        self.assertFalse(ap["duplicate"])
        self.assertEqual(work_plan.get(self.space, plan["plan_id"])["state"], work_plan.APPROVED)
        ex = work_plan.mark_executing(self.space, plan["plan_id"], task_id="task_xyz")
        self.assertEqual(ex["record"]["state"], work_plan.EXECUTING)
        self.assertEqual(work_plan.get(self.space, plan["plan_id"])["task_id"], "task_xyz")

    def test_invariant_b_representative_plan_cannot_auto_approve(self):
        plan = self._register(needs_approval=True)
        self.assertEqual(plan["approval_mode"], "representative")
        with self.assertRaises(work_plan.WorkPlanError):
            work_plan.approve(self.space, plan["plan_id"], actor="공간관리", mode="auto_manager")
        # 대표 승인은 가능
        ap = work_plan.approve(self.space, plan["plan_id"], actor="대표", mode="representative")
        self.assertEqual(ap["record"]["state"], work_plan.APPROVED)

    def test_approve_is_idempotent(self):
        plan = self._register(needs_approval=False)
        a1 = work_plan.approve(self.space, plan["plan_id"], actor="공간관리", mode="auto_manager")
        a2 = work_plan.approve(self.space, plan["plan_id"], actor="공간관리", mode="auto_manager")
        self.assertFalse(a1["duplicate"])
        self.assertTrue(a2["duplicate"])

    def test_reject_pending_and_block_after_start(self):
        plan = self._register(needs_approval=True)
        rj = work_plan.reject(self.space, plan["plan_id"], actor="대표", reason="방향 다름")
        self.assertEqual(rj["record"]["state"], work_plan.REJECTED)
        # 거절된 계획은 승인 불가
        with self.assertRaises(work_plan.WorkPlanError):
            work_plan.approve(self.space, plan["plan_id"], actor="대표", mode="representative")

    def test_cannot_execute_without_approval(self):
        plan = self._register(needs_approval=True)
        with self.assertRaises(work_plan.WorkPlanError):
            work_plan.mark_executing(self.space, plan["plan_id"], task_id="t1")

    def test_set_approval_message(self):
        plan = self._register(needs_approval=True)
        work_plan.set_approval_message(self.space, plan["plan_id"], "msg-123")
        self.assertEqual(work_plan.get(self.space, plan["plan_id"])["approval_message_id"], "msg-123")

    def test_supersede_unstarted(self):
        plan = self._register(needs_approval=True)
        out = work_plan.supersede(self.space, [plan["plan_id"]], reason="generation changed")
        self.assertEqual(out[0]["record"]["state"], work_plan.SUPERSEDED)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.space = PREFIX + "snap"
        _mkspace(self.space)

    def tearDown(self):
        _cleanup()

    def test_snapshot_counts_auto_and_representative_pending(self):
        # auto 1개, representative 1개 등록
        work_plan.register(
            self.space, requesting_agent="a_0001", worker="w_0001",
            objective="auto 작업", plan_steps=["1"],
            assessment=work_plan.assess_approval("auto 작업", ["1"], False),
        )
        work_plan.register(
            self.space, requesting_agent="a_0002", worker="w_0002",
            objective="대외비 공유 작업", plan_steps=["1"],
            assessment=work_plan.assess_approval("대외비 공유 작업", ["1"], False),
        )
        snap = work_plan.snapshot(self.space)
        self.assertEqual(snap["pending_count"], 2)
        self.assertEqual(snap["auto_approvable_pending_count"], 1)
        self.assertEqual(snap["representative_pending_count"], 1)
        self.assertFalse(snap["ledger_corrupt"])


if __name__ == "__main__":
    unittest.main()
