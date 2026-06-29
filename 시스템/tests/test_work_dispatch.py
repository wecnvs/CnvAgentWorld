#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""비동기 작업 디스패치(Phase A) — detached 분리·동시상한·인라인 폴백·run_work 러너."""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import room_manager, work_plan, run_work  # noqa: E402
from core.paths import SPACES  # noqa: E402

PREFIX = "tmp_dispatch_"


def _mkspace(name):
    (SPACES / name).mkdir(parents=True, exist_ok=True)
    return SPACES / name


def _cleanup():
    for p in SPACES.glob(PREFIX + "*"):
        shutil.rmtree(p, ignore_errors=True)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.space = PREFIX + "a"
        _mkspace(self.space)
        # 비동기 경로 검증을 위해 run_engine을 원본으로 확정(다른 테스트가 몽키패치 남겼을 수 있음)
        room_manager.engine.run_engine = room_manager.engine._ORIGINAL_RUN_ENGINE
        room_manager.WORK_DISPATCH_ASYNC = True

    def tearDown(self):
        _cleanup()

    def _approved_plan(self):
        reg = work_plan.register(
            self.space, requesting_agent="기획_0001", worker="구현_0002",
            objective="기능 구현", plan_steps=["1. 함"],
            assessment=work_plan.assess_approval("기능 구현", ["1. 함"], False),
        )
        pid = reg["record"]["plan_id"]
        work_plan.approve(self.space, pid, actor="공간관리", mode="auto_manager")
        return pid

    def _call(self, pid):
        return room_manager._dispatch_work_plan(
            self.space, plan_id=pid, wake="기획_0001", worker="구현_0002",
            objective_for_work="기능 구현", effect_id="eff-1", context={"intent_id": "i1"},
            claim=None, handoff_context_pack={}, turn_handoff_pack={},
        )

    def test_async_dispatch_spawns_detached_and_returns_fast(self):
        pid = self._approved_plan()
        fake = MagicMock(); fake.pid = 4242
        with patch.object(room_manager.subprocess, "Popen", return_value=fake) as popen, \
             patch.object(room_manager.task_registry, "snapshot", return_value={"active_count": 0}):
            msg = self._call(pid)
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        self.assertIn("core.run_work", cmd)                 # detached 러너 호출
        self.assertTrue(popen.call_args[1].get("start_new_session"))  # 세션 분리
        self.assertIn("디스패치됨", msg)
        dfile = SPACES / self.space / "dispatch" / f"{pid}.json"
        self.assertTrue(dfile.exists())
        self.assertEqual(json.loads(dfile.read_text(encoding="utf-8"))["plan_id"], pid)

    def test_ceiling_defers_without_dispatch(self):
        pid = self._approved_plan()
        with patch.object(room_manager.subprocess, "Popen") as popen, \
             patch.object(room_manager.task_registry, "snapshot",
                          return_value={"active_count": room_manager.MAX_IN_FLIGHT_TASKS}):
            msg = self._call(pid)
        popen.assert_not_called()                            # 상한 초과 → 디스패치 안 함
        self.assertIn("보류", msg)
        # plan은 approved로 남아 재시도 가능
        self.assertEqual(work_plan.get(self.space, pid)["state"], work_plan.APPROVED)

    def test_inline_when_run_engine_monkeypatched(self):
        # 테스트 환경(run_engine 몽키패치)에서는 인라인 동기 → _execute_work_plan 직접 호출(무회귀)
        pid = self._approved_plan()
        with patch.object(room_manager.engine, "run_engine", lambda *a, **k: "x"), \
             patch.object(room_manager, "_execute_work_plan", return_value="INLINE") as ex, \
             patch.object(room_manager.subprocess, "Popen") as popen:
            msg = self._call(pid)
        ex.assert_called_once()
        popen.assert_not_called()
        self.assertEqual(msg, "INLINE")

    def test_popen_failure_falls_back_inline(self):
        pid = self._approved_plan()
        with patch.object(room_manager.subprocess, "Popen", side_effect=OSError("no exec")), \
             patch.object(room_manager.task_registry, "snapshot", return_value={"active_count": 0}), \
             patch.object(room_manager, "_execute_work_plan", return_value="FALLBACK") as ex:
            msg = self._call(pid)
        ex.assert_called_once()                              # Popen 실패 → 인라인 폴백(유실 방지)
        self.assertEqual(msg, "FALLBACK")

    def test_run_work_runner_invokes_execute_and_cleans_file(self):
        dfile = SPACES / self.space / "d.json"
        dfile.write_text(json.dumps({
            "space": self.space, "plan_id": "p1", "wake": "a", "worker": "w",
            "objective_for_work": "o", "effect_id": "e", "context": {},
        }), encoding="utf-8")
        with patch.object(room_manager, "_execute_work_plan", return_value="ok") as ex:
            rc = run_work.main([str(dfile)])
        self.assertEqual(rc, 0)
        ex.assert_called_once()
        self.assertEqual(ex.call_args[1]["plan_id"], "p1")
        self.assertFalse(dfile.exists())                    # 디스패치 파일 정리


if __name__ == "__main__":
    unittest.main()
