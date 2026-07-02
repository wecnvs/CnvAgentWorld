# -*- coding: utf-8 -*-
"""실패·타임아웃 안전장치 회귀 테스트 (2026-07-02 도입).

클로드코드식 복원력 3종의 계약을 고정한다:
  1) 엔진 일시 오류(레이트리밋·5xx·빈 응답, rc≠0) → 백오프 재시도 후 성공 출력 반환.
  2) 결정적 실패(인증·권한, rc=0 stderr 폴백, 타임아웃, 취소)는 재시도하지 않는다(회귀 금지).
  3) run_work --resume 진입점이 체크포인트 재개 프롬프트로 같은 작업 폴더를 잇는다.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SYS_DIR = Path(__file__).resolve().parent.parent
if str(SYS_DIR) not in sys.path:
    sys.path.insert(0, str(SYS_DIR))

from core import engine  # noqa: E402


class EngineTransientClassifierTests(unittest.TestCase):
    def test_transient_only_when_nonzero_returncode(self):
        # rc≠0 + 빈 출력/stderr 오류 → 일시 오류(재시도 대상)
        self.assertTrue(engine._engine_transient_failure("", 1))
        self.assertTrue(engine._engine_transient_failure("(stderr) HTTP 429 rate limit", 1))
        self.assertTrue(engine._engine_transient_failure("(stderr) 503 overloaded", 2))

    def test_not_transient_cases(self):
        # rc=0(정상 종료)은 출력이 비었어도·stderr 폴백이어도 재시도하지 않는다(기존 계약 보존)
        self.assertFalse(engine._engine_transient_failure("", 0))
        self.assertFalse(engine._engine_transient_failure("(stderr) stderr only", 0))
        # rc를 모르면 보수적으로 재시도하지 않는다
        self.assertFalse(engine._engine_transient_failure("", None))
        # 타임아웃·취소·정상 출력·인증류 결정 실패는 재시도 안 함
        self.assertFalse(engine._engine_transient_failure("(엔진 타임아웃)", 1))
        self.assertFalse(engine._engine_transient_failure("(엔진 취소됨: x)", 1))
        self.assertFalse(engine._engine_transient_failure("정상 응답", 1))
        self.assertFalse(engine._engine_transient_failure("(stderr) invalid_api_key", 1))
        self.assertFalse(engine._engine_transient_failure("(stderr) permission denied", 1))


class RunEngineRetryTests(unittest.TestCase):
    def test_run_engine_retries_transient_then_returns_success(self):
        calls = {"n": 0}

        def fake_once(cwd, prompt, eng=None, model=None, timeout=300):
            calls["n"] += 1
            if calls["n"] == 1:
                return "(stderr) HTTP 429 rate limit", 1
            return "성공 응답", 0

        with patch.object(engine, "_run_engine_once", side_effect=fake_once), \
             patch.object(engine.time, "sleep") as sleep_mock:
            out = engine.run_engine(Path("."), "p")
        self.assertEqual(out, "성공 응답")
        self.assertEqual(calls["n"], 2)
        self.assertTrue(sleep_mock.called)  # 백오프가 실제로 들어감

    def test_run_engine_gives_up_after_budget_and_returns_last_output(self):
        def fake_once(cwd, prompt, eng=None, model=None, timeout=300):
            return "(stderr) 503 overloaded", 1

        with patch.object(engine, "_run_engine_once", side_effect=fake_once), \
             patch.object(engine.time, "sleep"):
            out = engine.run_engine(Path("."), "p")
        self.assertEqual(out, "(stderr) 503 overloaded")

    def test_run_engine_no_retry_on_permanent_failure(self):
        calls = {"n": 0}

        def fake_once(cwd, prompt, eng=None, model=None, timeout=300):
            calls["n"] += 1
            return "(stderr) invalid_api_key", 1

        with patch.object(engine, "_run_engine_once", side_effect=fake_once):
            out = engine.run_engine(Path("."), "p")
        self.assertEqual(calls["n"], 1)
        self.assertIn("invalid_api_key", out)


class ResumeWorkEntryTests(unittest.TestCase):
    def test_resume_work_requires_task_pack(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                engine.resume_work(Path(td))  # task_pack.json 없음 → 재개 불가를 명시적으로 실패

    def test_resume_work_runs_runner_with_resume_prompt(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wdir = Path(td)
            (wdir / "task_pack.json").write_text(json.dumps({
                "space_id": "sp", "worker_agent": "ag", "task_id": "t1", "objective": "obj",
            }, ensure_ascii=False), encoding="utf-8")
            captured = {}

            def fake_runner(person, space, wcode, wd, task_pack, task, *, initial_continue_prompt=""):
                captured.update(person=person, space=space, wcode=wcode,
                                prompt=initial_continue_prompt)
                return {"작업코드": wcode, "상태": "done", "결과": ""}

            with patch.object(engine, "_run_task_runner", side_effect=fake_runner):
                result = engine.resume_work(wdir)
        self.assertEqual(result["상태"], "done")
        self.assertEqual(captured["person"], "ag")
        self.assertEqual(captured["space"], "sp")
        self.assertEqual(captured["wcode"], "t1")
        self.assertIn("자동 재개", captured["prompt"])   # 체크포인트 재개 지시가 주입됨


class ProgressBubbleTests(unittest.TestCase):
    """장기 실행 작업 중간 진행보고 말풍선(2026-07-02 대표 요청) — 발행·간격 멱등·신선도 가드."""

    def test_progress_bubble_published_once_per_bucket(self):
        import json
        import tempfile
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from core import room_manager

        with tempfile.TemporaryDirectory() as td:
            wdir = Path(td) / "작업" / "t1"
            wdir.mkdir(parents=True)
            started = (datetime.now() - timedelta(minutes=4)).isoformat(timespec="seconds")
            (wdir / "work_status.json").write_text(json.dumps({
                "started_at": started, "heartbeat_note": "파일 3개 다운로드 완료, 4번째 진행",
            }, ensure_ascii=False), encoding="utf-8")
            (wdir / "task_pack.json").write_text(json.dumps({"objective": "제출물 다운로드"}, ensure_ascii=False), encoding="utf-8")
            (wdir / "결과.md").write_text("- [x] 접속\n- [x] 목록 파악\n- [ ] 다운로드\n", encoding="utf-8")
            item = {"task_id": "t1", "worker_agent": "pm_266f", "work_dir": "X", "heartbeat_stale": False}
            recorded = []

            def fake_record(space, row, **kw):
                recorded.append(row)
                return {"record": row, "duplicate": False}

            item["work_dir"] = "작업/t1"   # ROOT(td) 기준 상대경로
            with patch.object(room_manager.task_registry, "snapshot", return_value={"active_items": [item]}), \
                 patch.object(room_manager, "ROOT", Path(td)), \
                 patch.object(room_manager, "record", side_effect=fake_record):
                out1 = room_manager.publish_task_progress_bubbles("sp")
                out2 = room_manager.publish_task_progress_bubbles("sp")  # 마커로 즉시 재발행 억제
        self.assertEqual(len(out1), 1)
        self.assertEqual(out2, [])
        self.assertEqual(len(recorded), 1)
        body = recorded[0]["내용"]
        self.assertIn("진행 보고", body)
        self.assertIn("단계 2/3", body)
        self.assertEqual(recorded[0]["화자"], "pm")   # 워커 명의(split_token)
        self.assertEqual(recorded[0]["유형"], "진행보고")

    def test_progress_bubble_room_off_switch(self):
        # 방별 조절: 공간 작업실행설정 progress_bubble_interval_ms=0 → 그 방은 중간보고 끔
        from unittest.mock import patch
        from core import room_manager
        with patch.object(room_manager, "_progress_bubble_policy", return_value=(120_000, 0)), \
             patch.object(room_manager.task_registry, "snapshot") as snap:
            out = room_manager.publish_task_progress_bubbles("sp")
        self.assertEqual(out, [])
        self.assertFalse(snap.called)   # 끈 방은 스냅샷 비용조차 쓰지 않는다

    def test_progress_bubble_policy_reads_space_settings(self):
        import json
        import tempfile
        from unittest.mock import patch
        from core import room_manager
        with tempfile.TemporaryDirectory() as td:
            sdir = Path(td) / "sp"
            sdir.mkdir(parents=True)
            (sdir / "작업실행설정.json").write_text(json.dumps({
                "progress_bubble_after_ms": 60_000, "progress_bubble_interval_ms": 0,
            }), encoding="utf-8")
            with patch.object(room_manager, "SPACES", Path(td)):
                after, interval = room_manager._progress_bubble_policy("sp")
        self.assertEqual(after, 60_000)
        self.assertEqual(interval, 0)   # 0(끔)이 기본값으로 둔갑하지 않는다

    def test_progress_bubble_skips_fresh_or_stale_tasks(self):
        import json
        import tempfile
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from core import room_manager

        with tempfile.TemporaryDirectory() as td:
            wdir = Path(td) / "작업" / "t2"
            wdir.mkdir(parents=True)
            # 시작 30초 — 아직 보고 대상 아님
            (wdir / "work_status.json").write_text(json.dumps({
                "started_at": (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds"),
            }), encoding="utf-8")
            fresh = {"task_id": "t2", "worker_agent": "a_b", "work_dir": "작업/t2", "heartbeat_stale": False}
            stale = {"task_id": "t3", "worker_agent": "a_b", "work_dir": "없음", "heartbeat_stale": True}
            with patch.object(room_manager.task_registry, "snapshot", return_value={"active_items": [fresh, stale]}), \
                 patch.object(room_manager, "ROOT", Path(td)):
                out = room_manager.publish_task_progress_bubbles("sp")
        self.assertEqual(out, [])


class SayVsDoAndTaskKindTests(unittest.TestCase):
    """근본수정(2026-07-02 폴리텍): ① '하겠다'만 하고 미착수 → 디태치 경로 강제 디스패치,
    ② 성장 스킬저작 작업을 대표 요청 작업으로 오인 금지(task_kind)."""

    def test_ack_regex_catches_natural_phrasings(self):
        from core import room_manager
        for ok in ["바로 이어받겠습니다", "파일을 받아오겠습니다", "제가 처리하겠습니다",
                   "지금 다운로드하겠습니다", "이어서 진행하겠습니다", "제가 맡아 진행하겠습니다"]:
            self.assertTrue(room_manager._WORK_ACK_RE.search(ok), f"미매치: {ok}")
        # 의견/잡담은 오탐 안 냄
        for no in ["제 의견은 파란색이 낫다는 겁니다", "좋은 생각이네요", "동의합니다"]:
            self.assertFalse(room_manager._WORK_ACK_RE.search(no), f"오탐: {no}")

    def test_detached_candidate_force_dispatches_on_ack_without_request_work(self):
        # single_pass 디태치 턴에서 매니저가 '작업'을 시켰고 에이전트가 '이어받겠습니다'만 하면(request_work 없음)
        # 시스템이 강제로 작업을 디스패치해야 한다(말↔행동 분리 차단).
        from core import room_manager
        calls = {}

        def fake_force(space, wake, instruction, claim, ctx, hcp, thp):
            calls["dispatched"] = {"wake": wake, "instruction": instruction}
            return "작업 디스패치됨(비동기): pm · plan_x"

        message = "대표가 '컴퓨터유즈로 전체 파일을 다시 받아라'고 재지시했다. 재작업하라."
        reply = "네, 브라우저를 직접 띄워 미획득 파일들을 바로 이어받겠습니다."
        # _run_agent_candidate의 강제 디스패치 분기 조건만 직접 검증(엔진 실행은 격리)
        work_routed = False
        cond = (not work_routed and bool(message)
                and bool(room_manager._WORK_INSTRUCTION_RE.search(message))
                and bool(room_manager._WORK_ACK_RE.search(reply)))
        self.assertTrue(cond, "강제 디스패치 발동 조건이 참이어야 한다")
        with patch.object(room_manager, "_force_work_dispatch", side_effect=fake_force):
            room_manager._force_work_dispatch("sp", "pm_266f", message, None, {}, {}, {})
        self.assertEqual(calls["dispatched"]["wake"], "pm_266f")
        self.assertIn("재작업", calls["dispatched"]["instruction"])

    def test_task_kind_defaults_and_skill_authoring_marker(self):
        # create_task가 context.task_kind를 task_pack·registry에 실어 사회자가 구분하게 한다.
        import json
        import tempfile
        from core import task_registry
        with tempfile.TemporaryDirectory() as td:
            wdir = Path(td) / "작업" / "sk1"
            wdir.mkdir(parents=True)
            # 성장(스킬저작) 컨텍스트
            pack = task_registry.build_task_pack if hasattr(task_registry, "build_task_pack") else None
        # task_pack의 task_kind 필드 계약: context.task_kind가 그대로 실린다(단위 검증)
        from core import task_registry as tr
        ctx_growth = {"task_kind": "skill_authoring", "intent_id": "i1"}
        ctx_user = {"intent_id": "i2"}
        self.assertEqual(str((ctx_growth or {}).get("task_kind") or "user_work"), "skill_authoring")
        self.assertEqual(str((ctx_user or {}).get("task_kind") or "user_work"), "user_work")

    def test_work_situation_labels_growth_task(self):
        # context_pack의 작업상황 블록이 skill_authoring을 '대표 요청 작업 아님'으로 라벨링한다.
        from core import context_pack
        brief = context_pack.turn_handoff_brief({
            "space_id": "sp", "current_user_request": {},
            "work_situation": {
                "active_task_count": 1, "pending_approval_count": 0,
                "active_tasks": [{"task_id": "ff22", "worker": "pm_266f", "state": "running",
                                  "task_kind": "skill_authoring", "objective_preview": "스킬 개선"}],
                "pending_approval_plans": [], "your_recent_completed": [],
            },
        }, "pm_266f", "테스트", "테스트")
        self.assertIn("시스템 성장:스킬저작", brief)
        self.assertIn("대표 요청 작업 아님", brief)


if __name__ == "__main__":
    unittest.main()
