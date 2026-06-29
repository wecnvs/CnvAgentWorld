#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChatAgentResult work_request의 plan/needs_approval/risk 파싱 — 설계_작업계획승인.md P2."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import chat_result  # noqa: E402

MEMBERS = {"기획자_aaaa", "구현자_bbbb"}


def _wr(result):
    return chat_result.work_request(result, default_worker="기획자_aaaa", member_tokens=MEMBERS)


class WorkRequestPlanParseTest(unittest.TestCase):
    def test_full_plan_and_explicit_needs_approval(self):
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "public_reply": "이렇게 할게요",
            "work_request": {
                "objective": "기능 X 구현",
                "suggested_worker": "구현자_bbbb",
                "plan": ["1. 설계", "2. 구현", "  ", "3. 검수"],
                "needs_approval": True,
                "approval_reason": "지침 변경 포함",
                "risk": {"level": "high", "reason": "비가역"},
            },
        })
        self.assertEqual(r["worker"], "구현자_bbbb")
        self.assertEqual(r["plan"], ["1. 설계", "2. 구현", "3. 검수"])  # 빈 항목 제거
        self.assertIs(r["needs_approval"], True)
        self.assertEqual(r["approval_reason"], "지침 변경 포함")
        self.assertEqual(r["risk_level"], "high")
        self.assertEqual(r["risk_reason"], "비가역")
        self.assertEqual(r["public_reply"], "이렇게 할게요")

    def test_explicit_needs_approval_false(self):
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "work_request": {"objective": "본문 5장", "needs_approval": False},
        })
        self.assertIs(r["needs_approval"], False)

    def test_missing_needs_approval_is_none(self):
        # 키 자체가 없으면 None(미선언) → 게이트에서 보수적 True로 처리
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "work_request": {"objective": "뭔가"},
        })
        self.assertIsNone(r["needs_approval"])
        self.assertEqual(r["plan"], [])  # 호출부가 [objective]로 채움

    def test_plan_as_string_is_wrapped(self):
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "work_request": {"objective": "x", "plan": "한 줄 계획"},
        })
        self.assertEqual(r["plan"], ["한 줄 계획"])

    def test_invalid_risk_level_normalized_empty(self):
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "work_request": {"objective": "x", "risk": {"level": "MAYBE"}},
        })
        self.assertEqual(r["risk_level"], "")

    def test_plan_capped_at_12(self):
        steps = [f"{i}. s" for i in range(20)]
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "work_request": {"objective": "x", "plan": steps},
        })
        self.assertEqual(len(r["plan"]), 12)

    def test_backward_compatible_no_work_request_fields(self):
        # plan/risk 없는 기존 형태 — objective만 있어도 동작
        r = _wr({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "public_reply": "작업 필요",
            "work_request": {"objective": "레거시 작업"},
        })
        self.assertEqual(r["objective"], "레거시 작업")
        self.assertEqual(r["plan"], [])
        self.assertIsNone(r["needs_approval"])
        self.assertEqual(r["risk_level"], "")

    def test_non_request_action_returns_none(self):
        self.assertIsNone(_wr({"schema": "ChatAgentResult.v1", "action": "reply"}))


if __name__ == "__main__":
    unittest.main()
