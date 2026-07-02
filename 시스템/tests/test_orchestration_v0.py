#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import io
import json
import shutil
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))
sys.path.insert(0, str(ROOT / "시스템" / "대시보드" / "서버"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from core import candidate_queue, chat_policy, context_pack, engine, lesson_ledger, orchestration, people as people_core, publish_ledger, release_queue, response_obligation, room_manager, runtime, space_memory, spaces as spaces_core, task_registry, work_settings  # noqa: E402
from core.paths import PEOPLE, SPACES  # noqa: E402
from core.spaces import MANAGER_DIRNAME  # noqa: E402
from core.transcript import read  # noqa: E402
from 엔진 import world as cli_world  # noqa: E402
from routers import people as dashboard_people_router, spaces as dashboard_spaces_router  # noqa: E402


PREFIX = "tmp_orchv0_"


def cleanup():
    for base in (SPACES, PEOPLE):
        for path in base.glob(PREFIX + "*"):
            shutil.rmtree(path, ignore_errors=True)


def make_space(name, members=()):
    sdir = SPACES / name
    (sdir / MANAGER_DIRNAME).mkdir(parents=True, exist_ok=True)
    (sdir / "대화.jsonl").write_text("", encoding="utf-8")
    (sdir / "멤버.json").write_text(
        json.dumps([
            {"이름": token, "코드": token[-4:], "토큰": token}
            for token in members
        ], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (sdir / "공간지침.md").write_text("", encoding="utf-8")
    (sdir / "요약.md").write_text("", encoding="utf-8")
    for token in members:
        pdir = PEOPLE / token
        pdir.mkdir(parents=True, exist_ok=True)
        seat = pdir / "공간" / name
        seat.mkdir(parents=True, exist_ok=True)
        (seat / "대화.jsonl").write_text("", encoding="utf-8")
    return sdir


def enqueue_test_release(space, release_id="rel-1", public_summary="승인된 공개문", *, context=None):
    if context is None:
        post = room_manager.post(space, "작업 결과를 확인해줘", run_manager=False, client_message_id=f"client-{release_id}")
        context = post["orchestration"]
    work_dir = SPACES / space / "test_work" / release_id
    work_dir.mkdir(parents=True, exist_ok=True)
    task_pack = {
        "task_pack_id": f"taskpack-{release_id}",
        "task_pack_checksum": f"checksum-{release_id}",
        **context,
    }
    request = {
        "schema": "ReleaseRequest.v1",
        "release_id": release_id,
        "source_task_id": f"task-{release_id}",
        "task_pack_id": task_pack["task_pack_id"],
        "task_pack_checksum_seen": task_pack["task_pack_checksum"],
        "release_kind": "done",
        "public_summary": public_summary,
        **context,
    }
    return release_queue.enqueue_release(
        space,
        release_request=request,
        work_dir=work_dir,
        task_pack=task_pack,
    )


class OrchestrationV0Tests(unittest.TestCase):
    def setUp(self):
        cleanup()

    def tearDown(self):
        cleanup()

    def _force_task_heartbeat_old(self, space, task_id, *, at="2000-01-01T00:00:00", phase="old_phase"):
        registry_path = SPACES / space / "task_registry.jsonl"
        rows = [
            json.loads(line)
            for line in registry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for row in reversed(rows):
            if row.get("task_id") == task_id:
                row["last_heartbeat_at"] = at
                row["heartbeat_phase"] = phase
                break
        registry_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

    def _collect_parallel_candidates(self, space, member_a, member_b, *, agent_replies=None, client_message_id="client-1", manager_requested=False):
        agent_replies = agent_replies or {
            member_a: "A 후보 공개",
            member_b: "B 후보 공개",
        }
        post = room_manager.post(
            space,
            "두 관점으로 검토해줘",
            run_manager=False,
            client_message_id=client_message_id,
            manager_requested=manager_requested,
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "parallel_pass",
                    "wake": "",
                    "message": "",
                    "reason": "서로 다른 관점이 필요하다",
                    "targets": [
                        {"wake": member_a, "message": "A 후보를 만들어줘", "reason": "A 관점"},
                        {"wake": member_b, "message": "B 후보를 만들어줘", "reason": "B 관점"},
                    ],
                    "join_policy": "timeout_then_partial",
                    "presentation_mode": "silent_reference",
                }, ensure_ascii=False)
            token = cwd.parent.parent.name
            return agent_replies.get(token, f"{token} 후보 공개")

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "parallel collect", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        snapshot = candidate_queue.snapshot(space)
        pending = sorted(snapshot["pending_items"], key=lambda item: item.get("target_agent", ""))
        self.assertTrue(result["ok"])
        self.assertEqual(snapshot["pending_count"], 2)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        return context, pending

    def test_delete_person_removes_agent_folder_and_space_membership(self):
        space_a = PREFIX + "delperson_a"
        space_b = PREFIX + "delperson_b"
        target = PREFIX + "agentAzz99"
        other = PREFIX + "agentBzz99"
        make_space(space_a, [target, other])
        make_space(space_b, [target])

        result = people_core.delete_person(target)

        self.assertTrue(result["ok"])
        self.assertFalse((PEOPLE / target).exists())
        self.assertTrue((PEOPLE / other).exists())
        for space in (space_a, space_b):
            members = json.loads((SPACES / space / "멤버.json").read_text(encoding="utf-8"))
            self.assertFalse(any(m.get("토큰") == target for m in members))
        members_a = json.loads((SPACES / space_a / "멤버.json").read_text(encoding="utf-8"))
        self.assertTrue(any(m.get("토큰") == other for m in members_a))

    def test_delete_space_removes_space_folder_and_agent_seats(self):
        space = PREFIX + "delspace"
        other_space = PREFIX + "other_space"
        member_a = PREFIX + "agent_delsa"
        member_b = PREFIX + "agent_delsb"
        make_space(space, [member_a, member_b])
        make_space(other_space, [member_a])

        result = spaces_core.delete_space(space)

        self.assertTrue(result["ok"])
        self.assertFalse((SPACES / space).exists())
        self.assertFalse((PEOPLE / member_a / "공간" / space).exists())
        self.assertFalse((PEOPLE / member_b / "공간" / space).exists())
        self.assertTrue((PEOPLE / member_a / "공간" / other_space).exists())
        self.assertTrue((PEOPLE / member_a).exists())

    def test_dashboard_delete_routes_remove_people_and_spaces(self):
        space = PREFIX + "delapi"
        person = PREFIX + "agent_delapi"
        make_space(space, [person])
        app = FastAPI()
        app.include_router(dashboard_people_router.router)
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)

        delete_person = client.delete(f"/api/people/{person}")
        self.assertEqual(delete_person.status_code, 200)
        self.assertFalse((PEOPLE / person).exists())
        members = json.loads((SPACES / space / "멤버.json").read_text(encoding="utf-8"))
        self.assertFalse(any(m.get("토큰") == person for m in members))

        delete_space = client.delete(f"/api/spaces/{space}")
        self.assertEqual(delete_space.status_code, 200)
        self.assertFalse((SPACES / space).exists())

    def test_manager_prompt_includes_member_role_and_runtime_for_turn_choice(self):
        space = PREFIX + "profileprompt"
        member = PREFIX + "agent_profile"
        make_space(space, [member])
        pdir = PEOPLE / member
        seat = pdir / "공간" / space
        (pdir / "role.md").write_text("너는 테스트 기획 담당이다.", encoding="utf-8")
        runtime.write_runtime(pdir, "claude", "sonnet", source="test-root")
        runtime.write_runtime(seat, "gemini", "Gemini 3.1 Pro (High)", source="test-seat")

        prompt = room_manager._space_context(
            space,
            "대표가 기획 검토를 요청함",
            {"intent_id": "intent-test", "conversation_thread_id": "thread-test"},
        )

        self.assertIn("## 멤버 프로필과 런타임", prompt)
        self.assertIn(member, prompt)
        self.assertIn("너는 테스트 기획 담당이다.", prompt)
        self.assertIn('"seat_runtime"', prompt)
        self.assertIn('"engine": "gemini"', prompt)
        self.assertIn('"model": "Gemini 3.1 Pro (High)"', prompt)
        self.assertIn('"default_runtime"', prompt)
        self.assertIn('"engine": "claude"', prompt)
        self.assertIn('"model": "sonnet"', prompt)

    def test_lesson_ledger_records_evaluation_lesson_and_application_idempotently(self):
        space = PREFIX + "lessonunit"
        make_space(space)
        post = room_manager.post(space, "모바일에서 버튼이 안 눌렸어", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]

        first = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space_manager",
            what_failed=["모바일 버튼 동작을 확인하지 않았다"],
            user_feedback_refs=[post["ack"]["message_id"]],
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "verification_rule",
                "scope": "space",
                "status": "candidate",
                "instruction": "모바일 UI 수정은 모바일 viewport 또는 touch event 검증을 함께 수행한다.",
                "evidence_type": "user_correction",
                "evidence_level": "user_directive",
                "confidence": 0.8,
                "applies_when": {"space_id": space, "keywords": ["모바일", "버튼"]},
            },
        )
        duplicate = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space_manager",
            what_failed=["모바일 버튼 동작을 확인하지 않았다"],
            user_feedback_refs=[post["ack"]["message_id"]],
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "verification_rule",
                "scope": "space",
                "status": "candidate",
                "instruction": "모바일 UI 수정은 모바일 viewport 또는 touch event 검증을 함께 수행한다.",
            },
        )
        lesson_id = first["record"]["created_lesson_ids"][0]
        app = lesson_ledger.record_lesson_application(
            space,
            lesson_id=lesson_id,
            pack_id="ctx_test",
            manifest_hash_seen="hash_test",
            agent="agent_a001",
            mode="chat",
            applied=True,
            how="모바일 버튼 검증을 수행함",
            outcome="success",
        )
        app_duplicate = lesson_ledger.record_lesson_application(
            space,
            lesson_id=lesson_id,
            pack_id="ctx_test",
            manifest_hash_seen="hash_test",
            agent="agent_a001",
            mode="chat",
            applied=True,
            how="모바일 버튼 검증을 수행함",
            outcome="success",
        )

        status = room_manager.status(space)
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(app["duplicate"])
        self.assertTrue(app_duplicate["duplicate"])
        self.assertEqual(status["learning"]["lesson_count"], 1)
        self.assertEqual(status["learning"]["lesson_application_count"], 1)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("corrected"), 1)
        self.assertEqual(status["learning"]["growth_gap_count"], 1)
        self.assertEqual(status["learning"]["growth_gap_state_counts"].get("lesson_created"), 1)

    def test_lesson_ledger_requires_disposition_for_attention_outcome(self):
        space = PREFIX + "lessoncontract"
        make_space(space)
        with self.assertRaises(lesson_ledger.LessonLedgerError):
            lesson_ledger.record_post_interaction_evaluation(
                space,
                outcome="failed",
                source_event="test_failure",
                lesson_candidate_needed=True,
            )

    def test_lesson_promotion_candidates_are_idempotent_and_reviewable(self):
        space = PREFIX + "lessonpromo"
        make_space(space)
        for target, instruction in [
            ("knowledge", "반복 실패한 모바일 터치 검증 사례는 지식 후보로 축적한다."),
            ("skill", "반복되는 모바일 회귀 검증 절차는 스킬 후보로 승격 검토한다."),
        ]:
            lesson_ledger.record_post_interaction_evaluation(
                space,
                outcome="corrected",
                source_event=f"promotion_{target}",
                actor="대표",
                target="space",
                lesson_candidate_needed=True,
                lesson_candidate={
                    "kind": "growth_rule",
                    "scope": "space",
                    "status": "active",
                    "promotion_target": target,
                    "instruction": instruction,
                    "evidence_level": "user_directive",
                    "confidence": 0.9,
                    "applies_when": {"space_id": space, "agent_modes": ["manager"], "keywords": []},
                },
            )
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="promotion_none",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "instruction": "명시되지 않은 레슨은 자동 승격 후보로 만들지 않는다.",
            },
        )

        first = lesson_ledger.generate_promotion_candidates(space, actor="대표")
        duplicate = lesson_ledger.generate_promotion_candidates(space, actor="대표")
        by_target = {item["target_kind"]: item for item in first["candidates"]}
        lesson_ledger.review_promotion_candidate(
            space,
            by_target["knowledge"]["promotion_id"],
            decision="approve",
            actor="대표",
            reason="지식 후보로 적절함",
        )
        lesson_ledger.review_promotion_candidate(
            space,
            by_target["skill"]["promotion_id"],
            decision="reject",
            actor="대표",
            reason="아직 반복 검증 부족",
        )

        status = room_manager.status(space)
        learning = status["learning"]
        self.assertEqual(first["created_count"], 2)
        self.assertEqual(duplicate["created_count"], 0)
        self.assertEqual(duplicate["duplicate_count"], 2)
        self.assertEqual(learning["promotion_candidate_count"], 2)
        self.assertEqual(learning["promotion_approved_count"], 1)
        self.assertEqual(learning["promotion_rejected_count"], 1)
        self.assertEqual(learning["promotion_candidate_target_counts"].get("knowledge"), 1)
        self.assertEqual(learning["promotion_candidate_target_counts"].get("skill"), 1)
        self.assertFalse(learning["promotion_review_required"])
        self.assertEqual(learning["growth_gap_count"], 3)
        self.assertEqual(learning["growth_gap_state_counts"].get("promotion_approved"), 1)
        self.assertEqual(learning["growth_gap_state_counts"].get("promotion_rejected"), 1)
        self.assertEqual(learning["growth_gap_state_counts"].get("lesson_created"), 1)
        self.assertEqual(learning["growth_gap_open_count"], 0)

    def test_growth_gap_tracks_no_change_and_resource_triage(self):
        space = PREFIX + "growthgap"
        make_space(space)
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="failed",
            source_event="known_failure_no_new_lesson",
            actor="시스템",
            target="space",
            lesson_candidate_needed=True,
            no_lesson_reason="known_failure_already_covered_by_existing_lesson",
        )
        lesson_ledger.record_post_task_evaluation(
            space,
            task_id="task-growth-gap",
            outcome="corrected",
            actor="검수자",
            task_title="없는 스킬 확인",
            result_summary="마땅한 스킬이 없어 별도 triage 필요",
            lesson_candidate_needed=True,
            resource_change_needed=True,
            no_lesson_reason="skill_or_knowledge_target_not_decided_yet",
        )

        learning = room_manager.status(space)["learning"]
        self.assertEqual(learning["growth_gap_count"], 2)
        self.assertEqual(learning["growth_gap_state_counts"].get("no_change"), 1)
        self.assertEqual(learning["growth_gap_state_counts"].get("resource_gap_needs_triage"), 1)
        self.assertEqual(learning["growth_gap_open_count"], 1)
        open_item = learning["growth_gap_open_items"][0]
        self.assertEqual(open_item["recommended_next_action"], "decide_skill_or_knowledge_or_no_change")
        self.assertEqual(open_item["target_kind"], "none")

    def test_dashboard_learning_promotion_api_scans_and_reviews(self):
        space = PREFIX + "lessonpromoapi"
        make_space(space)
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="promotion_api",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "promotion_target": "knowledge",
                "instruction": "대시보드에서 성장 후보를 검토 가능하게 노출한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
            },
        )
        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)

        scan = client.post(f"/api/spaces/{space}/learning/promotions/scan", json={"actor": "대표", "limit": 20})
        self.assertEqual(scan.status_code, 200)
        promotion_id = scan.json()["candidates"][0]["promotion_id"]
        approve = client.post(
            f"/api/spaces/{space}/learning/promotions/{promotion_id}/approve",
            json={"actor": "대표", "reason": "검토 완료"},
        )
        status_response = client.get(f"/api/spaces/{space}/status")

        self.assertEqual(approve.status_code, 200)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["learning"]["promotion_approved_count"], 1)

    def test_approved_lesson_promotion_apply_creates_resource_and_is_idempotent(self):
        space = PREFIX + "lessonapply"
        make_space(space)
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="promotion_apply",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "promotion_target": "knowledge",
                "instruction": "승인된 레슨은 적용 단계에서 발견 가능한 지식으로 생성한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
            },
        )
        scan = lesson_ledger.generate_promotion_candidates(space, actor="대표")
        promotion_id = scan["candidates"][0]["promotion_id"]
        with self.assertRaises(lesson_ledger.LessonLedgerError):
            lesson_ledger.apply_promotion_candidate(space, promotion_id, actor="대표")
        lesson_ledger.review_promotion_candidate(space, promotion_id, decision="approve", actor="대표", reason="적용 승인")
        promotion_rows = lesson_ledger._rows(lesson_ledger._promotion_candidates_path(space))
        promotion = lesson_ledger._latest_by_id(promotion_rows, "promotion_id")[promotion_id]
        precomputed_target = lesson_ledger._target_resource_path(space, promotion)
        if precomputed_target.parent.exists():
            shutil.rmtree(precomputed_target.parent, ignore_errors=True)
        target_path = None
        try:
            applied = lesson_ledger.apply_promotion_candidate(space, promotion_id, actor="대표", reason="테스트 적용")
            duplicate = lesson_ledger.apply_promotion_candidate(space, promotion_id, actor="대표", reason="테스트 적용")
            app_row = applied["application"]
            target_path = ROOT / app_row["target_path"]
            content = target_path.read_text(encoding="utf-8")
            learning = room_manager.status(space)["learning"]

            self.assertTrue(applied["ok"])
            self.assertEqual(app_row["state"], "applied")
            self.assertTrue(target_path.exists())
            self.assertIn("name:", content)
            self.assertIn("description:", content)
            self.assertIn("source_promotion_id", content)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(learning["resource_apply_applied_count"], 1)
            self.assertEqual(learning["promotion_apply_pending_count"], 0)
            self.assertEqual(learning["promotion_items"][0]["apply_state"], "applied")
            self.assertEqual(learning["growth_gap_state_counts"].get("promotion_applied"), 1)
        finally:
            if target_path and target_path.exists():
                shutil.rmtree(target_path.parent, ignore_errors=True)

    def test_lesson_promotion_apply_blocks_existing_different_resource_file(self):
        space = PREFIX + "lessonapplyblock"
        make_space(space)
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="promotion_apply_block",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "promotion_target": "skill",
                "instruction": "기존 스킬 파일이 있으면 덮어쓰지 않고 적용 차단한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
            },
        )
        scan = lesson_ledger.generate_promotion_candidates(space, actor="대표")
        promotion_id = scan["candidates"][0]["promotion_id"]
        lesson_ledger.review_promotion_candidate(space, promotion_id, decision="approve", actor="대표", reason="차단 테스트")
        promotion_rows = lesson_ledger._rows(lesson_ledger._promotion_candidates_path(space))
        promotion = lesson_ledger._latest_by_id(promotion_rows, "promotion_id")[promotion_id]
        target_path = lesson_ledger._target_resource_path(space, promotion)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("existing different content", encoding="utf-8")
        try:
            result = lesson_ledger.apply_promotion_candidate(space, promotion_id, actor="대표", reason="차단 테스트")
            learning = room_manager.status(space)["learning"]

            self.assertFalse(result["ok"])
            self.assertEqual(result["application"]["state"], "blocked_path_exists")
            self.assertEqual(target_path.read_text(encoding="utf-8"), "existing different content")
            self.assertEqual(learning["resource_apply_blocked_count"], 1)
            self.assertEqual(learning["promotion_apply_blocked_count"], 1)
            self.assertEqual(learning["growth_gap_open_count"], 1)
            self.assertEqual(learning["growth_gap_state_counts"].get("resource_apply_blocked"), 1)
        finally:
            shutil.rmtree(target_path.parent, ignore_errors=True)

    def test_dashboard_learning_promotion_api_applies_approved_candidate(self):
        space = PREFIX + "lessonapplyapi"
        make_space(space)
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="promotion_apply_api",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "promotion_target": "knowledge",
                "instruction": "대시보드 API로 승인된 지식 후보를 적용한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
            },
        )
        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)
        target_path = None
        try:
            scan = client.post(f"/api/spaces/{space}/learning/promotions/scan", json={"actor": "대표", "limit": 20})
            promotion_id = scan.json()["candidates"][0]["promotion_id"]
            client.post(
                f"/api/spaces/{space}/learning/promotions/{promotion_id}/approve",
                json={"actor": "대표", "reason": "적용 승인"},
            )
            promotion_rows = lesson_ledger._rows(lesson_ledger._promotion_candidates_path(space))
            promotion = lesson_ledger._latest_by_id(promotion_rows, "promotion_id")[promotion_id]
            precomputed_target = lesson_ledger._target_resource_path(space, promotion)
            if precomputed_target.parent.exists():
                shutil.rmtree(precomputed_target.parent, ignore_errors=True)
            apply_response = client.post(
                f"/api/spaces/{space}/learning/promotions/{promotion_id}/apply",
                json={"actor": "대표", "reason": "API 적용"},
            )
            target_path = ROOT / apply_response.json()["application"]["target_path"]
            status_response = client.get(f"/api/spaces/{space}/status")

            self.assertEqual(apply_response.status_code, 200)
            self.assertTrue(apply_response.json()["ok"])
            self.assertEqual(status_response.json()["learning"]["resource_apply_applied_count"], 1)
        finally:
            if target_path and target_path.exists():
                shutil.rmtree(target_path.parent, ignore_errors=True)

    def test_status_marks_invalid_json_promotion_candidates_corrupt(self):
        space = PREFIX + "lessonpromobad"
        make_space(space)
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        (learning / "promotion_candidates.jsonl").write_text(
            '{"promotion_id":"ok","state":"pending_review"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)
        self.assertTrue(status["learning"]["ledger_corrupt"])
        self.assertIn("promotion_candidates.jsonl: invalid_json_lines=1", ";".join(status["learning"]["ledger_errors"]))
        self.assertTrue(any(row.get("상태") == "lesson_ledger_corrupt" for row in status["failures"]))

    def test_post_adds_intent_thread_generation_and_effect_id(self):
        space = PREFIX + "post"
        make_space(space)

        first = room_manager.post(space, "hello", run_manager=False, client_message_id="client-1")
        duplicate = room_manager.post(space, "hello retry", run_manager=False, client_message_id="client-1")
        rows = read(space)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["intent_id"].startswith("intent_"))
        self.assertTrue(row["conversation_thread_id"].startswith("thread_"))
        self.assertEqual(row["room_generation"], 1)
        self.assertTrue(row["effect_id"].startswith("effect_ingress_"))
        self.assertEqual(row["ingress_type"], "message")
        self.assertFalse(row["cancel_replan_fence"])
        self.assertFalse(first["ack"]["duplicate"])
        self.assertTrue(duplicate["ack"]["duplicate"])
        self.assertEqual(first["ack"]["message_id"], duplicate["ack"]["message_id"])
        self.assertEqual(first["ack"]["intent_id"], duplicate["ack"]["intent_id"])

    def test_chat_input_always_wakes_space_manager_before_agent_handoff(self):
        space = PREFIX + "managerfirst"
        member = PREFIX + "agent_0001"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine
        calls = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            calls.append(cwd.name)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "대표에게 짧게 답하세요.",
                    "reason": "공간관리자가 판단해 단일 멤버에게 턴을 넘김",
                }, ensure_ascii=False)
            return "안녕하세요. 바로 응답했습니다."

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.post(space, "하이", run_manager=True, client_message_id="client-manager-first")
        finally:
            room_manager.engine.run_engine = original_run_engine

        rows = read(space)
        assistants = [row for row in rows if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        manager_logs = [
            json.loads(line)
            for line in (SPACES / space / MANAGER_DIRNAME / "진행기록.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        self.assertTrue(result["ok"])
        self.assertEqual(calls, [MANAGER_DIRNAME, space])
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0]["내용"], "안녕하세요. 바로 응답했습니다.")
        self.assertEqual(status["last_action"], "pass")
        self.assertFalse(status["status_stale"])
        self.assertEqual(status["manager_read_lag"], 0)
        self.assertEqual(manager_logs[-1]["attempts"][0]["attempt"], 1)
        self.assertNotIn("fast_path", manager_logs[-1]["attempts"][0])

    def test_agent_running_has_chat_specific_stale_threshold(self):
        space = PREFIX + "chatstale"
        member = PREFIX + "agent_0001"
        make_space(space, [member])
        room_manager.post(space, "하이", run_manager=False, client_message_id="client-stale")
        context = orchestration.context_from_message(read(space)[-1], space)

        room_manager._write_state(
            space,
            "agent_running",
            current=member,
            target=member,
            status_updated_at=(datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds"),
            read_until_event_seq=1,
            **room_manager._context_fields(context),
        )
        waiting = room_manager.status(space)
        self.assertFalse(waiting["status_stale"])
        self.assertEqual(waiting["active_stale_threshold_ms"], room_manager.CHAT_AGENT_STALE_MS)

        room_manager._write_state(
            space,
            "agent_running",
            current=member,
            target=member,
            status_updated_at=(datetime.now() - timedelta(seconds=240)).isoformat(timespec="seconds"),
            read_until_event_seq=1,
            **room_manager._context_fields(context),
        )
        stale = room_manager.status(space)
        self.assertTrue(stale["status_stale"])

    def test_cancel_text_is_plain_message_until_space_manager_decides(self):
        space = PREFIX + "canceltext"
        make_space(space)

        room_manager.post(space, "hello", run_manager=False, client_message_id="client-1")
        result = room_manager.post(space, "취소", run_manager=False, client_message_id="client-2")
        rows = read(space)
        status = room_manager.status(space)

        self.assertEqual(rows[0]["room_generation"], 1)
        self.assertEqual(rows[1]["room_generation"], 1)
        self.assertEqual(rows[1]["ingress_type"], "message")
        self.assertFalse(rows[1]["cancel_replan_fence"])
        self.assertEqual(result["ack"]["room_generation"], 1)
        self.assertEqual(status["current_room_generation"], 1)
        self.assertEqual(status["learning"]["lesson_count"], 0)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 0)

    def test_concurrent_duplicate_cancel_text_stays_plain_message_once(self):
        space = PREFIX + "canceltextrace"
        make_space(space)
        original_record = room_manager.record
        barrier = threading.Barrier(8)

        def delayed_record(*args, **kwargs):
            barrier.wait(timeout=5)
            return original_record(*args, **kwargs)

        try:
            room_manager.record = delayed_record
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _i: room_manager.post(
                        space, "취소", run_manager=False, client_message_id="same-cancel"
                    ),
                    range(8),
                ))
        finally:
            room_manager.record = original_record

        rows = read(space)
        status = room_manager.status(space)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["room_generation"], 1)
        self.assertEqual(rows[0]["ingress_type"], "message")
        self.assertFalse(rows[0]["cancel_replan_fence"])
        self.assertEqual(status["current_room_generation"], 1)
        self.assertEqual(sum(1 for r in results if not r["ack"]["duplicate"]), 1)
        self.assertEqual(sum(1 for r in results if r["ack"]["duplicate"]), 7)
        self.assertEqual(status["learning"]["lesson_count"], 0)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 0)

    def test_agent_reply_effect_id_is_stable_across_claim_rollover(self):
        space = PREFIX + "stableeffect"
        member = PREFIX + "agent_b002"
        make_space(space, [member])
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        original_is_current = room_manager.manager_claim.is_current

        try:
            room_manager.engine.run_engine = lambda *args, **kwargs: "reply"
            room_manager.manager_claim.is_current = lambda *args, **kwargs: True
            room_manager._run_agent_turn(space, member, "do it", {
                "claim_token": "claim-a",
                "fencing_token": "fence-a",
                "owner_boot_id": "boot-a",
            }, context)
            room_manager._run_agent_turn(space, member, "do it", {
                "claim_token": "claim-b",
                "fencing_token": "fence-b",
                "owner_boot_id": "boot-b",
            }, context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.manager_claim.is_current = original_is_current

        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertEqual(len(assistant_rows), 1)
        row = assistant_rows[0]
        self.assertTrue(row["message_id"].startswith("msg_pub_"))
        self.assertTrue(row["effect_id"].startswith("effect_agent_reply_"))
        self.assertTrue(row["publish_ledger_claim"].startswith("pledger_"))
        self.assertEqual(row["manager_claim_token"], "claim-a")
        status = room_manager.status(space)
        self.assertGreaterEqual(status["publish_ledger"]["counts"].get("committed", 0), 1)
        self.assertGreaterEqual(status["context_packs"]["delivery_counts"].get("agent_wake", 0), 1)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("success"), 1)

    def test_manager_pass_delivers_context_pack_and_turn_handoff_prompt(self):
        space = PREFIX + "handoff"
        member = PREFIX + "agent_c003"
        make_space(space, [member])
        context_pack.append_north_star_goal(space, "이 방은 대표와 에이전트가 원활하게 협업한다.", source="test")
        post = room_manager.post(space, "하이", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        prompts = []
        running_statuses = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                self.assertIn("ContextPack.compat_minimal.v1", prompt)
                self.assertIn("space_memory", prompt)
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "인사에 짧게 답해줘",
                    "reason": "대표가 인사를 했다",
                }, ensure_ascii=False)
            running_statuses.append(room_manager.status(space))
            prompts.append(prompt)
            return "안녕하세요."

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("TurnHandoffBrief", prompts[0])
        self.assertIn("TurnHandoffPack.compat_minimal.v1", prompts[0])
        self.assertIn("ContextPack.compat_minimal.v1", prompts[0])
        self.assertIn("현재 맥락 projection", prompts[0])
        self.assertIn("space_memory_projection", prompts[0])
        self.assertIn("ChatAgentResult.v1", prompts[0])
        self.assertIn("request_work_route", prompts[0])
        self.assertTrue(running_statuses)
        running_active = next(w for w in running_statuses[0]["active_wakes"] if w.get("type") == "agent")
        self.assertEqual(running_active["actor"], member)
        self.assertTrue(running_active["context_pack_id"].startswith("ctx_"))
        self.assertTrue(running_active["wake_id"].startswith("wake_"))
        self.assertTrue(running_active["turn_handoff_id"].startswith("turn_"))
        self.assertTrue(running_active["wake_pack_manifest_id"].startswith("manifest_"))
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertEqual(len(assistant_rows), 1)
        row = assistant_rows[0]
        self.assertTrue(row["context_pack_id"].startswith("ctx_"))
        self.assertTrue(row["wake_id"].startswith("wake_"))
        self.assertTrue(row["turn_handoff_id"].startswith("turn_"))
        status = room_manager.status(space)
        self.assertGreaterEqual(status["context_packs"]["delivery_counts"].get("manager_tick", 0), 1)
        self.assertGreaterEqual(status["context_packs"]["delivery_counts"].get("agent_wake", 0), 1)
        self.assertEqual(status["context_packs"]["north_star_goal_count"], 1)
        self.assertEqual(status["context_packs"]["latest_memory_source"], "space_memory_projection")
        self.assertEqual(status["context_packs"]["latest_memory_projection_lag"], 0)
        self.assertEqual(status["space_memory"]["projection_available"], True)
        self.assertEqual(status["context_packs"]["turn_handoff_count"], 1)
        handoff = status["context_packs"]["latest_turn_handoff"]
        self.assertEqual(handoff["schema"], "TurnHandoffObservation.v1")
        self.assertEqual(handoff["target_agent"], member)
        self.assertEqual(handoff["delivery_type"], "agent_wake")
        self.assertTrue(handoff["manifest_id"].startswith("manifest_"))
        self.assertTrue(handoff["context_pack_id"].startswith("ctx_"))
        self.assertTrue(handoff["wake_id"].startswith("wake_"))
        self.assertTrue(handoff["turn_handoff_id"].startswith("turn_"))
        self.assertEqual(handoff["response_target"]["source_event_seq"], context["source_event_seq"])
        self.assertEqual(handoff["return_contract"]["structured_request_schema"], "ChatAgentResult.v1")
        self.assertEqual(handoff["return_contract"]["request_work_route"], "space_manager_task_registry")
        self.assertIn("인사에 짧게 답해줘", handoff["manager_message_preview"])
        self.assertIn("대표가 인사를 했다", handoff["why_you"])
        self.assertIn("TurnHandoffBrief", handoff["turn_handoff_brief_preview"])
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("success"), 1)

    def test_context_pack_carries_space_memory_projection(self):
        space = PREFIX + "memoryproj"
        member = PREFIX + "agent_mem1"
        make_space(space, [member])
        (SPACES / space / "요약.md").write_text("legacy summary hint", encoding="utf-8")
        first = room_manager.post(space, "이 방에서는 원활한 협업이 중요해", run_manager=False, client_message_id="client-mem-1")
        second = room_manager.post(space, "최근 요청을 기준으로 답해줘", run_manager=False, client_message_id="client-mem-2")
        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="memory projection test",
            context=second["orchestration"],
            target_agent=member,
        )

        projection = pack["space_memory_projection"]
        self.assertEqual(pack["memory_source"], "space_memory_projection")
        self.assertTrue(pack["memory_projection_id"].startswith("memproj_"))
        self.assertEqual(pack["memory_applied_event_seq"], second["ack"]["event_seq"])
        self.assertEqual(projection["applied_event_seq"], second["ack"]["event_seq"])
        self.assertEqual(projection["projection_lag"], 0)
        self.assertEqual(projection["source"], "event_log_deterministic_v1")
        self.assertEqual(projection["projection_method"]["kind"], "bounded_event_projection_v1")
        self.assertIn("최근 요청을 기준으로 답해줘", projection["active_context_summary"])
        self.assertTrue(any(item["event_seq"] == first["ack"]["event_seq"] for item in projection["representative_requests"]))
        self.assertTrue(any(item["event_seq"] == second["ack"]["event_seq"] for item in projection["user_directive_items"]))
        self.assertTrue(projection["active_topic_threads"])
        self.assertIn("precedence_policy", projection)
        self.assertEqual(projection["precedence_policy"]["semantic_conflict_detection"], "not_performed_by_deterministic_projection")
        self.assertFalse(projection["conflict_hints"]["semantic_conflicts_detected"])
        self.assertTrue(any(ref["message_id"] == second["ack"]["message_id"] for ref in projection["source_refs"]))
        self.assertEqual(space_memory.snapshot(space)["projection_available"], True)
        brief = context_pack.turn_handoff_brief(pack, member, "맥락 확인", "projection v1 검증")
        self.assertIn("대표 지시 누적", brief)
        self.assertIn("현재 방향 종합", brief)
        self.assertIn("주제 상태", brief)
        self.assertIn("최근 요청을 기준으로 답해줘", brief)

    def test_space_memory_projection_tracks_dormant_threads_without_semantic_conflict_guessing(self):
        space = PREFIX + "memorytopics"
        member = PREFIX + "agent_mem2"
        make_space(space, [member])
        posts = []
        for idx in range(12):
            posts.append(room_manager.post(space, f"오래된 주제 {idx}", run_manager=False, client_message_id=f"client-topic-old-{idx}"))
        latest = room_manager.post(space, "최신 주제는 별도로 진행해줘", run_manager=False, client_message_id="client-topic-latest")
        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="memory projection topic test",
            context=latest["orchestration"],
            target_agent=member,
        )

        projection = pack["space_memory_projection"]
        self.assertTrue(projection["active_topic_threads"])
        self.assertTrue(projection["dormant_topic_threads"])
        self.assertLessEqual(len(projection["user_directive_items"]), space_memory.MAX_USER_DIRECTIVES)
        self.assertEqual(projection["precedence_policy"]["clock"], "event_seq")
        self.assertEqual(projection["conflict_hints"]["candidate_count"], 0)
        self.assertTrue(any(item["event_seq"] == latest["ack"]["event_seq"] for item in projection["user_directive_items"]))
        prompt_snapshot = room_manager._prompt_room_status_snapshot(space)
        self.assertTrue(prompt_snapshot["space_memory"]["user_directive_items"])
        self.assertTrue(prompt_snapshot["space_memory"]["active_topic_threads"])
        self.assertLessEqual(len(projection["topic_threads"]), space_memory.MAX_TOPIC_THREADS)
        self.assertTrue(all(len(item.get("recent_items", [])) <= space_memory.MAX_THREAD_ITEMS for item in projection["topic_threads"]))
        self.assertTrue(all(len(item.get("content_preview", "")) <= space_memory.MAX_TEXT_CHARS + 3 for item in projection["user_directive_items"]))

    def test_space_memory_rebuilds_latest_v0_projection_to_v1(self):
        space = PREFIX + "memorymigrate"
        member = PREFIX + "agent_mem3"
        make_space(space, [member])
        latest = room_manager.post(space, "v0 projection이 있어도 v1으로 재구성해줘", run_manager=False, client_message_id="client-migrate")
        path = space_memory.projection_path(space)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": space_memory.PROJECTION_SCHEMA,
            "space_id": space,
            "projection_id": "memproj_old",
            "version": 9,
            "state": "clean",
            "source": "event_log_deterministic_v0",
            "applied_event_seq": latest["ack"]["event_seq"],
            "applied_message_count": 1,
            "active_context_summary": "old",
            "active_context": [],
            "representative_requests": [],
            "source_refs": [],
            "projection_method": {"kind": "bounded_event_projection_v0"},
        }, ensure_ascii=False), encoding="utf-8")

        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="memory projection migration test",
            context=latest["orchestration"],
            target_agent=member,
        )

        projection = pack["space_memory_projection"]
        self.assertEqual(projection["source"], "event_log_deterministic_v1")
        self.assertEqual(projection["projection_method"]["kind"], "bounded_event_projection_v1")
        self.assertEqual(projection["projection_version"], 10)
        self.assertTrue(projection["user_directive_items"])

    def test_context_pack_memory_fallback_keeps_v1_defaults(self):
        space = PREFIX + "memoryfallback"
        member = PREFIX + "agent_mem4"
        make_space(space, [member])
        post = room_manager.post(space, "projection fallback도 같은 키를 유지해야 해", run_manager=False, client_message_id="client-mem-fallback")
        original_ensure = context_pack.space_memory.ensure_projection

        def broken_projection(_space):
            raise RuntimeError("forced projection failure")

        try:
            context_pack.space_memory.ensure_projection = broken_projection
            pack = context_pack.build_context_pack(
                space,
                mode="chat",
                event="memory projection fallback test",
                context=post["orchestration"],
                target_agent=member,
            )
        finally:
            context_pack.space_memory.ensure_projection = original_ensure

        projection = pack["space_memory_projection"]
        self.assertEqual(pack["memory_source"], "legacy_summary")
        self.assertFalse(projection["projection_available"])
        self.assertTrue(projection["projection_corrupt"])
        self.assertEqual(projection["user_directive_items"], [])
        self.assertEqual(projection["topic_threads"], [])
        self.assertEqual(projection["active_topic_threads"], [])
        self.assertEqual(projection["dormant_topic_threads"], [])
        self.assertEqual(projection["precedence_policy"], {})
        self.assertEqual(projection["conflict_hints"], {})

    def test_space_memory_does_not_semantically_resolve_conflicting_user_messages(self):
        space = PREFIX + "memoryconflict"
        member = PREFIX + "agent_mem5"
        make_space(space, [member])
        first = room_manager.post(space, "A 방식으로 진행해줘", run_manager=False, client_message_id="client-conflict-1")
        second = room_manager.post(space, "A 방식으로 진행하지 마", run_manager=False, client_message_id="client-conflict-2")
        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="memory projection conflict test",
            context=second["orchestration"],
            target_agent=member,
        )

        projection = pack["space_memory_projection"]
        self.assertTrue(any(item["event_seq"] == first["ack"]["event_seq"] for item in projection["user_directive_items"]))
        self.assertTrue(any(item["event_seq"] == second["ack"]["event_seq"] for item in projection["user_directive_items"]))
        self.assertFalse(projection["conflict_hints"]["semantic_conflicts_detected"])
        self.assertEqual(projection["conflict_hints"]["candidate_count"], 0)
        self.assertEqual(projection["conflict_hints"]["items"], [])
        self.assertIn("confirmed_conflicts", projection["precedence_policy"]["rule"])

    def test_context_pack_current_user_request_uses_source_context_not_latest_message(self):
        space = PREFIX + "memorycurrent"
        member = PREFIX + "agent_mem6"
        make_space(space, [member])
        first = room_manager.post(space, "첫 번째 요청에 답해야 해", run_manager=False, client_message_id="client-current-1")
        second = room_manager.post(space, "두 번째 최신 요청은 별도야", run_manager=False, client_message_id="client-current-2")
        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="memory current request isolation test",
            context=first["orchestration"],
            target_agent=member,
        )

        self.assertEqual(pack["current_user_request"]["message_id"], first["ack"]["message_id"])
        self.assertIn("첫 번째 요청", pack["current_user_request"]["content"])
        self.assertNotIn("두 번째 최신 요청", pack["current_user_request"]["content"])
        self.assertIn("두 번째 최신 요청", pack["active_context_summary"])
        self.assertTrue(any(item["event_seq"] == second["ack"]["event_seq"] for item in pack["space_memory_projection"]["user_directive_items"]))

    def test_response_obligation_opens_assigns_and_answers_agent_reply(self):
        space = PREFIX + "obligation"
        member = PREFIX + "agent_ob01"
        make_space(space, [member])
        post = room_manager.post(
            space,
            "이 요청에 답해줘",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-obligation",
        )
        opened = response_obligation.snapshot(space)
        self.assertEqual(opened["open_count"], 1)
        self.assertEqual(opened["state_counts"].get("open"), 1)
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "대표 요청에 답해줘",
                    "reason": "대표가 답변을 요구했다",
                }, ensure_ascii=False)
            return "요청에 대한 답변입니다."

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        status = room_manager.status(space)
        obligations = status["response_obligations"]
        self.assertEqual(obligations["open_count"], 0)
        self.assertEqual(obligations["state_counts"].get("answered"), 1)
        latest = obligations["latest"][-1]
        self.assertEqual(latest["state"], "answered")
        self.assertEqual(latest["responder"], member)
        self.assertTrue(latest["published_message_id"].startswith("msg_pub_"))
        flow_obligation = next(item for item in status["chat_flow"]["phases"] if item["key"] == "obligation")
        self.assertEqual(flow_obligation["state"], "done")

    def test_response_obligation_aging_marks_overdue_without_auto_closing(self):
        space = PREFIX + "oblage"
        make_space(space)
        post = room_manager.post(
            space,
            "오래 열린 요청",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-oblage",
        )
        path = SPACES / space / "response_obligations.jsonl"
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        row["created_at"] = "2000-01-01T00:00:00"
        row["updated_at"] = "2000-01-01T00:00:00"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

        status = room_manager.status(space)
        obligations = status["response_obligations"]
        self.assertEqual(obligations["open_count"], 1)
        self.assertEqual(obligations["overdue_open_count"], 1)
        self.assertEqual(obligations["state_counts"].get("open"), 1)
        self.assertEqual(obligations["auto_closed_count"], 0)
        open_item = obligations["open_items"][0]
        self.assertTrue(open_item["overdue"])
        self.assertEqual(open_item["auto_policy"], "observe_only")
        self.assertGreater(open_item["age_ms"], open_item["timeout_threshold_ms"])
        prompt = room_manager._prompt_room_status_snapshot(space)
        self.assertEqual(prompt["response_obligations"]["overdue_open_count"], 1)

    def test_delegated_response_obligation_is_protected_from_generic_overdue(self):
        space = PREFIX + "obldelegated"
        member = PREFIX + "agent_obld"
        make_space(space, [member])
        post = room_manager.post(
            space,
            "작업으로 위임된 요청",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-obldelegated",
        )
        context = post["orchestration"]
        response_obligation.delegate_to_task(
            space,
            context,
            task_id="task-obldelegated",
            worker_agent=member,
            reason="테스트 위임",
        )
        rows = [json.loads(line) for line in (SPACES / space / "response_obligations.jsonl").read_text(encoding="utf-8").splitlines()]
        rows[-1]["created_at"] = "2000-01-01T00:00:00"
        rows[-1]["updated_at"] = "2000-01-01T00:00:00"
        (SPACES / space / "response_obligations.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        obligations = room_manager.status(space)["response_obligations"]
        self.assertEqual(obligations["open_count"], 1)
        self.assertEqual(obligations["overdue_open_count"], 0)
        item = obligations["open_items"][0]
        self.assertEqual(item["state"], "delegated")
        self.assertIn("delegated_task_or_release", item["policy_blockers"])
        self.assertEqual(item["policy_reason"], "delegated_obligation_requires_task_release_state")

    def test_auto_continue_chains_agent_turns_with_hard_cap_and_off_by_default(self):
        # 자동 연속: auto_continue=True면 매니저 pass 후 대표 입력 없이도 다음 턴이 이어지고,
        # 무한루프 없이 AUTO_CONTINUE_MAX_TURNS에서 멈춘다(런어웨이 방지). 기본(off)은 1턴만.
        space = PREFIX + "autocont"
        member = PREFIX + "agent_ac01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                # 매니저는 항상 같은 멤버에게 pass (연속 유도)
                return json.dumps({
                    "action": "pass", "wake": member,
                    "message": "이어서 진행해줘", "reason": "협업 계속",
                }, ensure_ascii=False)
            # 멤버는 평범한 공개 답변(말풍선) — request_work 아님
            return "계속 진행 중입니다."

        # off (기본): 1턴만
        post1 = room_manager.post(space, "시작", run_manager=False, client_message_id="c1")
        try:
            room_manager.engine.run_engine = fake_run_engine
            res_off = room_manager.tick(space, "이벤트", post1["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine
        self.assertEqual(res_off.get("auto_continue_turns", 0), 0)

        # on: 캡까지 연속하고 멈춘다(무한루프 아님)
        post2 = room_manager.post(space, "다시 시작", run_manager=False, client_message_id="c2")
        try:
            room_manager.engine.run_engine = fake_run_engine
            res_on = room_manager.tick(space, "이벤트2", post2["orchestration"], auto_continue=True)
        finally:
            room_manager.engine.run_engine = original_run_engine
        self.assertEqual(res_on.get("auto_continue_turns"), room_manager.AUTO_CONTINUE_MAX_TURNS)
        auto_events = [e for e in res_on.get("events", []) if e.get("type") == "manager_auto_continue"]
        self.assertEqual(len(auto_events), room_manager.AUTO_CONTINUE_MAX_TURNS)

    def test_auto_continue_yields_to_new_representative_input(self):
        # 자동 연속 도중 대표가 새 요청을 끼워넣으면, 자동 연속을 멈추고 그 입력에 양보한다.
        space = PREFIX + "autoyield"
        member = PREFIX + "agent_ay01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine

        member_calls = {"n": 0}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({"action": "pass", "wake": member, "message": "이어서", "reason": "x"}, ensure_ascii=False)
            member_calls["n"] += 1
            # 자동 연속 도중(2번째 멤버 턴)에 대표가 새 요청을 끼워넣는다(manager 처리 요청)
            if member_calls["n"] == 2:
                room_manager.post(space, "잠깐, 이거 먼저 해줘", run_manager=True, client_message_id="mid-1")
            return "진행 중입니다."

        post = room_manager.post(space, "쭉 진행해", run_manager=False, client_message_id="c1")
        try:
            room_manager.engine.run_engine = fake_run_engine
            res = room_manager.tick(space, "이벤트", post["orchestration"], auto_continue=True)
        finally:
            room_manager.engine.run_engine = original_run_engine
        # 캡(6)까지 가지 않고 새 입력에 양보해 일찍 멈춘다
        self.assertLess(res.get("auto_continue_turns", 0), room_manager.AUTO_CONTINUE_MAX_TURNS)
        self.assertTrue(any(e.get("type") == "manager_auto_continue_yielded" for e in res.get("events", [])))

    def test_launch_app_is_precondition_not_terminal_answer(self):
        # 회귀(라이브 스트랜드): 대표의 복합요청("revit을 실행해서 거기서 작업하려고 팀을 구성하자")에서
        # 매니저가 launch_app만 하고 응답의무를 answered로 '조기 종결'해 방이 idle+answered로 스트랜드됐다
        # (팀 구성·방지침 방치 — idle 방은 어떤 백스톱도 안 잡는다). 수정: launch_app은 '작업의 전제'라
        # 응답의무를 종결하지 않고, 성공 시 auto-continue로 본 턴을 이어 남은 요청을 처리한다.
        space = PREFIX + "launchcont"
        member = PREFIX + "agent_lc01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine
        fake_app = {"name": "TestApp", "dir": "앱/test/testapp"}

        # A) launch 단독(auto_continue 기본 off)은 응답의무를 answered로 닫지 않는다(조기 종결 금지)
        def fake_launch_only(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps({"action": "launch_app", "app": "TestApp",
                                   "wake": "", "message": "", "reason": "실행 후 팀 구성"}, ensure_ascii=False)
            return "ok"

        post_a = room_manager.post(space, "TestApp 실행해서 거기서 작업하게 팀 구성해줘",
                                   run_manager=False, manager_requested=True, client_message_id="lc-a")
        with patch.object(room_manager, "_resolve_app", return_value=fake_app), \
             patch("core.apps.run_app", return_value={"running": True, "pid": 4242}):
            try:
                room_manager.engine.run_engine = fake_launch_only
                res_a = room_manager.tick(space, "이벤트A", post_a["orchestration"])  # auto_continue off
            finally:
                room_manager.engine.run_engine = original_run_engine
        self.assertTrue(any(e.get("type") == "app_launched" and e.get("ok") for e in res_a.get("events", [])))
        obl_a = response_obligation.snapshot(space)
        self.assertEqual(obl_a["open_count"], 1)                       # 여전히 열림 → 스트랜드 아님
        self.assertIsNone(obl_a["state_counts"].get("answered"))       # launch로 answered 금지

        # B) launch 성공은 auto-continue를 켠다. 단순 실행이면 이어진 턴의 stop이 의무를 닫는다(회귀안전)
        space_b = PREFIX + "launchpure"
        make_space(space_b, [member + "b"])
        manager_calls = {"n": 0}

        def fake_launch_then_stop(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                manager_calls["n"] += 1
                if manager_calls["n"] == 1:
                    return json.dumps({"action": "launch_app", "app": "TestApp",
                                       "wake": "", "message": "", "reason": "실행"}, ensure_ascii=False)
                return json.dumps({"action": "stop", "wake": "", "message": "",
                                   "reason": "실행이 요청의 전부"}, ensure_ascii=False)
            return "ok"

        post_b = room_manager.post(space_b, "TestApp 켜줘",
                                   run_manager=False, manager_requested=True, client_message_id="lc-b")
        with patch.object(room_manager, "_resolve_app", return_value=fake_app), \
             patch("core.apps.run_app", return_value={"running": True, "pid": 4243}):
            try:
                room_manager.engine.run_engine = fake_launch_then_stop
                res_b = room_manager.tick(space_b, "이벤트B", post_b["orchestration"], auto_continue=True)
            finally:
                room_manager.engine.run_engine = original_run_engine
        self.assertGreaterEqual(res_b.get("auto_continue_turns", 0), 1)  # 실행 후 이어짐
        self.assertGreaterEqual(manager_calls["n"], 2)                   # 매니저가 한 번 더 판단
        obl_b = response_obligation.snapshot(space_b)
        self.assertEqual(obl_b["open_count"], 0)                         # 이어진 stop이 의무를 닫음

    def test_app_detect_running_instances_and_no_duplicate_launch(self):
        # 회귀(라이브): pidfile만 보던 실행감지가 대시보드 밖에서 켜진 인스턴스를 놓쳐 launch_app이
        # 중복 Revit을 띄웠고 → localhost:8600 브리지 바인딩 충돌(레빗_bcd7). 수정: 프로세스 시그니처로
        # target에서 실제 실행 중 인스턴스(+PID)를 감지 → 중복 실행 방지 + PID surface(에이전트 타깃팅).
        from core import apps as core_apps
        from unittest.mock import patch
        rel = "앱/대외비/원격레빗"   # process:Revit, target:desktop-frvh9d8(cu-helper)
        fake = {"ok": True, "name": "Revit", "procs": [
            {"pid": 21072, "title": "Autodesk Revit 2026.4 - [프로젝트1]", "start": "2026-06-29T21:50:44"},
            {"pid": 20752, "title": "", "start": "2026-07-01T11:47:05"}]}
        # /proclist가 두 인스턴스를 반환한다고 가정(VM 헬퍼 배포 후 상황)
        with patch.object(core_apps, "_http_json", return_value=fake):
            det = core_apps.detect_running_instances(rel, fresh=True)
            self.assertTrue(det["detected"])
            self.assertEqual([i["pid"] for i in det["instances"]], [21072, 20752])
            # run_app 중복가드: pidfile 없어도 외부 인스턴스 감지 → 안 띄우고 PID 보고
            with patch.object(core_apps, "_running_state", return_value={"running": False, "pid": None, "port": None}):
                res = core_apps.run_app(rel)
                self.assertTrue(res["already"])
                self.assertTrue(res["detected_external"])
                self.assertEqual(res["pid"], 21072)
                self.assertEqual(res["instance_count"], 2)
        # 헬퍼 미응답(옛 helper·채널 다운) → graceful: 빈 목록, run_app은 정상 진행 경로(오차단 없음)
        with patch.object(core_apps, "_http_json", side_effect=Exception("no /proclist")):
            det2 = core_apps.detect_running_instances(rel, fresh=True)
            self.assertFalse(det2["detected"])
            self.assertEqual(det2["instances"], [])

    def test_app_process_signature_launcher_and_webapp_guard(self):
        # 회귀(라이브): 메모장-맥(run='open -a TextEdit') 종료가 안 됐다. 시그니처를 런처 'open'으로
        # 잘못 뽑아 pgrep -f가 OpenGL(CVMServer)·opendirectoryd 같은 무관 시스템 프로세스를 오탐 →
        # 엉뚱한 걸 죽이려다 실패. 또 web-app(python3 http.server)은 시그니처 'python3'로 잡으면
        # 대시보드 서버 등 무관 python 프로세스를 오종료할 위험 → 웹앱·범용 인터프리터는 감지 제외.
        from core import apps as core_apps
        # 1) macOS 런처 open -a X → 실제 앱명(런처 'open' 아님)
        self.assertEqual(core_apps._process_signature({"run": "open -a TextEdit"}), "TextEdit")
        self.assertEqual(core_apps._process_signature({"run": "open -a 'Google Chrome'"}), "Google Chrome")
        self.assertEqual(core_apps._process_signature({"run": "\"C:\\x\\Revit.exe\""}), "Revit")
        self.assertEqual(core_apps._process_signature({"process": "Foo", "run": "open -a Bar"}), "Foo")  # explicit 우선
        self.assertIn("python3", core_apps._GENERIC_PROC_SIGNATURES)
        self.assertIn("open", core_apps._GENERIC_PROC_SIGNATURES)
        # 2) 웹앱은 프로세스명 감지 제외(빠른메모장=python3 http.server) — 어떤 프로세스 조회도 하지 않음
        d = core_apps.detect_running_instances("앱/추가/빠른메모장", fresh=True)
        self.assertFalse(d["detected"])
        self.assertEqual(d.get("reason"), "web_app_uses_pidfile")

    def test_computer_use_task_gets_elevated_scope_and_caps(self):
        # 회귀(라이브): 원격 컴퓨터유즈 작업(레빗 mcpbridge 경고창 진단)이 보수 기본 scope
        # (network:none·external_side_effects:forbidden·vision:false)로 나가, 에이전트가 '내 scope 밖'
        # 이라 정직하게 BLOCKED로 멈췄다(레빗_bcd7 329e/240a). 러너는 --dangerously-skip-permissions라
        # 샌드박스를 하드강제하지 않으므로, CU 작업만 scope/능력을 '인가'로 상향하면 에이전트가 대시보드
        # /api/cu로 실제 화면을 보고 조작할 수 있다. 비-CU 작업은 보수 기본 그대로(무회귀)여야 한다.
        space = PREFIX + "cutask"
        worker = PREFIX + "cu_agent01"
        make_space(space, [worker])
        seat = PEOPLE / worker / "공간" / space
        seat.mkdir(parents=True, exist_ok=True)
        rt = {"engine": "claude", "model": "claude-opus-4-8"}

        # A) 원격 CU 작업(등록 타깃 desktop-frvh9d8 + CU 마커) → 인가 scope + 상향 능력 + how-to 블록
        cu_dir = seat / "작업" / "cu01"
        cu = room_manager.task_registry.create_task(
            space, worker=worker, task_id="cu01",
            objective="원격 Windows Revit(desktop-frvh9d8)의 mcpbridge 경고창을 화면 캡처해 진단·해소한다",
            work_dir=cu_dir, runtime_info=rt,
        )
        pack = cu["task_pack"]
        self.assertIn("computer_use", pack["scope"]["network_policy"])
        self.assertIn("computer_use_allowed", pack["scope"]["external_side_effects"])
        self.assertTrue(pack["scope"]["execute_paths"])            # cu 도구 경로 인가
        self.assertIn("computer_use", pack)                        # 에이전트용 how-to
        self.assertEqual(pack["computer_use"]["target"], "desktop-frvh9d8")
        caps = json.loads((cu_dir / "runtime_capabilities.json").read_text(encoding="utf-8"))
        self.assertTrue(caps["supports_network"])
        self.assertTrue(caps["supports_image_inspection"])

        # B) 일반(비-CU) 작업 — 새 계약(2026-07-02 능력 봉인 해제): 능력은 엔진 실제 프로필
        #    (claude=셸·네트워크·서브에이전트 가능), scope는 리서치 허용 + 외부 발신만 금지,
        #    CU how-to 블록은 여전히 CU 작업에만 붙는다(오탐 없음).
        n_dir = seat / "작업" / "n01"
        normal = room_manager.task_registry.create_task(
            space, worker=worker, task_id="n01",
            objective="철근 배근 스킬 케이스를 정리하고 문서를 갱신한다",
            work_dir=n_dir, runtime_info=rt,
        )
        npack = normal["task_pack"]
        self.assertIn("research_allowed", npack["scope"]["network_policy"])
        self.assertIn("forbidden", npack["scope"]["external_side_effects"])   # 외부 발신 금지는 유지(결재 몫)
        self.assertEqual(npack["scope"]["write_paths"], [str(n_dir.relative_to(task_registry.ROOT).as_posix())])  # 쓰기 격리 유지
        self.assertNotIn("computer_use", npack)
        ncaps = json.loads((n_dir / "runtime_capabilities.json").read_text(encoding="utf-8"))
        self.assertTrue(ncaps["supports_network"])            # claude 엔진 실제 능력 그대로 선언
        self.assertTrue(ncaps["supports_image_inspection"])
        self.assertTrue(ncaps["supports_native_subagents"])
        # 미지 엔진은 보수 폴백(능력을 모르면 부풀리지 않는다)
        u_dir = seat / "작업" / "u01"
        unknown = room_manager.task_registry.create_task(
            space, worker=worker, task_id="u01",
            objective="미지 엔진 작업", work_dir=u_dir,
            runtime_info={"engine": "unknown-engine", "model": "x"},
        )
        ucaps = json.loads((u_dir / "runtime_capabilities.json").read_text(encoding="utf-8"))
        self.assertFalse(ucaps["supports_network"])
        self.assertFalse(ucaps["supports_shell"])

    def test_rapid_inputs_not_dropped_obligation_sweep(self):
        # 회귀(라이브): 빠르게 연속으로 온 대표 메시지 2·3이 누락됐다. 매니저가 첫 입력만 처리하고
        # 멈추면(stop), 빠르게 온 나머지는 read_until만 전진한 채 'open 응답의무'로 남아 답을 못 받는다.
        # 종료 전 obligation sweep이 미응답 입력을 오래된 순으로 하나씩 구동해 모두 닫아야 한다.
        space = PREFIX + "rapiddrop"
        member = PREFIX + "agent_rd01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                # 자동 연속 턴에선 stop → 첫 입력만 처리하고 멈춘 '드롭' 상황을 재현.
                # 그 외(최초 이벤트 + 누락방지 sweep)는 담당 멤버에게 pass해 그 입력을 답하게 한다.
                if "자동 연속" in prompt:
                    return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "일단 멈춤"}, ensure_ascii=False)
                return json.dumps({"action": "pass", "wake": member, "message": "답해줘", "reason": "응답"}, ensure_ascii=False)
            return "네, 답변드립니다."

        # 빠른 연속 3건 — 각자 응답의무를 연다(manager_requested=True). 실제 tick은 첫 입력 것 하나만 돌린다.
        p1 = room_manager.post(space, "채널 이름 후보 3개", run_manager=False, manager_requested=True, client_message_id="rapid-0")
        room_manager.post(space, "썸네일 톤은 어떤 색?", run_manager=False, manager_requested=True, client_message_id="rapid-1")
        room_manager.post(space, "구독 유도 멘트 한 줄", run_manager=False, manager_requested=True, client_message_id="rapid-2")
        # 처리 전: 3건 모두 open
        before = response_obligation.snapshot(space)
        self.assertEqual(before["state_counts"].get("open", 0), 3)

        try:
            room_manager.engine.run_engine = fake_run_engine
            res = room_manager.tick(space, "첫 입력", p1["orchestration"], auto_continue=True)
        finally:
            room_manager.engine.run_engine = original_run_engine

        # sweep이 미응답 입력 2건(2·3)을 하나씩 구동해 닫았다 — 누락 0.
        sweep_events = [e for e in res.get("events", []) if e.get("type") == "manager_obligation_sweep"]
        self.assertEqual(len(sweep_events), 2, res.get("events"))
        after = response_obligation.snapshot(space)
        self.assertEqual(after["state_counts"].get("open", 0), 0)
        self.assertEqual(after["state_counts"].get("answered", 0), 3)

    def test_obligation_sweep_off_when_auto_continue_disabled(self):
        # 무회귀: auto_continue=False(기본·테스트·수동 tick)면 sweep은 돌지 않는다(한 tick=한 턴 계약 유지).
        space = PREFIX + "nosweep"
        member = PREFIX + "agent_ns01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "멈춤"}, ensure_ascii=False)
            return "ok"

        p1 = room_manager.post(space, "메시지 A", run_manager=False, manager_requested=True, client_message_id="na-0")
        room_manager.post(space, "메시지 B", run_manager=False, manager_requested=True, client_message_id="na-1")
        try:
            room_manager.engine.run_engine = fake_run_engine
            res = room_manager.tick(space, "이벤트", p1["orchestration"])  # auto_continue 기본 False
        finally:
            room_manager.engine.run_engine = original_run_engine
        self.assertEqual([e for e in res.get("events", []) if e.get("type") == "manager_obligation_sweep"], [])
        self.assertNotIn("obligation_sweeps", res)

    def test_recover_space_redrives_stalled_manager_queued_state(self):
        # 서버 재시작 등으로 manager_queued(고아) 상태에 멈춘 공간을 recover_space가 재구동한다.
        space = PREFIX + "recoverstall"
        member = PREFIX + "agent_rs01"
        make_space(space, [member])
        # 인위적으로 manager_queued(redrive 대기) 상태를 만든다(고아 시뮬레이션)
        room_manager._write_state(
            space, "manager_queued", actor="공간관리", label="새 입력 재처리 대기",
            manager_redrive_required=True,
        )
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "복구 후 멈춤"}, ensure_ascii=False)
            return "ok"

        try:
            room_manager.engine.run_engine = fake_run_engine
            res = room_manager.recover_space(space)
        finally:
            room_manager.engine.run_engine = original_run_engine
        self.assertTrue(res.get("recovered"))
        self.assertEqual(res.get("prior_state"), "manager_queued")
        # 복구 후 매니저가 판단을 마쳐 idle로 정착
        state = json.loads((SPACES / space / "관리자" / "상태.json").read_text(encoding="utf-8"))
        self.assertEqual(state.get("상태"), "idle")

    def test_recover_space_skips_idle_state(self):
        # 정상(idle) 공간은 복구 대상이 아니다 — 불필요한 재구동 안 함.
        space = PREFIX + "recoveridle"
        member = PREFIX + "agent_ri01"
        make_space(space, [member])
        room_manager._write_state(space, "idle", actor="공간관리", label="대기")
        res = room_manager.recover_space(space)
        self.assertFalse(res.get("recovered"))

    def test_auto_continue_handback_marks_representative_when_manager_stops(self):
        # 자동 연속이 매니저 stop으로 끝나면 대표 핸드백 마커가 설정되고, 대표 재발언 시 해제된다.
        space = PREFIX + "autohandback"
        member = PREFIX + "agent_ah01"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine
        calls = {"manager": 0}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                calls["manager"] += 1
                if calls["manager"] == 1:
                    return json.dumps({"action": "pass", "wake": member, "message": "한 번만 답해줘", "reason": "x"}, ensure_ascii=False)
                return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "대표 승인 필요"}, ensure_ascii=False)
            return "검토 결과 보고합니다."

        post = room_manager.post(space, "시작", run_manager=False, client_message_id="c1")
        try:
            room_manager.engine.run_engine = fake_run_engine
            res = room_manager.tick(space, "이벤트", post["orchestration"], auto_continue=True)
        finally:
            room_manager.engine.run_engine = original_run_engine
        self.assertTrue(any(e.get("type") == "manager_handback_to_representative" for e in res.get("events", [])))
        marker = room_manager.read_representative_handback(space)
        self.assertTrue(marker.get("needs_representative"))
        # 대표가 다시 발언하면 해제
        room_manager.post(space, "확인했어 계속해", run_manager=False, client_message_id="c2")
        cleared = room_manager.read_representative_handback(space)
        self.assertFalse(cleared.get("needs_representative"))

    def test_manager_parallel_pass_collects_candidates_without_public_or_task_side_effects(self):
        space = PREFIX + "parallelcand"
        member_a = PREFIX + "agent_pa01"
        member_b = PREFIX + "agent_pb02"
        make_space(space, [member_a, member_b])
        post = room_manager.post(space, "두 관점으로 검토해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "parallel_pass",
                    "wake": "",
                    "message": "",
                    "reason": "서로 다른 관점이 필요하다",
                    "targets": [
                        {"wake": member_a, "message": "찬성 관점 후보를 만들어줘", "reason": "A 관점"},
                        {"wake": member_b, "message": "위험 관점 후보를 만들어줘", "reason": "B 관점"},
                    ],
                    "join_policy": "timeout_then_partial",
                    "presentation_mode": "silent_reference",
                }, ensure_ascii=False)
            self.assertIn("병렬 후보 응답 규칙", prompt)
            return f"{cwd.parent.parent.name} 후보 의견"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        candidate_snapshot = candidate_queue.snapshot(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertTrue(result["ok"])
        self.assertEqual(len([e for e in result["events"] if e.get("type") == "parallel_candidate"]), 2)
        self.assertEqual(assistant_rows, [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 2)
        self.assertEqual(candidate_snapshot["pending_count"], 2)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["tasks"]["task_count"], 0)
        self.assertEqual(status["release_queue"]["release_count"], 0)
        self.assertEqual(status["context_packs"]["delivery_counts"].get("manager_tick"), 1)
        self.assertEqual(status["context_packs"]["delivery_counts"].get("parallel_candidate_wake"), 2)
        self.assertNotIn("agent_wake", status["context_packs"]["delivery_counts"])
        self.assertEqual(status["last_action"], "parallel_pass")

    def test_self_growth_routing_persists_guide_and_knowledge(self):
        # 자기성장 신뢰성: durable 피드백이 실제로 방지침/지식메모에 저장된다(거짓 기록 아님), 중복 방지.
        space = PREFIX + "selfgrow"
        member = PREFIX + "agent_sg01"
        make_space(space, [member])
        orig = room_manager.engine.run_engine

        def drive(decision_json, cid, event):
            post = room_manager.post(space, "피드백", run_manager=False, client_message_id=cid)
            def fake(cwd, prompt, *a, **k):
                return decision_json if Path(cwd).name == MANAGER_DIRNAME else "x"
            try:
                room_manager.engine.run_engine = fake
                room_manager.tick(space, event, post["orchestration"])
            finally:
                room_manager.engine.run_engine = orig

        # 방지침(update_guide)
        drive(json.dumps({"action": "update_guide", "wake": "", "message": "환영카드는 파란색 톤에 제목을 크게", "reason": "대표 규칙"}, ensure_ascii=False), "sg-1", "e1")
        guide = (SPACES / space / "공간지침.md").read_text(encoding="utf-8")
        self.assertIn("환영카드는 파란색 톤에 제목을 크게", guide)
        self.assertIn("학습된 규칙", guide)

        # 지식(propose_knowledge) — 방 지식메모(감사) + 전역 지식 자원으로 졸업(발견기로 찾아 참고)
        from core import knowledge_ledger as KL
        import shutil as _sh
        kname = PREFIX + "배포규칙"
        kdir_pre = KL.knowledge_dir(kname)
        if kdir_pre:
            _sh.rmtree(kdir_pre, ignore_errors=True)
        try:
            drive(json.dumps({"action": "propose_knowledge", "wake": "", "message": "배포는 금요일에 하지 않는다",
                              "knowledge": kname, "description": "배포 일정 기준 — 언제 배포 금지. '배포','금요일','릴리즈'. 핵심: 배포 금지일",
                              "reason": "대표 기준"}, ensure_ascii=False), "sg-2", "e2")
            memo = (SPACES / space / "지식메모.md").read_text(encoding="utf-8")
            self.assertIn("배포는 금요일에 하지 않는다", memo)                       # 방 감사기록
            kdir = KL.knowledge_dir(kname)
            self.assertIsNotNone(kdir, "전역 지식 자원 미생성(졸업 실패)")            # 전역 졸업
            self.assertIn("배포는 금요일에 하지 않는다", (kdir / "지식.md").read_text(encoding="utf-8"))
            gate = KL.check_knowledge_discoverable(kname, [kname, "배포 금요일"])
            self.assertTrue(gate["discoverable"], "졸업한 지식이 발견기로 안 찾아짐")  # '찾아서'
        finally:
            kdir = KL.knowledge_dir(kname)
            if kdir:
                _sh.rmtree(kdir, ignore_errors=True)

        # 멱등: 같은 규칙 다시 → 중복 안 쌓임
        drive(json.dumps({"action": "update_guide", "wake": "", "message": "환영카드는 파란색 톤에 제목을 크게", "reason": "재기록"}, ensure_ascii=False), "sg-3", "e3")
        guide2 = (SPACES / space / "공간지침.md").read_text(encoding="utf-8")
        self.assertEqual(guide2.count("환영카드는 파란색 톤에 제목을 크게"), 1)

    def test_propose_case_also_delegates_body_authoring(self):
        # 케이스만 쌓지 말고: 대표 '다듬어줘'(propose_case)도 doer에게 skill-creator 기준 본문 고도화 위임.
        from core import skill_smith
        space = PREFIX + "casedeleg"
        member = PREFIX + "doer_cd01"
        make_space(space, [member])
        sk = PREFIX + "기존다듬을스킬"
        sdir = skill_smith.SKILLS / "추가" / sk
        skill_smith.create_skill(sk, description="문서 미리보기 스킬", body="# x\n", grade="추가")
        post = room_manager.post(space, "그 스킬 다듬어줘", run_manager=False, client_message_id="cd-1")
        ctx = post["orchestration"]
        decision = {"action": "propose_case", "wake": "", "message": "", "reason": "개선", "skill": sk,
                    "candidate": {"condition": "파일 만들 때", "instruction": "미리보기 포함", "polarity": "worked",
                                  "action": "add_case", "routing_kind": "procedural",
                                  "judgment_rationale": "r", "source_quote": "q", "sensitivity": "public"}}
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            (cwd / "결과.md").write_text("done", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return "ok"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "propose_case", ctx)
        finally:
            room_manager.engine.run_engine = orig
            shutil.rmtree(sdir, ignore_errors=True)
        p = SPACES / space / "work_plans.jsonl"
        objs = [json.loads(l).get("objective", "") for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
        self.assertTrue(any("skill-creator" in o and sk in o for o in objs), "propose_case가 본문 고도화 위임 안 함")

    def test_propose_case_supersede_without_target_coerces_to_add_case(self):
        # 회귀: 매니저가 기존 case_id를 모른 채 supersede를 고르면(대상 없음) 교훈을 버리지 말고
        # add_case로 강등해 실제로 케이스를 쌓는다(라이브 실증서 7->7 발의실패 → 7->8로 고친 버그).
        from core import skill_smith, case_ledger
        space = PREFIX + "supersede_fallback"
        member = PREFIX + "doer_sf01"
        make_space(space, [member])
        sk = PREFIX + "강등될스킬"
        sdir = skill_smith.SKILLS / "추가" / sk
        skill_smith.create_skill(sk, description="문서 미리보기 스킬", body="# x\n", grade="추가")
        n0 = len(case_ledger.read_cases(sdir))
        post = room_manager.post(space, "그 결과 다시 제대로 해", run_manager=False, client_message_id="sf-1")
        ctx = post["orchestration"]
        decision = {"action": "propose_case", "wake": "", "message": "", "reason": "교정", "skill": sk,
                    "candidate": {"condition": "파일 만들 때", "instruction": "미리보기 포함", "polarity": "worked",
                                  "action": "supersede", "routing_kind": "procedural",  # 대상(supersedes) 없음 — 강등돼야
                                  "judgment_rationale": "r", "source_quote": "q", "sensitivity": "public"}}
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            (cwd / "결과.md").write_text("done", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return "ok"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "propose_case", ctx)
        finally:
            room_manager.engine.run_engine = orig
            n1 = len(case_ledger.read_cases(sdir))
            shutil.rmtree(sdir, ignore_errors=True)
        self.assertEqual(n1, n0 + 1, "supersede(대상없음)가 add_case로 강등돼 케이스가 쌓이지 않음")

    def test_self_growth_publishes_acknowledgment(self):
        # 갭 A: 자기성장(규칙 반영) 시 공간관리가 채팅으로 '반영했어요' 확인을 남긴다(대표가 멈췄는지 안다).
        space = PREFIX + "growth_ack"
        make_space(space, [PREFIX + "ag_ack"])
        post = room_manager.post(space, "환영카드는 파란톤으로 해. 기억해.", run_manager=False, client_message_id="ack-1")
        ctx = post["orchestration"]
        decision = {"action": "update_guide", "wake": "", "message": "환영카드는 파란톤으로", "reason": "대표 규칙"}
        orig = room_manager.engine.run_engine
        try:
            room_manager.engine.run_engine = lambda cwd, prompt, *a, **k: (
                json.dumps(decision, ensure_ascii=False) if Path(cwd).name == MANAGER_DIRNAME else "x")
            room_manager.tick(space, "ack", ctx)
        finally:
            room_manager.engine.run_engine = orig
        notes = [r for r in read(space) if r.get("화자") == "공간관리" and "반영했어요" in (r.get("내용") or "")]
        self.assertTrue(notes, "자기성장 확인 메시지가 방에 안 떴음")

    def test_should_auto_continue_after_self_growth(self):
        # 갭 B: 자기성장 후 자동연속이 켜져 '그 규칙대로 재작업' 턴을 이을 수 있다.
        for act in ("update_guide", "propose_case", "propose_knowledge", "propose_skill"):
            self.assertTrue(room_manager._should_auto_continue(PREFIX + "nx", {"ok": True, "decision": {"action": act}}), act)
        self.assertFalse(room_manager._should_auto_continue(PREFIX + "nx", {"ok": True, "decision": {"action": "stop"}}))

    def test_auto_continue_keeps_dialogue_with_free_members_during_live_work(self):
        # 대표 신고 근본수정: 라이브 백그라운드 작업이 있어도 '작업을 안 쥔 자유 멤버'가 있으면
        # 자동연속으로 다자대화를 잇는다(예전엔 무조건 멈춰 1인 독백). 얘기할 상대가 작업자뿐이면 멈춘다.
        space = PREFIX + "livework"
        worker = PREFIX + "lw_worker"
        reviewer = PREFIX + "lw_reviewer"
        make_space(space, [worker, reviewer])
        pass_dec = {"ok": True, "decision": {"action": "pass", "wake": worker}}

        # (1) worker만 live 작업 → 자유 멤버 reviewer가 있으므로 대화를 잇는다
        snap_worker_busy = {"active_items": [
            {"worker_agent": worker, "heartbeat_stale": False, "heartbeat_startup_grace": False},
        ]}
        with patch.object(room_manager.task_registry, "snapshot", return_value=snap_worker_busy):
            self.assertEqual(room_manager._live_work_worker_tokens(space), {worker})
            self.assertEqual(room_manager._free_dialogue_members(space), {reviewer})
            self.assertTrue(room_manager._has_live_work_task(space))
            self.assertTrue(room_manager._should_auto_continue(space, pass_dec))

        # (2) 모든 멤버가 작업 중 → 얘기할 상대가 작업자뿐 → 멈춰 완료를 기다린다
        snap_all_busy = {"active_items": [
            {"worker_agent": worker, "heartbeat_stale": False},
            {"worker_agent": reviewer, "heartbeat_stale": False},
        ]}
        with patch.object(room_manager.task_registry, "snapshot", return_value=snap_all_busy):
            self.assertEqual(room_manager._free_dialogue_members(space), set())
            self.assertFalse(room_manager._should_auto_continue(space, pass_dec))

        # (3) 콜드스타트 grace 작업도 live로 센다(첫 heartbeat 전 stale이어도 작업자로 계수)
        snap_grace = {"active_items": [
            {"worker_agent": worker, "heartbeat_stale": True, "heartbeat_startup_grace": True},
        ]}
        with patch.object(room_manager.task_registry, "snapshot", return_value=snap_grace):
            self.assertEqual(room_manager._live_work_worker_tokens(space), {worker})
            self.assertTrue(room_manager._has_live_work_task(space))

    def test_reap_surfaces_stranded_task_without_completion_evidence(self):
        # 대표 신고 근본수정: 완료/취소 근거 없이 heartbeat가 오래 끊긴 '무진행 스트랜드'(워커가 done/error도
        # 못 쓴 채 죽거나 락대기로 멈춤)는 조용히 active에 박제하지 않고 error(중단 보고)로 그때까지의 산출을
        # release로 surface한다(결과가 반드시 대화로 돌아오게). heartbeat가 아직 잠깐만 끊겼으면 보존한다.
        space = PREFIX + "strand"
        member = PREFIX + "agent_str1"
        make_space(space, [member])
        work_dir = PEOPLE / member / "공간" / space / "작업" / "strandtask"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "결과.md").write_text("# 진행\n- 벽 1개 그림(캡처 cap09)\n### 검증 (진행 중)", encoding="utf-8")
        (work_dir / "task_pack.json").write_text(json.dumps({
            "task_id": "strandtask", "worker_agent": member, "task_pack_id": "tp_strand",
            "objective": "벽 그리기", "room_generation": 1, "source_event_seq": 1,
        }, ensure_ascii=False), encoding="utf-8")
        rel = work_dir.relative_to(task_registry.ROOT).as_posix()
        base_item = {
            "task_id": "strandtask", "worker_agent": member, "work_dir": rel,
            "state": "running", "heartbeat_stale": True, "heartbeat_startup_grace": False,
            "cancel_requested": False,
        }

        # (0) 자동재개 예산이 남아 있으면 error 종결 대신 체크포인트 자동재개를 먼저 디스패치한다
        #     (클로드코드식 복원 — 실패 보고로 끝내지 않고 잇는다). Popen은 mock으로 실제 기동 차단.
        (work_dir / "상태.json").write_text(json.dumps({"상태": "running"}, ensure_ascii=False), encoding="utf-8")
        stranded = dict(base_item, heartbeat_age_ms=task_registry.TASK_STRAND_REPORT_GRACE_MS + 60_000)
        with patch.object(task_registry, "snapshot", return_value={"active_items": [stranded]}), \
             patch.object(task_registry.subprocess, "Popen") as popen_mock:
            reaped0 = task_registry.reap_stale_tasks(space)
        self.assertTrue(popen_mock.called)
        resume_cmd = popen_mock.call_args[0][0]
        self.assertIn("--resume", resume_cmd)
        self.assertEqual(reaped0[0].get("reaped_as"), "auto_resumed")
        marker = json.loads((work_dir / "자동재개.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["count"], 1)
        # 상태.json은 error로 바뀌지 않는다(재개가 이어간다)
        st0 = json.loads((work_dir / "상태.json").read_text(encoding="utf-8"))
        self.assertEqual(st0["상태"], "running")

        # (1) 자동재개 예산 소진 후에는 종전 계약대로 error 중단보고로 산출 surface
        (work_dir / "자동재개.json").write_text(json.dumps({
            "schema": "TaskAutoResume.v1", "count": task_registry.TASK_AUTO_RESUME_LIMIT,
        }, ensure_ascii=False), encoding="utf-8")
        (work_dir / "상태.json").write_text(json.dumps({"상태": "running"}, ensure_ascii=False), encoding="utf-8")
        with patch.object(task_registry, "snapshot", return_value={"active_items": [stranded]}):
            reaped = task_registry.reap_stale_tasks(space)
        # 내 분기가 발동: 상태.json이 error(중단 보고)로 재기록되고 finalize가 호출된다.
        # (error → release surface 링크 자체는 test_engine_work_exception_finalizes_task_as_error가 커버.)
        st_after = json.loads((work_dir / "상태.json").read_text(encoding="utf-8"))
        self.assertEqual(st_after["상태"], "error")
        self.assertIn("스트랜드", st_after.get("사유", ""))
        self.assertIn("자동재개", st_after.get("사유", ""))       # 재개 이력 고지
        self.assertIn("체크포인트 보존됨", st_after.get("사유", ""))  # 보존 내용 고지(전부 유실 오해 방지)
        self.assertTrue(reaped)
        self.assertEqual(reaped[0].get("task_id"), "strandtask")

        # (2) 음성 대조: heartbeat_age가 grace 미만이면 아직 회수하지 않는다(막 끊긴 것일 수 있음 — 보존)
        (work_dir / "상태.json").write_text(json.dumps({"상태": "running"}, ensure_ascii=False), encoding="utf-8")
        fresh = dict(base_item, heartbeat_age_ms=1000)
        with patch.object(task_registry, "snapshot", return_value={"active_items": [fresh]}):
            task_registry.reap_stale_tasks(space)
        self.assertEqual(json.loads((work_dir / "상태.json").read_text(encoding="utf-8"))["상태"], "running")

    def test_normalize_decision_degrades_single_target_parallel_to_pass(self):
        # Gemini 구제: parallel_pass인데 유효 target 1개뿐이면 단일 pass로 강등(실패 대신 1명이라도 진행).
        space = PREFIX + "normdeg"
        a = PREFIX + "nd_a"
        b = PREFIX + "nd_b"
        make_space(space, [a, b])
        toks = {a, b}
        d = {"action": "parallel_pass", "reason": "r",
             "targets": [{"wake": a, "message": "혼자 의견", "reason": "x"}]}
        out = room_manager._normalize_decision(space, d, toks)
        self.assertEqual(out["action"], "pass")
        self.assertEqual(out["wake"], a)
        self.assertEqual(out["message"], "혼자 의견")
        # 2개면 그대로 parallel_pass 유지
        d2 = {"action": "parallel_pass", "reason": "r",
              "targets": [{"wake": a, "message": "A", "reason": "x"}, {"wake": b, "message": "B", "reason": "y"}]}
        self.assertEqual(room_manager._normalize_decision(space, d2, toks)["action"], "parallel_pass")

    def test_normalize_decision_resolves_target_name_aliases(self):
        # targets의 wake를 표시이름/코드로 줘도 토큰으로 해석한다(Gemini가 이름으로 부르는 경우 구제).
        space = PREFIX + "normalias"
        a = PREFIX + "alpha_x"     # 이름조각(alpha)이 유일해야 함 — 모호하면 set 순서로 비결정 해석
        b = PREFIX + "beta_y"
        make_space(space, [a, b])
        toks = {a, b}
        name_a = a.rsplit("_", 1)[0] if "_" in a else a   # = PREFIX+"alpha"
        d = {"action": "parallel_pass", "reason": "r",
             "targets": [{"wake": name_a, "message": "A", "reason": "x"}, {"wake": b, "message": "B", "reason": "y"}]}
        out = room_manager._normalize_decision(space, d, toks)
        self.assertEqual(out["action"], "parallel_pass")
        self.assertEqual({t["wake"] for t in out["targets"]}, {a, b})

    def test_work_ack_without_request_work_forces_dispatch(self):
        # 안전장치(대표 제안): 작업 지시에 에이전트가 'request_work' 없이 '하겠습니다'만 하면
        # 시스템이 그 작업을 강제 디스패치한다(말로만 접수→떠밀기).
        space = PREFIX + "forcedwork"
        member = PREFIX + "fw_agent"
        make_space(space, [member])
        post = room_manager.post(space, "문서 만들어줘", run_manager=False, client_message_id="fw-1")
        context = post["orchestration"]
        decision = {"action": "pass", "wake": member,
                    "message": "퍼널 전략을 md 문서로 작성해줘", "reason": "문서 작업"}
        original = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            # 작업 폴더(강제 디스패치된 work 실행)면 산출물 생성하고 끝
            if "작업" in str(cwd):
                (cwd / "결과.md").write_text("done", encoding="utf-8")
                (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
                return "ok"
            # 채팅 턴: request_work 없이 '하겠습니다'류 접수만
            return "네 대표님, 바로 md 문서로 작성하겠습니다."

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "forced work test", context)
        finally:
            room_manager.engine.run_engine = original
        status = room_manager.status(space)
        self.assertGreaterEqual(status["tasks"]["task_count"], 1, "착수-미실행 안전장치가 작업을 강제 디스패치하지 않음")

    def test_chat_answer_without_work_instruction_not_force_dispatched(self):
        # 오탐 방지: 매니저 지시가 '작업'이 아니라 단순 질문/답변이면 강제 디스패치하지 않는다.
        space = PREFIX + "nofalseforce"
        member = PREFIX + "nf_agent"
        make_space(space, [member])
        post = room_manager.post(space, "안녕?", run_manager=False, client_message_id="nf-1")
        context = post["orchestration"]
        decision = {"action": "pass", "wake": member, "message": "대표 인사에 답해줘", "reason": "인사"}
        original = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            return "안녕하세요 대표님! 무엇을 도와드릴까요?"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "no false force", context)
        finally:
            room_manager.engine.run_engine = original
        self.assertEqual(room_manager.status(space)["tasks"]["task_count"], 0)

    def test_publish_normalizes_absolute_and_fileurl_paths_to_root_relative(self):
        # 회귀: 에이전트가 file:///Users/.../CnvAgentWorld/<rel> 절대경로/URL로 적어도 공개 직전에
        # 루트상대로 보정한다(미리보기 동작 + 사용자명 노출 차단). 이미 상대경로면 그대로.
        from core import publish_ledger
        root = str(publish_ledger.ROOT)
        cases = [
            (f"[x.md](file://{root}/에이전트/a/작업/b/x.md)", "[x.md](에이전트/a/작업/b/x.md)"),
            (f"결과: {root}/에이전트/a/작업/결과.md 확인", "결과: 에이전트/a/작업/결과.md 확인"),
            ("에이전트/a/작업/결과.md", "에이전트/a/작업/결과.md"),
        ]
        for raw, expected in cases:
            self.assertEqual(publish_ledger._to_root_relative_paths(raw), expected)
        self.assertNotIn(root, publish_ledger._to_root_relative_paths(cases[0][0]))

    def test_phase2_prompt_focuses_on_action_payload(self):
        # 2단계(스마트): 복잡 액션은 '전용 필드'만 선택지 나열해 받는다. 비대상 액션은 None(일반 재시도).
        members = {PREFIX + "m_a", PREFIX + "m_b"}
        p = room_manager._phase2_prompt(PREFIX + "nx", "parallel_pass",
                                        {"action": "parallel_pass", "reason": "토론"}, members)
        self.assertIsNotNone(p)
        self.assertIn("targets", p)
        for m in members:
            self.assertIn(m, p)              # 멤버 토큰이 선택지로 나열됨
        self.assertIn("2단계", p)
        # propose_case 등 비대상은 None
        self.assertIsNone(room_manager._phase2_prompt(PREFIX + "nx", "propose_case", {"action": "propose_case"}, members))
        self.assertIsNone(room_manager._phase2_prompt(PREFIX + "nx", "stop", {"action": "stop"}, members))

    def test_parallel_pass_recovers_via_phase2_focused_retry(self):
        # 근본수정 실증: 1차에 parallel_pass targets를 빈 채로 내도(Gemini 흔한 실패),
        # 2단계 초점 재요청으로 targets를 채워 성공한다 — manager_failed로 막히지 않는다.
        space = PREFIX + "phase2recover"
        a = PREFIX + "p2a"
        b = PREFIX + "p2b"
        make_space(space, [a, b])
        post = room_manager.post(space, "둘이 의견 모아줘", run_manager=False, client_message_id="p2-1")
        context = post["orchestration"]
        original = room_manager.engine.run_engine
        seen_phase2 = {"hit": False}

        def fake(cwd, prompt, *args, **kwargs):
            if "2단계" in prompt:
                seen_phase2["hit"] = True
                return json.dumps({
                    "action": "parallel_pass", "wake": "", "message": "", "reason": "토론",
                    "targets": [
                        {"wake": a, "message": "관점 A로 의견", "reason": "A"},
                        {"wake": b, "message": "관점 B로 의견", "reason": "B"},
                    ],
                }, ensure_ascii=False)
            # 1차: targets 비움(흔한 Gemini 실패 재현)
            return json.dumps({"action": "parallel_pass", "wake": "", "message": "",
                               "reason": "토론", "targets": []}, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake
            result = room_manager.tick(space, "phase2 recover", context)
        finally:
            room_manager.engine.run_engine = original

        self.assertTrue(seen_phase2["hit"], "2단계 초점 프롬프트가 발동하지 않음")
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"]["action"], "parallel_pass")
        # manager_failed 없이 정상 처리
        self.assertNotEqual(room_manager.status(space).get("last_action"), "manager_failed")

    def test_retry_prompt_escalates_to_simplest_on_last_attempt(self):
        # 근본수정: Gemini가 parallel_pass targets를 빈 채로 반복 실패→manager_failed로 막히던 문제.
        # 마지막 재시도에선 복잡한 형식을 버리고 단순 pass/stop으로 빠지게 유도해 자가복구한다.
        early = room_manager._retry_prompt("BASE", "raw", "parallel_pass targets의 wake와 message가 비어 있으면 안 됨", 2)
        last = room_manager._retry_prompt("BASE", "raw", "parallel_pass targets의 wake와 message가 비어 있으면 안 됨",
                                          room_manager.MAX_DECISION_ATTEMPTS)
        self.assertNotIn("마지막 시도", early)
        self.assertIn("마지막 시도", last)
        self.assertIn("pass", last)
        self.assertIn("stop", last)

    def test_should_auto_continue_after_publish_each_for_debate(self):
        # Fix ②: 다자 의견 공개(publish_each) 후 자동연속이 켜져 '반응 라운드'(토론)를 이을 수 있다.
        self.assertTrue(room_manager._should_auto_continue(
            PREFIX + "nx", {"ok": True, "decision": {"action": "publish_each"}}))
        # stale/실패면 잇지 않는다.
        self.assertFalse(room_manager._should_auto_continue(
            PREFIX + "nx", {"ok": True, "stale": True, "decision": {"action": "publish_each"}}))
        self.assertFalse(room_manager._should_auto_continue(
            PREFIX + "nx", {"ok": False, "decision": {"action": "publish_each"}}))

    def test_manager_decision_parses_code_fenced_json(self):
        # LLM이 ```json 코드펜스로 감싸도 매니저가 결정을 읽어야 한다.
        # (엄격 파서는 펜스를 빈 dict로 처리→'필수 필드 빠짐'→3회 재시도 후 stop으로 매니저가 멈추던 버그.)
        space = PREFIX + "fence"
        make_space(space, [PREFIX + "agent_fc"])
        post = room_manager.post(space, "안녕", run_manager=False, client_message_id="fc-1")
        ctx = post["orchestration"]
        orig = room_manager.engine.run_engine
        fenced = "```json\n" + json.dumps(
            {"action": "stop", "wake": "", "message": "", "reason": "인사 응답 후 대기"}, ensure_ascii=False) + "\n```"
        try:
            room_manager.engine.run_engine = lambda cwd, prompt, *a, **k: (
                fenced if Path(cwd).name == MANAGER_DIRNAME else "x")
            result = room_manager.tick(space, "fence test", ctx)
        finally:
            room_manager.engine.run_engine = orig
        self.assertTrue(result.get("ok"))
        self.assertEqual((result.get("decision") or {}).get("action"), "stop")   # 펜스에도 파싱됨
        self.assertFalse(any(e.get("type") == "manager_failed" for e in result.get("events", [])))

    def test_publish_each_shows_all_candidates_as_own_bubbles(self):
        # 캐주얼 단톡: publish_each → 각 후보를 그 멤버 말풍선으로 따로 공개(공간관리 아님), 폐기 0.
        space = PREFIX + "publisheach"
        a = PREFIX + "agent_pe01"; b = PREFIX + "agent_pe02"
        make_space(space, [a, b])
        context, pending = self._collect_parallel_candidates(
            space, a, b, agent_replies={a: "나는 팥빙수가 좋아!", b: "나는 냉면!"}, client_message_id="pe-1",
        )
        ids = [p["candidate_id"] for p in pending]
        original = room_manager.engine.run_engine

        def fake(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps({"action": "publish_each", "wake": "", "message": "", "candidate_ids": ids,
                                   "reason": "캐주얼 단톡 각자 공개"}, ensure_ascii=False)
            return "x"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "publish each", context)
        finally:
            room_manager.engine.run_engine = original

        assistant = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertEqual(len(assistant), 2)                       # 2개 말풍선(합치지 않음)
        self.assertNotIn("공간관리", [r.get("화자") for r in assistant])  # 사회자 침묵
        contents = " ".join(r.get("내용", "") for r in assistant)
        self.assertIn("팥빙수", contents)
        self.assertIn("냉면", contents)
        sc = candidate_queue.snapshot(space)["state_counts"]
        self.assertEqual(sc.get("discarded", 0), 0)               # 폐기 0(모두 공개)
        self.assertEqual(sc.get("selected_published", 0), 2)

    def test_parallel_pass_auto_continues_to_synthesize_pending_candidates(self):
        # 회귀: parallel_pass로 후보만 모은 뒤 방이 조용히 멈추던 구멍을 막는다.
        # auto_continue=True면 관리자가 다시 떠서 pending 후보를 synthesize로 공개한다.
        space = PREFIX + "parallelautosynth"
        member_a = PREFIX + "agent_psa1"
        member_b = PREFIX + "agent_psb2"
        make_space(space, [member_a, member_b])
        post = room_manager.post(space, "두 관점으로 검토해줘", run_manager=False, client_message_id="client-autosynth")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                pending = candidate_queue.snapshot(space)["pending_items"]
                if not pending:
                    # 첫 턴: 두 멤버에게 병렬 위임(후보만 수집)
                    return json.dumps({
                        "action": "parallel_pass", "wake": "", "message": "",
                        "reason": "서로 다른 관점이 필요하다",
                        "targets": [
                            {"wake": member_a, "message": "찬성 관점 후보", "reason": "A"},
                            {"wake": member_b, "message": "위험 관점 후보", "reason": "B"},
                        ],
                        "join_policy": "timeout_then_partial",
                        "presentation_mode": "silent_reference",
                    }, ensure_ascii=False)
                # 자동 연속 턴: 관리자가 pending 후보를 합성해 방에 공개
                return json.dumps({
                    "action": "synthesize_candidates", "wake": "",
                    "candidate_ids": [item["candidate_id"] for item in pending],
                    "message": "두 관점을 합치면: 합성된 공개 답변",
                    "reason": "후보 합성 공개",
                }, ensure_ascii=False)
            return f"{cwd.parent.parent.name} 후보 의견"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "이벤트", context, auto_continue=True)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        # 자동 연속이 최소 1턴 일어났고, pending 후보가 모두 정리됐으며, 합성문이 실제로 방에 공개됐다.
        self.assertGreaterEqual(result.get("auto_continue_turns", 0), 1)
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(len(assistant_rows), 1)
        self.assertIn("합성된 공개 답변", assistant_rows[0]["내용"])

    def test_pending_candidates_drained_after_chain_when_autocontinue_stops(self):
        # 회귀(라이브): 자동 연속이 토론 턴을 다 써서 pending 후보를 못 비운 채 멈추면(stall),
        # 체인 종료 전 candidate drain 안전망이 후보를 공개해 비운다 — 후보가 큐에 썩지 않게 한다.
        space = PREFIX + "canddrain"
        member_a = PREFIX + "agent_cda1"
        member_b = PREFIX + "agent_cdb2"
        make_space(space, [member_a, member_b])
        post = room_manager.post(space, "두 관점 줘", run_manager=False, client_message_id="cd-1")
        context = post["orchestration"]
        original = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            if Path(cwd).name == MANAGER_DIRNAME:
                pending = candidate_queue.snapshot(space)["pending_items"]
                if "미공개 후보 정리" in prompt:                # candidate drain 턴: 실제 공개
                    return json.dumps({
                        "action": "publish_each",
                        "candidate_ids": [i["candidate_id"] for i in pending],
                        "wake": "", "message": "", "reason": "정리",
                    }, ensure_ascii=False)
                if pending:                                     # 자동 연속 턴: 후보 둔 채 계속 멈춤(stall)
                    return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "후보 둔 채 멈춤"}, ensure_ascii=False)
                return json.dumps({                             # 첫 턴: 병렬 수집
                    "action": "parallel_pass", "wake": "", "message": "", "reason": "두 관점",
                    "targets": [{"wake": member_a, "message": "A"}, {"wake": member_b, "message": "B"}],
                    "join_policy": "timeout_then_partial", "presentation_mode": "silent_reference",
                }, ensure_ascii=False)
            return f"{Path(cwd).parent.parent.name} 의견"

        try:
            room_manager.engine.run_engine = fake
            result = room_manager.tick(space, "이벤트", context, auto_continue=True)
        finally:
            room_manager.engine.run_engine = original

        drains = [e for e in result.get("events", []) if e.get("type") == "manager_candidate_drained"]
        self.assertGreaterEqual(len(drains), 1, result.get("events"))
        self.assertEqual(room_manager.status(space)["candidate_queue"]["pending_count"], 0)
        assistant_rows = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertEqual(len(assistant_rows), 2)               # 후보 2개 공개(잔류 0)

    def test_parallel_pass_obligation_stays_assigned_until_candidate_is_published(self):
        space = PREFIX + "parallelobligation"
        member_a = PREFIX + "agent_poa1"
        member_b = PREFIX + "agent_pob2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(
            space,
            member_a,
            member_b,
            manager_requested=True,
            client_message_id="client-parallel-obligation",
        )
        selected_id = next(item["candidate_id"] for item in pending if item["target_agent"] == member_a)
        status_after_parallel = room_manager.status(space)
        obligation_after_parallel = status_after_parallel["response_obligations"]
        self.assertEqual(obligation_after_parallel["open_count"], 1)
        self.assertEqual(obligation_after_parallel["state_counts"].get("assigned"), 1)
        assigned = obligation_after_parallel["open_items"][0]
        self.assertEqual(assigned["assigned_to"], f"parallel_pass:{member_a},{member_b}")
        self.assertEqual(status_after_parallel["candidate_queue"]["pending_count"], 2)

        rows = [
            json.loads(line)
            for line in (SPACES / space / "response_obligations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows[-1]["created_at"] = "2000-01-01T00:00:00"
        rows[-1]["updated_at"] = "2000-01-01T00:00:00"
        (SPACES / space / "response_obligations.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        overdue_status = room_manager.status(space)
        self.assertEqual(overdue_status["response_obligations"]["open_count"], 1)
        self.assertEqual(overdue_status["response_obligations"]["overdue_open_count"], 1)
        self.assertEqual(overdue_status["response_obligations"]["auto_closed_count"], 0)
        self.assertEqual(overdue_status["candidate_queue"]["pending_count"], 2)

        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn("select_candidate", prompt)
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "병렬 후보 중 A를 공개",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate review", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        final_status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(final_status["response_obligations"]["open_count"], 0)
        self.assertEqual(final_status["response_obligations"]["state_counts"].get("answered"), 1)
        self.assertEqual(final_status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(final_status["last_action"], "select_candidate")

    def test_parallel_pass_request_work_dispatches_concurrent_tasks(self):
        # 계약(2026-06): parallel_pass 후보가 request_work를 내면 단일 pass와 동일하게 '동시 작업'으로
        # 디스패치한다(request_work_via_manager 이행). 한 번의 parallel_pass로 멤버들이 각자 자기 작업을
        # 동시에 띄운다. 후보(토론)는 그대로 남는다. (이전엔 후보로만 남고 작업이 유실됐다 — 그 갭을 메움.)
        space = PREFIX + "parallelworkreq"
        member_a = PREFIX + "agent_pwa1"
        member_b = PREFIX + "agent_pwb2"
        make_space(space, [member_a, member_b])
        post = room_manager.post(space, "작업이 필요한지 병렬로 봐줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "parallel_pass",
                    "wake": "",
                    "message": "",
                    "reason": "작업 필요 여부를 비교한다",
                    "targets": [
                        {"wake": member_a, "message": "작업 제안 후보를 만들어줘", "reason": "작업 관점"},
                        {"wake": member_b, "message": "대화로 충분한지 후보를 만들어줘", "reason": "대화 관점"},
                    ],
                }, ensure_ascii=False)
            return json.dumps({
                "schema": "ChatAgentResult.v1",
                "action": "request_work",
                "public_reply": "",
                "work_request": {
                    "objective": "병렬 후보가 제안한 작업",
                    "constraints": [],
                    "suggested_worker": member_a,
                },
                "manager_requests": [],
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        pending = status["candidate_queue"]["pending_items"]
        self.assertTrue(result["ok"])
        # 후보(토론)는 그대로 남고 + 멤버별 작업이 동시에 디스패치된다(2명 → 2작업).
        self.assertEqual(status["candidate_queue"]["pending_count"], 2)
        self.assertTrue(all(item.get("structured_action") == "request_work" for item in pending))
        self.assertEqual(status["tasks"]["task_count"], 2)
        # 작업 디스패치(collection 시점)일 뿐, 사회자가 후보를 공개(publish_each/select)하기 전이라 방엔 말풍선 없음.
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])

    def test_parallel_pass_stale_candidate_does_not_enqueue_or_publish(self):
        space = PREFIX + "parallelstale"
        member_a = PREFIX + "agent_psa1"
        member_b = PREFIX + "agent_psb2"
        make_space(space, [member_a, member_b])
        post = room_manager.post(space, "늦은 후보를 막아줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "parallel_pass",
                    "wake": "",
                    "message": "",
                    "reason": "stale guard 테스트",
                    "targets": [
                        {"wake": member_a, "message": "후보 A", "reason": "A"},
                        {"wake": member_b, "message": "후보 B", "reason": "B"},
                    ],
                }, ensure_ascii=False)
            orchestration.advance_generation(space, "parallel candidate stale test")
            return "늦은 후보"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(status["candidate_queue"]["candidate_count"], 0)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["tasks"]["task_count"], 0)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("superseded"), 2)

    def test_parallel_pass_timeout_then_partial_cancels_slow_candidate_without_late_enqueue(self):
        space = PREFIX + "parallelpartial"
        member_fast = PREFIX + "agent_ptf1"
        member_slow = PREFIX + "agent_pts2"
        make_space(space, [member_fast, member_slow])
        post = room_manager.post(space, "빠른 후보만 먼저 모아줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_command = room_manager.engine._engine_command
        original_join_timeout = room_manager._parallel_join_timeout
        original_drain = room_manager.PARALLEL_CANDIDATE_CANCEL_DRAIN_SECONDS

        decision = json.dumps({
            "action": "parallel_pass",
            "wake": "",
            "message": "",
            "reason": "partial timeout test",
            "targets": [
                {"wake": member_fast, "message": "fast", "reason": "fast"},
                {"wake": member_slow, "message": "slow", "reason": "slow"},
            ],
            "join_policy": "timeout_then_partial",
            "presentation_mode": "silent_reference",
        }, ensure_ascii=False)

        def fake_command(cwd, prompt, engine_name, model):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return [sys.executable, "-c", f"import sys; sys.stdout.write({decision!r})"]
            token = cwd.parent.parent.name
            if token == member_fast:
                return [sys.executable, "-c", "import sys; sys.stdout.write('fast candidate')"]
            return [sys.executable, "-c", "import time, sys; time.sleep(5); sys.stdout.write('late candidate')"]

        try:
            room_manager.engine._engine_command = fake_command
            room_manager._parallel_join_timeout = lambda n: 1.0
            room_manager.PARALLEL_CANDIDATE_CANCEL_DRAIN_SECONDS = 2.0
            started = time.monotonic()
            result = room_manager.tick(space, "test event", context)
            elapsed = time.monotonic() - started
            time.sleep(0.5)
        finally:
            room_manager.engine._engine_command = original_command
            room_manager._parallel_join_timeout = original_join_timeout
            room_manager.PARALLEL_CANDIDATE_CANCEL_DRAIN_SECONDS = original_drain

        status = room_manager.status(space)
        event_types = [event.get("type") for event in result.get("events", [])]
        self.assertTrue(result["ok"])
        self.assertLess(elapsed, 4.0)
        self.assertIn("parallel_candidate", event_types)
        self.assertIn("parallel_candidate_timeout", event_types)
        self.assertEqual(status["candidate_queue"]["pending_count"], 1)
        self.assertEqual(status["candidate_queue"]["error_count"], 1)
        self.assertEqual(status["candidate_queue"]["candidate_count"], 2)
        self.assertEqual(status["candidate_queue"]["pending_items"][0]["target_agent"], member_fast)
        self.assertEqual(status["candidate_queue"]["error_items"][0]["target_agent"], member_slow)
        self.assertIn("TimeoutExpired", status["candidate_queue"]["error_items"][0]["error"])
        prompt_status = room_manager._prompt_room_status_snapshot(space)
        self.assertEqual(prompt_status["candidate_queue"]["error_items"][0]["target_agent"], member_slow)
        self.assertIn("TimeoutExpired", prompt_status["candidate_queue"]["error_items"][0]["error"])
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["tasks"]["task_count"], 0)
        self.assertEqual(status["release_queue"]["release_count"], 0)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])

    def test_parallel_pass_wait_all_waits_for_slow_candidate(self):
        space = PREFIX + "parallelwaitall"
        member_fast = PREFIX + "agent_pwf1"
        member_slow = PREFIX + "agent_pws2"
        make_space(space, [member_fast, member_slow])
        post = room_manager.post(space, "모두 기다려줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_command = room_manager.engine._engine_command

        decision = json.dumps({
            "action": "parallel_pass",
            "wake": "",
            "message": "",
            "reason": "wait all test",
            "targets": [
                {"wake": member_fast, "message": "fast", "reason": "fast"},
                {"wake": member_slow, "message": "slow", "reason": "slow"},
            ],
            "join_policy": "wait_all",
            "presentation_mode": "silent_reference",
        }, ensure_ascii=False)

        def fake_command(cwd, prompt, engine_name, model):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return [sys.executable, "-c", f"import sys; sys.stdout.write({decision!r})"]
            token = cwd.parent.parent.name
            if token == member_slow:
                return [sys.executable, "-c", "import time, sys; time.sleep(0.55); sys.stdout.write('slow candidate')"]
            return [sys.executable, "-c", "import sys; sys.stdout.write('fast candidate')"]

        try:
            room_manager.engine._engine_command = fake_command
            started = time.monotonic()
            result = room_manager.tick(space, "test event", context)
            elapsed = time.monotonic() - started
        finally:
            room_manager.engine._engine_command = original_command

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertEqual(status["candidate_queue"]["pending_count"], 2)
        self.assertEqual(status["candidate_queue"]["error_count"], 0)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])

    def test_parallel_pass_schema_rejects_duplicate_or_unsupported_targets(self):
        space = PREFIX + "parallelschema"
        member_a = PREFIX + "agent_psc1"
        member_b = PREFIX + "agent_psc2"
        make_space(space, [member_a, member_b])
        members = {member_a, member_b}
        valid = {
            "action": "parallel_pass",
            "wake": "",
            "message": "",
            "reason": "valid",
            "targets": [
                {"wake": member_a, "message": "A", "reason": "A"},
                {"wake": member_b, "message": "B", "reason": "B"},
            ],
            "join_policy": "timeout_then_partial",
            "presentation_mode": "silent_reference",
        }
        duplicate = {**valid, "targets": [
            {"wake": member_a, "message": "A", "reason": "A"},
            {"wake": member_a, "message": "B", "reason": "B"},
        ]}
        unsupported_mode = {**valid, "presentation_mode": "individual_bubbles"}
        valid_select = {
            "action": "select_candidate",
            "wake": "",
            "message": "",
            "reason": "select",
            "candidate_id": "candidate_1",
        }
        invalid_select = {**valid_select, "candidate_ids": ["candidate_1", "candidate_2"]}
        valid_synthesize = {
            "action": "synthesize_candidates",
            "wake": "",
            "message": "합성 공개문",
            "reason": "synthesize",
            "candidate_ids": ["candidate_1", "candidate_2"],
        }
        invalid_synthesize = {**valid_synthesize, "message": ""}
        valid_discard = {
            "action": "discard_candidate",
            "wake": "",
            "message": "",
            "reason": "discard",
            "candidate_ids": ["candidate_1"],
        }

        self.assertEqual(room_manager._decision_error(valid, members), "")
        self.assertIn("중복", room_manager._decision_error(duplicate, members))
        self.assertIn("presentation_mode", room_manager._decision_error(unsupported_mode, members))
        self.assertEqual(room_manager._decision_error(valid_select, members), "")
        self.assertIn("하나", room_manager._decision_error(invalid_select, members))
        self.assertEqual(room_manager._decision_error(valid_synthesize, members), "")
        self.assertIn("합성문", room_manager._decision_error(invalid_synthesize, members))
        self.assertEqual(room_manager._decision_error(valid_discard, members), "")

    def test_candidate_queue_corruption_is_visible_in_status(self):
        space = PREFIX + "candidatebadjson"
        make_space(space)
        (SPACES / space / "public_reply_candidates.jsonl").write_text(
            '{"candidate_id":"ok","state":"pending_synthesis"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["candidate_queue"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["candidate_queue"]["ledger_errors"]))
        self.assertTrue(any(f.get("상태") == "candidate_queue_corrupt" for f in status["failures"]))
        self.assertTrue(any("CandidateQueue" in action for action in status["recovery_actions"]))

    def test_candidate_select_publishes_one_candidate_and_closes_peers(self):
        space = PREFIX + "candselect"
        member_a = PREFIX + "agent_csa1"
        member_b = PREFIX + "agent_csb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        selected_id = next(item["candidate_id"] for item in pending if item["target_agent"] == member_a)
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn("select_candidate", prompt)
            self.assertIn(selected_id, prompt)
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "A 후보가 가장 적합하다",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate review", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        rows = read(space)
        assistant_rows = [row for row in rows if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0]["내용"], "A 후보 공개")
        self.assertEqual(assistant_rows[0]["candidate_publish_mode"], "select")
        self.assertEqual(assistant_rows[0]["candidate_id"], selected_id)
        self.assertTrue(assistant_rows[0]["publish_ledger_claim"].startswith("pledger_"))
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("selected_published"), 1)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("discarded"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)
        self.assertEqual(status["last_action"], "select_candidate")
        terminal = [
            row for row in candidate_queue.snapshot(space)["latest"]
            if row.get("candidate_id") == selected_id and row.get("state") == "selected_published"
        ]
        self.assertTrue(terminal)
        self.assertTrue(terminal[-1].get("transition_manager_claim_token", "").startswith("manager_claim_"))
        self.assertTrue(terminal[-1].get("transition_manager_fencing_token", "").startswith("manager_fence_"))

    def test_candidate_synthesis_publishes_one_manager_message_and_closes_candidates(self):
        space = PREFIX + "candsynth"
        member_a = PREFIX + "agent_cya1"
        member_b = PREFIX + "agent_cyb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        candidate_ids = [item["candidate_id"] for item in pending]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn("synthesize_candidates", prompt)
            return json.dumps({
                "action": "synthesize_candidates",
                "wake": "",
                "message": "두 후보를 합쳐 공개하는 답변",
                "reason": "두 관점을 함께 반영한다",
                "candidate_ids": candidate_ids,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate synthesize", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0]["화자"], "공간관리")
        self.assertEqual(assistant_rows[0]["내용"], "두 후보를 합쳐 공개하는 답변")
        self.assertEqual(assistant_rows[0]["candidate_publish_mode"], "synthesize")
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("synthesized_published"), 2)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)
        self.assertEqual(status["last_action"], "synthesize_candidates")

    def test_candidate_discard_closes_candidates_without_public_message(self):
        space = PREFIX + "canddiscard"
        member_a = PREFIX + "agent_cda1"
        member_b = PREFIX + "agent_cdb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        candidate_ids = [item["candidate_id"] for item in pending]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "discard_candidate",
                "wake": "",
                "message": "",
                "reason": "이번 요청에는 후보를 쓰지 않는다",
                "candidate_ids": candidate_ids,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate discard", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("discarded"), 2)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["last_action"], "discard_candidate")

    def test_candidate_select_request_work_without_public_reply_is_rejected_without_task(self):
        space = PREFIX + "candworkreject"
        member_a = PREFIX + "agent_cwa1"
        member_b = PREFIX + "agent_cwb2"
        request_work = json.dumps({
            "schema": "ChatAgentResult.v1",
            "action": "request_work",
            "public_reply": "",
            "work_request": {"objective": "후보가 제안한 작업", "suggested_worker": member_a},
            "manager_requests": [],
        }, ensure_ascii=False)
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(
            space,
            member_a,
            member_b,
            agent_replies={member_a: request_work, member_b: request_work},
        )
        selected_id = pending[0]["candidate_id"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "작업 요청 후보를 바로 공개하려는 시도",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate select invalid", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 2)
        # 작업은 collection 시점에 동시 디스패치됨(2명 → 2작업). 그러나 public_reply 없는 request_work 후보를
        # select_candidate로 '공개'하려는 시도는 거부된다(공개할 토론 텍스트가 없음) — 공개/방 말풍선 0.
        self.assertEqual(status["tasks"]["task_count"], 2)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["last_action"], "candidate_select_failed")

    def test_candidate_stale_selection_supersedes_turn_without_publish(self):
        space = PREFIX + "candstale"
        member_a = PREFIX + "agent_cta1"
        member_b = PREFIX + "agent_ctb2"
        make_space(space, [member_a, member_b])
        _old_context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        selected_id = pending[0]["candidate_id"]
        orchestration.advance_generation(space, "newer user request")
        new_post = room_manager.post(space, "새 요청", run_manager=False, client_message_id="client-2")
        new_context = new_post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "오래된 후보 선택 시도",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate stale", new_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("superseded"), 2)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["last_action"], "candidate_stale")

    def test_candidate_selection_retry_does_not_duplicate_public_message(self):
        space = PREFIX + "candretry"
        member_a = PREFIX + "agent_cra1"
        member_b = PREFIX + "agent_crb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        selected_id = pending[0]["candidate_id"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "같은 후보 재시도",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            first = room_manager.tick(space, "candidate select first", context)
            second = room_manager.tick(space, "candidate select retry", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("selected_published"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_candidate_queue_mark_failure_after_publish_recovers_on_retry(self):
        space = PREFIX + "candmarkretry"
        member_a = PREFIX + "agent_cma1"
        member_b = PREFIX + "agent_cmb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        selected_id = pending[0]["candidate_id"]
        original_run_engine = room_manager.engine.run_engine
        original_mark_selected = room_manager.candidate_queue.mark_selected
        calls = {"count": 0}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "전이 실패 후 재시도",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("simulated candidate queue transition crash")
            return original_mark_selected(*args, **kwargs)

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.candidate_queue.mark_selected = fail_once
            first = room_manager.tick(space, "candidate select first", context)
            mid_status = room_manager.status(space)
            second = room_manager.tick(space, "candidate select retry", context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.candidate_queue.mark_selected = original_mark_selected

        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(mid_status["last_action"], "candidate_select_failed")
        self.assertEqual(mid_status["candidate_queue"]["pending_count"], 2)
        self.assertEqual(mid_status["publish_ledger"]["counts"].get("committed"), 1)
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("selected_published"), 1)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("discarded"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_candidate_publish_failure_does_not_close_candidate(self):
        space = PREFIX + "candpubfail"
        member_a = PREFIX + "agent_cfa1"
        member_b = PREFIX + "agent_cfb2"
        make_space(space, [member_a, member_b])
        context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        selected_id = pending[0]["candidate_id"]
        original_run_engine = room_manager.engine.run_engine
        original_append = room_manager.publish_ledger.append_public_message

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "select_candidate",
                "wake": "",
                "message": "",
                "reason": "공개 실패 복구 확인",
                "candidate_id": selected_id,
            }, ensure_ascii=False)

        def reject_publish(*args, **kwargs):
            raise publish_ledger.PublishLedgerError("simulated candidate publish rejection")

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.publish_ledger.append_public_message = reject_publish
            result = room_manager.tick(space, "candidate publish fail", context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.publish_ledger.append_public_message = original_append

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 2)
        self.assertEqual(status["publish_ledger"]["counts"].get("claimed"), 1)
        self.assertIsNone(status["publish_ledger"]["counts"].get("committed"))
        self.assertEqual(status["last_action"], "candidate_select_failed")

    def test_candidate_stale_discard_supersedes_without_publish(self):
        space = PREFIX + "candstalediscard"
        member_a = PREFIX + "agent_csa3"
        member_b = PREFIX + "agent_csb4"
        make_space(space, [member_a, member_b])
        _old_context, pending = self._collect_parallel_candidates(space, member_a, member_b)
        candidate_ids = [item["candidate_id"] for item in pending]
        orchestration.advance_generation(space, "newer user request")
        new_post = room_manager.post(space, "새 요청", run_manager=False, client_message_id="client-2")
        new_context = new_post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "discard_candidate",
                "wake": "",
                "message": "",
                "reason": "오래된 후보 폐기 시도",
                "candidate_ids": candidate_ids,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "candidate stale discard", new_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("superseded"), 2)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("discarded", 0), 0)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["last_action"], "candidate_stale")

    def test_candidate_synthesis_discards_unselected_same_turn_peers(self):
        space = PREFIX + "candthree"
        member_a = PREFIX + "agent_c3a1"
        member_b = PREFIX + "agent_c3b2"
        member_c = PREFIX + "agent_c3c3"
        make_space(space, [member_a, member_b, member_c])
        post = room_manager.post(space, "세 관점으로 봐줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def collect_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "parallel_pass",
                    "wake": "",
                    "message": "",
                    "reason": "세 관점 수집",
                    "targets": [
                        {"wake": member_a, "message": "A 후보", "reason": "A"},
                        {"wake": member_b, "message": "B 후보", "reason": "B"},
                        {"wake": member_c, "message": "C 후보", "reason": "C"},
                    ],
                }, ensure_ascii=False)
            return f"{cwd.parent.parent.name} 후보"

        try:
            room_manager.engine.run_engine = collect_engine
            room_manager.tick(space, "parallel collect three", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        pending = sorted(candidate_queue.snapshot(space)["pending_items"], key=lambda item: item.get("target_agent", ""))
        selected_ids = [pending[0]["candidate_id"], pending[1]["candidate_id"]]

        def synth_engine(cwd, prompt, *args, **kwargs):
            return json.dumps({
                "action": "synthesize_candidates",
                "wake": "",
                "message": "두 후보만 합성",
                "reason": "세 번째 후보는 중복이라 제외",
                "candidate_ids": selected_ids,
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = synth_engine
            result = room_manager.tick(space, "candidate synthesize partial", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(status["candidate_queue"]["pending_count"], 0)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("synthesized_published"), 2)
        self.assertEqual(status["candidate_queue"]["state_counts"].get("discarded"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_candidate_terminal_retry_closes_peers_after_partial_queue_transition(self):
        space = PREFIX + "candpartialq"
        member_a = PREFIX + "agent_cqa1"
        member_b = PREFIX + "agent_cqb2"
        member_c = PREFIX + "agent_cqc3"
        make_space(space, [member_a, member_b, member_c])
        post = room_manager.post(space, "후보 전이 복구", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        turn_id = orchestration.effect_id("parallel_turn_test", space, context["intent_id"], context["source_event_seq"])
        ids = []
        for member, reply in ((member_a, "A"), (member_b, "B"), (member_c, "C")):
            item = candidate_queue.enqueue_candidate(
                space,
                turn_id=turn_id,
                target_agent=member,
                manager_message="후보",
                reply=reply,
                context=context,
            )
            ids.append(item["event"]["candidate_id"])

        selected = candidate_queue.mark_selected(
            space,
            ids[0],
            actor="공간관리",
            reason="partial selected",
            publish_effect_id="effect_candidate_select_partial",
            published_message_id="msg_candidate_select_partial",
            discard_turn_peers=False,
        )
        mid = candidate_queue.snapshot(space)
        retry = candidate_queue.mark_selected(
            space,
            ids[0],
            actor="공간관리",
            reason="retry selected",
            publish_effect_id="effect_candidate_select_partial",
            published_message_id="msg_candidate_select_partial",
            discard_turn_peers=True,
        )
        final = candidate_queue.snapshot(space)

        self.assertFalse(selected["duplicate"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(mid["pending_count"], 2)
        self.assertEqual(final["pending_count"], 0)
        self.assertEqual(final["state_counts"].get("selected_published"), 1)
        self.assertEqual(final["state_counts"].get("discarded"), 2)

    def test_candidate_synthesis_retry_closes_peers_after_partial_queue_transition(self):
        space = PREFIX + "candsynpartialq"
        member_a = PREFIX + "agent_cya3"
        member_b = PREFIX + "agent_cyb4"
        member_c = PREFIX + "agent_cyc5"
        make_space(space, [member_a, member_b, member_c])
        post = room_manager.post(space, "후보 합성 전이 복구", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        turn_id = orchestration.effect_id("parallel_turn_test", space, context["intent_id"], context["source_event_seq"])
        ids = []
        for member, reply in ((member_a, "A"), (member_b, "B"), (member_c, "C")):
            item = candidate_queue.enqueue_candidate(
                space,
                turn_id=turn_id,
                target_agent=member,
                manager_message="후보",
                reply=reply,
                context=context,
            )
            ids.append(item["event"]["candidate_id"])
        selected_ids = ids[:2]

        first = candidate_queue.mark_synthesized(
            space,
            selected_ids,
            actor="공간관리",
            reason="partial synthesis",
            public_summary="합성",
            publish_effect_id="effect_candidate_synth_partial",
            published_message_id="msg_candidate_synth_partial",
            discard_turn_peers=False,
        )
        mid = candidate_queue.snapshot(space)
        retry = candidate_queue.mark_synthesized(
            space,
            selected_ids,
            actor="공간관리",
            reason="retry synthesis",
            public_summary="합성",
            publish_effect_id="effect_candidate_synth_partial",
            published_message_id="msg_candidate_synth_partial",
            discard_turn_peers=True,
        )
        final = candidate_queue.snapshot(space)

        self.assertFalse(first["duplicate"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(mid["pending_count"], 1)
        self.assertEqual(final["pending_count"], 0)
        self.assertEqual(final["state_counts"].get("synthesized_published"), 2)
        self.assertEqual(final["state_counts"].get("discarded"), 1)

    def test_chat_agent_result_request_work_routes_to_task_without_public_reply(self):
        space = PREFIX + "chatwork"
        member = PREFIX + "agent_cw03"
        make_space(space, [member])
        post = room_manager.post(
            space,
            "이걸 작업으로 처리해줘",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-1",
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "작업 필요 여부를 판단해줘",
                    "reason": "대표가 작업을 요청했다",
                }, ensure_ascii=False)
            if cwd.parent.name == "작업":
                task_pack = json.loads((cwd / "task_pack.json").read_text(encoding="utf-8"))
                self.assertEqual(task_pack["requested_by"], f"chat_agent:{member}")
                self.assertEqual(task_pack["approved_by"], "space_manager_chat_request")
                (cwd / "결과.md").write_text("작업 결과", encoding="utf-8")
                (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
                return ""
            return json.dumps({
                "schema": "ChatAgentResult.v1",
                "wake_id": "wake-test",
                "room_generation_seen": context["room_generation"],
                "intent_id": context["intent_id"],
                "action": "request_work",
                "public_reply": "",
                "work_request": {
                    "objective": "대표 요청을 실제 작업으로 처리한다.",
                    "constraints": ["결과는 ReleaseQueue 경유"],
                    "suggested_worker": member,
                },
                "manager_requests": [],
                "boundary_decision": {
                    "mode": "needs_work",
                    "reason": "작업이 필요함",
                },
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        activity_states = [row.get("상태") for row in status.get("activity") or []]
        self.assertTrue(result["ok"])
        self.assertEqual(assistant_rows, [])
        self.assertEqual(status["tasks"]["task_count"], 1)
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["release_queue"]["release_count"], 1)
        self.assertEqual(status["release_queue"]["pending_count"], 1)
        self.assertIn("chat_request_work_received", activity_states)
        self.assertEqual(status["response_obligations"]["open_count"], 1)
        self.assertEqual(status["response_obligations"]["state_counts"].get("delegated"), 1)
        delegated = status["response_obligations"]["open_items"][-1]
        self.assertEqual(delegated["state"], "delegated")
        self.assertEqual(delegated["assigned_to"], member)
        self.assertEqual(delegated["task_id"], status["tasks"]["latest_task_id"])

        release_id = status["release_queue"]["latest_release_id"]
        room_manager.approve_release(space, release_id, actor="대표", reason="테스트 승인")
        room_manager.publish_release(space, release_id, actor="대표")
        after_publish = room_manager.status(space)
        self.assertEqual(after_publish["response_obligations"]["open_count"], 0)
        self.assertEqual(after_publish["response_obligations"]["state_counts"].get("answered"), 1)
        self.assertIn("task_created_from_chat_request", activity_states)

    def test_request_work_needs_approval_gates_execution_until_approved(self):
        # 설계_작업계획승인.md P3: needs_approval=true면 계획만 등록·결재 대기, 작업 미실행.
        # 대표 승인(execute_approved_plan) 후에야 작업이 생성된다.
        from core import work_plan
        space = PREFIX + "planapproval"
        member = PREFIX + "agent_pa07"
        make_space(space, [member])
        post = room_manager.post(
            space, "지침을 바꾸는 작업을 해줘", run_manager=False,
            manager_requested=True, client_message_id="client-approval-1",
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        work_calls = {"n": 0}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass", "wake": member,
                    "message": "작업 필요 여부를 판단해줘", "reason": "대표 요청",
                }, ensure_ascii=False)
            if cwd.parent.name == "작업":
                work_calls["n"] += 1
                (cwd / "결과.md").write_text("작업 결과", encoding="utf-8")
                (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
                return ""
            return json.dumps({
                "schema": "ChatAgentResult.v1",
                "room_generation_seen": context["room_generation"],
                "intent_id": context["intent_id"],
                "action": "request_work",
                "public_reply": "이렇게 할게요: 지침 변경은 승인받고 진행하겠습니다.",
                "work_request": {
                    "objective": "law.md 지침을 수정한다",
                    "plan": ["1. 변경안 정리", "2. law.md 편집"],
                    "needs_approval": True,
                    "approval_reason": "지침 변경 포함",
                    "suggested_worker": member,
                },
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)

            status = room_manager.status(space)
            assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
            plan_snap = work_plan.snapshot(space)
            approval = room_manager.read_approval_required(space)
            # 게이트: 작업 미생성, 계획은 결재 대기, 결재 말풍선 공개, 의무 assigned
            self.assertTrue(result["ok"])
            self.assertEqual(work_calls["n"], 0)
            self.assertEqual(status["tasks"]["task_count"], 0)
            self.assertEqual(plan_snap["representative_pending_count"], 1)
            self.assertEqual(len(approval.get("pending") or []), 1)
            self.assertEqual(len(assistant_rows), 1)
            self.assertIn("이렇게 할게요", assistant_rows[0]["내용"])
            self.assertEqual(status["response_obligations"]["state_counts"].get("assigned"), 1)

            # 대표 승인 → 실행
            plan_id = plan_snap["representative_pending_items"][0]["plan_id"]
            room_manager.execute_approved_plan(space, plan_id, actor="대표", context=context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        after = room_manager.status(space)
        self.assertEqual(work_calls["n"], 1)
        self.assertEqual(after["tasks"]["task_count"], 1)
        self.assertEqual(work_plan.get(space, plan_id)["state"], work_plan.DONE)
        self.assertEqual(len((room_manager.read_approval_required(space).get("pending") or [])), 0)

    def test_plan_approval_router_endpoints(self):
        # 설계_작업계획승인.md P5: 결재 라우터 — GET approvals / approve(큐) / reject.
        from core import work_plan
        space = PREFIX + "planrouter"
        member = PREFIX + "agent_pr08"
        make_space(space, [member])
        reg = work_plan.register(
            space, requesting_agent=member, worker=member,
            objective="대외비 문서를 외부에 공유", plan_steps=["1. 공유"],
            assessment=work_plan.assess_approval("대외비 문서를 외부에 공유", ["1. 공유"], None),
        )
        plan = reg["record"]
        self.assertTrue(plan["needs_approval"])
        room_manager._mark_approval_required(space, plan)
        plan_id = plan["plan_id"]

        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)

        approvals = client.get(f"/api/spaces/{space}/approvals")
        self.assertEqual(approvals.status_code, 200)
        self.assertEqual(len(approvals.json().get("pending") or []), 1)

        with patch.object(room_manager, "execute_approved_plan", return_value="ok") as m:
            ap = client.post(f"/api/spaces/{space}/plans/{plan_id}/approve", json={"actor": "대표"})
        self.assertEqual(ap.status_code, 200)
        self.assertTrue(ap.json()["approved"])
        m.assert_called_once()

        reg2 = work_plan.register(
            space, requesting_agent=member, worker=member,
            objective="law.md 지침 수정", plan_steps=["1. 편집"],
            assessment=work_plan.assess_approval("law.md 지침 수정", ["1. 편집"], None),
            context={"intent_id": "intent_2"},
        )
        plan2_id = reg2["record"]["plan_id"]
        room_manager._mark_approval_required(space, reg2["record"])
        rj = client.post(f"/api/spaces/{space}/plans/{plan2_id}/reject", json={"actor": "대표", "reason": "필요없음"})
        self.assertEqual(rj.status_code, 200)
        self.assertEqual(work_plan.get(space, plan2_id)["state"], work_plan.REJECTED)
        remaining = [p["plan_id"] for p in (client.get(f"/api/spaces/{space}/approvals").json().get("pending") or [])]
        self.assertNotIn(plan2_id, remaining)

    def test_reflow_publishes_completed_results_and_skips_stale(self):
        # 설계_대화작업분리 Phase B: 완료된 (비동기) 작업 결과를 reflow가 대화로 공개, 세대불일치는 건너뜀.
        space = PREFIX + "reflow"
        member = PREFIX + "agent_rf01"
        make_space(space, [member])
        post = room_manager.post(space, "작업 결과 확인", run_manager=False, client_message_id="rf-c1")
        ctx = post["orchestration"]
        enqueue_test_release(space, "rel-ok", public_summary="완료된 결과물입니다", context=ctx)

        res = room_manager.reflow(space)
        self.assertEqual(res["published"], 1)
        assistant_rows = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertTrue(any("완료된 결과물" in (r.get("내용") or "") for r in assistant_rows))

        # 멱등: 더 공개할 것 없음
        self.assertEqual(room_manager.reflow(space)["published"], 0)

        # 늦은/취소(세대 불일치) 결과는 공개하지 않는다
        enqueue_test_release(space, "rel-stale", public_summary="늦은 결과물", context=ctx)
        orchestration.advance_generation(space, "대표 새 입력 시뮬", source_message_id="x")
        res3 = room_manager.reflow(space)
        self.assertEqual(res3["published"], 0)
        self.assertTrue(any(e.get("type") == "reflow_stale_skipped" for e in res3["events"]))
        self.assertEqual([r for r in read(space) if "늦은 결과물" in (r.get("내용") or "")], [])

    def test_reflow_publishes_result_mentioning_risk_words(self):
        # 회귀(실증 2026-06-29): 결과 보고서가 'law.md를 읽었다'고 *언급*해도 공개돼야 한다 —
        # 결과 텍스트 위험 스캔은 거의 모든 작업보고서를 오분류해 막았다. 방 공개=내부 메시지=저위험.
        # 위험은 작업 *시작*(work_plan)에서 게이트한다.
        space = PREFIX + "reflow_riskwords"
        make_space(space, [PREFIX + "agent_rf02"])
        post = room_manager.post(space, "작업해줘", run_manager=False, client_message_id="rfw-c1")
        ctx = post["orchestration"]
        summary = "# 작업 완료 보고서\n- [x] 필수 파일 읽기(law.md, law_work.md, 지시.md)\n- [x] 마크다운 작성·저장"
        enqueue_test_release(space, "rel-report", public_summary=summary, context=ctx)
        res = room_manager.reflow(space)
        self.assertEqual(res["published"], 1)                                   # 위험 단어 언급해도 공개됨
        rows = [r.get("내용") or "" for r in read(space) if r.get("역할") == "assistant"]
        self.assertTrue(any("작업 완료 보고서" in c for c in rows))

    def test_chat_agent_result_request_work_with_public_reply_announces_in_room_but_keeps_obligation_delegated(self):
        # 에이전트가 request_work를 하면서 public_reply도 남기면, 방에 착수 알림 말풍선이
        # 공개되어 협업이 보여야 한다. 단 작업은 아직 진행 중이므로 응답의무는 delegated로
        # 유지되어야 한다(착수 알림이 응답의무를 answered로 닫으면 안 됨).
        space = PREFIX + "chatworkannounce"
        member = PREFIX + "agent_cwa04"
        make_space(space, [member])
        post = room_manager.post(
            space,
            "이걸 작업으로 처리하되 방에 계획도 알려줘",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-1",
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        announce_text = "[기획] 요구사항을 이렇게 정리했고, 구현자에게 작업을 넘깁니다."

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "정리하고 필요하면 작업으로 넘겨줘",
                    "reason": "대표 요청",
                }, ensure_ascii=False)
            if cwd.parent.name == "작업":
                (cwd / "결과.md").write_text("작업 결과", encoding="utf-8")
                (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
                return ""
            return json.dumps({
                "schema": "ChatAgentResult.v1",
                "action": "request_work",
                "public_reply": announce_text,
                "work_request": {
                    "objective": "대표 요청을 실제 작업으로 처리한다.",
                    "constraints": ["결과는 ReleaseQueue 경유"],
                    "suggested_worker": member,
                },
                "manager_requests": [],
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertTrue(result["ok"])
        # 착수 알림이 방에 에이전트 말풍선으로 공개됐다
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0].get("내용"), announce_text)
        self.assertEqual(assistant_rows[0].get("화자"), member.rsplit("_", 1)[0])
        # 작업은 생성됐다
        self.assertEqual(status["tasks"]["task_count"], 1)
        # 응답의무는 여전히 delegated (착수 알림이 닫지 않음)
        self.assertEqual(status["response_obligations"]["open_count"], 1)
        self.assertEqual(status["response_obligations"]["state_counts"].get("delegated"), 1)

    def test_workspace_intro_collaboration_demo_creates_shared_file_and_publishes_via_release_queue(self):
        space = PREFIX + "introcollab"
        planner = PREFIX + "agent_intro_plan"
        writer = PREFIX + "agent_intro_write"
        make_space(space, [planner, writer])
        shared_dir = SPACES / space / "공유파일"
        shared_dir.mkdir(parents=True, exist_ok=True)
        shared_file = shared_dir / "CnvAgentWorld_워크스페이스_소개.md"
        post = room_manager.post(
            space,
            "에이전트들이 협업해서 이 워크스페이스 소개 파일을 만들어줘.",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-intro-demo",
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        intro_text = (
            "# CnvAgentWorld 워크스페이스 소개\n\n"
            "CnvAgentWorld는 에이전트, 공간, 공간관리자, 작업 실행 폴더가 함께 움직이는 협업형 워크스페이스다.\n\n"
            "## 핵심 흐름\n"
            "- 대표가 공간 채팅에 요청을 남기면 공간관리자가 멤버 역할과 런타임을 보고 턴을 넘긴다.\n"
            "- 채팅에이전트는 필요하면 작업에이전트에게 일을 위임하도록 request_work를 반환한다.\n"
            "- 작업 결과는 바로 방에 공개되지 않고 ReleaseQueue 승인 흐름을 거쳐 공개된다.\n"
        )

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": planner,
                    "message": "워크스페이스 소개 파일 작성이 필요한지 판단하고, 필요하면 작업에이전트에게 위임해줘.",
                    "reason": "대표가 협업 산출물 파일 생성을 요청했다",
                }, ensure_ascii=False)
            if cwd.parent.name == "작업":
                task_pack = json.loads((cwd / "task_pack.json").read_text(encoding="utf-8"))
                self.assertEqual(task_pack["requested_by"], f"chat_agent:{planner}")
                self.assertEqual(task_pack["approved_by"], "space_manager_chat_request")
                self.assertEqual(task_pack["worker_agent"], writer)
                self.assertIn("워크스페이스 소개", task_pack["objective"])
                (cwd / "결과.md").write_text(intro_text, encoding="utf-8")
                (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
                shared_file.write_text(intro_text, encoding="utf-8")
                return "소개 파일 작성 완료"
            return json.dumps({
                "schema": "ChatAgentResult.v1",
                "action": "request_work",
                "public_reply": "",
                "work_request": {
                    "objective": (
                        "CnvAgentWorld 워크스페이스 소개 파일을 작성하라. "
                        "결과.md와 공간 공유파일/CnvAgentWorld_워크스페이스_소개.md에 같은 내용을 남겨라."
                    ),
                    "suggested_worker": writer,
                    "constraints": [
                        "기존 파일을 덮어쓰지 말고 현재 작업 공간의 공유파일에 산출물을 남긴다.",
                        "작업 결과는 ReleaseQueue 승인 전까지 방에 직접 공개하지 않는다.",
                    ],
                },
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "workspace intro collaboration demo", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertTrue(shared_file.exists())
        self.assertIn("CnvAgentWorld 워크스페이스 소개", shared_file.read_text(encoding="utf-8"))
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["tasks"]["task_count"], 1)
        self.assertEqual(status["tasks"]["latest_worker"], writer)
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["release_queue"]["pending_count"], 1)
        release_id = status["release_queue"]["latest_release_id"]
        task_id = status["tasks"]["latest_task_id"]
        pending = status["release_queue"]["pending_items"][-1]
        self.assertEqual(pending["release_id"], release_id)
        self.assertEqual(pending["source_task_id"], task_id)
        self.assertTrue(pending["publish_blocked_until_approval"])
        self.assertEqual(pending["approval_state"], "pending")
        room_manager.approve_release(space, release_id, actor="대표", reason="소개 파일 실증 승인")
        room_manager.publish_release(space, release_id, actor="공간관리")
        after_publish = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertEqual(after_publish["release_queue"]["latest_state"], "published")
        self.assertEqual(after_publish["response_obligations"]["open_count"], 0)
        self.assertEqual(len(assistant_rows), 1)
        published = assistant_rows[0]
        self.assertIn("CnvAgentWorld 워크스페이스 소개", published["내용"])
        self.assertEqual(published["release_id"], release_id)
        self.assertEqual(published["source_task_id"], task_id)
        self.assertEqual(published["task_pack_id"], pending["task_pack_id"])
        self.assertEqual(published["publish_effect_id"], after_publish["release_queue"]["items"][-1]["publish_effect_id"])

    def test_space_manager_salvages_decision_json_with_surrounding_prose(self):
        # 계약 변경(2026-06, Gemini 호환): 응답에 산문이 섞여도 'action'을 가진 결정 JSON을 구제한다.
        # (Gemini/Antigravity가 설명을 덧붙여 엄격 파싱이 매번 거부→manager_failed로 막히던 근본원인 해소.)
        space = PREFIX + "strictjson"
        make_space(space)
        post = room_manager.post(space, "JSON 산문 구제 테스트", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        prompts = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            prompts.append(prompt)
            return ('네, 토론이 충분해 보이니 대표님께 넘기겠습니다.\n'
                    '{"action":"stop","wake":"","message":"","reason":"산문 뒤 결정 JSON"}\n'
                    '필요하면 더 도와드릴게요.')

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "salvage json", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        # 산문이 앞뒤로 있어도 한 번에 구제 — 재시도 없이 결정 채택.
        self.assertTrue(result["ok"])
        self.assertEqual(result["events"][0]["attempts"], 1)
        self.assertEqual(result["decision"]["reason"], "산문 뒤 결정 JSON")
        self.assertEqual(len(prompts), 1)

    def test_manager_decision_salvage_picks_last_action_json(self):
        # salvage는 'action'을 가진 마지막 JSON을 고른다(실제 결정은 보통 끝에 온다). action 없는 JSON은 무시.
        text = ('우선 {"note":"이건 결정 아님"} 같은 메모가 있고\n'
                '{"action":"pass","wake":"멤버","message":"먼저 초안","reason":"초안"}\n'
                '최종: {"action":"stop","wake":"","message":"","reason":"최종 결정"}')
        out = room_manager._extract_decision_json(text)
        self.assertEqual(out.get("action"), "stop")
        self.assertEqual(out.get("reason"), "최종 결정")
        # 완전 비-JSON / action 없는 응답은 여전히 빈 dict → 재시도 유지.
        self.assertEqual(room_manager._extract_decision_json("그냥 설명만 합니다."), {})
        self.assertEqual(room_manager._extract_decision_json('{"note":"action 없음"}'), {"note": "action 없음"})

    def test_space_manager_prompt_includes_member_role_and_runtime_profiles(self):
        space = PREFIX + "roster"
        member = PREFIX + "agent_role01"
        make_space(space, [member])
        (PEOPLE / member / "role.md").write_text(
            "# 역할\n\n분석 담당 에이전트. 구조 검토와 리스크 점검을 우선한다.\n",
            encoding="utf-8",
        )
        runtime.write_runtime(PEOPLE / member / "공간" / space, "codex", "gpt-5.4-mini", source="test-seat")
        post = room_manager.post(space, "누가 답하면 좋을까", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        manager_prompts = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                manager_prompts.append(prompt)
                return json.dumps({
                    "action": "stop",
                    "wake": "",
                    "message": "",
                    "reason": "프로필 확인 테스트",
                }, ensure_ascii=False)
            return "unexpected"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        self.assertEqual(len(manager_prompts), 1)
        self.assertIn("멤버 프로필과 런타임", manager_prompts[0])
        self.assertIn("분석 담당 에이전트", manager_prompts[0])
        self.assertIn("seat_runtime", manager_prompts[0])
        self.assertIn("codex", manager_prompts[0])
        self.assertIn("gpt-5.4-mini", manager_prompts[0])
        self.assertIn("agent_runtime_file", manager_prompts[0])
        self.assertIn('"role_status": "ok"', manager_prompts[0])
        self.assertIn("RoomSourceHealth.prompt.v1", manager_prompts[0])
        self.assertIn("RoomStatusSnapshot.prompt.v1", manager_prompts[0])
        self.assertIn("release_queue", manager_prompts[0])
        self.assertIn("tasks", manager_prompts[0])
        self.assertIn("## 출력 계약", manager_prompts[0])
        self.assertIn("전체 응답은 유효한 JSON 객체 하나만 허용된다", manager_prompts[0])
        self.assertLess(manager_prompts[0].index("## 출력 계약"), manager_prompts[0].index("## 이번 이벤트"))
        self.assertIn("## 운영 원칙", manager_prompts[0])
        self.assertIn("일상 대화, 인사, 짧은 질문", manager_prompts[0])
        self.assertIn("병렬 의견/작업 분배", manager_prompts[0])

    def test_space_manager_prompt_marks_member_profile_fallbacks(self):
        space = PREFIX + "rosterbad"
        member = PREFIX + "agent_rolebad"
        make_space(space, [member])
        (PEOPLE / member / "공간" / space / "agent_runtime.json").write_text("{bad json}\n", encoding="utf-8")
        post = room_manager.post(space, "런타임이 깨졌을 때도 검토 가능해야 해", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        manager_prompts = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                manager_prompts.append(prompt)
                return json.dumps({
                    "action": "stop",
                    "wake": "",
                    "message": "",
                    "reason": "프로필 fallback 확인",
                }, ensure_ascii=False)
            return "unexpected"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        self.assertEqual(len(manager_prompts), 1)
        self.assertIn("멤버 프로필과 런타임", manager_prompts[0])
        self.assertIn('"role_status": "missing"', manager_prompts[0])
        self.assertIn("default_read_error", manager_prompts[0])
        self.assertIn("runtime_error", manager_prompts[0])

    def test_space_manager_prompt_marks_members_json_source_health(self):
        space = PREFIX + "membersbad"
        make_space(space)
        (SPACES / space / "멤버.json").write_text("{bad json}\n", encoding="utf-8")
        post = room_manager.post(space, "멤버 파일 상태도 보여야 해", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        manager_prompts = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                manager_prompts.append(prompt)
                return json.dumps({
                    "action": "stop",
                    "wake": "",
                    "message": "",
                    "reason": "멤버 파일 손상 확인",
                }, ensure_ascii=False)
            return "unexpected"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        self.assertEqual(len(manager_prompts), 1)
        self.assertIn("RoomSourceHealth.prompt.v1", manager_prompts[0])
        self.assertIn('"members_json"', manager_prompts[0])
        self.assertIn('"status": "read_error"', manager_prompts[0])

    def test_active_lessons_are_injected_into_context_pack_and_prompt(self):
        space = PREFIX + "lessoninject"
        member = PREFIX + "agent_li01"
        make_space(space, [member])
        post = room_manager.post(space, "하이", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "style_rule",
                "scope": "space",
                "status": "active",
                "instruction": "답변은 항상 핵심 결론을 먼저 말한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
            },
        )
        lesson_id = lesson["record"]["created_lesson_ids"][0]
        original_run_engine = room_manager.engine.run_engine
        prompts = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "인사에 답해줘",
                    "reason": "대표가 인사를 했다",
                }, ensure_ascii=False)
            prompts.append(prompt)
            return "핵심 결론부터 답합니다."

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["ok"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("LessonPack.v1", prompts[0])
        self.assertIn("답변은 항상 핵심 결론을 먼저 말한다.", prompts[0])
        self.assertIn(lesson_id, status["context_packs"]["latest_included_lessons"])
        self.assertEqual(status["context_packs"]["latest_lesson_pack_status"], "ok")

    def test_candidate_correction_lesson_is_reference_only_in_context_pack(self):
        space = PREFIX + "lessonref"
        member = PREFIX + "agent_lr02"
        make_space(space, [member])
        post = room_manager.post(space, "취소하고 다시 해", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "anti_pattern",
                "scope": "space",
                "status": "candidate",
                "instruction": "취소 요청 이후에는 이전 세대 결과를 자동 공개하지 않는다.",
                "evidence_level": "user_directive",
                "confidence": 0.8,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": ["취소"]},
            },
        )
        lesson_id = lesson["record"]["created_lesson_ids"][0]

        pack = context_pack.build_context_pack(
            space,
            mode="chat",
            event="취소 요청 처리",
            context=context,
            target_agent=member,
        )
        lesson_pack = pack["lesson_pack"]
        self.assertEqual(lesson_pack["lesson_pack_status"], "ok")
        self.assertIn(lesson_id, [row["lesson_id"] for row in lesson_pack["reference_only"]])
        self.assertIn(lesson_id, [row["lesson_id"] for row in lesson_pack["recent_correction_lessons"]])
        self.assertNotIn(lesson_id, [row["lesson_id"] for row in lesson_pack["must_apply"]])

    def test_must_apply_lessons_are_prioritized_with_budget_marker(self):
        space = PREFIX + "lessonbudget"
        make_space(space)
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        rows = []
        for idx in range(4):
            rows.append({
                "schema": "LessonLedger.v1",
                "lesson_id": f"lesson_must_{idx}",
                "kind": "contract_rule",
                "scope": "space",
                "status": "active",
                "instruction": f"must lesson {idx}",
                "application_level": "must_apply",
                "must_apply": True,
                "confidence": 0.9 - (idx * 0.05),
                "evidence_level": "user_directive",
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
                "created_at": f"2026-06-26T00:00:0{idx}",
            })
        for idx in range(2):
            rows.append({
                "schema": "LessonLedger.v1",
                "lesson_id": f"lesson_may_{idx}",
                "kind": "style_rule",
                "scope": "space",
                "status": "active",
                "instruction": f"may lesson {idx}",
                "application_level": "may_apply",
                "confidence": 0.99,
                "evidence_level": "user_directive",
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
                "created_at": f"2026-06-26T00:01:0{idx}",
            })
        (learning / "lessons.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        pack = lesson_ledger.build_lesson_pack(
            space,
            mode="chat",
            context={},
            event="",
            max_lessons=3,
            max_must_apply=2,
        )

        self.assertEqual([row["lesson_id"] for row in pack["must_apply"]], ["lesson_must_0", "lesson_must_1"])
        self.assertEqual(len(pack["included_lessons"]), 3)
        self.assertTrue(any(row == {"lesson_id": "lesson_must_2", "reason": "must_apply_budget"} for row in pack["excluded_lessons"]))
        self.assertNotIn("lesson_must_2", [row["lesson_id"] for row in pack["active_space_lessons"]])

    def test_must_apply_lesson_report_is_stripped_and_recorded_before_publish(self):
        space = PREFIX + "lessonreport"
        member = PREFIX + "agent_lm03"
        make_space(space, [member])
        post = room_manager.post(space, "보고해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "contract_rule",
                "scope": "space",
                "status": "active",
                "instruction": "대표 요청에 답할 때는 적용한 레슨을 명시적으로 점검한다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
            },
        )
        lesson_id = lesson["record"]["created_lesson_ids"][0]
        original_run_engine = room_manager.engine.run_engine
        report = {
            "schema": "LessonApplicationReport.v1",
            "applications": [{
                "lesson_id": lesson_id,
                "applied": True,
                "not_applicable_reason": "",
                "how": "답변 작성 전에 must_apply 레슨을 점검했다.",
                "outcome": "success",
                "needs_lesson_update": False,
            }],
        }

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                "message": "보고해줘",
                "reason": "대표 요청 응답",
            }, ensure_ascii=False)
            self.assertIn("LessonApplicationReport.v1", prompt)
            return "공개 답변\n```json\n" + json.dumps(report, ensure_ascii=False) + "\n```"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertTrue(result["ok"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0]["내용"], "공개 답변")
        self.assertNotIn("LessonApplicationReport", assistant_rows[0]["내용"])
        self.assertEqual(status["learning"]["lesson_application_count"], 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_must_apply_lesson_missing_report_holds_publish(self):
        space = PREFIX + "lessonhold"
        member = PREFIX + "agent_lh04"
        make_space(space, [member])
        post = room_manager.post(space, "보고해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "contract_rule",
                "scope": "space",
                "status": "active",
                "instruction": "대표 요청에 답할 때는 적용한 레슨을 명시적으로 점검한다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
            },
        )
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "보고해줘",
                    "reason": "대표 요청 응답",
                }, ensure_ascii=False)
            return "보고만 하고 레슨 적용 보고는 누락"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertFalse(result["ok"])
        self.assertTrue(result["lesson_application_missing"])
        self.assertEqual(assistant_rows, [])
        self.assertEqual(status["상태"], "idle")
        self.assertEqual(status["last_action"], "lesson_application_missing")
        self.assertEqual(status["publish_ledger"]["counts"].get("committed", 0), 0)
        self.assertEqual(status["publish_ledger"]["counts"].get("claimed", 0), 0)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertEqual(status["publish_ledger"]["latest"], [])
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("rejected"), 1)
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("success"))
        self.assertTrue(any(row.get("상태") == "lesson_application_missing" for row in status["failures"]))

    def test_invalid_must_apply_report_holds_without_recording_application(self):
        space = PREFIX + "lessonbadapp"
        member = PREFIX + "agent_lb05"
        make_space(space, [member])
        post = room_manager.post(space, "보고해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "contract_rule",
                "scope": "space",
                "status": "active",
                "instruction": "must_apply 레슨은 적용 또는 비적용 사유를 보고한다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
            },
        )
        lesson_id = lesson["record"]["created_lesson_ids"][0]
        bad_report = {
            "schema": "LessonApplicationReport.v1",
            "applications": [{
                "lesson_id": lesson_id,
                "applied": False,
                "not_applicable_reason": "",
                "how": "",
                "outcome": "unclear",
            }],
        }
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "보고해줘",
                    "reason": "대표 요청 응답",
                }, ensure_ascii=False)
            return "공개하면 안 되는 답변\n" + json.dumps(bad_report, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertFalse(result["ok"])
        self.assertTrue(result["lesson_application_missing"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["learning"]["lesson_application_count"], 0)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)

    def test_lesson_pack_unavailable_holds_publish(self):
        space = PREFIX + "lessonunavail"
        member = PREFIX + "agent_lu06"
        make_space(space, [member])
        post = room_manager.post(space, "보고해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        (learning / "lessons.jsonl").write_text("{bad json}\n", encoding="utf-8")
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "보고해줘",
                    "reason": "대표 요청 응답",
                }, ensure_ascii=False)
            return "lesson pack unavailable 상태에서는 공개되면 안 됨"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertFalse(result["ok"])
        self.assertTrue(result["lesson_application_missing"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)
        self.assertTrue(status["learning"]["ledger_corrupt"])
        self.assertEqual(status["context_packs"]["latest_lesson_pack_status"], "unavailable")

    def test_must_apply_hold_releases_claim_and_redrives_new_input(self):
        space = PREFIX + "lessonredrive"
        member = PREFIX + "agent_lr07"
        make_space(space, [member])
        post = room_manager.post(space, "첫 요청", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            context=context,
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "contract_rule",
                "scope": "space",
                "status": "active",
                "instruction": "must_apply 레슨은 적용 보고 없이는 공개하지 않는다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["chat"], "keywords": []},
            },
        )
        original_run_engine = room_manager.engine.run_engine
        manager_calls = {"count": 0}
        agent_posts = {"done": False}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                manager_calls["count"] += 1
                if manager_calls["count"] == 1:
                    return json.dumps({
                        "action": "pass",
                        "wake": member,
                        "message": "첫 요청 응답",
                        "reason": "대표 요청 응답",
                    }, ensure_ascii=False)
                return json.dumps({
                    "action": "stop",
                    "wake": "",
                    "message": "",
                    "reason": "redrive된 새 입력은 여기서 멈춤",
                }, ensure_ascii=False)
            if not agent_posts["done"]:
                agent_posts["done"] = True
                room_manager.post(space, "새 요청", run_manager=True, client_message_id="client-2")
            return "보고 누락 응답"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        event_types = [event.get("type") for event in result.get("events", [])]
        self.assertFalse(result["ok"])
        self.assertTrue(result["lesson_application_missing"])
        self.assertIn("manager_claim_released", event_types)
        self.assertIn("manager_redrive_started", event_types)
        self.assertFalse(status["manager_claim"].get("active"))
        self.assertEqual(manager_calls["count"], 2)
        self.assertEqual(len([row for row in read(space) if row.get("역할") == "user"]), 2)
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertTrue(any(row.get("상태") == "lesson_application_missing" for row in status["failures"]))
        self.assertTrue(any(row.get("recovery_action") for row in status["failures"]))

    def test_rapid_input_redrive_preserves_all_inputs_in_status_and_prompt(self):
        space = PREFIX + "rapidinput"
        make_space(space)
        first = room_manager.post(space, "첫 입력", run_manager=False, client_message_id="rapid-first")
        context = first["orchestration"]
        manager_started = threading.Event()
        release_manager = threading.Event()
        prompts = []
        results = []
        errors = []
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                prompts.append(prompt)
                if len(prompts) == 1:
                    manager_started.set()
                    if not release_manager.wait(5):
                        raise TimeoutError("manager block was not released")
                return json.dumps({
                    "action": "stop",
                    "wake": "",
                    "message": "",
                    "reason": "rapid input test stop",
                }, ensure_ascii=False)
            return "unexpected agent wake"

        def run_manager():
            try:
                results.append(room_manager.tick(space, "대표가 방에 메시지를 남김: 첫 입력", context))
            except Exception as exc:
                errors.append(exc)

        try:
            room_manager.engine.run_engine = fake_run_engine
            thread = threading.Thread(target=run_manager)
            thread.start()
            self.assertTrue(manager_started.wait(5), "manager did not start")

            rapid_posts = [
                room_manager.post(
                    space,
                    f"빠른 입력 {idx}",
                    run_manager=True,
                    client_message_id=f"rapid-client-{idx}",
                )
                for idx in range(1, 4)
            ]
            active_status = room_manager.status(space)
            rapid = active_status["rapid_input"]
            previews = " ".join(item.get("text_preview", "") for item in rapid.get("pending_items") or [])
            self.assertTrue(active_status["manager_claim"]["active"])
            self.assertTrue(active_status["manager_redrive_required"])
            self.assertGreaterEqual(rapid["pending_input_count"], 3)
            self.assertIn("빠른 입력 1", previews)
            self.assertIn("빠른 입력 2", previews)
            self.assertIn("빠른 입력 3", previews)
            self.assertTrue(all(post["ok"] for post in rapid_posts))
            self.assertEqual(sum(1 for post in rapid_posts if post["ack"]["duplicate"]), 0)

            release_manager.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "manager thread did not finish")
        finally:
            release_manager.set()
            room_manager.engine.run_engine = original_run_engine

        if errors:
            raise errors[0]
        self.assertTrue(results and results[0]["ok"])
        event_types = [event.get("type") for event in results[0].get("events", [])]
        self.assertIn("manager_redrive_started", event_types)
        redrive_events = [
            event for event in results[0].get("events", [])
            if event.get("type") == "manager_redrive_started"
        ]
        self.assertTrue(any(event.get("coalesced_pending_count", 0) >= 3 for event in redrive_events))
        self.assertGreaterEqual(len(prompts), 2)
        redrive_prompt = prompts[1]
        self.assertIn("빠른 연속 입력 묶음", redrive_prompt)
        self.assertIn("빠른 입력 1", redrive_prompt)
        self.assertIn("빠른 입력 2", redrive_prompt)
        self.assertIn("빠른 입력 3", redrive_prompt)

    def test_duplicate_retry_recovers_when_read_boundary_only_covered_event(self):
        space = PREFIX + "duprecover"
        make_space(space)
        first = room_manager.post(space, "A 입력", run_manager=False, client_message_id="recover-a")
        second = room_manager.post(
            space,
            "B 입력",
            run_manager=False,
            client_message_id="recover-b",
            manager_requested=True,
        )
        room_manager.queue_manager(space, "A 입력 기준 큐", first["orchestration"])
        tick_calls = []
        original_tick = room_manager.tick

        def fake_tick(space_arg, event, context=None, **kwargs):
            tick_calls.append({"space": space_arg, "event": event, "context": context or {}})
            return {"ok": True, "events": [{"type": "fake_recovery_tick"}]}

        try:
            room_manager.tick = fake_tick
            duplicate = room_manager.post(
                space,
                "B 입력 재전송",
                run_manager=True,
                client_message_id="recover-b",
            )
        finally:
            room_manager.tick = original_tick

        self.assertTrue(duplicate["ack"]["duplicate"])
        self.assertEqual(duplicate["ack"]["message_id"], second["ack"]["message_id"])
        self.assertTrue(duplicate["manager_recovery_needed"])
        self.assertTrue(tick_calls)
        self.assertTrue(any(event.get("type") == "manager_recovery_needed" for event in duplicate["events"]))
        self.assertEqual(tick_calls[0]["context"]["source_event_seq"], second["ack"]["event_seq"])

    def test_learning_capture_failure_does_not_block_publish(self):
        space = PREFIX + "learnfailpublish"
        member = PREFIX + "agent_l777"
        make_space(space, [member])
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        original_is_current = room_manager.manager_claim.is_current
        original_record_eval = room_manager.lesson_ledger.record_post_interaction_evaluation

        def fail_eval(*args, **kwargs):
            raise RuntimeError("learning ledger unavailable")

        try:
            room_manager.engine.run_engine = lambda *args, **kwargs: "reply"
            room_manager.manager_claim.is_current = lambda *args, **kwargs: True
            room_manager.lesson_ledger.record_post_interaction_evaluation = fail_eval
            room_manager._run_agent_turn(space, member, "do it", {
                "claim_token": "claim-a",
                "fencing_token": "fence-a",
                "owner_boot_id": "boot-a",
            }, context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.manager_claim.is_current = original_is_current
            room_manager.lesson_ledger.record_post_interaction_evaluation = original_record_eval

        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        status = room_manager.status(space)
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 0)
        self.assertTrue(any(row.get("상태") == "learning_capture_failed" for row in status["activity"]))

    def test_publish_rejection_records_failed_evaluation_without_public_row(self):
        space = PREFIX + "publishrejecteval"
        member = PREFIX + "agent_r888"
        make_space(space, [member])
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        original_append = room_manager.publish_ledger.append_public_message

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "do it",
                    "reason": "test",
                }, ensure_ascii=False)
            return "reply"

        def reject_publish(*args, **kwargs):
            raise publish_ledger.PublishLedgerError("simulated publish rejection")

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.publish_ledger.append_public_message = reject_publish
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.publish_ledger.append_public_message = original_append

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertTrue(result["ok"])
        self.assertEqual(status["last_action"], "wake_failed")
        self.assertEqual(assistant_rows, [])
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("failed"), 1)
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("success"))

    def test_wake_failed_reopens_obligation_for_resweep(self):
        # 대표 신고 흐름 근본수정: 지목 에이전트 턴이 실패(wake_failed: 엔진 타임아웃/공개 거부 등)하면
        # 'assigned'로 잡아둔 응답의무를 'open'으로 되돌려 미응답 sweep이 재구동하게 한다(자가치유).
        # 안 되돌리면 대표 메시지가 assigned 고아로 남아 영영 무응답·무재시도로 스트랜드된다.
        space = PREFIX + "wakefailreopen"
        member = PREFIX + "agent_wf01"
        make_space(space, [member])
        # run_manager=False(자동 tick 안 함)로 두되 manager_requested=True로 응답의무는 연다.
        post = room_manager.post(space, "질문 하나 할게요", run_manager=False, manager_requested=True, client_message_id="c-wf1")
        context = post["orchestration"]
        # 의무가 실제로 열렸는지 선확인(전제)
        self.assertEqual(response_obligation.snapshot(space)["state_counts"].get("open"), 1)
        original_run_engine = room_manager.engine.run_engine
        original_append = room_manager.publish_ledger.append_public_message

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps({"action": "pass", "wake": member, "message": "답해줘", "reason": "t"}, ensure_ascii=False)
            return "reply"

        def reject_publish(*args, **kwargs):
            raise publish_ledger.PublishLedgerError("simulated engine timeout / publish fail")

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.publish_ledger.append_public_message = reject_publish
            room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.publish_ledger.append_public_message = original_append

        status = room_manager.status(space)
        self.assertEqual(status["last_action"], "wake_failed")
        # 의무가 'assigned' 고아가 아니라 'open'으로 되돌려져 미응답 sweep 재구동 대상이 된다
        counts = status["response_obligations"]["state_counts"]
        self.assertEqual(counts.get("open"), 1)
        self.assertIsNone(counts.get("assigned"))
        self.assertEqual(len(room_manager._open_user_obligations(space)), 1)

    def test_publish_stale_guard_race_records_superseded_not_failed(self):
        space = PREFIX + "publishstalerace"
        member = PREFIX + "agent_s889"
        make_space(space, [member])
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine
        original_append = room_manager.publish_ledger.append_public_message

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "do it",
                    "reason": "test",
                }, ensure_ascii=False)
            return "reply"

        def stale_publish(*args, **kwargs):
            raise publish_ledger.PublishLedgerError("intent_stale_guard_failed")

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.publish_ledger.append_public_message = stale_publish
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine
            room_manager.publish_ledger.append_public_message = original_append

        status = room_manager.status(space)
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(status["last_action"], "stale_agent_reply")
        self.assertEqual(assistant_rows, [])
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("superseded"), 1)
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("failed"))
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("success"))

    def test_manager_stop_does_not_create_agent_wake_pack_or_publish(self):
        space = PREFIX + "stopnopack"
        member = PREFIX + "agent_d004"
        make_space(space, [member])
        post = room_manager.post(
            space,
            "멈춰도 되는 상황",
            run_manager=False,
            manager_requested=True,
            client_message_id="client-1",
        )
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn("ContextPack.compat_minimal.v1", prompt)
            return json.dumps({
                "action": "stop",
                "wake": "",
                "message": "",
                "reason": "추가 턴이 필요 없음",
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        self.assertTrue(result["ok"])
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        status = room_manager.status(space)
        self.assertEqual(status["상태"], "idle")
        self.assertEqual(status["last_action"], "stop")
        self.assertGreaterEqual(status["context_packs"]["delivery_counts"].get("manager_tick", 0), 1)
        self.assertEqual(status["context_packs"]["delivery_counts"].get("agent_wake", 0), 0)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed", 0), 0)
        self.assertEqual(status["learning"]["post_interaction_evaluation_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("success"), 1)
        self.assertEqual(status["response_obligations"]["open_count"], 0)
        self.assertEqual(status["response_obligations"]["state_counts"].get("manager_closed"), 1)

    def test_status_survives_corrupt_context_pack_ledgers(self):
        space = PREFIX + "ctxcorrupt"
        make_space(space)
        room_manager.post(space, "hello", run_manager=False, client_message_id="client-1")
        (SPACES / space / "context_packs.jsonl").write_bytes(b"\xff\xfe\xfa")
        (SPACES / space / "wake_pack_manifest.jsonl").write_bytes(b"\xff\xfe\xfb")
        (SPACES / space / "north_star_goal_ledger.jsonl").write_bytes(b"\xff\xfe\xfc")

        status = room_manager.status(space)

        self.assertTrue(status["context_packs"]["ledger_corrupt"])
        self.assertTrue(status["context_packs"]["ledger_errors"])
        self.assertTrue(any(f.get("상태") == "context_pack_ledger_corrupt" for f in status["failures"]))
        self.assertTrue(any("context/wake pack ledger" in action for action in status["recovery_actions"]))

    def test_status_marks_invalid_json_context_pack_lines_corrupt(self):
        space = PREFIX + "ctxbadjson"
        make_space(space)
        (SPACES / space / "context_packs.jsonl").write_text(
            '{"context_pack_id":"ok"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["context_packs"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["context_packs"]["ledger_errors"]))
        self.assertTrue(any(f.get("상태") == "context_pack_ledger_corrupt" for f in status["failures"]))

    def test_context_pack_snapshot_keeps_legacy_turn_handoff_manifest_readable(self):
        space = PREFIX + "ctxlegacyhandoff"
        make_space(space)
        (SPACES / space / "context_packs.jsonl").write_text(
            json.dumps({
                "schema": "ContextPack.compat_minimal.v1",
                "context_pack_id": "ctx_legacy",
                "context_pack_checksum": "checksum_legacy",
                "lesson_pack": {"lesson_pack_status": "ok", "included_lessons": []},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (SPACES / space / "wake_pack_manifest.jsonl").write_text(
            json.dumps({
                "schema": "WakePackManifest.v1",
                "manifest_id": "manifest_legacy",
                "state": "context_delivered",
                "delivered_at": "2026-06-27T00:00:00+09:00",
                "space_id": space,
                "recipient": "agent_legacy",
                "delivery_type": "agent_wake",
                "context_pack_id": "ctx_legacy",
                "context_pack_checksum": "checksum_legacy",
                "wake_id": "wake_legacy",
                "turn_handoff_id": "turn_legacy",
                "turn_handoff_checksum": "turn_checksum_legacy",
                "source_event_seq": 7,
                "source_message_id": "msg_legacy",
                "lesson_pack_status": "ok",
                "included_lessons": [],
                "must_apply_lessons": [],
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        snap = context_pack.snapshot(space)

        self.assertEqual(snap["turn_handoff_count"], 1)
        self.assertEqual(snap["latest_turn_handoff_id"], "turn_legacy")
        self.assertEqual(snap["latest_turn_handoff"]["schema"], "TurnHandoffObservation.legacy_manifest.v1")
        self.assertEqual(snap["latest_turn_handoff"]["target_agent"], "agent_legacy")
        self.assertEqual(snap["latest_turn_handoff"]["source_event_seq"], 7)
        self.assertFalse(snap["ledger_corrupt"])

    def test_status_survives_corrupt_learning_ledgers(self):
        space = PREFIX + "learncorrupt"
        make_space(space)
        room_manager.post(space, "hello", run_manager=False, client_message_id="client-1")
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        (learning / "lessons.jsonl").write_bytes(b"\xff\xfe\xfa")
        (learning / "lesson_applications.jsonl").write_bytes(b"\xff\xfe\xfb")
        (learning / "post_interaction_evaluations.jsonl").write_bytes(b"\xff\xfe\xfc")
        (learning / "post_task_evaluations.jsonl").write_bytes(b"\xff\xfe\xfd")

        status = room_manager.status(space)

        self.assertTrue(status["learning"]["ledger_corrupt"])
        self.assertTrue(status["learning"]["ledger_errors"])
        self.assertTrue(any(f.get("상태") == "lesson_ledger_corrupt" for f in status["failures"]))
        self.assertTrue(any("learning ledger" in action for action in status["recovery_actions"]))

    def test_status_marks_invalid_json_learning_lines_corrupt(self):
        space = PREFIX + "learnbadjson"
        make_space(space)
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        (learning / "post_interaction_evaluations.jsonl").write_text(
            '{"evaluation_id":"ok","outcome":"success"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["learning"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["learning"]["ledger_errors"]))

    def test_append_public_message_rejects_without_publish_ledger_contract(self):
        space = PREFIX + "publishreject"
        make_space(space)
        with self.assertRaises(publish_ledger.PublishLedgerError):
            publish_ledger.append_public_message(
                space,
                publish_effect_id="",
                publish_ledger_claim="",
                manager_claim_token="",
                published_message_id="",
                intent_stale_guard_passed=False,
                speaker_name="agent",
                speaker_code="a001",
                role="assistant",
                content="blocked",
                context={},
            )
        self.assertEqual(read(space), [])

    def test_publish_ledger_reconciles_after_append_before_commit_crash(self):
        space = PREFIX + "publishreconcile"
        make_space(space)
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_is_current = publish_ledger.manager_claim.is_current
        effect_id = orchestration.effect_id(
            "agent_reply",
            space,
            "agent_a001",
            context["intent_id"],
            context["source_event_seq"],
            context["source_message_id"],
        )
        manager_claim_context = {
            "claim_token": "claim-a",
            "fencing_token": "fence-a",
            "owner_boot_id": "boot-a",
        }
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            claim = publish_ledger.claim_publish(
                space,
                publish_effect_id=effect_id,
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                context=context,
                publisher="space_manager",
                speaker="agent_a001",
            )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current
        original_append_jsonl = publish_ledger._append_jsonl

        def crash_on_commit(path, data):
            if data.get("state") == "committed":
                raise RuntimeError("simulated crash after transcript append")
            return original_append_jsonl(path, data)

        try:
            publish_ledger._append_jsonl = crash_on_commit
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            with self.assertRaises(RuntimeError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
        finally:
            publish_ledger._append_jsonl = original_append_jsonl
            publish_ledger.manager_claim.is_current = original_is_current

        self.assertEqual(len([row for row in read(space) if row.get("역할") == "assistant"]), 1)
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            result = publish_ledger.append_public_message(
                space,
                publish_effect_id=effect_id,
                publish_ledger_claim=claim["publish_ledger_claim"],
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                published_message_id=claim["published_message_id"],
                intent_stale_guard_passed=True,
                speaker_name="agent",
                speaker_code="a001",
                role="assistant",
                content="reply",
                context=context,
            )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current
        self.assertTrue(result["duplicate"])
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(room_manager.status(space)["publish_ledger"]["counts"].get("committed"), 1)

    def test_publish_ledger_rejects_wrong_contract_and_stale_context(self):
        space = PREFIX + "publishcontract"
        make_space(space)
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        effect_id = orchestration.effect_id(
            "agent_reply",
            space,
            "agent_a001",
            context["intent_id"],
            context["source_event_seq"],
            context["source_message_id"],
        )
        manager_claim_context = {
            "claim_token": "claim-a",
            "fencing_token": "fence-a",
            "owner_boot_id": "boot-a",
        }
        original_is_current = publish_ledger.manager_claim.is_current
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            claim = publish_ledger.claim_publish(
                space,
                publish_effect_id=effect_id,
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                context=context,
                publisher="space_manager",
                speaker="agent_a001",
            )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim="wrong-pledge",
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="wrong-claim",
                    manager_claim_context={**manager_claim_context, "claim_token": "wrong-claim"},
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id="msg_pub_wrong",
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            orchestration.advance_generation(space, "test_stale_publish")
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])

    def test_publish_ledger_rejects_when_manager_claim_not_current(self):
        space = PREFIX + "publishclaim"
        make_space(space)
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        effect_id = orchestration.effect_id("agent_reply", space, "agent_a001", context["intent_id"])
        manager_claim_context = {
            "claim_token": "claim-a",
            "fencing_token": "fence-a",
            "owner_boot_id": "boot-a",
        }
        original_is_current = publish_ledger.manager_claim.is_current
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: False
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.claim_publish(
                    space,
                    publish_effect_id=effect_id,
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    context=context,
                    publisher="space_manager",
                    speaker="agent_a001",
                )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current
        self.assertEqual(read(space)[-1]["역할"], "user")

    def test_committed_publish_rejects_wrong_duplicate_contract(self):
        space = PREFIX + "committedwrong"
        make_space(space)
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        effect_id = orchestration.effect_id("agent_reply", space, "agent_a001", context["intent_id"])
        manager_claim_context = {
            "claim_token": "claim-a",
            "fencing_token": "fence-a",
            "owner_boot_id": "boot-a",
        }
        original_is_current = publish_ledger.manager_claim.is_current
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            claim = publish_ledger.claim_publish(
                space,
                publish_effect_id=effect_id,
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                context=context,
                publisher="space_manager",
                speaker="agent_a001",
            )
            publish_ledger.append_public_message(
                space,
                publish_effect_id=effect_id,
                publish_ledger_claim=claim["publish_ledger_claim"],
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                published_message_id=claim["published_message_id"],
                intent_stale_guard_passed=True,
                speaker_name="agent",
                speaker_code="a001",
                role="assistant",
                content="reply",
                context=context,
            )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim="wrong-pledge",
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="wrong-claim",
                    manager_claim_context={**manager_claim_context, "claim_token": "wrong-claim"},
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context={**manager_claim_context, "fencing_token": "wrong-fence"},
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context={**manager_claim_context, "owner_boot_id": "wrong-boot"},
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: False
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current

    def test_publish_append_rejects_if_claim_expires_after_claim_publish(self):
        space = PREFIX + "appendclaimlost"
        make_space(space)
        post = room_manager.post(space, "work", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        effect_id = orchestration.effect_id("agent_reply", space, "agent_a001", context["intent_id"])
        manager_claim_context = {
            "claim_token": "claim-a",
            "fencing_token": "fence-a",
            "owner_boot_id": "boot-a",
        }
        original_is_current = publish_ledger.manager_claim.is_current
        try:
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: True
            claim = publish_ledger.claim_publish(
                space,
                publish_effect_id=effect_id,
                manager_claim_token="claim-a",
                manager_claim_context=manager_claim_context,
                context=context,
                publisher="space_manager",
                speaker="agent_a001",
            )
            publish_ledger.manager_claim.is_current = lambda *args, **kwargs: False
            with self.assertRaises(publish_ledger.PublishLedgerError):
                publish_ledger.append_public_message(
                    space,
                    publish_effect_id=effect_id,
                    publish_ledger_claim=claim["publish_ledger_claim"],
                    manager_claim_token="claim-a",
                    manager_claim_context=manager_claim_context,
                    published_message_id=claim["published_message_id"],
                    intent_stale_guard_passed=True,
                    speaker_name="agent",
                    speaker_code="a001",
                    role="assistant",
                    content="reply",
                    context=context,
                )
        finally:
            publish_ledger.manager_claim.is_current = original_is_current
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])

    def test_legacy_engine_work_records_post_task_evaluation_without_changing_return_contract(self):
        space = PREFIX + "legacywork"
        member = PREFIX + "agent_w999"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "테스트 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(result["작업코드"])
        self.assertEqual(result["상태"], "done")
        self.assertEqual(result["결과"], "작업 완료")
        self.assertEqual(set(result.keys()), {"작업코드", "상태", "결과"})
        self.assertEqual(status["learning"]["post_task_evaluation_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("success"), 1)
        self.assertEqual(status["tasks"]["task_count"], 1)
        self.assertEqual(status["tasks"]["latest_state"], "done")
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        task_pack = json.loads((wdir / "task_pack.json").read_text(encoding="utf-8"))
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        self.assertEqual(task_pack["schema"], "TaskPack.compat_minimal.v1")
        self.assertEqual(task_pack["space_id"], space)
        self.assertEqual(task_pack["worker_agent"], member)
        self.assertEqual(task_pack["release_policy"]["do_not_publish_directly"], True)
        self.assertEqual(task_pack["release_policy"]["enqueue_release_queue"], True)
        self.assertEqual(task_pack["release_policy"]["enqueue_when"], ["done"])
        self.assertIn("law.md", "\n".join(task_pack["instruction_files"]))
        # 새 scope 계약(2026-07-02): 읽기는 워크스페이스 전체(".") — law.md·role.md도 그 안에 포함된다.
        self.assertIn(".", task_pack["scope"]["read_paths"])
        self.assertTrue(any(path.endswith("role.md") for path in task_pack["instruction_files"]))
        self.assertEqual(task_pack["lesson_pack"]["lesson_pack_status"], "ok")
        self.assertEqual(work_status["state"], "done")
        # P4(2026-07-02): verification은 더 이상 not_run이 아니라 finalize에서 객관 검증된다.
        # 이 fake는 산출물 없이 결과.md="작업 완료"만 남기므로 '거짓 성공 의심(suspect)'이 정답.
        self.assertEqual(work_status["verification"]["status"], "suspect")
        self.assertTrue(work_status["verification"].get("review_recommended"))
        self.assertEqual(release_request["schema"], "ReleaseRequest.v1")
        self.assertEqual(release_request["release_state"], "approval_pending")
        self.assertEqual(release_request["queue_state"], "enqueued")
        self.assertFalse(release_request["draft_only"])
        self.assertTrue(release_request["release_queue_id"])
        self.assertIn("not_publishable_reason", release_request)
        self.assertEqual(release_request["approval_state"], "pending")
        self.assertEqual(status["release_queue"]["release_count"], 1)
        self.assertEqual(status["release_queue"]["pending_count"], 1)
        self.assertEqual(status["release_queue"]["latest_source_task_id"], result["작업코드"])

    def test_work_instruction_includes_workdir_path_for_preview(self):
        # 갭 B 보강(실증): 에이전트가 미리보기 경로를 지어내지 않게, 지시.md가 작업 폴더 루트기준 경로를 명시한다.
        space = PREFIX + "wdirpath"
        member = PREFIX + "agent_wd1"
        make_space(space, [member])
        captured = {}
        orig = engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            captured["instr"] = (cwd / "지시.md").read_text(encoding="utf-8")
            captured["cwd"] = cwd
            (cwd / "결과.md").write_text("done", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake
            engine.work(member, space, "파일 작업")
        finally:
            engine.run_engine = orig
        rel = captured["cwd"].relative_to(PEOPLE.parent).as_posix()
        self.assertIn("너의 작업 폴더", captured["instr"])
        self.assertIn(rel, captured["instr"])                     # 정확한 작업 폴더 경로 명시
        self.assertIn("경로를 지어내지 말고", captured["instr"])  # 지어내기 금지 강조

    def test_engine_work_exception_finalizes_task_as_error(self):
        space = PREFIX + "workraises"
        member = PREFIX + "agent_wr99"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            # 진행(결과.md)을 남긴 뒤 크래시 — '완료했는데 막판 오류'를 모사. 산출이 사라지면 안 된다.
            (Path(cwd) / "결과.md").write_text("# 분석 완료\n핵심 결론: 8개 항목 정리함.", encoding="utf-8")
            raise RuntimeError("simulated engine crash")

        try:
            engine.run_engine = fake_run_engine
            with self.assertRaises(RuntimeError):
                engine.work(member, space, "실패하는 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        work_root = PEOPLE / member / "공간" / space / "작업"
        work_dirs = [path for path in work_root.iterdir() if path.is_dir()]
        self.assertEqual(len(work_dirs), 1)
        work_status = json.loads((work_dirs[0] / "work_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["tasks"]["task_count"], 1)
        self.assertEqual(status["tasks"]["latest_state"], "error")
        self.assertEqual(work_status["state"], "error")
        self.assertIn("simulated engine crash", (work_dirs[0] / "상태.json").read_text(encoding="utf-8"))
        # ★ 회귀(대표 라이브 발견): error여도 산출이 조용히 사라지지 않고 release로 surface돼야 한다.
        self.assertEqual(status["release_queue"]["release_count"], 1)
        pending = release_queue.snapshot(space).get("pending_items") or []
        self.assertEqual(len(pending), 1)
        summary = pending[0].get("public_summary", "")
        self.assertIn("끊겼", summary)                       # 오류 배너(자동 보고 끊김 안내)
        self.assertIn("분석 완료", summary)                   # 그때까지의 산출도 함께 실림

    def test_run_engine_polling_terminates_process_when_cancel_check_turns_true(self):
        space = PREFIX + "pollcancel"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command
        cancel_event = threading.Event()
        phases = []

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", "import time; time.sleep(5); print('late')"]

        timer = threading.Timer(0.2, cancel_event.set)
        try:
            engine._engine_command = fake_engine_command
            timer.start()
            output = engine.run_engine_polling(
                work_dir,
                "long running prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=10,
                cancel_check=cancel_event.is_set,
                heartbeat=lambda phase, note="": phases.append(phase),
                heartbeat_interval=0.1,
            )
        finally:
            timer.cancel()
            engine._engine_command = original_engine_command

        self.assertTrue(output.startswith("(엔진 취소됨"))
        self.assertIn("engine_process_started", phases)
        self.assertIn("engine_cancelled", phases)

    def test_run_engine_polling_consumes_large_stdout_without_deadlock(self):
        space = PREFIX + "pollstdout"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"]

        try:
            engine._engine_command = fake_engine_command
            output = engine.run_engine_polling(
                work_dir,
                "large stdout prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=5,
            )
        finally:
            engine._engine_command = original_engine_command

        self.assertEqual(len(output), 200000)
        self.assertTrue(output.startswith("xxx"))

    def test_run_engine_polling_kills_process_group_after_ignored_term(self):
        space = PREFIX + "pollkill"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command
        cancel_event = threading.Event()
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    time.sleep(1)\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", script]

        timer = threading.Timer(0.2, cancel_event.set)
        try:
            engine._engine_command = fake_engine_command
            timer.start()
            output = engine.run_engine_polling(
                work_dir,
                "ignore term prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=5,
                cancel_check=cancel_event.is_set,
                terminate_grace_seconds=0.2,
            )
        finally:
            timer.cancel()
            engine._engine_command = original_engine_command

        self.assertTrue(output.startswith("(엔진 취소됨"))

    def test_run_engine_polling_timeout_matches_legacy_timeout_text(self):
        space = PREFIX + "polltimeout"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", "import time; time.sleep(5)"]

        try:
            engine._engine_command = fake_engine_command
            output = engine.run_engine_polling(
                work_dir,
                "timeout prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=0.2,
                terminate_grace_seconds=0.1,
            )
        finally:
            engine._engine_command = original_engine_command

        self.assertEqual(output, "(엔진 타임아웃)")

    def test_gemini_engine_command_uses_agy_print_with_model(self):
        cmd = engine._engine_command(
            ROOT,
            "한 단어로 PONG만 답하세요.",
            "gemini",
            "Gemini 3.1 Pro (High)",
        )

        self.assertEqual(Path(cmd[0]).name, "agy")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertTrue(any(part.startswith("--print=") for part in cmd))
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "Gemini 3.1 Pro (High)")
        self.assertNotEqual(Path(cmd[0]).name, "gemini")

    def test_agy_timeout_status_line_is_scrubbed_from_gemini_output(self):
        raw = "\x04\x08\x08Error: timed out waiting for response\r"
        self.assertEqual(engine._clean_engine_output("gemini", raw), "")
        partial = "부분 답변\n\x04\x08\x08Error: timed out waiting for response\r"
        self.assertEqual(engine._clean_engine_output("gemini", partial), "부분 답변")

    def test_run_engine_polling_applies_dynamic_work_policy_loader(self):
        space = PREFIX + "polldynpolicy"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command
        original_bounds = dict(work_settings.BOUNDS)

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", "import time; time.sleep(5)"]

        def dynamic_policy():
            return {
                "runner_timeout_sec": 1,
                "heartbeat_interval_sec": 1,
            }

        started = time.monotonic()
        try:
            work_settings.BOUNDS["runner_timeout_sec"] = (0, 7200)
            engine._engine_command = fake_engine_command
            output = engine.run_engine_polling(
                work_dir,
                "dynamic timeout prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=10,
                heartbeat_interval=10,
                work_policy_loader=dynamic_policy,
                terminate_grace_seconds=0.1,
            )
        finally:
            work_settings.BOUNDS.clear()
            work_settings.BOUNDS.update(original_bounds)
            engine._engine_command = original_engine_command

        self.assertEqual(output, "(엔진 타임아웃)")
        self.assertLess(time.monotonic() - started, 3)

    def test_run_engine_polling_preserves_stdout_priority_and_stderr_fallback(self):
        space = PREFIX + "pollstderr"
        make_space(space)
        work_dir = SPACES / space / "polling"
        work_dir.mkdir(parents=True, exist_ok=True)
        original_engine_command = engine._engine_command
        calls = {"count": 0}

        def fake_engine_command(cwd, prompt, engine_name, model):
            calls["count"] += 1
            if calls["count"] == 1:
                return [sys.executable, "-c", "import sys; print('stdout ok'); print('stderr ignored', file=sys.stderr)"]
            return [sys.executable, "-c", "import sys; print('stderr only', file=sys.stderr)"]

        try:
            engine._engine_command = fake_engine_command
            stdout_output = engine.run_engine_polling(
                work_dir,
                "stdout prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=5,
            )
            stderr_output = engine.run_engine_polling(
                work_dir,
                "stderr prompt",
                engine="codex",
                model="gpt-5.5",
                timeout=5,
            )
        finally:
            engine._engine_command = original_engine_command

        self.assertEqual(stdout_output, "stdout ok")
        self.assertEqual(stderr_output, "(stderr) stderr only")

    def test_engine_work_polling_runner_cancels_running_process_and_blocks_release(self):
        space = PREFIX + "workpollcancel"
        member = PREFIX + "agent_wc10"
        make_space(space, [member])
        original_engine_command = engine._engine_command
        work_root = PEOPLE / member / "공간" / space / "작업"
        script = (
            "from pathlib import Path\n"
            "import json, time\n"
            "Path('started.txt').write_text('started', encoding='utf-8')\n"
            "time.sleep(5)\n"
            "Path('결과.md').write_text('late done', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", script]

        result_holder = {}
        error_holder = {}

        def run_work():
            try:
                result_holder["result"] = engine.work(member, space, "취소될 긴 작업")
            except Exception as exc:
                error_holder["error"] = exc

        try:
            engine._engine_command = fake_engine_command
            thread = threading.Thread(target=run_work, daemon=True)
            thread.start()
            wdir = None
            for _ in range(100):
                work_dirs = [path for path in work_root.iterdir() if path.is_dir()] if work_root.exists() else []
                if work_dirs and (work_dirs[0] / "started.txt").exists():
                    wdir = work_dirs[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(wdir)
            cancel = room_manager.request_task_cancel(space, wdir.name, reason="테스트 취소")
            self.assertTrue(cancel["ok"])
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        finally:
            engine._engine_command = original_engine_command

        self.assertNotIn("error", error_holder)
        result = result_holder["result"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(result["상태"], "cancelled")
        self.assertEqual(json.loads((wdir / "상태.json").read_text(encoding="utf-8"))["상태"], "cancelled")
        self.assertEqual(work_status["state"], "cancelled")
        self.assertTrue(work_status["cancel_requested"])
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_engine_work_polling_sees_progress_steering_without_stopping_process(self):
        space = PREFIX + "workpollprogress"
        member = PREFIX + "agent_wp10"
        make_space(space, [member])
        original_engine_command = engine._engine_command
        work_root = PEOPLE / member / "공간" / space / "작업"
        script = (
            "from pathlib import Path\n"
            "import json, time\n"
            "Path('started.txt').write_text('started', encoding='utf-8')\n"
            "time.sleep(1.2)\n"
            "Path('결과.md').write_text('progress steering 후 완료', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            return [sys.executable, "-c", script]

        result_holder = {}
        error_holder = {}

        def run_work():
            try:
                result_holder["result"] = engine.work(member, space, "진행 보고 steering 중 작업")
            except Exception as exc:
                error_holder["error"] = exc

        try:
            engine._engine_command = fake_engine_command
            thread = threading.Thread(target=run_work, daemon=True)
            thread.start()
            wdir = None
            for _ in range(100):
                work_dirs = [path for path in work_root.iterdir() if path.is_dir()] if work_root.exists() else []
                if work_dirs and (work_dirs[0] / "started.txt").exists():
                    wdir = work_dirs[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(wdir)
            progress = room_manager.request_task_steering(
                space,
                wdir.name,
                action="request_progress",
                instruction="현재 진행상황을 heartbeat에 남겨줘",
            )
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        finally:
            engine._engine_command = original_engine_command

        self.assertNotIn("error", error_holder)
        result = result_holder["result"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        registry_rows = [
            json.loads(line)
            for line in (SPACES / space / "task_registry.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(result["상태"], "done")
        self.assertGreaterEqual(int(work_status["last_seen_steering_seq"]), progress["steering_seq"])
        self.assertFalse(work_status["pending_steering_ack"])
        self.assertTrue(any(row.get("heartbeat_phase") == "steering_progress_seen" for row in registry_rows))

    def test_engine_work_polling_restarts_after_revise_steering_and_acks_it(self):
        space = PREFIX + "workpollrevise"
        member = PREFIX + "agent_wr10"
        make_space(space, [member])
        original_engine_command = engine._engine_command
        work_root = PEOPLE / member / "공간" / space / "작업"
        prompts = []
        first_script = (
            "from pathlib import Path\n"
            "import json, time\n"
            "Path('started_first.txt').write_text('started', encoding='utf-8')\n"
            "time.sleep(5)\n"
            "Path('결과.md').write_text('이전 지시 결과', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )
        second_script = (
            "from pathlib import Path\n"
            "import json\n"
            "Path('started_second.txt').write_text('started', encoding='utf-8')\n"
            "Path('결과.md').write_text('재지시 반영 결과', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            prompts.append(prompt)
            if len(prompts) == 1:
                return [sys.executable, "-c", first_script]
            return [sys.executable, "-c", second_script]

        result_holder = {}
        error_holder = {}

        def run_work():
            try:
                result_holder["result"] = engine.work(member, space, "재지시 전 작업")
            except Exception as exc:
                error_holder["error"] = exc

        try:
            engine._engine_command = fake_engine_command
            thread = threading.Thread(target=run_work, daemon=True)
            thread.start()
            wdir = None
            for _ in range(100):
                work_dirs = [path for path in work_root.iterdir() if path.is_dir()] if work_root.exists() else []
                if work_dirs and (work_dirs[0] / "started_first.txt").exists():
                    wdir = work_dirs[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(wdir)
            revise = room_manager.request_task_steering(
                space,
                wdir.name,
                action="revise_task",
                instruction="새 재지시: 최종 결과 문구는 재지시 반영 결과로 작성",
            )
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        finally:
            engine._engine_command = original_engine_command

        self.assertNotIn("error", error_holder)
        result = result_holder["result"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(len(prompts), 2)
        self.assertIn("새 재지시", prompts[1])
        self.assertEqual(result["상태"], "done")
        self.assertEqual(result["결과"], "재지시 반영 결과")
        self.assertGreaterEqual(int(work_status["last_seen_steering_seq"]), revise["steering_seq"])
        self.assertFalse(work_status["pending_steering_ack"])
        self.assertEqual(status["tasks"]["pending_steering_count"], 0)
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_engine_work_polling_revise_then_progress_keeps_revise_until_restart(self):
        space = PREFIX + "workpollmix"
        member = PREFIX + "agent_wmix10"
        make_space(space, [member])
        original_engine_command = engine._engine_command
        work_root = PEOPLE / member / "공간" / space / "작업"
        prompts = []
        first_script = (
            "from pathlib import Path\n"
            "import time\n"
            "Path('started_first.txt').write_text('started', encoding='utf-8')\n"
            "time.sleep(5)\n"
        )
        second_script = (
            "from pathlib import Path\n"
            "import json\n"
            "Path('started_second.txt').write_text('started', encoding='utf-8')\n"
            "Path('결과.md').write_text('혼합 steering 반영 결과', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            prompts.append(prompt)
            if len(prompts) == 1:
                return [sys.executable, "-c", first_script]
            return [sys.executable, "-c", second_script]

        result_holder = {}
        error_holder = {}

        def run_work():
            try:
                result_holder["result"] = engine.work(member, space, "혼합 steering 작업")
            except Exception as exc:
                error_holder["error"] = exc

        try:
            engine._engine_command = fake_engine_command
            thread = threading.Thread(target=run_work, daemon=True)
            thread.start()
            wdir = None
            for _ in range(100):
                work_dirs = [path for path in work_root.iterdir() if path.is_dir()] if work_root.exists() else []
                if work_dirs and (work_dirs[0] / "started_first.txt").exists():
                    wdir = work_dirs[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(wdir)
            revise = room_manager.request_task_steering(
                space,
                wdir.name,
                action="revise_task",
                instruction="혼합 재지시를 반영",
            )
            progress = room_manager.request_task_steering(
                space,
                wdir.name,
                action="request_progress",
                instruction="재지시를 반영하면서 진행상황도 남겨줘",
            )
            mid_status = room_manager.status(space)
            self.assertEqual(mid_status["tasks"]["pending_steering_count"], 1)
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
        finally:
            engine._engine_command = original_engine_command

        self.assertNotIn("error", error_holder)
        result = result_holder["result"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(result["상태"], "done")
        self.assertEqual(result["결과"], "혼합 steering 반영 결과")
        self.assertIn("혼합 재지시", prompts[1])
        self.assertIn("재지시를 반영하면서 진행상황", prompts[1])
        self.assertGreaterEqual(int(work_status["last_seen_steering_seq"]), progress["steering_seq"])
        self.assertGreaterEqual(int(work_status["last_seen_steering_seq"]), revise["steering_seq"])
        self.assertFalse(work_status["pending_steering_ack"])
        self.assertEqual(status["tasks"]["pending_steering_count"], 0)

    def test_engine_work_restarts_when_revise_arrives_after_engine_return_before_finalize(self):
        space = PREFIX + "workpollpostrev"
        member = PREFIX + "agent_wpostrev"
        make_space(space, [member])
        original_engine_command = engine._engine_command
        original_steering_events = engine._steering_events
        work_root = PEOPLE / member / "공간" / space / "작업"
        prompts = []
        injected = {"done": False}
        first_script = (
            "from pathlib import Path\n"
            "import json\n"
            "Path('결과.md').write_text('반환 직후 이전 결과', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )
        second_script = (
            "from pathlib import Path\n"
            "import json\n"
            "Path('결과.md').write_text('반환 직후 재지시 반영 결과', encoding='utf-8')\n"
            "Path('상태.json').write_text(json.dumps({'상태': 'done'}, ensure_ascii=False), encoding='utf-8')\n"
        )

        def fake_engine_command(cwd, prompt, engine_name, model):
            prompts.append(prompt)
            if len(prompts) == 1:
                return [sys.executable, "-c", first_script]
            return [sys.executable, "-c", second_script]

        def fake_steering_events(work_dir, *, after_seq=0):
            if not injected["done"] and Path(work_dir).parent == work_root:
                injected["done"] = True
                room_manager.request_task_steering(
                    space,
                    Path(work_dir).name,
                    action="revise_task",
                    instruction="반환 직후 재지시를 반영",
                )
            return original_steering_events(work_dir, after_seq=after_seq)

        try:
            engine._engine_command = fake_engine_command
            engine._steering_events = fake_steering_events
            result = engine.work(member, space, "반환 직후 재지시 작업")
        finally:
            engine._engine_command = original_engine_command
            engine._steering_events = original_steering_events

        wdir = next(path for path in work_root.iterdir() if path.is_dir())
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(len(prompts), 2)
        self.assertIn("반환 직후 재지시", prompts[1])
        self.assertEqual(result["상태"], "done")
        self.assertEqual(result["결과"], "반환 직후 재지시 반영 결과")
        self.assertFalse(work_status["pending_steering_ack"])
        self.assertEqual(status["tasks"]["pending_steering_count"], 0)
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_engine_work_cancel_sentinel_finalizes_as_cancelled_without_exception(self):
        space = PREFIX + "workcancelsentinel"
        member = PREFIX + "agent_wc11"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return "(엔진 취소됨: cancel_requested)"

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "취소 sentinel 작업")
        finally:
            engine.run_engine = original_run_engine

        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status_json = json.loads((wdir / "상태.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(result["상태"], "cancelled")
        self.assertEqual(work_status["state"], "cancelled")
        self.assertEqual(status_json["상태"], "cancelled")
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_engine_work_records_must_apply_lesson_report(self):
        space = PREFIX + "worklessonok"
        member = PREFIX + "agent_wo01"
        make_space(space, [member])
        lesson = lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "work_rule",
                "scope": "space",
                "status": "active",
                "instruction": "작업 완료 전 결과와 상태 파일을 함께 확인한다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["work"], "keywords": []},
            },
        )
        lesson_id = lesson["record"]["created_lesson_ids"][0]
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            self.assertIn("task_pack.json", prompt)
            (cwd / "결과.md").write_text("작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            (cwd / "레슨적용보고.json").write_text(json.dumps({
                "schema": "LessonApplicationReport.v1",
                "applications": [{
                    "lesson_id": lesson_id,
                    "applied": True,
                    "not_applicable_reason": "",
                    "how": "결과.md와 상태.json을 함께 확인했다.",
                    "outcome": "success",
                    "needs_lesson_update": False,
                }],
            }, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "테스트 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        task_pack = json.loads((wdir / "task_pack.json").read_text(encoding="utf-8"))
        self.assertEqual(result["상태"], "done")
        self.assertEqual(status["learning"]["lesson_application_count"], 1)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("success"), 1)
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["tasks"]["latest_must_apply_lessons"], [lesson_id])
        self.assertEqual(task_pack["lesson_pack"]["must_apply"][0]["lesson_id"], lesson_id)

    def test_engine_work_missing_must_apply_report_blocks_completion(self):
        space = PREFIX + "worklessonhold"
        member = PREFIX + "agent_wh02"
        make_space(space, [member])
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "work_rule",
                "scope": "space",
                "status": "active",
                "instruction": "작업 완료 전 레슨 적용 보고를 남긴다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["work"], "keywords": []},
            },
        )
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("작업 완료지만 보고 누락", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "테스트 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        self.assertEqual(result["상태"], "blocked")
        self.assertEqual(work_status["state"], "blocked")
        self.assertTrue(work_status["lesson_application_hold"])
        self.assertEqual(status["learning"]["lesson_application_count"], 0)
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("rejected"), 1)
        self.assertEqual(status["tasks"]["latest_state"], "blocked")
        self.assertTrue(status["tasks"]["latest_lesson_application_hold"])
        self.assertEqual(status["tasks"]["hold_task_count"], 1)
        self.assertIn("lesson_must_apply_without_application", status["tasks"]["latest_hold_error"])
        self.assertEqual(status["release_queue"]["release_count"], 0)
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertTrue(release_request["draft_only"])
        self.assertTrue(any(row.get("상태") == "task_lesson_application_hold" for row in status["failures"]))

    def test_engine_work_lesson_pack_unavailable_blocks_completion(self):
        space = PREFIX + "worklessonbad"
        member = PREFIX + "agent_wb03"
        make_space(space, [member])
        learning = SPACES / space / "learning"
        learning.mkdir(parents=True, exist_ok=True)
        (learning / "lessons.jsonl").write_text("{bad json}\n", encoding="utf-8")
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("공개되면 안 되는 작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "테스트 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertEqual(result["상태"], "blocked")
        self.assertTrue(status["learning"]["ledger_corrupt"])
        self.assertEqual(status["tasks"]["latest_lesson_pack_status"], "unavailable")
        self.assertTrue(status["tasks"]["latest_lesson_application_hold"])
        self.assertEqual(status["tasks"]["hold_task_count"], 1)
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_engine_work_error_is_not_overwritten_by_missing_must_apply_report(self):
        space = PREFIX + "workerror"
        member = PREFIX + "agent_we04"
        make_space(space, [member])
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "work_rule",
                "scope": "space",
                "status": "active",
                "instruction": "작업 완료 전 레슨 적용 보고를 남긴다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["work"], "keywords": []},
            },
        )
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("작업 실패", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "error", "사유": "simulated"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "테스트 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertEqual(result["상태"], "error")
        self.assertFalse(status["tasks"]["latest_lesson_application_hold"])
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("failed"), 1)
        # error는 hold로 덮이지 않고(위), error 그대로 release로 surface된다(조용한 실패 제거).
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_engine_work_partial_ready_stays_draft_not_enqueued(self):
        space = PREFIX + "workpartial"
        member = PREFIX + "agent_wp04"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("부분 결과", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "partial_ready"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "부분 결과 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        self.assertEqual(result["상태"], "partial_ready")
        self.assertEqual(work_status["release_queue_state"], "not_enqueued")
        self.assertEqual(release_request["release_kind"], "partial")
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertTrue(release_request["draft_only"])
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_engine_work_done_after_generation_change_stays_draft_not_enqueued(self):
        space = PREFIX + "workstalegen"
        member = PREFIX + "agent_ws04"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            orchestration.advance_generation(space, "test_work_generation_changed")
            (cwd / "결과.md").write_text("늦은 작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "세대 변경 중 완료된 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        self.assertEqual(result["상태"], "done")
        self.assertEqual(release_request["release_state"], "stale_generation")
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertTrue(release_request["draft_only"])
        self.assertIn("stale_generation", release_request["release_enqueue_error"])
        self.assertEqual(work_status["release_queue_state"], "not_enqueued")
        self.assertEqual(status["release_queue"]["release_count"], 0)
        self.assertEqual(status["tasks"]["latest_release_queue_state"], "not_enqueued")

    def test_release_queue_corrupt_during_work_finalize_keeps_task_status_visible(self):
        space = PREFIX + "releasecorruptwork"
        member = PREFIX + "agent_wr04"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            (SPACES / space / "release_queue.jsonl").write_text("{bad json}\n", encoding="utf-8")
            (cwd / "결과.md").write_text("작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            result = engine.work(member, space, "ReleaseQueue 손상 테스트")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        self.assertEqual(result["상태"], "done")
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["tasks"]["latest_release_queue_state"], "enqueue_failed")
        self.assertEqual(status["tasks"]["release_enqueue_failed_count"], 1)
        self.assertTrue(status["release_queue"]["ledger_corrupt"])
        self.assertEqual(status["release_queue"]["release_count"], 0)
        self.assertEqual(release_request["queue_state"], "enqueue_failed")
        self.assertTrue(release_request["draft_only"])
        self.assertEqual(work_status["release_queue_state"], "enqueue_failed")
        self.assertTrue(any(row.get("상태") == "task_release_enqueue_failed" for row in status["failures"]))

    def test_task_registry_corrupt_during_finalize_does_not_enqueue_release(self):
        space = PREFIX + "taskcorruptwork"
        member = PREFIX + "agent_wt04"
        make_space(space, [member])
        original_run_engine = engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            with (SPACES / space / "task_registry.jsonl").open("a", encoding="utf-8") as f:
                f.write("{bad json}\n")
            (cwd / "결과.md").write_text("작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            with self.assertRaises(task_registry.TaskRegistryError):
                engine.work(member, space, "TaskRegistry 손상 테스트")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertTrue(status["tasks"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["tasks"]["ledger_errors"]))
        self.assertEqual(status["release_queue"]["release_count"], 0)
        self.assertTrue(any(row.get("상태") == "task_registry_corrupt" for row in status["failures"]))

    def test_task_hold_remains_visible_after_later_task_event(self):
        space = PREFIX + "workholdpersist"
        member = PREFIX + "agent_wp05"
        make_space(space, [member])
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="user_correction",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "work_rule",
                "scope": "space",
                "status": "active",
                "instruction": "작업 완료 전 레슨 적용 보고를 남긴다.",
                "application_level": "must_apply",
                "must_apply": True,
                "evidence_level": "user_directive",
                "confidence": 0.95,
                "applies_when": {"space_id": space, "agent_modes": ["work"], "keywords": []},
            },
        )
        original_run_engine = engine.run_engine
        calls = {"count": 0}

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            calls["count"] += 1
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("작업 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            if calls["count"] == 2:
                task_pack = json.loads((cwd / "task_pack.json").read_text(encoding="utf-8"))
                lesson_id = task_pack["lesson_pack"]["must_apply"][0]["lesson_id"]
                (cwd / "레슨적용보고.json").write_text(json.dumps({
                    "schema": "LessonApplicationReport.v1",
                    "applications": [{
                        "lesson_id": lesson_id,
                        "applied": True,
                        "not_applicable_reason": "",
                        "how": "두 번째 작업에서 보고함",
                        "outcome": "success",
                        "needs_lesson_update": False,
                    }],
                }, ensure_ascii=False), encoding="utf-8")
            return ""

        try:
            engine.run_engine = fake_run_engine
            first = engine.work(member, space, "첫 작업")
            second = engine.work(member, space, "둘째 작업")
        finally:
            engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertEqual(first["상태"], "blocked")
        self.assertEqual(second["상태"], "done")
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["tasks"]["hold_task_count"], 1)
        self.assertEqual(status["release_queue"]["release_count"], 1)
        self.assertEqual(status["release_queue"]["pending_count"], 1)
        self.assertTrue(any(row.get("상태") == "task_lesson_application_hold" for row in status["failures"]))

    def test_task_registry_create_and_finalize_are_idempotent(self):
        space = PREFIX + "taskidem"
        member = PREFIX + "agent_wi06"
        make_space(space, [member])
        wdir = PEOPLE / member / "공간" / space / "작업" / "idem"
        runtime_info = {"engine": "codex", "model": "gpt-5.5"}

        created_1 = task_registry.create_task(
            space,
            worker=member,
            task_id="idem",
            objective="idempotent task",
            work_dir=wdir,
            runtime_info=runtime_info,
        )
        created_2 = task_registry.create_task(
            space,
            worker=member,
            task_id="idem",
            objective="idempotent task",
            work_dir=wdir,
            runtime_info=runtime_info,
        )
        task_pack = created_1["task_pack"]
        (wdir / "결과.md").write_text("ok", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        final_1 = task_registry.finalize_task(
            space, task_id="idem", worker=member, work_dir=wdir, task_pack=task_pack, objective="idempotent task"
        )
        final_2 = task_registry.finalize_task(
            space, task_id="idem", worker=member, work_dir=wdir, task_pack=task_pack, objective="idempotent task"
        )
        status = room_manager.status(space)

        self.assertTrue(created_2["manifest"]["duplicate"])
        self.assertEqual(final_1["state"], "done")
        self.assertEqual(final_2["state"], "done")
        self.assertEqual(status["tasks"]["task_event_count"], 3)
        self.assertEqual(status["tasks"]["task_pack_manifest_count"], 1)
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["release_queue"]["release_event_count"], 1)
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_task_cancel_request_writes_steering_advances_generation_and_blocks_release(self):
        space = PREFIX + "taskcancel"
        member = PREFIX + "agent_wc07"
        make_space(space, [member])
        post = room_manager.post(space, "긴 작업을 시작해줘", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "cancelrun"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="cancelrun",
            objective="cancellable task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        before_generation = orchestration.current_generation(space)

        cancel = room_manager.request_task_cancel(space, "cancelrun", reason="대표가 중단")
        after_generation = orchestration.current_generation(space)
        duplicate = room_manager.request_task_cancel(space, "cancelrun", reason="대표가 중단 재시도")
        status = room_manager.status(space)
        cancel_request = json.loads((wdir / "취소요청.json").read_text(encoding="utf-8"))
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        steering_files = list((wdir / "steering").glob("*.json"))

        self.assertTrue(cancel["ok"])
        self.assertFalse(cancel["duplicate"])
        self.assertTrue(cancel["generation_advanced"])
        self.assertEqual(after_generation, before_generation + 1)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(duplicate["generation_advanced"])
        self.assertEqual(orchestration.current_generation(space), after_generation)
        self.assertEqual(cancel_request["schema"], "TaskCancelRequest.v1")
        self.assertEqual(cancel_request["task_id"], "cancelrun")
        self.assertEqual(work_status["state"], "cancel_requested")
        self.assertTrue(work_status["cancel_requested"])
        self.assertEqual(len(steering_files), 1)
        self.assertEqual(status["tasks"]["cancel_requested_count"], 1)
        self.assertEqual(status["tasks"]["active_items"][0]["task_id"], "cancelrun")

        (wdir / "결과.md").write_text("취소 이후 늦게 끝난 결과", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        final = task_registry.finalize_task(
            space,
            task_id="cancelrun",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="cancellable task",
        )
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        final_status = room_manager.status(space)

        self.assertEqual(final["state"], "done")
        self.assertTrue(final["work_status"]["cancel_requested"])
        self.assertEqual(release_request["release_state"], "cancel_requested")
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertEqual(final_status["release_queue"]["release_count"], 0)
        self.assertEqual(final_status["tasks"]["cancel_requested_count"], 0)

    def test_cancel_text_does_not_cancel_running_tasks_before_manager_decision(self):
        space = PREFIX + "taskcanceltext"
        member = PREFIX + "agent_wc08"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "cancelpost"
        task_registry.create_task(
            space,
            worker=member,
            task_id="cancelpost",
            objective="cancel from chat",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        before_generation = orchestration.current_generation(space)

        cancel_post = room_manager.post(space, "취소하고 다시 해", run_manager=False, client_message_id="cancel-1")
        duplicate_post = room_manager.post(space, "취소하고 다시 해", run_manager=False, client_message_id="cancel-1")
        status = room_manager.status(space)
        steering_files = list((wdir / "steering").glob("*.json"))

        self.assertEqual(orchestration.current_generation(space), before_generation)
        self.assertFalse(any(event.get("type") == "task_cancel_requested" for event in cancel_post["events"]))
        self.assertFalse(any(event.get("type") == "task_cancel_requested" for event in duplicate_post["events"]))
        self.assertEqual(cancel_post["ack"]["ingress_type"], "message")
        self.assertEqual(len(steering_files), 0)
        self.assertEqual(status["tasks"]["cancel_requested_count"], 0)
        self.assertFalse((wdir / "취소요청.json").exists())

    def test_space_manager_cancel_task_action_requests_cancel_once(self):
        space = PREFIX + "taskcancelmgr"
        member = PREFIX + "agent_wc08"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "cancelpost"
        task_registry.create_task(
            space,
            worker=member,
            task_id="cancelpost",
            objective="cancel from manager action",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        cancel_post = room_manager.post(space, "취소하고 다시 해", run_manager=False, client_message_id="cancel-1")
        cancel_context = cancel_post["orchestration"]
        before_generation = orchestration.current_generation(space)
        original_run_engine = room_manager.engine.run_engine
        calls = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            calls.append(Path(cwd).name)
            self.assertIn('"task_id": "cancelpost"', prompt)
            return json.dumps({
                "action": "cancel_task",
                "wake": "",
                "message": "대표가 이 작업을 취소하고 다시 진행하라고 요청함",
                "reason": "대표의 최신 요청과 실행 중 작업이 같은 intent 흐름에 해당함",
                "task_id": "cancelpost",
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "대표 취소 요청 처리", cancel_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        steering_files = list((wdir / "steering").glob("*.json"))
        cancel_request = json.loads((wdir / "취소요청.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, [MANAGER_DIRNAME])
        self.assertEqual(orchestration.current_generation(space), before_generation + 1)
        self.assertTrue(any(event.get("type") == "task_cancel_requested" for event in result["events"]))
        self.assertEqual(len(steering_files), 1)
        self.assertEqual(cancel_request["task_id"], "cancelpost")
        self.assertEqual(status["tasks"]["cancel_requested_count"], 1)
        self.assertEqual(status["last_action"], "cancel_task")

    def test_space_manager_revise_task_action_records_control_context_and_blocks_unacked_release(self):
        space = PREFIX + "taskrevisemgr"
        member = PREFIX + "agent_wrevmgr"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        task_context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "revisepost"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="revisepost",
            objective="revise from manager action",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=task_context,
        )
        revise_post = room_manager.post(space, "방향을 바꿔서 다시 해", run_manager=False, client_message_id="revise-1")
        control_context = revise_post["orchestration"]
        before_generation = orchestration.current_generation(space)
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn('"task_id": "revisepost"', prompt)
            return json.dumps({
                "action": "revise_task",
                "wake": "",
                "message": "",
                "reason": "대표의 최신 재지시를 진행 중 작업에 반영",
                "task_id": "revisepost",
                "instruction": "새 기준으로 다시 작성",
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "대표 재지시 처리", control_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        item = status["tasks"]["active_items"][0]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        steering = json.loads(sorted((wdir / "steering").glob("*.json"))[0].read_text(encoding="utf-8"))

        self.assertEqual(orchestration.current_generation(space), before_generation)
        self.assertTrue(any(event.get("type") == "revise_task" for event in result["events"]))
        self.assertEqual(status["last_action"], "revise_task")
        self.assertTrue(item["pending_steering_ack"])
        self.assertEqual(item["latest_steering_action"], "revise_task")
        self.assertEqual(item["pending_ack_steering_action"], "revise_task")
        self.assertEqual(item["pending_ack_steering_instruction"], "새 기준으로 다시 작성")
        self.assertEqual(item["source_event_seq"], task_context["source_event_seq"])
        self.assertEqual(item["latest_steering_control_request_source_event_seq"], control_context["source_event_seq"])
        self.assertEqual(work_status["latest_steering_control_request_source_event_seq"], control_context["source_event_seq"])
        self.assertEqual(steering["control_request_source_event_seq"], control_context["source_event_seq"])

        (wdir / "결과.md").write_text("재지시를 못 본 결과", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        final = task_registry.finalize_task(
            space,
            task_id="revisepost",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="revise from manager action",
        )
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        self.assertTrue(final["work_status"]["pending_steering_ack"])
        self.assertEqual(release_request["release_state"], "steering_unacknowledged")
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertEqual(room_manager.status(space)["release_queue"]["release_count"], 0)

    def test_space_manager_request_progress_action_records_control_context_without_ack_or_generation(self):
        space = PREFIX + "taskprogressmgr"
        member = PREFIX + "agent_wpmgr"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        task_context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "progresspost"
        task_registry.create_task(
            space,
            worker=member,
            task_id="progresspost",
            objective="progress from manager action",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=task_context,
        )
        progress_post = room_manager.post(space, "진행 상황 알려줘", run_manager=False, client_message_id="progress-1")
        control_context = progress_post["orchestration"]
        before_generation = orchestration.current_generation(space)
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            self.assertIn('"task_id": "progresspost"', prompt)
            return json.dumps({
                "action": "request_progress",
                "wake": "",
                "message": "",
                "reason": "대표가 진행 상황을 요청함",
                "task_id": "progresspost",
                "instruction": "현재 진행 상황과 막힌 점을 work_status에 남겨줘",
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "대표 진행 보고 요청 처리", control_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        item = status["tasks"]["active_items"][0]
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        steering = json.loads(sorted((wdir / "steering").glob("*.json"))[0].read_text(encoding="utf-8"))

        self.assertEqual(orchestration.current_generation(space), before_generation)
        self.assertTrue(any(event.get("type") == "request_progress" for event in result["events"]))
        self.assertEqual(status["last_action"], "request_progress")
        self.assertFalse(item["pending_steering_ack"])
        self.assertEqual(item["latest_steering_action"], "request_progress")
        self.assertEqual(item["source_event_seq"], task_context["source_event_seq"])
        self.assertEqual(item["latest_steering_control_request_source_event_seq"], control_context["source_event_seq"])
        self.assertEqual(work_status["latest_steering_control_request_source_event_seq"], control_context["source_event_seq"])
        self.assertEqual(steering["control_request_source_event_seq"], control_context["source_event_seq"])
        self.assertFalse((wdir / "취소요청.json").exists())

    def test_task_cancel_does_not_advance_generation_when_task_is_closed_or_stale(self):
        space = PREFIX + "taskcancelguard"
        member = PREFIX + "agent_wc09"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        stale_wdir = PEOPLE / member / "공간" / space / "작업" / "staletask"
        stale_created = task_registry.create_task(
            space,
            worker=member,
            task_id="staletask",
            objective="stale task",
            work_dir=stale_wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        orchestration.advance_generation(space, "new request")
        generation_after_new_request = orchestration.current_generation(space)

        stale_cancel = room_manager.request_task_cancel(space, "staletask", reason="오래된 작업 중단")

        self.assertTrue(stale_cancel["ok"])
        self.assertFalse(stale_cancel["generation_advanced"])
        self.assertEqual(orchestration.current_generation(space), generation_after_new_request)
        self.assertTrue((stale_wdir / "취소요청.json").exists())

        closed_wdir = PEOPLE / member / "공간" / space / "작업" / "closedtask"
        closed_created = task_registry.create_task(
            space,
            worker=member,
            task_id="closedtask",
            objective="closed task",
            work_dir=closed_wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context={**context, "room_generation": orchestration.current_generation(space)},
        )
        (closed_wdir / "결과.md").write_text("done", encoding="utf-8")
        (closed_wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        task_registry.finalize_task(
            space,
            task_id="closedtask",
            worker=member,
            work_dir=closed_wdir,
            task_pack=closed_created["task_pack"],
            objective="closed task",
        )
        generation_before_closed_cancel = orchestration.current_generation(space)

        with self.assertRaises(task_registry.TaskRegistryError):
            room_manager.request_task_cancel(space, "closedtask", reason="이미 끝난 작업")
        self.assertEqual(orchestration.current_generation(space), generation_before_closed_cancel)

    def test_task_cancel_generation_advance_uses_expected_generation_guard(self):
        space = PREFIX + "taskcancelcas"
        member = PREFIX + "agent_wc10"
        make_space(space, [member])
        post = room_manager.post(space, "작업 시작", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "castask"
        task_registry.create_task(
            space,
            worker=member,
            task_id="castask",
            objective="cas task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        original_request_cancel = room_manager.task_registry.request_cancel

        def racing_request_cancel(*args, **kwargs):
            result = original_request_cancel(*args, **kwargs)
            orchestration.advance_generation(space, "concurrent message before cancel generation advance")
            return result

        try:
            room_manager.task_registry.request_cancel = racing_request_cancel
            cancel = room_manager.request_task_cancel(space, "castask", reason="대표 취소")
        finally:
            room_manager.task_registry.request_cancel = original_request_cancel

        self.assertTrue(cancel["ok"])
        self.assertFalse(cancel["generation_advanced"])
        self.assertEqual(orchestration.current_generation(space), context["room_generation"] + 1)
        self.assertTrue((wdir / "취소요청.json").exists())

    def test_task_heartbeat_is_preserved_after_finalize(self):
        space = PREFIX + "taskheartbeat"
        member = PREFIX + "agent_wh07"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "heartbeat"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="heartbeat",
            objective="heartbeat task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        task_registry.record_heartbeat(
            space,
            task_id="heartbeat",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="mid_step",
            note="중간 진행",
        )
        (wdir / "결과.md").write_text("ok", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")

        final = task_registry.finalize_task(
            space,
            task_id="heartbeat",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="heartbeat task",
        )
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)

        self.assertEqual(final["state"], "done")
        self.assertEqual(work_status["heartbeat_phase"], "mid_step")
        self.assertEqual(status["tasks"]["latest_heartbeat_phase"], "mid_step")
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_task_snapshot_exposes_steering_runtime_state_from_heartbeat_phase(self):
        space = PREFIX + "taskruntime"
        member = PREFIX + "agent_wruntime"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        cases = [
            ("detected", "steering_revise_detected", "revise_detected", "재지시 감지"),
            ("restart", "engine_restarting_for_revise", "revise_restarting", "재지시 재실행 중"),
            ("applied", "steering_revise_applied", "revise_applied", "재지시 반영 완료"),
            ("progress", "steering_progress_seen", "progress_seen", "진행보고 요청 확인"),
        ]
        created_by_task = {}
        for task_id, phase, _state, _label in cases:
            wdir = PEOPLE / member / "공간" / space / "작업" / task_id
            created = task_registry.create_task(
                space,
                worker=member,
                task_id=task_id,
                objective=f"runtime state task {task_id}",
                work_dir=wdir,
                runtime_info={"engine": "codex", "model": "gpt-5.5"},
                context=context,
            )
            created_by_task[task_id] = (wdir, created)
            task_registry.record_heartbeat(
                space,
                task_id=task_id,
                worker=member,
                work_dir=wdir,
                task_pack=created["task_pack"],
                phase=phase,
                note="runtime state",
            )
        status = room_manager.status(space)
        prompt_snapshot = room_manager._prompt_room_status_snapshot(space)
        items = {item["task_id"]: item for item in status["tasks"]["active_items"]}

        for task_id, _phase, state, label in cases:
            self.assertEqual(items[task_id]["steering_runtime_state"], state)
            self.assertEqual(items[task_id]["steering_runtime_label"], label)
        self.assertEqual(status["tasks"]["steering_runtime_count"], 4)
        self.assertEqual(status["tasks"]["steering_runtime_counts"]["revise_restarting"], 1)
        self.assertTrue(any(item["steering_runtime_state"] == "revise_restarting" for item in prompt_snapshot["tasks"]["active_items"]))

    def test_task_runtime_activity_projects_steering_and_heartbeats_read_only(self):
        space = PREFIX + "taskrunact"
        member = PREFIX + "agent_wrunact"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "runtimeflow"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="runtimeflow",
            objective="runtime activity task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        room_manager.request_task_steering(
            space,
            "runtimeflow",
            action="request_progress",
            instruction="부분 진행 보고",
        )
        task_registry.record_heartbeat(
            space,
            task_id="runtimeflow",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="steering_progress_seen",
            note="부분 보고 요청 확인",
        )
        room_manager.request_task_steering(
            space,
            "runtimeflow",
            action="revise_task",
            instruction="방금 지시를 반영해서 다시 실행",
        )
        task_registry.record_heartbeat(
            space,
            task_id="runtimeflow",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="engine_restarting_for_revise",
            note="재지시로 엔진 재실행",
        )
        room_manager.request_task_cancel(space, "runtimeflow", reason="대표가 중단 요청")

        registry_path = SPACES / space / "task_registry.jsonl"
        before_registry = registry_path.read_text(encoding="utf-8")
        before_work_status = (wdir / "work_status.json").read_text(encoding="utf-8")
        before_steering_files = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((wdir / "steering").glob("*.json"))
        }
        direct = task_registry.runtime_activity(space, limit=8)
        status = room_manager.status(space)
        prompt_snapshot = room_manager._prompt_room_status_snapshot(space)
        after_registry = registry_path.read_text(encoding="utf-8")
        after_work_status = (wdir / "work_status.json").read_text(encoding="utf-8")
        after_steering_files = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((wdir / "steering").glob("*.json"))
        }

        labels = [row["label"] for row in direct]
        self.assertEqual(labels[0], "작업 취소 요청")
        self.assertIn("작업 재지시 요청", labels)
        self.assertIn("재지시 재실행 중", labels)
        self.assertIn("진행보고 요청 확인", labels)
        self.assertIn("작업 부분 보고 요청", labels)
        restart = next(row for row in direct if row["label"] == "재지시 재실행 중")
        revise = next(row for row in direct if row["label"] == "작업 재지시 요청")
        self.assertEqual(restart["event"], "task_heartbeat")
        self.assertEqual(restart["state"], "revise_restarting")
        self.assertEqual(restart["task_id"], "runtimeflow")
        self.assertEqual(restart["at"], restart["last_heartbeat_at"])
        self.assertEqual(revise["event"], "task_steering_requested")
        self.assertEqual(revise["state"], "revise_requested")
        self.assertEqual(revise["detail"], "방금 지시를 반영해서 다시 실행")
        self.assertEqual(revise["at"], revise["steering_requested_at"])
        self.assertGreater(int(revise["steering_seq"]), 0)
        self.assertEqual(before_registry, after_registry)
        self.assertEqual(before_work_status, after_work_status)
        self.assertEqual(before_steering_files, after_steering_files)
        self.assertEqual(status["tasks"]["runtime_activity_count"], len(status["tasks"]["runtime_activity_items"]))
        self.assertEqual(status["task_runtime_activity"][0]["label"], "작업 취소 요청")
        self.assertEqual(prompt_snapshot["tasks"]["runtime_activity_count"], status["tasks"]["runtime_activity_count"])
        self.assertTrue(any(row.get("label") == "재지시 재실행 중" for row in prompt_snapshot["tasks"]["runtime_activity_items"]))
        self.assertTrue(all(row.get("type") == "task_runtime" for row in status["task_runtime_activity"]))

    def test_task_runtime_activity_uses_heartbeat_time_and_filters_general_heartbeat(self):
        space = PREFIX + "taskrunfilter"
        member = PREFIX + "agent_wrunfilter"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "runtimefilter"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="runtimefilter",
            objective="runtime filter task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        room_manager.request_task_steering(
            space,
            "runtimefilter",
            action="request_progress",
            instruction="진행 보고",
        )
        task_registry.record_heartbeat(
            space,
            task_id="runtimefilter",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="engine_start",
            note="일반 시작 heartbeat",
        )
        task_registry.record_heartbeat(
            space,
            task_id="runtimefilter",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="steering_progress_seen",
            note="진행 보고 요청 확인",
        )
        registry_path = SPACES / space / "task_registry.jsonl"
        registry_rows = [
            json.loads(line)
            for line in registry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for row in registry_rows:
            if row.get("task_id") != "runtimefilter" or row.get("event") != "task_heartbeat":
                continue
            if row.get("heartbeat_phase") == "engine_start":
                row["last_heartbeat_at"] = "2001-01-01T00:00:01"
            if row.get("heartbeat_phase") == "steering_progress_seen":
                row["last_heartbeat_at"] = "2001-01-01T00:00:05"
                row["latest_steering_requested_at"] = "2000-01-01T00:00:00"
        registry_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in registry_rows) + "\n",
            encoding="utf-8",
        )

        rows = task_registry.runtime_activity(space, limit=10)
        progress_seen = next(row for row in rows if row["label"] == "진행보고 요청 확인")

        self.assertNotIn("engine_start", [row.get("heartbeat_phase") for row in rows])
        self.assertEqual(progress_seen["at"], "2001-01-01T00:00:05")
        self.assertEqual(progress_seen["at"], progress_seen["last_heartbeat_at"])
        self.assertEqual(progress_seen["latest_steering_requested_at"], "2000-01-01T00:00:00")

    def test_task_active_items_prioritize_attention_before_prompt_limit(self):
        space = PREFIX + "taskpriority"
        member = PREFIX + "agent_wprio"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        for idx in range(10):
            task_id = f"plain{idx}"
            task_registry.create_task(
                space,
                worker=member,
                task_id=task_id,
                objective=f"plain task {idx}",
                work_dir=PEOPLE / member / "공간" / space / "작업" / task_id,
                runtime_info={"engine": "codex", "model": "gpt-5.5"},
                context=context,
            )
        important_dir = PEOPLE / member / "공간" / space / "작업" / "zzimportant"
        important = task_registry.create_task(
            space,
            worker=member,
            task_id="zzimportant",
            objective="important runtime task",
            work_dir=important_dir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        task_registry.record_heartbeat(
            space,
            task_id="zzimportant",
            worker=member,
            work_dir=important_dir,
            task_pack=important["task_pack"],
            phase="engine_restarting_for_revise",
            note="중요한 재실행",
        )
        status = room_manager.status(space)
        prompt_snapshot = room_manager._prompt_room_status_snapshot(space)

        self.assertEqual(status["tasks"]["active_items"][0]["task_id"], "zzimportant")
        self.assertTrue(any(item["task_id"] == "zzimportant" for item in prompt_snapshot["tasks"]["active_items"]))

    def test_work_settings_merge_precedence_and_task_pack_policy(self):
        space = PREFIX + "worksettings"
        member = PREFIX + "agent_wwset"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "runner_timeout_sec": 600,
            "heartbeat_interval_sec": 20,
            "heartbeat_stale_ms": 120000,
            "progress_report_due_ms": 120000,
        })
        work_settings.write_person_settings(member, {
            "runner_timeout_sec": 700,
            "heartbeat_interval_sec": 11,
        })
        merged_person = work_settings.write_person_settings(member, {"heartbeat_interval_sec": 9})
        work_settings.write_folder_settings(PEOPLE / member / "공간" / space, {
            "heartbeat_interval_sec": 4,
            "heartbeat_stale_ms": 150000,
            "progress_report_due_ms": 180000,
        })
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "policy"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="policy",
            objective="policy task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        capabilities = json.loads((wdir / "runtime_capabilities.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        item = status["tasks"]["active_items"][0]

        self.assertEqual(merged_person["runner_timeout_sec"], 700)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["runner_timeout_sec"], 700)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["heartbeat_interval_sec"], 4)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["heartbeat_stale_ms"], 150000)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["progress_report_due_ms"], 180000)
        self.assertEqual(work_status["runner_timeout_sec"], 700)
        self.assertEqual(work_status["heartbeat_interval_sec"], 4)
        self.assertEqual(capabilities["runner_timeout_sec"], 700)
        self.assertEqual(capabilities["heartbeat_stale_ms"], 150000)
        self.assertEqual(item["heartbeat_stale_threshold_ms"], 150000)
        self.assertEqual(item["progress_report_due_threshold_ms"], 180000)
        self.assertIn(f"seat:{member}->{space}", item["work_settings_source_chain"])

    def test_seat_work_settings_api_controls_member_space_policy(self):
        space = PREFIX + "seatwset"
        member = PREFIX + "agent_wseat"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "runner_timeout_sec": 600,
            "heartbeat_interval_sec": 20,
            "heartbeat_stale_ms": 90000,
            "progress_report_due_ms": 90000,
        })
        work_settings.write_person_settings(member, {
            "runner_timeout_sec": 700,
            "heartbeat_stale_ms": 120000,
        })

        before = spaces_core.read_seat_work_settings(space, member)
        updated = spaces_core.set_seat_work_settings(space, member, {
            "runner_timeout_sec": 999,
            "heartbeat_interval_sec": 4,
            "heartbeat_stale_ms": 999000,
            "progress_report_due_ms": 180000,
            "configured_keys": ["heartbeat_interval_sec", "progress_report_due_ms"],
        })
        work_settings.write_person_settings(member, {"runner_timeout_sec": 720})
        after_person_update = spaces_core.read_seat_work_settings(space, member)
        listed = next(s for s in spaces_core.list_spaces() if s["토큰"] == space)
        listed_member = next(m for m in listed["멤버"] if m["토큰"] == member)
        post = room_manager.post(space, "좌석 설정 작업", run_manager=False, client_message_id="client-1")
        wdir = PEOPLE / member / "공간" / space / "작업" / "seatpolicy"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="seatpolicy",
            objective="seat policy task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=post["orchestration"],
        )

        self.assertEqual(before["effective_settings"]["runner_timeout_sec"], 700)
        self.assertEqual(updated["effective_settings"]["runner_timeout_sec"], 700)
        self.assertEqual(updated["effective_settings"]["heartbeat_interval_sec"], 4)
        self.assertEqual(updated["effective_settings"]["heartbeat_stale_ms"], 120000)
        self.assertEqual(updated["effective_settings"]["progress_report_due_ms"], 180000)
        self.assertEqual(updated["seat_settings"]["configured_keys"], ["heartbeat_interval_sec", "progress_report_due_ms"])
        self.assertEqual(after_person_update["effective_settings"]["runner_timeout_sec"], 720)
        self.assertEqual(after_person_update["effective_settings"]["heartbeat_stale_ms"], 120000)
        self.assertEqual(after_person_update["effective_settings"]["heartbeat_interval_sec"], 4)
        self.assertEqual(listed_member["작업설정"]["heartbeat_interval_sec"], 4)
        self.assertIn(f"seat:{member}->{space}", updated["effective_settings"]["source_chain"])
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["runner_timeout_sec"], 720)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["heartbeat_interval_sec"], 4)

    def test_work_settings_updates_can_be_recorded_to_room_activity(self):
        space = PREFIX + "wsetact"
        member = PREFIX + "agent_wsetact"
        make_space(space, [member])

        space_result = spaces_core.set_work_settings(space, {
            "runner_timeout_sec": 610,
            "heartbeat_interval_sec": 8,
        })
        room_manager.record_space_work_settings_updated(space, space_result, actor="tester")
        seat_result = spaces_core.set_seat_work_settings(space, member, {
            "heartbeat_interval_sec": 4,
            "configured_keys": ["heartbeat_interval_sec"],
        })
        room_manager.record_seat_work_settings_updated(space, member, seat_result, actor="tester")

        rows = room_manager.activity(space, limit=10)
        states = [row.get("상태") for row in rows]
        seat_row = next(row for row in rows if row.get("상태") == "seat_work_settings_updated")

        self.assertIn("space_work_settings_updated", states)
        self.assertIn("seat_work_settings_updated", states)
        self.assertIn("timeout 610s", rows[-2]["detail"])
        self.assertEqual(seat_row["target"], member)
        self.assertEqual(seat_row["configured_keys"], ["heartbeat_interval_sec"])
        self.assertIn("직접 heartbeat_interval_sec", seat_row["detail"])

    def test_dashboard_activity_api_and_work_settings_patch_record_activity(self):
        space = PREFIX + "wsetroute"
        member = PREFIX + "agent_wsetroute"
        make_space(space, [member])
        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)

        space_response = client.patch(
            f"/api/spaces/{space}/work-settings",
            json={"runner_timeout_sec": 612, "heartbeat_interval_sec": 7},
        )
        seat_response = client.patch(
            f"/api/spaces/{space}/members/{member}/work-settings",
            json={"heartbeat_interval_sec": 5, "configured_keys": ["heartbeat_interval_sec"]},
        )
        activity_response = client.get(f"/api/spaces/{space}/activity?limit=20")

        self.assertEqual(space_response.status_code, 200)
        self.assertEqual(seat_response.status_code, 200)
        self.assertEqual(activity_response.status_code, 200)
        rows = activity_response.json()
        states = [row.get("상태") for row in rows]
        self.assertIn("space_work_settings_updated", states)
        self.assertIn("seat_work_settings_updated", states)
        self.assertTrue(any("timeout 612s" in row.get("detail", "") for row in rows))
        self.assertTrue(any(row.get("target") == member and row.get("configured_keys") == ["heartbeat_interval_sec"] for row in rows))

    def test_dashboard_representative_post_forces_space_manager_even_if_client_sends_false(self):
        space = PREFIX + "dashmgrforce"
        make_space(space)
        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)
        original_queue_manager = room_manager.queue_manager
        original_tick = room_manager.tick
        calls = {"queue": 0, "tick": 0}

        def fake_queue_manager(space_id, event, context=None):
            calls["queue"] += 1
            self.assertEqual(space_id, space)
            self.assertIn("대표가 방에 메시지를 남김", event)
            self.assertEqual(context.get("space_id"), space)
            return {"queue_event_type": "manager_queued"}

        def fake_tick(space_id, event="방 진행 필요", context=None, **kwargs):
            calls["tick"] += 1
            self.assertEqual(space_id, space)
            self.assertIn("대표가 방에 메시지를 남김", event)
            self.assertEqual(context.get("space_id"), space)
            return {"ok": True, "events": [{"type": "manager_decision", "action": "stop"}]}

        try:
            room_manager.queue_manager = fake_queue_manager
            room_manager.tick = fake_tick
            response = client.post(
                f"/api/spaces/{space}/post",
                json={
                    "text": "하이",
                    "requester": "대표",
                    "run_manager": False,
                    "client_message_id": "dash-client-1",
                },
            )
        finally:
            room_manager.queue_manager = original_queue_manager
            room_manager.tick = original_tick

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, {"queue": 1, "tick": 1})
        rows = read(space)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["run_manager_requested"])

    def test_dashboard_requester_spoof_cannot_skip_space_manager(self):
        space = PREFIX + "dashmgrspoof"
        make_space(space)
        app = FastAPI()
        app.include_router(dashboard_spaces_router.router)
        client = TestClient(app)
        original_queue_manager = room_manager.queue_manager
        original_tick = room_manager.tick
        calls = {"queue": 0, "tick": 0}

        def fake_queue_manager(*args, **kwargs):
            calls["queue"] += 1
            return {"queue_event_type": "manager_queued"}

        def fake_tick(*args, **kwargs):
            calls["tick"] += 1
            return {"ok": True, "events": []}

        try:
            room_manager.queue_manager = fake_queue_manager
            room_manager.tick = fake_tick
            response = client.post(
                f"/api/spaces/{space}/post",
                json={
                    "text": "내부 진행 기록",
                    "requester": "관리자에이전트",
                    "run_manager": False,
                    "client_message_id": "dash-internal-1",
                },
            )
        finally:
            room_manager.queue_manager = original_queue_manager
            room_manager.tick = original_tick

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, {"queue": 1, "tick": 1})
        rows = read(space)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["run_manager_requested"])

    def test_shared_chat_policy_forces_external_manager_even_if_requested_false(self):
        self.assertTrue(chat_policy.should_run_space_manager("대표", False))
        self.assertTrue(chat_policy.should_run_space_manager("", False))
        self.assertTrue(chat_policy.should_run_space_manager(None, False))
        self.assertTrue(chat_policy.should_run_space_manager("대표", True))
        self.assertTrue(chat_policy.should_run_space_manager("관리자에이전트", False))
        self.assertTrue(chat_policy.should_run_space_manager("공간관리", False))
        self.assertTrue(chat_policy.should_run_space_manager("대표", False, trusted_internal=True))
        self.assertFalse(chat_policy.should_run_space_manager("관리자에이전트", False, trusted_internal=True))
        self.assertFalse(chat_policy.should_run_space_manager("공간관리", False, trusted_internal=True))
        self.assertTrue(chat_policy.should_run_space_manager("공간관리", True))

    def test_core_internal_post_can_still_record_without_manager_when_explicit(self):
        space = PREFIX + "coreinternal"
        make_space(space)
        original_tick = room_manager.tick
        calls = {"tick": 0}

        def fake_tick(*args, **kwargs):
            calls["tick"] += 1
            return {"ok": True, "events": []}

        try:
            room_manager.tick = fake_tick
            result = room_manager.post(
                space,
                "내부 진행 기록",
                requester="관리자에이전트",
                run_manager=False,
                client_message_id="core-internal-1",
                manager_requested=False,
            )
        finally:
            room_manager.tick = original_tick

        self.assertEqual(calls, {"tick": 0})
        self.assertTrue(result["ok"])
        rows = read(space)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["run_manager_requested"])

    def test_engine_chat_defaults_to_space_manager_not_direct_agent(self):
        space = PREFIX + "enginechatmanaged"
        member = PREFIX + "agent1111"
        make_space(space, [member])
        original_run_engine = room_manager.engine.run_engine
        calls = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            calls.append(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return '{"action":"stop","wake":"","message":"","reason":"테스트 stop"}'
            return "DIRECT_AGENT_SHOULD_NOT_RUN"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = engine.chat(member, space, "하이", requester="대표", client_message_id="engine-chat-1")
        finally:
            room_manager.engine.run_engine = original_run_engine

        payload = json.loads(result)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["managed_by"], "space_manager")
        self.assertEqual(payload["direct_target_ignored"], member)
        self.assertTrue(calls)
        self.assertTrue(all(path == SPACES / space / MANAGER_DIRNAME for path in calls))
        rows = read(space)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["역할"], "user")
        self.assertEqual(rows[0]["내용"], "하이")
        self.assertTrue(rows[0]["run_manager_requested"])

    def test_engine_direct_diagnostic_does_not_write_transcript(self):
        space = PREFIX + "enginediag"
        member = PREFIX + "agent2222"
        make_space(space, [member])
        original_run_engine = engine.run_engine
        calls = []

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            calls.append(cwd)
            return "진단 응답"

        try:
            engine.run_engine = fake_run_engine
            with self.assertRaises(ValueError):
                engine.chat(member, space, "직접 점검", direct_diagnostic=True)
            reply = engine.chat(member, space, "직접 점검", record_request=False, direct_diagnostic=True)
        finally:
            engine.run_engine = original_run_engine

        self.assertEqual(reply, "진단 응답")
        self.assertEqual(calls, [PEOPLE / member / "공간" / space])
        self.assertEqual(read(space), [])

    def test_room_status_chat_flow_shows_manager_queue_and_decision(self):
        space = PREFIX + "chatflow"
        make_space(space)
        post = room_manager.post(space, "하이", run_manager=False, client_message_id="flow-1")
        event = "대표가 방에 메시지를 남김: 하이"
        room_manager.queue_manager(space, event, post["orchestration"])

        queued = room_manager.status(space)["chat_flow"]
        self.assertEqual(queued["schema"], "RoomChatFlowSnapshot.v1")
        self.assertEqual(queued["latest_message"]["event_seq"], post["ack"]["event_seq"])
        phase_map = {phase["key"]: phase for phase in queued["phases"]}
        self.assertEqual(phase_map["input"]["state"], "done")
        self.assertEqual(phase_map["manager"]["state"], "current")
        self.assertEqual(phase_map["decision"]["state"], "pending")

        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return '{"action":"stop","wake":"","message":"","reason":"실증용 정지"}'

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.tick(space, event, post["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine

        stopped = room_manager.status(space)["chat_flow"]
        phase_map = {phase["key"]: phase for phase in stopped["phases"]}
        self.assertEqual(phase_map["manager"]["state"], "done")
        self.assertEqual(phase_map["decision"]["state"], "done")
        self.assertEqual(phase_map["decision"]["action"], "stop")
        self.assertEqual(phase_map["output"]["state"], "stopped")
        self.assertEqual(stopped["decision"]["action"], "stop")

    def test_room_status_chat_flow_surfaces_manager_failure(self):
        space = PREFIX + "chatflowfail"
        make_space(space)
        post = room_manager.post(space, "하이", run_manager=False, client_message_id="flow-fail-1")
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            return "not-json"

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.tick(space, "대표가 방에 메시지를 남김: 하이", post["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine

        flow = room_manager.status(space)["chat_flow"]
        phase_map = {phase["key"]: phase for phase in flow["phases"]}
        self.assertEqual(phase_map["manager"]["state"], "failed")
        self.assertEqual(phase_map["decision"]["state"], "failed")
        self.assertEqual(phase_map["output"]["state"], "failed")
        self.assertEqual(flow["decision"]["action"], "manager_failed")
        self.assertTrue(flow["blockers"])

    def test_room_status_chat_flow_shows_pass_reply_completion(self):
        space = PREFIX + "chatflowpass"
        member = PREFIX + "flowagent"
        make_space(space, [member])
        post = room_manager.post(space, "짧게 답해줘", run_manager=False, client_message_id="flow-pass-1")
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "짧게 답해줘",
                    "reason": "멤버가 한 명이라 답변을 넘김",
                }, ensure_ascii=False)
            return "흐름 실증 응답"

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.tick(space, "대표가 방에 메시지를 남김: 짧게 답해줘", post["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine

        flow = room_manager.status(space)["chat_flow"]
        phase_map = {phase["key"]: phase for phase in flow["phases"]}
        self.assertEqual(phase_map["manager"]["state"], "done")
        self.assertEqual(phase_map["decision"]["action"], "pass")
        self.assertEqual(phase_map["turn"]["state"], "done")
        self.assertEqual(phase_map["turn"]["target"], member)
        self.assertEqual(phase_map["output"]["state"], "done")
        self.assertTrue(any(row.get("역할") == "assistant" and row.get("내용") == "흐름 실증 응답" for row in read(space)))

    def test_room_status_chat_flow_ignores_unrelated_pending_release(self):
        space = PREFIX + "chatflowrelease"
        member = PREFIX + "flowreleaseagent"
        make_space(space, [member])
        enqueue_test_release(space, release_id="rel-unrelated")
        post = room_manager.post(space, "새 대화는 바로 답해줘", run_manager=False, client_message_id="flow-release-1")
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "새 대화는 바로 답해줘",
                    "reason": "별도 pending release와 무관한 새 대화",
                }, ensure_ascii=False)
            return "새 대화 응답"

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.tick(space, "대표가 방에 메시지를 남김: 새 대화는 바로 답해줘", post["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine

        flow = room_manager.status(space)["chat_flow"]
        phase_map = {phase["key"]: phase for phase in flow["phases"]}
        self.assertEqual(phase_map["decision"]["action"], "pass")
        self.assertEqual(phase_map["turn"]["state"], "done")
        self.assertEqual(phase_map["output"]["state"], "done")
        self.assertEqual(phase_map["output"]["pending_release_count"], 0)

    def test_room_status_chat_flow_survives_long_activity_tail(self):
        space = PREFIX + "chatflowtail"
        member = PREFIX + "flowtailagent"
        make_space(space, [member])
        post = room_manager.post(space, "긴 활동 뒤에도 보이는지 확인", run_manager=False, client_message_id="flow-tail-1")
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "긴 활동 뒤에도 보이는지 확인",
                    "reason": "activity tail 검증",
                }, ensure_ascii=False)
            return "tail 응답"

        try:
            room_manager.engine.run_engine = fake_run_engine
            room_manager.tick(space, "대표가 방에 메시지를 남김: 긴 활동 뒤에도 보이는지 확인", post["orchestration"])
        finally:
            room_manager.engine.run_engine = original_run_engine

        for idx in range(100):
            room_manager._append_activity(space, {
                "상태": "manager_decision",
                "시각": room_manager.now_iso(),
                "actor": "공간관리",
                "label": "무관한 오래 꼬리 활동",
                "action": "stop",
                "target": "",
                "detail": f"noise {idx}",
                "intent_id": f"noise-intent-{idx}",
                "source_event_seq": 900000 + idx,
            })

        flow = room_manager.status(space)["chat_flow"]
        phase_map = {phase["key"]: phase for phase in flow["phases"]}
        self.assertEqual(phase_map["manager"]["state"], "done")
        self.assertEqual(phase_map["decision"]["action"], "pass")
        self.assertEqual(phase_map["turn"]["state"], "done")
        self.assertEqual(phase_map["output"]["state"], "done")

    def test_cli_space_post_no_manager_cannot_skip_by_spoofing_internal_requester(self):
        calls = []

        def fake_post(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return {"ok": True, "ack": {"message_id": "msg-test"}, "events": []}

        argv = [
            "world.py",
            "space-post",
            "--space",
            "cli-space",
            "--text",
            "내부인 척",
            "--requester",
            "관리자에이전트",
            "--no-manager",
            "--client-message-id",
            "cli-1",
        ]
        with patch.object(sys, "argv", argv), patch.object(cli_world.room_manager, "post", fake_post):
            with redirect_stdout(io.StringIO()):
                cli_world.main()

        self.assertEqual(len(calls), 1)
        args = calls[0]["args"]
        kwargs = calls[0]["kwargs"]
        self.assertEqual(args[:3], ("cli-space", "내부인 척", "관리자에이전트"))
        self.assertTrue(args[3])
        self.assertEqual(args[4], "cli-1")
        self.assertTrue(kwargs["manager_requested"])

    def test_cli_chat_default_is_managed_and_direct_diagnostic_is_explicit(self):
        calls = []

        def fake_chat(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return "ok"

        managed_argv = [
            "world.py",
            "chat",
            "--person",
            "agent-1",
            "--space",
            "space-1",
            "--text",
            "하이",
            "--requester",
            "대표",
            "--client-message-id",
            "chat-1",
        ]
        diagnostic_argv = [
            "world.py",
            "chat",
            "--person",
            "agent-1",
            "--space",
            "space-1",
            "--text",
            "엔진 점검",
            "--direct-diagnostic",
        ]
        with patch.object(cli_world.engine, "chat", fake_chat):
            with patch.object(sys, "argv", managed_argv), redirect_stdout(io.StringIO()):
                cli_world.main()
            with patch.object(sys, "argv", diagnostic_argv), redirect_stdout(io.StringIO()):
                cli_world.main()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["args"][:4], ("agent-1", "space-1", "하이", "대표"))
        self.assertEqual(calls[0]["kwargs"], {"client_message_id": "chat-1"})
        self.assertEqual(calls[1]["args"][:4], ("agent-1", "space-1", "엔진 점검", "대표"))
        self.assertTrue(calls[1]["kwargs"]["direct_diagnostic"])
        self.assertFalse(calls[1]["kwargs"]["record_request"])

    def test_join_creates_non_overriding_seat_work_settings_file(self):
        person = people_core.create_person(PREFIX + "joinperson", engine="codex", model="gpt-5.5")
        space = spaces_core.create_space(PREFIX + "joinspace", engine="codex", model="gpt-5.5")
        work_settings.write_space_settings(space, {"runner_timeout_sec": 650})
        work_settings.write_person_settings(person, {"runner_timeout_sec": 750})

        joined = spaces_core.join(person, space)
        seat_file = PEOPLE / person / "공간" / space / work_settings.SETTINGS_FILENAME
        effective = work_settings.resolve_work_settings(space, person)

        self.assertTrue(joined)
        self.assertTrue(seat_file.exists())
        self.assertEqual(json.loads(seat_file.read_text(encoding="utf-8"))["configured_keys"], [])
        self.assertEqual(effective["runner_timeout_sec"], 750)
        self.assertNotIn(f"seat:{person}->{space}", effective["source_chain"])

    def test_late_join_projection_baseline_ignores_prejoin_history_but_detects_new_miss(self):
        person = people_core.create_person(PREFIX + "latejoinperson", engine="codex", model="gpt-5.5")
        space = spaces_core.create_space(PREFIX + "latejoinspace", engine="codex", model="gpt-5.5")
        room_manager.post(space, "입장 전 대화 1", run_manager=False, client_message_id="latejoin-old-1")
        room_manager.post(space, "입장 전 대화 2", run_manager=False, client_message_id="latejoin-old-2")

        joined = spaces_core.join(person, space)
        seat = PEOPLE / person / "공간" / space
        baseline_file = seat / spaces_core.PROJECTION_BASELINE_FILENAME
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
        member = json.loads((SPACES / space / "멤버.json").read_text(encoding="utf-8"))[0]
        joined_status = room_manager.status(space)

        self.assertTrue(joined)
        self.assertEqual(baseline["schema"], "SeatProjectionBaseline.v1")
        self.assertEqual(baseline["baseline_event_seq"], 2)
        self.assertEqual(member["projection_baseline_event_seq"], 2)
        self.assertEqual(joined_status["projection_lag"], 0)
        self.assertFalse(joined_status["status_stale"])
        self.assertEqual(joined_status["projection_lag_by_member"], [])

        room_manager.post(space, "입장 후 대화", run_manager=False, client_message_id="latejoin-new-1")
        after_delivery = room_manager.status(space)
        self.assertEqual(after_delivery["projection_lag"], 0)
        self.assertFalse(after_delivery["status_stale"])

        (seat / "대화.jsonl").write_text("", encoding="utf-8")
        missed = room_manager.status(space)
        lag = missed["projection_lag_by_member"][0]
        self.assertEqual(missed["projection_lag"], 1)
        self.assertTrue(missed["status_stale"])
        self.assertEqual(lag["token"], person)
        self.assertEqual(lag["tail_lag"], 1)
        self.assertEqual(lag["missing_count"], 1)
        self.assertEqual(lag["projection_baseline_event_seq"], 2)
        self.assertEqual(lag["projection_required_event_count"], 1)
        self.assertTrue(lag["late_join_baseline"])
        prompt_status = room_manager._prompt_room_status_snapshot(space)
        prompt_lag = prompt_status["member_projection_lag"][0]
        self.assertEqual(prompt_lag["projection_baseline_event_seq"], 2)
        self.assertEqual(prompt_lag["projection_required_event_count"], 1)
        self.assertTrue(prompt_lag["late_join_baseline"])

    def test_projection_without_baseline_still_detects_existing_member_missing_history(self):
        space = PREFIX + "legacyproj"
        member = PREFIX + "agent_legacyproj"
        make_space(space, [member])
        room_manager.post(space, "기존 멤버 대화 1", run_manager=False, client_message_id="legacy-proj-1")
        room_manager.post(space, "기존 멤버 대화 2", run_manager=False, client_message_id="legacy-proj-2")
        seat_file = PEOPLE / member / "공간" / space / "대화.jsonl"
        seat_file.write_text("", encoding="utf-8")

        status = room_manager.status(space)
        lag = status["projection_lag_by_member"][0]
        self.assertEqual(status["projection_lag"], 2)
        self.assertTrue(status["status_stale"])
        self.assertEqual(lag["missing_count"], 2)
        self.assertEqual(lag["projection_baseline_event_seq"], 0)
        self.assertEqual(lag["projection_required_event_count"], 2)
        self.assertFalse(lag["late_join_baseline"])

    def test_late_join_blocks_concurrent_post_until_member_projection_is_registered(self):
        person = people_core.create_person(PREFIX + "joinraceperson", engine="codex", model="gpt-5.5")
        space = spaces_core.create_space(PREFIX + "joinracespace", engine="codex", model="gpt-5.5")
        room_manager.post(space, "입장 전 대화", run_manager=False, client_message_id="joinrace-old")
        original_copy_runtime = spaces_core.runtime.copy_runtime
        join_inside_lock = threading.Event()
        release_join = threading.Event()
        results = {}
        errors = []

        def slow_copy_runtime(*args, **kwargs):
            join_inside_lock.set()
            if not release_join.wait(5):
                raise TimeoutError("join race test was not released")
            return original_copy_runtime(*args, **kwargs)

        def run_join():
            try:
                results["joined"] = spaces_core.join(person, space)
            except Exception as exc:
                errors.append(exc)

        def run_post():
            try:
                results["post"] = room_manager.post(
                    space,
                    "join 중 들어온 대화",
                    run_manager=False,
                    client_message_id="joinrace-during",
                )
            except Exception as exc:
                errors.append(exc)

        try:
            spaces_core.runtime.copy_runtime = slow_copy_runtime
            join_thread = threading.Thread(target=run_join)
            join_thread.start()
            self.assertTrue(join_inside_lock.wait(5), "join did not enter locked section")
            post_thread = threading.Thread(target=run_post)
            post_thread.start()
            time.sleep(0.05)
            self.assertTrue(post_thread.is_alive(), "post should wait for join transcript lock")
            release_join.set()
            join_thread.join(timeout=8)
            post_thread.join(timeout=8)
        finally:
            release_join.set()
            spaces_core.runtime.copy_runtime = original_copy_runtime

        if errors:
            raise errors[0]
        self.assertTrue(results["joined"])
        self.assertFalse(results["post"]["ack"]["duplicate"])
        status = room_manager.status(space)
        seat_file = PEOPLE / person / "공간" / space / "대화.jsonl"
        seat_rows = [
            json.loads(line)
            for line in seat_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        seat_text = seat_file.read_text(encoding="utf-8")
        self.assertEqual(status["projection_lag"], 0)
        self.assertFalse(status["status_stale"])
        self.assertIn("join 중 들어온 대화", seat_text)
        self.assertNotIn("입장 전 대화", seat_text)
        self.assertEqual(len(read(space)), 2)
        self.assertEqual(len(seat_rows), 1)

    def test_preexisting_task_work_settings_override_create_policy(self):
        space = PREFIX + "taskwsetpre"
        member = PREFIX + "agent_wsetpre"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "runner_timeout_sec": 600,
            "heartbeat_interval_sec": 20,
        })
        work_settings.write_person_settings(member, {
            "runner_timeout_sec": 700,
        })
        wdir = PEOPLE / member / "공간" / space / "작업" / "prepolicy"
        work_settings.write_folder_settings(wdir, {
            "runner_timeout_sec": 900,
            "heartbeat_interval_sec": 6,
        })
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="prepolicy",
            objective="pre policy task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=post["orchestration"],
        )
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        item = room_manager.status(space)["tasks"]["active_items"][0]

        self.assertEqual(created["task_pack"]["work_runtime_policy"]["runner_timeout_sec"], 900)
        self.assertEqual(created["task_pack"]["work_runtime_policy"]["heartbeat_interval_sec"], 6)
        self.assertEqual(work_status["runner_timeout_sec"], 900)
        self.assertEqual(item["runner_timeout_sec"], 900)
        self.assertIn("work:prepolicy", item["work_settings_source_chain"])

    def test_task_work_settings_threshold_survives_steering_heartbeat_and_cancel(self):
        space = PREFIX + "taskwset"
        member = PREFIX + "agent_wtwset"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "heartbeat_stale_ms": 120000,
            "progress_report_due_ms": 120000,
        })
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "taskpolicy"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="taskpolicy",
            objective="task policy task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        registry_path = SPACES / space / "task_registry.jsonl"
        rows = [
            json.loads(line)
            for line in registry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows[-1]["last_heartbeat_at"] = (datetime.now() - timedelta(seconds=90)).isoformat(timespec="seconds")
        registry_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        not_due = room_manager.status(space)

        self.assertEqual(not_due["tasks"]["stale_task_count"], 0)
        self.assertEqual(not_due["tasks"]["progress_report_due_count"], 0)

        updated = room_manager.update_task_work_settings(
            space,
            "taskpolicy",
            {"heartbeat_stale_ms": 60000, "progress_report_due_ms": 60000},
        )
        due = room_manager.status(space)
        item = due["tasks"]["active_items"][0]
        self.assertTrue(updated["ok"])
        self.assertEqual(item["heartbeat_stale_threshold_ms"], 60000)
        self.assertEqual(item["progress_report_due_threshold_ms"], 60000)
        self.assertTrue(item["heartbeat_stale"])
        self.assertTrue(item["progress_report_due"])

        room_manager.request_task_steering(space, "taskpolicy", action="request_progress", instruction="진행 보고")
        after_steering = room_manager.status(space)["tasks"]["active_items"][0]
        stale_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        stale_status.update({
            "heartbeat_stale_ms": 120000,
            "progress_report_due_ms": 120000,
            "heartbeat_stale_threshold_ms": 120000,
            "progress_report_due_threshold_ms": 120000,
        })
        (wdir / "work_status.json").write_text(json.dumps(stale_status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        task_registry.record_heartbeat(
            space,
            task_id="taskpolicy",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="steering_progress_seen",
            note="진행 보고 확인",
        )
        after_heartbeat = room_manager.status(space)["tasks"]["active_items"][0]
        room_manager.request_task_cancel(space, "taskpolicy", reason="설정 보존 확인")
        after_cancel = room_manager.status(space)["tasks"]["active_items"][0]

        self.assertEqual(after_steering["heartbeat_stale_threshold_ms"], 60000)
        self.assertEqual(after_heartbeat["heartbeat_stale_threshold_ms"], 60000)
        self.assertEqual(after_cancel["heartbeat_stale_threshold_ms"], 60000)
        self.assertEqual(after_cancel["progress_report_due_threshold_ms"], 60000)

    def test_finalize_preserves_latest_work_settings_over_stale_status(self):
        space = PREFIX + "taskwsetfin"
        member = PREFIX + "agent_wsetfin"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        wdir = PEOPLE / member / "공간" / space / "작업" / "finpolicy"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="finpolicy",
            objective="final policy task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=post["orchestration"],
        )
        room_manager.update_task_work_settings(
            space,
            "finpolicy",
            {"heartbeat_stale_ms": 60000, "progress_report_due_ms": 60000, "runner_timeout_sec": 450},
        )
        stale_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        stale_status.update({
            "runner_timeout_sec": 300,
            "heartbeat_stale_ms": 120000,
            "progress_report_due_ms": 120000,
            "heartbeat_stale_threshold_ms": 120000,
            "progress_report_due_threshold_ms": 120000,
        })
        (wdir / "work_status.json").write_text(json.dumps(stale_status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (wdir / "결과.md").write_text("완료", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        (wdir / "레슨적용보고.json").write_text(
            json.dumps({"schema": "LessonApplicationReport.v1", "applications": []}, ensure_ascii=False),
            encoding="utf-8",
        )

        final = task_registry.finalize_task(
            space,
            task_id="finpolicy",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="final policy task",
        )
        latest_row = [
            json.loads(line)
            for line in (SPACES / space / "task_registry.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ][-1]

        self.assertEqual(final["work_status"]["runner_timeout_sec"], 450)
        self.assertEqual(final["work_status"]["heartbeat_stale_threshold_ms"], 60000)
        self.assertEqual(latest_row["progress_report_due_threshold_ms"], 60000)

    def test_engine_work_passes_work_settings_to_runner(self):
        space = PREFIX + "engwset"
        member = PREFIX + "agent_wengset"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "runner_timeout_sec": 123,
            "heartbeat_interval_sec": 7,
            "heartbeat_stale_ms": 120000,
            "progress_report_due_ms": 180000,
        })
        captured = {}
        original_polling = engine.run_engine_polling

        def fake_polling(cwd, prompt, *args, **kwargs):
            captured.update(kwargs)
            cwd = Path(cwd)
            (cwd / "결과.md").write_text("완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            (cwd / "레슨적용보고.json").write_text(
                json.dumps({"schema": "LessonApplicationReport.v1", "applications": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            return "ok"

        try:
            engine.run_engine_polling = fake_polling
            result = engine.work(member, space, "설정 runner 확인")
        finally:
            engine.run_engine_polling = original_polling

        wdir = PEOPLE / member / "공간" / space / "작업" / result["작업코드"]
        task_pack = json.loads((wdir / "task_pack.json").read_text(encoding="utf-8"))

        self.assertEqual(captured["timeout"], 123)
        self.assertEqual(captured["heartbeat_interval"], 7)
        self.assertEqual(task_pack["work_runtime_policy"]["runner_timeout_sec"], 123)
        self.assertEqual(task_pack["work_runtime_policy"]["heartbeat_interval_sec"], 7)

    def test_engine_work_reloads_task_settings_before_revise_restart(self):
        space = PREFIX + "engwsetrev"
        member = PREFIX + "agent_wengrev"
        make_space(space, [member])
        work_settings.write_space_settings(space, {
            "runner_timeout_sec": 120,
            "heartbeat_interval_sec": 9,
        })
        calls = []
        original_polling = engine.run_engine_polling

        def fake_polling(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            calls.append({"timeout": kwargs.get("timeout"), "heartbeat_interval": kwargs.get("heartbeat_interval")})
            if len(calls) == 1:
                room_manager.request_task_steering(
                    space,
                    cwd.name,
                    action="revise_task",
                    instruction="설정 변경 후 재실행",
                )
                room_manager.update_task_work_settings(
                    space,
                    cwd.name,
                    {"runner_timeout_sec": 240, "heartbeat_interval_sec": 3},
                )
                kwargs["cancel_check"]()
                return "(엔진 취소됨: revise_task)"
            (cwd / "결과.md").write_text("재실행 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            (cwd / "레슨적용보고.json").write_text(
                json.dumps({"schema": "LessonApplicationReport.v1", "applications": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            return "ok"

        try:
            engine.run_engine_polling = fake_polling
            result = engine.work(member, space, "설정 변경 뒤 재실행")
        finally:
            engine.run_engine_polling = original_polling

        self.assertEqual(result["상태"], "done")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0]["timeout"], 120)
        self.assertEqual(calls[0]["heartbeat_interval"], 9)
        self.assertEqual(calls[1]["timeout"], 240)
        self.assertEqual(calls[1]["heartbeat_interval"], 3)

    def test_task_snapshot_marks_stale_heartbeat_without_closing_task(self):
        space = PREFIX + "taskhbstale"
        member = PREFIX + "agent_whstale"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "hbstale"
        task_registry.create_task(
            space,
            worker=member,
            task_id="hbstale",
            objective="stale heartbeat task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        fresh = room_manager.status(space)
        self.assertEqual(fresh["tasks"]["running_count"], 1)
        self.assertEqual(fresh["tasks"]["stale_task_count"], 0)
        self.assertFalse(fresh["tasks"]["active_items"][0]["heartbeat_stale"])
        self.assertEqual(
            fresh["tasks"]["active_items"][0]["heartbeat_stale_threshold_ms"],
            task_registry.TASK_HEARTBEAT_STALE_MS,
        )

        registry_path = SPACES / space / "task_registry.jsonl"
        rows = [
            json.loads(line)
            for line in registry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows[-1]["last_heartbeat_at"] = "2000-01-01T00:00:00"
        rows[-1]["heartbeat_phase"] = "old_phase"
        registry_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        stale = room_manager.status(space)
        stale_item = stale["tasks"]["active_items"][0]
        self.assertEqual(stale["tasks"]["running_count"], 1)
        self.assertEqual(stale["tasks"]["stale_task_count"], 1)
        self.assertTrue(stale_item["heartbeat_stale"])
        self.assertGreater(stale_item["heartbeat_age_ms"], task_registry.TASK_HEARTBEAT_STALE_MS)
        self.assertEqual(stale_item["heartbeat_stale_threshold_ms"], task_registry.TASK_HEARTBEAT_STALE_MS)
        self.assertEqual(stale["release_queue"]["release_count"], 0)
        self.assertTrue(any("heartbeat" in action for action in stale["recovery_actions"]))

    def test_status_marks_progress_report_due_without_writing_steering(self):
        space = PREFIX + "taskreportdue"
        member = PREFIX + "agent_wrdue"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "reportdue"
        task_registry.create_task(
            space,
            worker=member,
            task_id="reportdue",
            objective="report due task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        self._force_task_heartbeat_old(space, "reportdue")
        work_status_before = (wdir / "work_status.json").read_text(encoding="utf-8")

        first = room_manager.status(space)
        prompt_snapshot = room_manager._prompt_room_status_snapshot(space)
        second = room_manager.status(space)

        self.assertEqual(len(list((wdir / "steering").glob("*.json"))), 0)
        self.assertEqual((wdir / "work_status.json").read_text(encoding="utf-8"), work_status_before)
        self.assertEqual(first["tasks"]["progress_report_due_count"], 1)
        self.assertEqual(second["tasks"]["progress_report_due_count"], 1)
        self.assertTrue(first["tasks"]["active_items"][0]["progress_report_due"])
        self.assertTrue(prompt_snapshot["tasks"]["active_items"][0]["progress_report_due"])

    def test_tick_requests_due_progress_once_without_generation_or_cancel(self):
        space = PREFIX + "taskautoprog"
        member = PREFIX + "agent_waprog"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "autoprog"
        task_registry.create_task(
            space,
            worker=member,
            task_id="autoprog",
            objective="auto progress task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        self._force_task_heartbeat_old(space, "autoprog")
        before_generation = orchestration.current_generation(space)
        original_run_engine = room_manager.engine.run_engine

        try:
            room_manager.engine.run_engine = lambda *args, **kwargs: json.dumps({
                "action": "stop",
                "message": "",
                "reason": "자동 진행 보고 요청 후 대기",
            }, ensure_ascii=False)
            first = room_manager.tick(space, "heartbeat progress due", context)
            work_status_after_first = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
            second = room_manager.tick(space, "heartbeat progress due retry", context)
            work_status_after_second = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))
        finally:
            room_manager.engine.run_engine = original_run_engine

        steering_files = sorted((wdir / "steering").glob("*.json"))
        steering = json.loads(steering_files[0].read_text(encoding="utf-8"))
        status = room_manager.status(space)
        activities = room_manager.activity(space, limit=20)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(any(e.get("type") == "task_progress_due_requested" for e in first["events"]))
        self.assertFalse(any(e.get("type") == "task_progress_due_requested" for e in second["events"]))
        self.assertEqual(len(steering_files), 1)
        self.assertEqual(steering["action"], "request_progress")
        self.assertEqual(steering["reason_code"], task_registry.TASK_PROGRESS_REPORT_DUE_REASON_CODE)
        self.assertEqual(work_status_after_first["latest_steering_action"], "request_progress")
        self.assertEqual(work_status_after_first["latest_steering_reason_code"], task_registry.TASK_PROGRESS_REPORT_DUE_REASON_CODE)
        self.assertEqual(work_status_after_second["updated_at"], work_status_after_first["updated_at"])
        self.assertEqual(status["tasks"]["progress_report_due_count"], 0)
        self.assertEqual(status["tasks"]["progress_report_requested_count"], 1)
        self.assertFalse((wdir / "취소요청.json").exists())
        self.assertEqual(orchestration.current_generation(space), before_generation)
        self.assertEqual(len([row for row in activities if row.get("steering_reason_code") == task_registry.TASK_PROGRESS_REPORT_DUE_REASON_CODE]), 1)

    def test_stale_generation_tick_does_not_request_due_progress(self):
        space = PREFIX + "taskstalectx"
        member = PREFIX + "agent_wstctx"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        stale_context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "stalectx"
        task_registry.create_task(
            space,
            worker=member,
            task_id="stalectx",
            objective="stale context due task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=stale_context,
        )
        self._force_task_heartbeat_old(space, "stalectx")
        orchestration.advance_generation(space, "newer message before stale tick")
        original_run_engine = room_manager.engine.run_engine

        try:
            room_manager.engine.run_engine = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("engine should not run for stale context"))
            result = room_manager.tick(space, "old due tick", stale_context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertFalse(result["ok"])
        self.assertTrue(result["generation_stale"])
        self.assertFalse(any(e.get("type") == "task_progress_due_requested" for e in result["events"]))
        self.assertEqual(len(list((wdir / "steering").glob("*.json"))), 0)
        self.assertEqual(status["tasks"]["progress_report_due_count"], 1)

    def test_task_progress_request_writes_repeatable_steering_without_generation_or_cancel(self):
        space = PREFIX + "taskprogress"
        member = PREFIX + "agent_wprogress"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "progress"
        task_registry.create_task(
            space,
            worker=member,
            task_id="progress",
            objective="progress task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        before_generation = orchestration.current_generation(space)

        first = room_manager.request_task_steering(
            space,
            "progress",
            action="request_progress",
            instruction="진행 보고",
        )
        second = room_manager.request_task_steering(
            space,
            "progress",
            action="request_progress",
            instruction="진행 보고",
        )
        status = room_manager.status(space)
        steering_files = sorted((wdir / "steering").glob("*.json"))

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertFalse(first["duplicate"])
        self.assertFalse(second["duplicate"])
        self.assertEqual(orchestration.current_generation(space), before_generation)
        self.assertFalse((wdir / "취소요청.json").exists())
        self.assertEqual(len(steering_files), 2)
        self.assertEqual(status["tasks"]["pending_steering_count"], 0)
        self.assertEqual(status["tasks"]["active_items"][0]["latest_steering_action"], "request_progress")
        self.assertFalse(status["tasks"]["active_items"][0]["pending_steering_ack"])

    def test_manual_progress_suppresses_automatic_due_but_remains_repeatable(self):
        space = PREFIX + "taskmanualdue"
        member = PREFIX + "agent_wmanual"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "manualdue"
        task_registry.create_task(
            space,
            worker=member,
            task_id="manualdue",
            objective="manual due task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        self._force_task_heartbeat_old(space, "manualdue")
        original_run_engine = room_manager.engine.run_engine

        try:
            first = room_manager.request_task_steering(
                space,
                "manualdue",
                action="request_progress",
                instruction="진행 보고",
            )
            second = room_manager.request_task_steering(
                space,
                "manualdue",
                action="request_progress",
                instruction="진행 보고",
            )
            room_manager.engine.run_engine = lambda *args, **kwargs: json.dumps({
                "action": "stop",
                "message": "",
                "reason": "수동 보고 요청이 이미 있음",
            }, ensure_ascii=False)
            tick_result = room_manager.tick(space, "heartbeat progress due", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        steering_files = sorted((wdir / "steering").glob("*.json"))
        status = room_manager.status(space)

        self.assertFalse(first["duplicate"])
        self.assertFalse(second["duplicate"])
        self.assertTrue(tick_result["ok"])
        self.assertEqual(len(steering_files), 2)
        self.assertEqual(status["tasks"]["progress_report_due_count"], 0)
        self.assertEqual(status["tasks"]["progress_report_requested_count"], 1)

    def test_due_progress_skips_unacked_revise_steering(self):
        space = PREFIX + "taskrevisedue"
        member = PREFIX + "agent_wrdued"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "revisedue"
        task_registry.create_task(
            space,
            worker=member,
            task_id="revisedue",
            objective="revise due task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        self._force_task_heartbeat_old(space, "revisedue")
        original_run_engine = room_manager.engine.run_engine

        try:
            revise = room_manager.request_task_steering(
                space,
                "revisedue",
                action="revise_task",
                instruction="재지시를 먼저 반영",
            )
            room_manager.engine.run_engine = lambda *args, **kwargs: json.dumps({
                "action": "stop",
                "message": "",
                "reason": "재지시 ack 대기",
            }, ensure_ascii=False)
            tick_result = room_manager.tick(space, "heartbeat progress due", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        steering_files = sorted((wdir / "steering").glob("*.json"))
        status = room_manager.status(space)

        self.assertTrue(revise["pending_steering_ack"])
        self.assertTrue(tick_result["ok"])
        self.assertEqual(len(steering_files), 1)
        self.assertEqual(status["tasks"]["pending_steering_count"], 1)
        self.assertEqual(status["tasks"]["progress_report_due_count"], 0)
        self.assertEqual(status["tasks"]["active_items"][0]["latest_steering_action"], "revise_task")

    def test_manual_progress_after_unacked_revise_keeps_pending_ack_visible(self):
        space = PREFIX + "taskrevprog"
        member = PREFIX + "agent_wrpvis"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "revprog"
        task_registry.create_task(
            space,
            worker=member,
            task_id="revprog",
            objective="revise then progress task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        revise = room_manager.request_task_steering(
            space,
            "revprog",
            action="revise_task",
            instruction="먼저 이 재지시를 반영",
        )
        progress = room_manager.request_task_steering(
            space,
            "revprog",
            action="request_progress",
            instruction="현재 상황도 알려줘",
        )
        status = room_manager.status(space)
        work_status = json.loads((wdir / "work_status.json").read_text(encoding="utf-8"))

        self.assertTrue(revise["pending_steering_ack"])
        self.assertTrue(progress["pending_steering_ack"])
        self.assertEqual(len(list((wdir / "steering").glob("*.json"))), 2)
        self.assertEqual(status["tasks"]["pending_steering_count"], 1)
        self.assertTrue(status["tasks"]["active_items"][0]["pending_steering_ack"])
        self.assertEqual(status["tasks"]["active_items"][0]["latest_steering_action"], "request_progress")
        self.assertEqual(status["tasks"]["active_items"][0]["pending_ack_steering_action"], "revise_task")
        self.assertEqual(status["tasks"]["active_items"][0]["pending_ack_steering_seq"], revise["steering_seq"])
        self.assertIn("먼저 이 재지시", status["tasks"]["active_items"][0]["pending_ack_steering_instruction"])
        self.assertTrue(work_status["pending_steering_ack"])
        self.assertEqual(work_status["pending_ack_steering_action"], "revise_task")

    def test_equal_second_progress_request_does_not_suppress_later_due(self):
        space = PREFIX + "taskeqtime"
        member = PREFIX + "agent_weqtm"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "eqtime"
        task_registry.create_task(
            space,
            worker=member,
            task_id="eqtime",
            objective="equal timestamp task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        room_manager.request_task_steering(
            space,
            "eqtime",
            action="request_progress",
            instruction="진행 보고",
        )
        registry_path = SPACES / space / "task_registry.jsonl"
        rows = [
            json.loads(line)
            for line in registry_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for row in rows:
            if row.get("task_id") == "eqtime":
                row["last_heartbeat_at"] = "2000-01-01T00:00:00"
                row["heartbeat_phase"] = "same_second_heartbeat"
                if row.get("event") == "task_steering_requested":
                    row["steering_requested_at"] = "2000-01-01T00:00:00"
                    row["latest_steering_requested_at"] = "2000-01-01T00:00:00"
        registry_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

        status = room_manager.status(space)
        item = status["tasks"]["active_items"][0]
        self.assertFalse(item["progress_report_requested_since_heartbeat"])
        self.assertTrue(item["progress_report_due"])
        self.assertEqual(status["tasks"]["progress_report_due_count"], 1)

    def test_unacked_revise_task_blocks_release_until_worker_ack(self):
        space = PREFIX + "taskrevise"
        member = PREFIX + "agent_wrevise"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        unacked_dir = PEOPLE / member / "공간" / space / "작업" / "unacked"
        unacked = task_registry.create_task(
            space,
            worker=member,
            task_id="unacked",
            objective="revise unacked task",
            work_dir=unacked_dir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        revise = room_manager.request_task_steering(
            space,
            "unacked",
            action="revise_task",
            instruction="방향을 바꿔서 다시 정리",
        )
        mid_status = room_manager.status(space)
        (unacked_dir / "결과.md").write_text("재지시를 못 본 결과", encoding="utf-8")
        (unacked_dir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")

        final = task_registry.finalize_task(
            space,
            task_id="unacked",
            worker=member,
            work_dir=unacked_dir,
            task_pack=unacked["task_pack"],
            objective="revise unacked task",
        )
        release_request = json.loads((unacked_dir / "release_request.json").read_text(encoding="utf-8"))
        blocked_status = room_manager.status(space)

        self.assertTrue(revise["pending_steering_ack"])
        self.assertEqual(mid_status["tasks"]["pending_steering_count"], 1)
        self.assertTrue(mid_status["tasks"]["active_items"][0]["pending_steering_ack"])
        self.assertEqual(final["state"], "done")
        self.assertTrue(final["work_status"]["pending_steering_ack"])
        self.assertEqual(release_request["release_state"], "steering_unacknowledged")
        self.assertEqual(release_request["queue_state"], "not_enqueued")
        self.assertEqual(blocked_status["release_queue"]["release_count"], 0)

        acked_dir = PEOPLE / member / "공간" / space / "작업" / "acked"
        acked = task_registry.create_task(
            space,
            worker=member,
            task_id="acked",
            objective="revise acked task",
            work_dir=acked_dir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context={**context, "room_generation": orchestration.current_generation(space)},
        )
        acked_revise = room_manager.request_task_steering(
            space,
            "acked",
            action="revise_task",
            instruction="이 재지시는 반영됨",
        )
        work_status = json.loads((acked_dir / "work_status.json").read_text(encoding="utf-8"))
        work_status["last_seen_steering_seq"] = acked_revise["steering_seq"]
        work_status["pending_steering_ack"] = False
        (acked_dir / "work_status.json").write_text(json.dumps(work_status, ensure_ascii=False), encoding="utf-8")
        (acked_dir / "결과.md").write_text("재지시 반영 결과", encoding="utf-8")
        (acked_dir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")

        acked_final = task_registry.finalize_task(
            space,
            task_id="acked",
            worker=member,
            work_dir=acked_dir,
            task_pack=acked["task_pack"],
            objective="revise acked task",
        )
        acked_release_request = json.loads((acked_dir / "release_request.json").read_text(encoding="utf-8"))
        acked_status = room_manager.status(space)

        self.assertFalse(acked_final["work_status"]["pending_steering_ack"])
        self.assertEqual(acked_release_request["queue_state"], "enqueued")
        self.assertEqual(acked_status["release_queue"]["release_count"], 1)

    def test_done_task_with_missing_release_followup_is_visible_in_status_failures(self):
        space = PREFIX + "taskrelmiss"
        member = PREFIX + "agent_wrelmiss"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "relmiss"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="relmiss",
            objective="release follow-up interruption task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        (wdir / "결과.md").write_text("완료 결과", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        original_enqueue = task_registry.release_queue.enqueue_release

        def interrupted_enqueue(*args, **kwargs):
            raise RuntimeError("simulated release follow-up crash")

        try:
            task_registry.release_queue.enqueue_release = interrupted_enqueue
            with self.assertRaises(RuntimeError):
                task_registry.finalize_task(
                    space,
                    task_id="relmiss",
                    worker=member,
                    work_dir=wdir,
                    task_pack=created["task_pack"],
                    objective="release follow-up interruption task",
                )
        finally:
            task_registry.release_queue.enqueue_release = original_enqueue

        status = room_manager.status(space)
        self.assertEqual(status["tasks"]["release_followup_missing_count"], 1)
        self.assertEqual(status["tasks"]["release_followup_missing_items"][0]["task_id"], "relmiss")
        self.assertEqual(status["tasks"]["latest_release_followup_missing_task_id"], "relmiss")
        self.assertTrue(any(row.get("상태") == "task_release_followup_missing" for row in status["failures"]))
        self.assertTrue(any("release follow-up" in item for item in status["recovery_actions"]))
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_heartbeat_after_cancel_preserves_cancel_and_blocks_release(self):
        space = PREFIX + "taskhbcancel"
        member = PREFIX + "agent_wh08"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "hbcancel"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="hbcancel",
            objective="heartbeat cancel task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        room_manager.request_task_cancel(space, "hbcancel", reason="대표 취소")

        hb = task_registry.record_heartbeat(
            space,
            task_id="hbcancel",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="engine_returned",
            note="취소 이후 늦은 heartbeat",
        )
        (wdir / "결과.md").write_text("취소 이후 늦은 결과", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        final = task_registry.finalize_task(
            space,
            task_id="hbcancel",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="heartbeat cancel task",
        )
        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)

        self.assertEqual(hb["event"]["state"], "cancel_requested")
        self.assertTrue(hb["work_status"]["cancel_requested"])
        self.assertTrue(final["work_status"]["cancel_requested"])
        self.assertEqual(release_request["release_state"], "cancel_requested")
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_finalize_rechecks_cancel_before_release_enqueue(self):
        space = PREFIX + "taskcancelrace"
        member = PREFIX + "agent_wrace"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "cancelrace"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="cancelrace",
            objective="cancel race task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        (wdir / "결과.md").write_text("cancel race result", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        original_read_work_status = task_registry._read_work_status
        calls = {"count": 0}

        def racing_read_work_status(path):
            calls["count"] += 1
            old = original_read_work_status(path)
            if calls["count"] == 1:
                try:
                    task_registry._read_work_status = original_read_work_status
                    task_registry.request_cancel(space, "cancelrace", actor="대표", reason="finalize와 동시 취소")
                finally:
                    task_registry._read_work_status = racing_read_work_status
                return {**old, "cancel_requested": False, "cancellation_request_id": ""}
            return old

        try:
            task_registry._read_work_status = racing_read_work_status
            final = task_registry.finalize_task(
                space,
                task_id="cancelrace",
                worker=member,
                work_dir=wdir,
                task_pack=created["task_pack"],
                objective="cancel race task",
            )
        finally:
            task_registry._read_work_status = original_read_work_status

        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(final["state"], "done")
        self.assertTrue(final["work_status"]["cancel_requested"])
        self.assertEqual(release_request["release_state"], "cancel_requested")
        self.assertEqual(status["release_queue"]["release_count"], 0)

    def test_finalize_release_enqueue_is_serialized_against_concurrent_cancel(self):
        space = PREFIX + "taskcancellock"
        member = PREFIX + "agent_wlock"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "lockrace"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="lockrace",
            objective="cancel lock race task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        (wdir / "결과.md").write_text("lock race result", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        original_enqueue = task_registry.release_queue.enqueue_release
        cancel_started = threading.Event()
        cancel_finished = threading.Event()
        cancel_result = {}
        cancel_threads = []

        def racing_enqueue(*args, **kwargs):
            def request_cancel_while_finalize_holds_task_lock():
                cancel_started.set()
                try:
                    cancel_result["value"] = room_manager.request_task_cancel(space, "lockrace", reason="enqueue 중 취소")
                except Exception as exc:
                    cancel_result["error"] = exc
                finally:
                    cancel_finished.set()

            thread = threading.Thread(target=request_cancel_while_finalize_holds_task_lock)
            cancel_threads.append(thread)
            thread.start()
            self.assertTrue(cancel_started.wait(timeout=1))
            time.sleep(0.2)
            self.assertFalse(cancel_finished.is_set())
            return original_enqueue(*args, **kwargs)

        try:
            task_registry.release_queue.enqueue_release = racing_enqueue
            final = task_registry.finalize_task(
                space,
                task_id="lockrace",
                worker=member,
                work_dir=wdir,
                task_pack=created["task_pack"],
                objective="cancel lock race task",
            )
        finally:
            task_registry.release_queue.enqueue_release = original_enqueue
        for thread in cancel_threads:
            thread.join(timeout=2)

        release_request = json.loads((wdir / "release_request.json").read_text(encoding="utf-8"))
        status = room_manager.status(space)
        self.assertEqual(final["state"], "done")
        self.assertTrue(cancel_finished.is_set())
        self.assertIn("already closed", str(cancel_result.get("error", "")))
        self.assertFalse((wdir / "취소요청.json").exists())
        self.assertEqual(release_request["queue_state"], "enqueued")
        self.assertEqual(status["release_queue"]["release_count"], 1)

    def test_late_heartbeat_after_finalize_does_not_reopen_task(self):
        space = PREFIX + "taskhblate"
        member = PREFIX + "agent_wh09"
        make_space(space, [member])
        post = room_manager.post(space, "작업 진행", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        wdir = PEOPLE / member / "공간" / space / "작업" / "hblate"
        created = task_registry.create_task(
            space,
            worker=member,
            task_id="hblate",
            objective="late heartbeat task",
            work_dir=wdir,
            runtime_info={"engine": "codex", "model": "gpt-5.5"},
            context=context,
        )
        (wdir / "결과.md").write_text("ok", encoding="utf-8")
        (wdir / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
        task_registry.finalize_task(
            space,
            task_id="hblate",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            objective="late heartbeat task",
        )

        hb = task_registry.record_heartbeat(
            space,
            task_id="hblate",
            worker=member,
            work_dir=wdir,
            task_pack=created["task_pack"],
            phase="late",
            note="완료 후 늦은 heartbeat",
        )
        status = room_manager.status(space)

        self.assertTrue(hb["skipped"])
        self.assertEqual(status["tasks"]["latest_state"], "done")
        self.assertEqual(status["tasks"]["running_count"], 0)

    def test_release_queue_forces_pending_even_when_worker_claims_approval(self):
        space = PREFIX + "releaseforce"
        make_space(space)
        post = room_manager.post(space, "malicious done", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        work_dir = SPACES / space / "test_work" / "force"
        work_dir.mkdir(parents=True, exist_ok=True)
        task_pack = {
            "task_pack_id": "taskpack-force",
            "task_pack_checksum": "checksum-force",
            **context,
        }
        request = {
            "schema": "ReleaseRequest.v1",
            "release_id": "rel-force",
            "source_task_id": "task-force",
            "task_pack_id": task_pack["task_pack_id"],
            "task_pack_checksum_seen": task_pack["task_pack_checksum"],
            "public_summary": "승인 없이 공개하면 안 됨",
            "approval_required": False,
            "approval_state": "granted",
            "publish_blocked_until_approval": False,
            **context,
        }

        result = release_queue.enqueue_release(space, release_request=request, work_dir=work_dir, task_pack=task_pack)
        event = result["event"]
        updated = result["release_request"]

        self.assertTrue(updated["approval_required"])
        self.assertEqual(updated["approval_state"], "pending")
        self.assertTrue(updated["publish_blocked_until_approval"])
        self.assertEqual(updated["release_state"], "approval_pending")
        self.assertTrue(event["approval_required"])
        self.assertEqual(event["approval_state"], "pending")
        self.assertTrue(event["publish_blocked_until_approval"])

    def test_release_queue_approve_publish_drains_to_one_public_message(self):
        space = PREFIX + "releasepublish"
        make_space(space)
        enqueue_test_release(space, "rel-publish", "대표 승인 후 공개되는 문장")

        with self.assertRaises(release_queue.ReleaseQueueError):
            room_manager.publish_release(space, "rel-publish", text="승인되지 않은 바꿔치기 문장")

        approved = room_manager.approve_release(space, "rel-publish", reason="검수 완료")
        published = room_manager.publish_release(space, "rel-publish")
        duplicate = room_manager.publish_release(space, "rel-publish")
        rows = read(space)
        assistant_rows = [row for row in rows if row.get("역할") == "assistant"]
        status = room_manager.status(space)

        self.assertEqual(approved["event"]["approval_state"], "granted")
        self.assertTrue(published["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0]["내용"], "대표 승인 후 공개되는 문장")
        self.assertEqual(status["release_queue"]["state_counts"].get("published"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_release_queue_reject_blocks_publish_without_public_message(self):
        space = PREFIX + "releasereject"
        make_space(space)
        enqueue_test_release(space, "rel-reject", "공개되면 안 되는 문장")

        rejected = room_manager.reject_release(space, "rel-reject", reason="대표 반려")

        with self.assertRaises(release_queue.ReleaseQueueError):
            room_manager.publish_release(space, "rel-reject")
        status = room_manager.status(space)
        self.assertEqual(rejected["event"]["approval_state"], "rejected")
        self.assertEqual([row for row in read(space) if row.get("역할") == "assistant"], [])
        self.assertEqual(status["release_queue"]["state_counts"].get("rejected"), 1)
        self.assertEqual(status["publish_ledger"]["effect_count"], 0)

    def test_release_queue_ready_to_publish_without_granted_approval_cannot_publish(self):
        space = PREFIX + "releaselegacy"
        make_space(space)
        post = room_manager.post(space, "legacy ready", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        (SPACES / space / "release_queue.jsonl").write_text(json.dumps({
            "schema": "ReleaseQueueEvent.v1",
            "event_id": "legacy-ready",
            "event": "release_ready_legacy",
            "state": "ready_to_publish",
            "approval_state": "",
            "release_queue_id": "rq-legacy",
            "release_id": "rel-legacy",
            "source_task_id": "task-legacy",
            "task_pack_id": "taskpack-legacy",
            "task_pack_checksum_seen": "checksum-legacy",
            "public_summary": "명시 승인 전에는 공개 금지",
            **context,
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        with self.assertRaises(release_queue.ReleaseQueueError):
            room_manager.publish_release(space, "rel-legacy")

        approved = room_manager.approve_release(space, "rel-legacy")
        published = room_manager.publish_release(space, "rel-legacy")
        self.assertEqual(approved["event"]["approval_state"], "granted")
        self.assertTrue(published["ok"])
        self.assertEqual(len([row for row in read(space) if row.get("역할") == "assistant"]), 1)

    def test_release_queue_stale_generation_blocks_review_transition(self):
        space = PREFIX + "releasestale"
        make_space(space)
        enqueue_test_release(space, "rel-stale", "오래된 세대 결과")
        orchestration.advance_generation(space, "newer user request")

        with self.assertRaises(release_queue.ReleaseQueueError):
            room_manager.approve_release(space, "rel-stale")
        status = room_manager.status(space)
        self.assertEqual(status["release_queue"]["pending_count"], 1)
        self.assertEqual(status["release_queue"]["state_counts"].get("approval_pending"), 1)

    def test_release_publish_retry_after_public_append_before_queue_mark_is_idempotent(self):
        space = PREFIX + "releaseretry"
        make_space(space)
        enqueue_test_release(space, "rel-retry", "재시도해도 하나만 공개")
        room_manager.approve_release(space, "rel-retry")
        original_mark = room_manager.release_queue.mark_published
        calls = {"count": 0}

        def crash_before_mark(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("simulated crash before release queue mark")
            return original_mark(*args, **kwargs)

        try:
            room_manager.release_queue.mark_published = crash_before_mark
            with self.assertRaises(RuntimeError):
                room_manager.publish_release(space, "rel-retry")
        finally:
            room_manager.release_queue.mark_published = original_mark

        retry = room_manager.publish_release(space, "rel-retry")
        assistant_rows = [row for row in read(space) if row.get("역할") == "assistant"]
        status = room_manager.status(space)

        self.assertTrue(retry["duplicate"])
        self.assertEqual(len(assistant_rows), 1)
        self.assertEqual(assistant_rows[0]["내용"], "재시도해도 하나만 공개")
        self.assertEqual(status["release_queue"]["state_counts"].get("published"), 1)
        self.assertEqual(status["publish_ledger"]["counts"].get("committed"), 1)

    def test_status_marks_invalid_json_release_queue_lines_corrupt(self):
        space = PREFIX + "releasebadjson"
        make_space(space)
        (SPACES / space / "release_queue.jsonl").write_text(
            '{"release_id":"ok","state":"approval_pending"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["release_queue"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["release_queue"]["ledger_errors"]))
        self.assertTrue(any(f.get("상태") == "release_queue_corrupt" for f in status["failures"]))

    def test_status_marks_invalid_json_publish_ledger_lines_corrupt(self):
        space = PREFIX + "publishbadjson"
        make_space(space)
        (SPACES / space / "publish_ledger.jsonl").write_text(
            '{"publish_effect_id":"ok","state":"committed"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["publish_ledger"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["publish_ledger"]["ledger_errors"]))
        self.assertTrue(any(f.get("상태") == "publish_ledger_corrupt" for f in status["failures"]))

    def test_status_marks_invalid_json_task_registry_lines_corrupt(self):
        space = PREFIX + "taskbadjson"
        make_space(space)
        (SPACES / space / "task_registry.jsonl").write_text(
            '{"task_id":"ok","state":"done"}\n{bad json}\n',
            encoding="utf-8",
        )

        status = room_manager.status(space)

        self.assertTrue(status["tasks"]["ledger_corrupt"])
        self.assertIn("invalid_json_lines=1", ";".join(status["tasks"]["ledger_errors"]))
        self.assertTrue(any(f.get("상태") == "task_registry_corrupt" for f in status["failures"]))

    def test_agent_reply_is_blocked_when_generation_changes_before_publish(self):
        space = PREFIX + "stale"
        member = PREFIX + "agent_a001"
        make_space(space, [member])
        post = room_manager.post(space, "work on this", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps({
                    "action": "pass",
                    "wake": member,
                    "message": "do it",
                    "reason": "test",
                }, ensure_ascii=False)
            orchestration.advance_generation(space, "test_generation_change")
            return "late reply"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        rows = read(space)
        status = room_manager.status(space)
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["역할"], "user")
        self.assertFalse(status["manager_claim"].get("active"))
        self.assertEqual(status["상태"], "idle")
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("superseded"), 1)
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("success"))

    def test_manager_result_is_closed_when_generation_changes_before_decision_apply(self):
        space = PREFIX + "managerstale"
        make_space(space)
        post = room_manager.post(space, "plan", run_manager=False, client_message_id="client-1")
        context = post["orchestration"]
        original_run_engine = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *args, **kwargs):
            orchestration.advance_generation(space, "test_manager_generation_change")
            return json.dumps({
                "action": "stop",
                "wake": "",
                "message": "",
                "reason": "late",
            }, ensure_ascii=False)

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "test event", context)
        finally:
            room_manager.engine.run_engine = original_run_engine

        status = room_manager.status(space)
        self.assertFalse(result["ok"])
        self.assertTrue(result["generation_stale"])
        self.assertFalse(status["manager_claim"].get("active"))
        self.assertEqual(status["상태"], "idle")
        self.assertEqual(status["last_action"], "stale_generation")
        self.assertEqual(status["learning"]["evaluation_outcomes"].get("superseded"), 1)
        self.assertIsNone(status["learning"]["evaluation_outcomes"].get("success"))


class OrphanedRedriveTests(unittest.TestCase):
    """체인이 claim을 쥔 동안 들어온 입력이 release 때만 redrive로 마킹돼 아무도 처리 못 하는
    '고아 redrive'(방 멈춤) 탐지. _run_tick_chain 종료 전 drain이 이걸 보고 재구동한다."""

    def setUp(self):
        cleanup()

    def tearDown(self):
        cleanup()

    def _write_claim(self, space, **over):
        from core import manager_claim
        claim = {"state": "released", "claim_seq": 6, "read_until_event_seq": 6,
                 "manager_redrive_required": True, "redrive_events": [{"event_seq": 7}]}
        claim.update(over)
        manager_claim._claim_path(space).write_text(json.dumps(claim, ensure_ascii=False), encoding="utf-8")

    def _set_last_seq(self, space, n):
        (SPACES / space / "이벤트상태.json").write_text(
            json.dumps({"last_event_seq": n, "updated": "2026-06-29T00:00:00"}), encoding="utf-8")

    def test_detects_orphaned_redrive(self):
        space = PREFIX + "orphan"
        make_space(space)
        self._set_last_seq(space, 7)
        self._write_claim(space)                                # released + redrive + read_until 6 < last 7
        self.assertTrue(room_manager._has_orphaned_redrive(space))

    def test_no_orphan_when_caught_up(self):
        space = PREFIX + "orphan2"
        make_space(space)
        self._set_last_seq(space, 6)
        self._write_claim(space, read_until_event_seq=6)        # 다 읽음 → 고아 아님
        self.assertFalse(room_manager._has_orphaned_redrive(space))

    def test_no_orphan_when_flag_clear(self):
        space = PREFIX + "orphan3"
        make_space(space)
        self._set_last_seq(space, 7)
        self._write_claim(space, manager_redrive_required=False)  # redrive 마킹 없음 → 고아 아님
        self.assertFalse(room_manager._has_orphaned_redrive(space))

    def test_no_orphan_when_running(self):
        space = PREFIX + "orphan4"
        make_space(space)
        self._set_last_seq(space, 7)
        self._write_claim(space, state="running")              # 다른 체인이 처리 중 → 건드리지 않음
        self.assertFalse(room_manager._has_orphaned_redrive(space))


class RapidMultiBubbleTests(unittest.TestCase):
    """Phase 1 하니스: 대표가 말풍선을 빠르게 여러 개 올릴 때(매니저 판단 도중 도착=coalesce)
    전부 처리·유실0·고아redrive0·방 클린. ad-hoc 라이브 대신 엔진 모킹으로 결정적·반복 검증."""

    def setUp(self):
        cleanup()

    def tearDown(self):
        cleanup()

    def _burst_and_drain(self, space, *, n, tag):
        # 첫 메시지 → 매니저 판단 '도중' 나머지 n-1개가 빠르게 도착(claim 점유 중이라 coalesce).
        first = room_manager.post(space, f"{tag}-질문0", run_manager=False,
                                  client_message_id=f"{tag}-c0", manager_requested=True)
        ctx = first["orchestration"]
        state = {"posted": False}
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                if not state["posted"]:
                    state["posted"] = True
                    for i in range(1, n):
                        p = room_manager.post(space, f"{tag}-질문{i}", run_manager=False,
                                              client_message_id=f"{tag}-c{i}", manager_requested=True)
                        room_manager.queue_manager(space, f"대표 메시지 {tag}-질문{i}", p["orchestration"])
                return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "확인"}, ensure_ascii=False)
            return f"{cwd.parent.parent.name} 응답"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, f"대표 메시지 {tag}-질문0", ctx, auto_continue=True)
        finally:
            room_manager.engine.run_engine = orig

    def test_rapid_burst_no_loss_no_orphan(self):
        space = PREFIX + "rapid1"
        make_space(space)
        self._burst_and_drain(space, n=5, tag="r0")
        user_msgs = [r for r in read(space) if r.get("역할") == "user"]
        self.assertEqual(len(user_msgs), 5)                            # 5개 입력 전부 기록(유실0)
        self.assertFalse(room_manager._has_orphaned_redrive(space))    # 고아 redrive 없음(방 안 멈춤)
        self.assertEqual(room_manager.manager_claim.snapshot(space).get("state"), "released")  # 방 클린

    def test_rapid_burst_repeated(self):
        # 한 번 성공 ≠ 검증: 8라운드 반복, 매 라운드 끝에 클린해야 한다.
        space = PREFIX + "rapid2"
        make_space(space)
        for rnd in range(8):
            self._burst_and_drain(space, n=4, tag=f"R{rnd}")
            self.assertFalse(room_manager._has_orphaned_redrive(space), f"round {rnd}: 고아 redrive 잔존")
            self.assertEqual(room_manager.manager_claim.snapshot(space).get("state"), "released", f"round {rnd}: claim 미해제")
        user_msgs = [r for r in read(space) if r.get("역할") == "user"]
        self.assertEqual(len(user_msgs), 8 * 4)                        # 32개 누적 전부 기록(라운드 간 유실0)


class ConcurrentMultiAgentTests(unittest.TestCase):
    """Phase 2 하니스(요구 #3): 사회자가 여러 에이전트에 동시 위임(parallel_pass) → 동시 결과 회수
    → 사회자가 다음 맥락을 파악해 자동 공개·조절(publish_each/synthesize) + 중복위임 안 함·완료 시 멈춤."""

    def setUp(self):
        cleanup()

    def tearDown(self):
        cleanup()

    def _dispatch(self, space, members, *, mode, tag):
        # 사회자가 자동으로: 1) 동시 위임 2) 수집된 동시 결과를 공개 3) 다 끝나면 멈춤(중복위임 금지).
        post = room_manager.post(space, f"{tag} 여러 관점으로 검토해줘", run_manager=False,
                                 client_message_id=f"{tag}-c1", manager_requested=True)
        ctx = post["orchestration"]
        state = {"dispatched": False}
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                snap = candidate_queue.snapshot(space)
                pend = sorted(it.get("candidate_id") for it in snap.get("pending_items", []) if it.get("candidate_id"))
                if pend:
                    if mode == "synthesize":
                        return json.dumps({"action": "synthesize_candidates", "candidate_ids": pend,
                                           "wake": "", "message": f"{tag} 종합: 세 의견을 모으면 결론은 ~이다.",
                                           "reason": "여러 관점을 한 답으로 합침"}, ensure_ascii=False)
                    return json.dumps({"action": "publish_each", "candidate_ids": pend,
                                       "wake": "", "message": "", "reason": "각자 의견을 그 멤버 말풍선으로 공개"}, ensure_ascii=False)
                if not state["dispatched"]:
                    state["dispatched"] = True
                    return json.dumps({"action": "parallel_pass", "wake": "", "message": "", "reason": "동시 위임",
                                       "targets": [{"wake": m, "message": f"{m} 의견 줘", "reason": "관점"} for m in members],
                                       "join_policy": "timeout_then_partial", "presentation_mode": "silent_reference"}, ensure_ascii=False)
                return json.dumps({"action": "stop", "wake": "", "message": "", "reason": "다 공개됨 — 대표에게 턴"}, ensure_ascii=False)
            return f"{cwd.parent.parent.name}의 의견({tag})"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, f"{tag} 검토", ctx, auto_continue=True)
        finally:
            room_manager.engine.run_engine = orig

    def test_three_agents_publish_each(self):
        space = PREFIX + "cma_pe"
        members = [PREFIX + "agA", PREFIX + "agB", PREFIX + "agC"]
        make_space(space, members)
        self._dispatch(space, members, mode="publish_each", tag="T")
        assistants = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertEqual(len(assistants), 3)                                  # 동시 결과 3개 모두 회수·공개
        self.assertEqual(candidate_queue.snapshot(space).get("pending_count"), 0)
        contents = {r.get("내용") for r in assistants}
        self.assertEqual(len(contents), 3)                                    # 3개 모두 서로 다른 에이전트 결과(중복/유실 없음)
        self.assertTrue(all(r.get("화자") != "공간관리" for r in assistants))  # 각자 말풍선(사회자 침묵)
        self.assertEqual(room_manager.manager_claim.snapshot(space).get("state"), "released")

    def test_three_agents_synthesize(self):
        space = PREFIX + "cma_sy"
        members = [PREFIX + "agX", PREFIX + "agY", PREFIX + "agZ"]
        make_space(space, members)
        self._dispatch(space, members, mode="synthesize", tag="S")
        assistants = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertEqual(len(assistants), 1)                                  # 합성=한 답
        self.assertEqual(assistants[0].get("화자"), "공간관리")               # 합성문은 사회자 명의
        self.assertEqual(candidate_queue.snapshot(space).get("pending_count"), 0)

    def test_concurrent_dispatch_repeated(self):
        # 한 번 성공 ≠ 검증: 5라운드 반복, 매 라운드 3 동시결과 전부 공개·후보 0·중복위임 없음.
        space = PREFIX + "cma_rep"
        members = [PREFIX + "agR1", PREFIX + "agR2", PREFIX + "agR3"]
        make_space(space, members)
        for rnd in range(5):
            self._dispatch(space, members, mode="publish_each", tag=f"R{rnd}")
            self.assertEqual(candidate_queue.snapshot(space).get("pending_count"), 0, f"round {rnd}: 후보 잔존")
            self.assertFalse(room_manager._has_orphaned_redrive(space), f"round {rnd}: 고아 redrive")
        assistants = [r for r in read(space) if r.get("역할") == "assistant"]
        self.assertEqual(len(assistants), 5 * 3)                              # 누적 15개 동시결과 전부 공개(유실0)


class ProposeSkillDispatchTests(unittest.TestCase):
    """propose_skill: 마땅한 스킬이 없을 때 사회자가 새 스킬을 만들고 첫 케이스로 담는 경로(런타임 신규생성 배선)."""

    def setUp(self):
        cleanup()
        self._skill_dirs = []

    def tearDown(self):
        for sdir in self._skill_dirs:
            shutil.rmtree(sdir, ignore_errors=True)
        cleanup()

    def _decision_for_skill(self, name):
        return {
            "action": "propose_skill", "wake": "", "message": "",
            "reason": "마땅한 스킬이 없어 새로 만든다",
            "skill": name,
            "description": f"{name} — 단톡방에서 환영카드 만들 때 색상 규칙. '환영카드 만들어줘','카드 색','환영 카드'. 핵심: 환영카드, 색상, 톤",
            "candidate": {
                "condition": "환영카드 색상 톤을 정할 때", "instruction": "파란톤을 기본으로 한다",
                "polarity": "worked", "routing_kind": "procedural",
                "judgment_rationale": "대표가 색 규칙을 durable하게 지시", "source_quote": "환영카드는 파란톤으로",
                "sensitivity": "public",
            },
        }

    def test_propose_skill_creates_discoverable_skill_with_seed_case(self):
        from core import skill_smith
        space = PREFIX + "skill"
        make_space(space, [PREFIX + "m1"])
        name = PREFIX + "환영카드색규칙"
        sdir = skill_smith.SKILLS / "추가" / name
        self._skill_dirs.append(sdir)
        post = room_manager.post(space, "환영카드는 파란톤으로 해줘. 기억해.", run_manager=False, client_message_id="c-skill")
        context = post["orchestration"]
        decision = self._decision_for_skill(name)
        original = room_manager.engine.run_engine

        def fake_run_engine(cwd, prompt, *a, **k):
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            return "ok"

        try:
            room_manager.engine.run_engine = fake_run_engine
            result = room_manager.tick(space, "propose_skill", context)
        finally:
            room_manager.engine.run_engine = original

        self.assertTrue(result.get("ok"))
        # 1) 새 스킬 SKILL.md 생성 + 본문에 규칙(상시)
        self.assertTrue((sdir / "SKILL.md").exists(), "새 스킬 SKILL.md 없음")
        self.assertIn("파란톤", (sdir / "SKILL.md").read_text(encoding="utf-8"))
        # 2) durable 교훈이 첫 케이스로 시딩됨
        cases_path = sdir / "cases.jsonl"
        self.assertTrue(cases_path.exists(), "첫 케이스 cases.jsonl 없음")
        cases = [json.loads(l) for l in cases_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["instruction"], "파란톤을 기본으로 한다")
        self.assertEqual(cases[0]["action"], "add_case")
        # 3) '찾아지는 스킬'(대표 요구) — 발견 게이트 통과
        gate = skill_smith.check_discoverable(name, [name, "환영카드 색"], top=3)
        self.assertTrue(gate["discoverable"], f"새 스킬 미발견: {gate}")

    def _run_propose_skill(self, space, decision, dedup_same_as):
        # dedup LLM 호출('스킬 중복 판정' 프롬프트)엔 same_as를, 매니저 결정엔 decision을 돌려준다.
        post = room_manager.post(space, "테스트", run_manager=False, client_message_id=f"c-{space}")
        ctx = post["orchestration"]
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            if "스킬 중복 판정" in prompt:
                return json.dumps({"same_as": dedup_same_as, "reason": "테스트"}, ensure_ascii=False)
            if Path(cwd).name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            return "ok"

        try:
            room_manager.engine.run_engine = fake
            return room_manager.tick(space, "propose_skill", ctx)
        finally:
            room_manager.engine.run_engine = orig

    def test_propose_skill_delegates_authoring_to_doer_with_skill_creator(self):
        # 설계 §4: 스킬 생성 시 크루드 인라인 본문이 아니라 doer에게 skill-creator 기준 본문 저작을 위임한다.
        from core import skill_smith
        space = PREFIX + "skillauthor"
        member = PREFIX + "doer_sa01"
        make_space(space, [member])
        name = PREFIX + "신규스킬저작"
        self._skill_dirs.append(skill_smith.SKILLS / "추가" / name)
        post = room_manager.post(space, "스킬 만들어줘", run_manager=False, client_message_id="sa-1")
        ctx = post["orchestration"]
        decision = self._decision_for_skill(name)
        orig = room_manager.engine.run_engine

        def fake(cwd, prompt, *a, **k):
            cwd = Path(cwd)
            if cwd.name == MANAGER_DIRNAME:
                return json.dumps(decision, ensure_ascii=False)
            (cwd / "결과.md").write_text("skill-creator로 본문 작성 완료", encoding="utf-8")
            (cwd / "상태.json").write_text(json.dumps({"상태": "done"}, ensure_ascii=False), encoding="utf-8")
            return "ok"

        try:
            room_manager.engine.run_engine = fake
            room_manager.tick(space, "propose_skill", ctx)
        finally:
            room_manager.engine.run_engine = orig
        wp_path = SPACES / space / "work_plans.jsonl"
        self.assertTrue(wp_path.exists(), "스킬 저작 work_plan 미등록")
        objectives = [json.loads(l).get("objective", "") for l in wp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(any("skill-creator" in o for o in objectives), "skill-creator 기준 저작 위임 objective 없음")
        workers = [json.loads(l).get("worker", "") for l in wp_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertIn(member, workers)                            # doer(멤버)에게 위임

    def test_propose_skill_redirects_when_semantic_duplicate(self):
        # 의미상 같은 스킬이 있으면(LLM 판정) 새로 만들지 말고 기존 스킬에 케이스 추가(redirect).
        from core import skill_smith
        space = PREFIX + "sem_dup"
        make_space(space, [PREFIX + "ms1"])
        existing = PREFIX + "미리보기출력기존"
        edir = skill_smith.SKILLS / "추가" / existing
        self._skill_dirs.append(edir)
        skill_smith.create_skill(existing, description="문서 html md 미리보기 말풍선 출력 결과",
                                 body="# x\n\n## 절차\n1. 보여준다\n", grade="추가")
        cpath = edir / "cases.jsonl"
        base_n = sum(1 for _ in open(cpath)) if cpath.exists() else 0
        requested = PREFIX + "문서미리보기신규"
        rdir = skill_smith.SKILLS / "추가" / requested
        self._skill_dirs.append(rdir)
        decision = {
            "action": "propose_skill", "wake": "", "message": "", "reason": "미리보기",
            "skill": requested, "description": "문서 html md 미리보기 말풍선 출력 결과",
            "candidate": {"condition": "html/md 만들 때", "instruction": "말풍선 미리보기로 보여준다",
                          "polarity": "worked", "routing_kind": "procedural",
                          "judgment_rationale": "r", "source_quote": "q", "sensitivity": "public"},
        }
        result = self._run_propose_skill(space, decision, dedup_same_as=existing)
        self.assertTrue(result.get("ok"))
        self.assertFalse((rdir / "SKILL.md").exists(), "중복 새 스킬 생성됨(의미 redirect 실패)")
        now_n = sum(1 for _ in open(cpath)) if cpath.exists() else 0
        self.assertEqual(now_n, base_n + 1, "기존 스킬에 케이스 안 늘어남")
        self.assertTrue(any(e.get("type") == "skill_create_redirected" for e in result.get("events", [])))

    def test_propose_skill_creates_when_semantically_distinct_despite_lexical_overlap(self):
        # 핵심: 어휘는 강하게 겹쳐도(후보 게이트 통과) LLM이 '목적 다름'이라 하면 신규 생성(환영카드↔미리보기 오매치 해결).
        from core import skill_smith
        space = PREFIX + "sem_new"
        make_space(space, [PREFIX + "ms2"])
        existing = PREFIX + "미리보기출력기존2"
        edir = skill_smith.SKILLS / "추가" / existing
        self._skill_dirs.append(edir)
        # 일부러 요청과 어휘가 강하게 겹치는 기존 스킬(후보 게이트 통과 유도)
        skill_smith.create_skill(existing, description="환영카드 색상 미리보기 말풍선 카드 출력 결과",
                                 body="# x\n", grade="추가")
        requested = PREFIX + "환영카드색규칙신규"
        rdir = skill_smith.SKILLS / "추가" / requested
        self._skill_dirs.append(rdir)
        decision = {
            "action": "propose_skill", "wake": "", "message": "", "reason": "색규칙",
            "skill": requested, "description": "환영카드 색상 미리보기 말풍선 카드 출력 결과",
            "candidate": {"condition": "환영카드 색 정할 때", "instruction": "파란톤을 기본으로 한다",
                          "polarity": "worked", "routing_kind": "procedural",
                          "judgment_rationale": "r", "source_quote": "q", "sensitivity": "public"},
        }
        result = self._run_propose_skill(space, decision, dedup_same_as="")   # LLM: 목적 다름
        self.assertTrue(result.get("ok"))
        self.assertTrue((rdir / "SKILL.md").exists(), "의미상 다른데 신규 생성 안 됨(어휘 false-positive 회귀)")
        self.assertTrue(any(e.get("type") == "skill_created" for e in result.get("events", [])))

    def test_decision_error_validates_propose_skill(self):
        ok = self._decision_for_skill(PREFIX + "x")
        self.assertEqual(room_manager._decision_error(ok), "")
        cases = {
            "skill": {**ok, "skill": ""},
            "description": {**ok, "description": ""},
            "candidate": {**ok, "candidate": None},
            "wake": {**ok, "wake": "someone"},
        }
        for needle, bad in cases.items():
            self.assertIn(needle, room_manager._decision_error(bad), f"{needle} 검증 누락")
        # candidate 내부 필드 누락
        bad = {**ok, "candidate": {**ok["candidate"], "instruction": ""}}
        self.assertIn("필드 누락", room_manager._decision_error(bad))


if __name__ == "__main__":
    unittest.main()
