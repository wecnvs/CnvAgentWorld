#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""채팅 턴 비동기 디스패치(§9.3 갭1) — detached 분리·중복가드·깊이캡·인라인 폴백·run_chat_turn 러너·결정적 공개."""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import room_manager, run_chat_turn, candidate_queue  # noqa: E402
from core.paths import SPACES  # noqa: E402

PREFIX = "tmp_chatdisp_"


def _mkspace(name):
    (SPACES / name).mkdir(parents=True, exist_ok=True)
    return SPACES / name


def _cleanup():
    for p in SPACES.glob(PREFIX + "*"):
        shutil.rmtree(p, ignore_errors=True)


class ChatDispatchTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.space = PREFIX + "a"
        _mkspace(self.space)
        room_manager.engine.run_engine = room_manager.engine._ORIGINAL_RUN_ENGINE
        room_manager.CHAT_DISPATCH_ASYNC = True

    def tearDown(self):
        _cleanup()

    def _call(self, context=None):
        return room_manager._dispatch_chat_turn(
            self.space, wake="구현_0002", message="의견 줘",
            context=context or {"intent_id": "i1"}, reason="테스트",
        )

    def test_async_dispatch_spawns_detached_and_returns_true(self):
        fake = MagicMock(); fake.pid = 4242
        with patch.object(room_manager.subprocess, "Popen", return_value=fake) as popen:
            ok = self._call()
        self.assertTrue(ok)
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        self.assertIn("core.run_chat_turn", cmd)
        self.assertTrue(popen.call_args[1].get("start_new_session"))
        files = list((SPACES / self.space / "dispatch_chat").glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["wake"], "구현_0002")
        self.assertTrue(data["turn_id"].startswith(room_manager.CHAT_TURN_ID_PREFIX))

    def test_duplicate_in_flight_skips_new_popen_but_reports_dispatched(self):
        fake = MagicMock(); fake.pid = 1
        with patch.object(room_manager.subprocess, "Popen", return_value=fake) as popen:
            self.assertTrue(self._call())
            self.assertTrue(self._call())  # 같은 멤버 — 새 프로세스 없이 '이미 생각 중' 처리
        popen.assert_called_once()

    def test_depth_cap_falls_back_inline(self):
        with patch.object(room_manager.subprocess, "Popen") as popen:
            ok = self._call(context={"chat_chain_depth": room_manager.CHAT_CHAIN_MAX_DEPTH})
        self.assertFalse(ok)   # 인라인 폴백 → 기존 체인 상한으로 수렴
        popen.assert_not_called()

    def test_inline_when_run_engine_monkeypatched(self):
        with patch.object(room_manager.engine, "run_engine", lambda *a, **k: "x"), \
             patch.object(room_manager.subprocess, "Popen") as popen:
            ok = self._call()
        self.assertFalse(ok)
        popen.assert_not_called()

    def test_popen_failure_falls_back_inline_and_cleans_file(self):
        with patch.object(room_manager.subprocess, "Popen", side_effect=OSError("no exec")):
            ok = self._call()
        self.assertFalse(ok)
        self.assertEqual(list((SPACES / self.space / "dispatch_chat").glob("*.json")), [])

    def test_runner_invokes_candidate_publish_and_tick_and_cleans_file(self):
        ddir = SPACES / self.space / "dispatch_chat"
        ddir.mkdir(parents=True, exist_ok=True)
        dfile = ddir / "chatturn_x.json"
        dfile.write_text(json.dumps({
            "space": self.space, "wake": "구현_0002", "message": "m",
            "reason": "r", "context": {"chat_chain_depth": 1}, "turn_id": "chatturn_x",
        }), encoding="utf-8")
        with patch.object(room_manager, "_run_agent_candidate", return_value={"ok": True}) as cand, \
             patch.object(room_manager, "publish_ready_chat_candidates", return_value={"published": 1}) as pub, \
             patch.object(room_manager, "tick", return_value={"ok": True}) as tick:
            rc = run_chat_turn.main([str(dfile)])
        self.assertEqual(rc, 0)
        cand.assert_called_once()
        self.assertEqual(cand.call_args[1]["join_policy"], "single_pass")
        pub.assert_called_once()
        tick.assert_called_once()
        # 깊이 증가 확인(연속 폭주 방지 캡의 재료)
        self.assertEqual(tick.call_args[0][2]["chat_chain_depth"], 2)
        self.assertFalse(dfile.exists())

    def test_runner_failure_records_and_skips_tick(self):
        ddir = SPACES / self.space / "dispatch_chat"
        ddir.mkdir(parents=True, exist_ok=True)
        dfile = ddir / "chatturn_y.json"
        dfile.write_text(json.dumps({
            "space": self.space, "wake": "구현_0002", "message": "m",
            "reason": "r", "context": {}, "turn_id": "chatturn_y",
        }), encoding="utf-8")
        with patch.object(room_manager, "_run_agent_candidate", side_effect=RuntimeError("engine down")), \
             patch.object(room_manager, "record_chat_turn_failure") as rec, \
             patch.object(room_manager, "publish_ready_chat_candidates", return_value={"published": 0}), \
             patch.object(room_manager, "tick") as tick:
            rc = run_chat_turn.main([str(dfile)])
        self.assertEqual(rc, 1)
        rec.assert_called_once()
        tick.assert_not_called()   # 실패 턴은 대화 연속을 만들지 않는다(의무 재개→sweep이 잇는다)
        self.assertFalse(dfile.exists())

    def test_publish_ready_publishes_single_pass_candidate_as_member_bubble(self):
        # 후보를 직접 심고(결정적) publish_ready가 멤버 말풍선으로 공개 + pending 소진을 확인한다.
        space_dir = SPACES / self.space
        (space_dir / "관리자").mkdir(parents=True, exist_ok=True)
        candidate_queue.enqueue_candidate(
            self.space,
            turn_id="chatturn_pub1",
            target_agent="구현_0002",
            manager_message="의견 줘",
            reply="제 의견은 A입니다",
            context={"intent_id": "i1", "conversation_thread_id": "t1",
                     "room_generation": None, "source_event_seq": 1, "source_message_id": "m1"},
            join_policy="single_pass",
            presentation_mode="direct_publish",
        )
        result = room_manager.publish_ready_chat_candidates(self.space)
        self.assertEqual(result["published"], 1)
        rows = [json.loads(line) for line in
                (space_dir / "대화.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(r.get("화자") == "구현" and "제 의견은 A" in str(r.get("내용")) for r in rows))
        self.assertEqual(int(candidate_queue.snapshot(self.space).get("pending_count") or 0), 0)
        # 멱등 — 재호출해도 재공개 없음
        again = room_manager.publish_ready_chat_candidates(self.space)
        self.assertEqual(again["published"], 0)


class ChatDispatchTickTest(unittest.TestCase):
    """tick 통합 — 디스패치된 pass가 에필로그에서 죽지 않고(claim 고아 방지) 상태를 유지하는지."""

    def setUp(self):
        _cleanup()
        self._orig_engine = room_manager.engine.run_engine

    def tearDown(self):
        room_manager.engine.run_engine = self._orig_engine
        _cleanup()

    def test_tick_pass_with_dispatch_completes_without_crash(self):
        from tests.test_orchestration_v0 import make_space
        space = PREFIX + "tick"
        member = PREFIX + "agent_t1"
        make_space(space, [member])
        room_manager.post(space, "의견 줘", run_manager=False, client_message_id="c1")

        def fake_manager(cwd, prompt, *args, **kwargs):
            return json.dumps({"action": "pass", "wake": member, "message": "한 줄 의견 부탁"},
                              ensure_ascii=False)

        room_manager.engine.run_engine = fake_manager
        with patch.object(room_manager, "_dispatch_chat_turn", return_value=True):
            result = room_manager.tick(space, "대표 메시지 처리", None, auto_continue=True)
        # 실증 버그: 디스패치 경로가 에필로그에서 미할당 reply를 참조 → UnboundLocalError로
        # tick 전체가 죽고 claim이 고아가 돼 방이 15분+ 멈췄다. 이 테스트가 그 회귀를 막는다.
        self.assertTrue(result.get("ok"))
        types = [e.get("type") for e in result.get("events") or []]
        self.assertIn("chat_turn_dispatched", types)
        state = json.loads((SPACES / space / "관리자" / "상태.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("상태"), "agent_running")  # 자식 공개 전까지 '생각 중' 유지


if __name__ == "__main__":
    unittest.main()
