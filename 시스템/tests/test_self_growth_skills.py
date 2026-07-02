#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""자기성장 스킬/지식 엔진 회귀 테스트 (P0~P6).

격리 원칙:
- case_ledger/resource_body/knowledge_ledger 코어 로직 → tempdir에 Path를 직접 넘겨 완전 격리.
- skill_smith/discovery/deploy_guard(=ROOT/스킬 스캔 의존) → 유니크 sentinel 이름으로 실제 폴더 생성 후 정리.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "시스템"))

from core import case_ledger as C          # noqa: E402
from core import resource_body as RB       # noqa: E402
from core import knowledge_ledger as K     # noqa: E402
from core import skill_smith as S          # noqa: E402
from core import deploy_guard as G         # noqa: E402


def _write_skill_md(sdir: Path, *, name="t", version=1, body="# t\n\n## 절차\n1. 한다\n"):
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text(
        f"---\nskill_id: skill_{name}\nname: {name}\ndescription: 테스트\nversion: {version}\n---\n\n{body}",
        encoding="utf-8",
    )


def _cand(**over):
    base = dict(condition="모바일", instruction="버튼 크게", polarity="worked",
                action="add_case", routing_kind="procedural",
                judgment_rationale="작게 보임", source_quote="버튼 작아", sensitivity="public")
    base.update(over)
    return base


class CaseLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self._tmp.name) / "skill"
        _write_skill_md(self.sdir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_contract_rejections(self):
        for bad in (
            _cand(source_quote=""),          # 필드 누락
            _cand(polarity="good"),          # 잘못된 enum
            _cand(routing_kind="factual"),   # 지식 라우팅
            _cand(action="new_skill"),       # P4 경로
            _cand(action="supersede"),       # 대상 없음
        ):
            with self.assertRaises(C.CaseLedgerError):
                C.propose_case(self.sdir, bad)

    def test_daepyo_public_to_cases_jsonl(self):
        r = C.propose_case(self.sdir, _cand(), proposed_by="대표", from_daepyo=True)
        self.assertEqual(r["status"], "provisional_must")
        self.assertTrue(r["must_apply"])
        self.assertEqual(r["approved_by"], "대표")
        self.assertTrue((self.sdir / "cases.jsonl").exists())
        self.assertFalse((self.sdir / "cases.local.jsonl").exists())

    def test_nondaepyo_confidential_sidecar(self):
        r = C.propose_case(self.sdir, _cand(sensitivity=None, condition="고객 환불"), proposed_by="구현자")
        self.assertEqual(r["status"], "candidate")
        self.assertEqual(r["sensitivity"], "confidential")   # 미판단 → 보수적 기본값
        self.assertTrue((self.sdir / "cases.local.jsonl").exists())

    def test_supersede_excludes_old(self):
        r1 = C.propose_case(self.sdir, _cand(), from_daepyo=True)
        r2 = C.propose_case(self.sdir, _cand(instruction="버튼 48px", action="supersede",
                                             supersedes=[r1["case_id"]]), from_daepyo=True)
        by_id = {c["case_id"]: c for c in C.read_cases(self.sdir)}
        self.assertEqual(by_id[r1["case_id"]]["status"], "superseded")
        pack = C.build_case_pack(self.sdir)
        must_ids = [v["case_id"] for v in pack["must_apply"]]
        self.assertIn(r2["case_id"], must_ids)
        self.assertNotIn(r1["case_id"], must_ids)

    def test_build_case_pack_tiers(self):
        m = C.propose_case(self.sdir, _cand(), from_daepyo=True)                # provisional_must
        c = C.propose_case(self.sdir, _cand(condition="태블릿"), from_daepyo=False)  # candidate
        pack = C.build_case_pack(self.sdir)
        self.assertIn(m["case_id"], [v["case_id"] for v in pack["must_apply"]])
        self.assertIn(c["case_id"], [v["case_id"] for v in pack["reference_only"]])

    def test_events_and_maturity(self):
        r = C.propose_case(self.sdir, _cand(), from_daepyo=True)
        C.record_case_event(self.sdir, r["case_id"], "worked")
        C.record_case_event(self.sdir, r["case_id"], "harmful")
        m = C.maturity(self.sdir)
        self.assertEqual(m["cases"], 1)
        self.assertEqual((m["worked"], m["harmful"]), (1, 1))
        self.assertTrue(m["warn_harmful"])

    def test_promote_guards(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=False)   # candidate, worked 0
        with self.assertRaises(C.CaseLedgerError):
            C.promote_case(self.sdir, c["case_id"], by="공간관리", rationale="x", method="second_judgment")
        C.record_case_event(self.sdir, c["case_id"], "worked")
        r = C.promote_case(self.sdir, c["case_id"], by="공간관리", rationale="검증", method="second_judgment")
        self.assertEqual(r["status"], "active")

    def test_worked_threshold(self):
        # §9.1: 승격은 '횟수'가 아니라 '서로 다른 독립 확인자 수'. 단일출처 자기강화 차단.
        c = C.propose_case(self.sdir, _cand(condition="A"), proposed_by="기획", from_daepyo=False)
        cid = c["case_id"]
        with self.assertRaises(C.CaseLedgerError):          # 확인 0 → 거부
            C.promote_case(self.sdir, cid, by="x", rationale="x", method="worked_threshold")
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        with self.assertRaises(C.CaseLedgerError):          # 같은 출처 2번 → 여전히 거부
            C.promote_case(self.sdir, cid, by="x", rationale="단일출처", method="worked_threshold")
        C.record_case_event(self.sdir, cid, "worked", by="검토")   # 서로 다른 독립 확인자
        r = C.promote_case(self.sdir, cid, by="x", rationale="2출처", method="worked_threshold")
        self.assertEqual(r["status"], "active")

    def test_worked_threshold_blocks_self_promotion(self):
        # 제안자 본인만 worked 하면 독립 확인자 없어 거부(자기강화 오염 루프 차단).
        c = C.propose_case(self.sdir, _cand(condition="B"), proposed_by="작가", from_daepyo=False)
        cid = c["case_id"]
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        with self.assertRaises(C.CaseLedgerError):
            C.promote_case(self.sdir, cid, by="작가", rationale="자기confirm", method="worked_threshold")

    def test_owner_approval(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=False)   # candidate, worked 0
        r = C.promote_case(self.sdir, c["case_id"], by="대표", rationale="승인", method="owner_approval")
        self.assertEqual(r["status"], "active")
        self.assertTrue(r["must_apply"])
        self.assertEqual(r["evidence_level"], "user_directive")

    def test_demote(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=True)
        r = C.demote_case(self.sdir, c["case_id"], by="공간관리", rationale="결과 나쁨")
        self.assertEqual(r["status"], "candidate")
        self.assertFalse(r["must_apply"])

    def test_expire_stale_candidate(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=False)   # candidate, worked 0
        exp = C.expire_stale_candidates(self.sdir, now="2099-01-01T00:00:00")
        self.assertIn(c["case_id"], [e["case_id"] for e in exp])

    def test_case_convergence(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=False)   # candidate
        conv = {x["case_id"]: x for x in C.case_convergence(self.sdir)}
        self.assertFalse(conv[c["case_id"]]["ready_to_promote"])
        C.record_case_event(self.sdir, c["case_id"], "worked")
        C.record_case_event(self.sdir, c["case_id"], "worked")
        conv = {x["case_id"]: x for x in C.case_convergence(self.sdir)}
        self.assertTrue(conv[c["case_id"]]["ready_to_promote"])     # worked≥2, harmful 0
        C.record_case_event(self.sdir, c["case_id"], "harmful")
        conv = {x["case_id"]: x for x in C.case_convergence(self.sdir)}
        self.assertFalse(conv[c["case_id"]]["ready_to_promote"])
        self.assertTrue(conv[c["case_id"]]["needs_review"])

    def test_review_queue_flags(self):
        c = C.propose_case(self.sdir, _cand(), from_daepyo=True)
        C.record_case_event(self.sdir, c["case_id"], "harmful")
        q = C.review_queue(self.sdir)
        self.assertIn(c["case_id"], [x["case_id"] for x in q])

    def test_dedup_exact(self):
        C.propose_case(self.sdir, _cand(condition="A", instruction="X", case_id="d1"), from_daepyo=True)
        C.propose_case(self.sdir, _cand(condition="A", instruction="X", case_id="d2"), from_daepyo=True)
        retired = C.dedup_cases(self.sdir)
        self.assertEqual(len(retired), 1)

    def test_curator_report(self):
        for i in range(5):
            C.propose_case(self.sdir, _cand(condition=f"C{i}", instruction=f"I{i}"), from_daepyo=True)
        rep = C.curator_report(self.sdir)
        self.assertIn("body_revision", [s["type"] for s in rep["suggestions"]])

    # ---- §9.1 모순 자동 격리 -------------------------------------------------
    def test_conflict_quarantines_nondaepyo_candidate(self):
        # 기존 확정 케이스와 반대 polarity + 조건 겹침 → 비대표 발의는 자동 격리(주입 제외).
        old = C.propose_case(self.sdir, _cand(condition="환영 카드 색상 톤 정할 때",
                                              instruction="파란톤 큰제목", polarity="worked"), from_daepyo=True)
        new = C.propose_case(self.sdir, _cand(condition="환영 카드 색상 톤 정할 때",
                                              instruction="빨간톤이 낫다", polarity="failed"), proposed_by="작가")
        self.assertEqual(new["status"], "conflict")
        self.assertIn(old["case_id"], new["conflicts_with"])
        pack = C.build_case_pack(self.sdir)
        injected = [v["case_id"] for t in ("must_apply", "may_apply", "reference_only") for v in pack[t]]
        self.assertNotIn(new["case_id"], injected)        # 격리됐으니 주입 안 됨
        self.assertIn(old["case_id"], injected)            # 기존 확정은 그대로 유지

    def test_conflict_daepyo_quarantines_old(self):
        # 대표=사람=최종권위: 새 지시는 적용(provisional_must)하되, 모순되는 기존 케이스를 격리(삭제 아님).
        old = C.propose_case(self.sdir, _cand(condition="버튼 배치 정할 때",
                                              instruction="하단 고정", polarity="worked"), from_daepyo=True)
        new = C.propose_case(self.sdir, _cand(condition="버튼 배치 정할 때",
                                              instruction="상단이 낫더라", polarity="failed"),
                             from_daepyo=True, proposed_by="대표")
        self.assertEqual(new["status"], "provisional_must")             # 대표 지시는 적용됨
        self.assertIn(old["case_id"], new["conflicts_with"])
        by_id = {c["case_id"]: c for c in C.read_cases(self.sdir)}
        self.assertEqual(by_id[old["case_id"]]["status"], "conflict")   # 기존은 격리(recency로 조용히 안 덮음)
        self.assertIn(old["case_id"], [x["case_id"] for x in C.review_queue(self.sdir)])

    def test_no_conflict_when_condition_disjoint(self):
        C.propose_case(self.sdir, _cand(condition="알파 상황", instruction="X", polarity="worked"), from_daepyo=True)
        new = C.propose_case(self.sdir, _cand(condition="완전 다른 베타 영역 처리",
                                              instruction="Y", polarity="failed"), proposed_by="작가")
        self.assertEqual(new["status"], "candidate")
        self.assertEqual(new["conflicts_with"], [])

    def test_no_conflict_same_polarity(self):
        # 같은 polarity는 자동격리 대상 아님(supersede/에이전트 판단 영역).
        C.propose_case(self.sdir, _cand(condition="공유 조건 영역", instruction="A안", polarity="worked"), from_daepyo=True)
        new = C.propose_case(self.sdir, _cand(condition="공유 조건 영역", instruction="B안", polarity="worked"), proposed_by="작가")
        self.assertEqual(new["status"], "candidate")

    def test_declared_conflicts_with_quarantines(self):
        # 에이전트가 conflicts_with를 명시하면 휴리스틱(조건/극성)과 무관하게 격리.
        old = C.propose_case(self.sdir, _cand(condition="지터 케이스 엑스", instruction="P", polarity="worked"), from_daepyo=True)
        new = C.propose_case(self.sdir, _cand(condition="아무 상관없는 조건 제트", instruction="Q", polarity="worked",
                                              conflicts_with=[old["case_id"]]), proposed_by="작가")
        self.assertEqual(new["status"], "conflict")
        self.assertIn(old["case_id"], new["conflicts_with"])

    def test_quarantined_case_blocks_auto_promotion(self):
        # 격리된 케이스는 독립 확인자 2명이 있어도 worked_threshold 자동승격 불가(사람이 해소).
        C.propose_case(self.sdir, _cand(condition="격리 대상 조건", instruction="원안", polarity="worked"), from_daepyo=True)
        q = C.propose_case(self.sdir, _cand(condition="격리 대상 조건", instruction="반대안", polarity="failed"), proposed_by="작가")
        self.assertEqual(q["status"], "conflict")
        cid = q["case_id"]
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        C.record_case_event(self.sdir, cid, "worked", by="검토")
        with self.assertRaises(C.CaseLedgerError):
            C.promote_case(self.sdir, cid, by="x", rationale="격리인데 승격시도", method="worked_threshold")

    # ---- applies_when 분기(more-specific-wins) -----------------------------
    def test_branch_conflict_resolves_by_narrowing(self):
        # 대표가 모순 지시 → 기존이 conflict로 격리됨. 조건을 좁혀 분기하면 격리 해제 + 조건 반영.
        old = C.propose_case(self.sdir, _cand(condition="결과 보고 형식", instruction="요약 먼저", polarity="worked"), from_daepyo=True)
        C.propose_case(self.sdir, _cand(condition="결과 보고 형식", instruction="원문 먼저가 낫더라", polarity="failed"),
                       from_daepyo=True, proposed_by="대표")
        self.assertEqual({c["case_id"]: c for c in C.read_cases(self.sdir)}[old["case_id"]]["status"], "conflict")
        out = C.branch_conflict(self.sdir, [{"case_id": old["case_id"],
                                             "applies_when": {"space_id": "방A", "task_types": ["내부보고"]}}],
                                by="공간관리", rationale="방A 내부보고에 한정")
        self.assertEqual(out[0]["status"], "provisional_must")          # 격리 전 상태로 복귀
        self.assertEqual(out[0]["applies_when"]["space_id"], "방A")     # 좁힌 조건 반영

    def test_branch_requires_narrowing(self):
        old = C.propose_case(self.sdir, _cand(condition="공유 영역", instruction="A", polarity="worked"), from_daepyo=True)
        C.propose_case(self.sdir, _cand(condition="공유 영역", instruction="B 반대", polarity="failed"),
                       from_daepyo=True, proposed_by="대표")
        with self.assertRaises(C.CaseLedgerError):                       # 조건 안 좁히면 거부
            C.branch_conflict(self.sdir, [{"case_id": old["case_id"], "restore_to": "active"}],
                              by="공간관리", rationale="그냥 풀기")

    def test_branch_only_conflict_status(self):
        c = C.propose_case(self.sdir, _cand(condition="단독", instruction="X"), from_daepyo=True)  # provisional_must
        with self.assertRaises(C.CaseLedgerError):                       # conflict 아닌 건 분기 불가
            C.branch_conflict(self.sdir, [{"case_id": c["case_id"], "applies_when": {"keywords": ["k"]}}],
                              by="공간관리", rationale="x")

    def test_more_specific_wins_ordering(self):
        # 같은 등급이면 더 특수한(조건 많은) 케이스가 must 목록에서 먼저 온다(more-specific-wins 정렬 힌트).
        g = C.propose_case(self.sdir, _cand(condition="일반 상황", instruction="일반", polarity="worked"), from_daepyo=True)
        s = C.propose_case(self.sdir, _cand(condition="특수 상황", instruction="특수", polarity="worked",
                                            applies_when={"task_types": ["보고"], "keywords": ["VIP"]}), from_daepyo=True)
        order = [v["case_id"] for v in C.build_case_pack(self.sdir)["must_apply"]]
        self.assertLess(order.index(s["case_id"]), order.index(g["case_id"]))

    # ---- confidence 비균일 감쇠 -------------------------------------------
    def test_confidence_derived_signals(self):
        # 독립확인자↑, harmful↓, 시간감쇠↓ 가 파생 confidence에 반영(비균일).
        c = C.propose_case(self.sdir, _cand(condition="신뢰A"), from_daepyo=False, proposed_by="기획")
        cid = c["case_id"]
        c0 = C.derive_confidence(self.sdir, cid)
        C.record_case_event(self.sdir, cid, "worked", by="작가")
        C.record_case_event(self.sdir, cid, "worked", by="검토")     # 서로 다른 독립 확인자 2
        c_confirmed = C.derive_confidence(self.sdir, cid)
        self.assertGreater(c_confirmed, c0)
        C.record_case_event(self.sdir, cid, "harmful", by="대표")
        c_harm = C.derive_confidence(self.sdir, cid)
        self.assertLess(c_harm, c_confirmed)
        c_future = C.derive_confidence(self.sdir, cid, now="2099-01-01T00:00:00")
        self.assertLess(c_future, c_harm)                            # 시간 감쇠

    def test_confidence_conflict_lowers(self):
        old = C.propose_case(self.sdir, _cand(condition="신뢰 충돌 영역", instruction="원안", polarity="worked"), from_daepyo=True)
        base_conf = C.derive_confidence(self.sdir, old["case_id"])
        C.propose_case(self.sdir, _cand(condition="신뢰 충돌 영역", instruction="반대", polarity="failed"),
                       from_daepyo=True, proposed_by="대표")           # old → conflict 격리
        self.assertLess(C.derive_confidence(self.sdir, old["case_id"]), base_conf)

    def test_confidence_exposed_in_views(self):
        r = C.propose_case(self.sdir, _cand(), from_daepyo=True)
        views = C.build_case_pack(self.sdir)["must_apply"]
        self.assertIsInstance(views[0]["confidence"], float)
        conv = {x["case_id"]: x for x in C.case_convergence(self.sdir)}
        self.assertIsInstance(conv[r["case_id"]]["confidence"], float)

    # ---- 이중 메모리(부정 교훈) ------------------------------------------
    def test_case_negatives_dual_memory(self):
        w = C.propose_case(self.sdir, _cand(condition="긍정 상황", instruction="해라", polarity="worked"), from_daepyo=True)
        f = C.propose_case(self.sdir, _cand(condition="실패 상황", instruction="이건 실패", polarity="failed"), from_daepyo=True)
        g = C.propose_case(self.sdir, _cand(condition="격리 영역", instruction="원안", polarity="worked"), from_daepyo=True)
        C.propose_case(self.sdir, _cand(condition="격리 영역", instruction="반대", polarity="failed"),
                       from_daepyo=True, proposed_by="대표")     # g → conflict 격리
        ids = [n["case_id"] for n in C.case_negatives(self.sdir)]
        self.assertIn(f["case_id"], ids)                          # failed = 부정 교훈
        self.assertIn(g["case_id"], ids)                          # conflict 격리도 부정 교훈
        self.assertNotIn(w["case_id"], ids)                       # worked 긍정은 제외
        pv_ids = [p["case_id"] for p in C.case_preview(self.sdir)]
        self.assertIn(w["case_id"], pv_ids)                       # 미리보기는 긍정만
        self.assertNotIn(f["case_id"], pv_ids)
        self.assertNotIn(g["case_id"], pv_ids)

    # ---- 사이코펀시 / 평가자 신뢰성 -------------------------------------
    def test_evaluator_reliability_tracks_bad_confirmations(self):
        # '나쁜 평가자'가 worked한 케이스가 harmful로 드러나면 신뢰도↓ + 사이코펀시 깃발.
        for i in range(3):
            c = C.propose_case(self.sdir, _cand(condition=f"신뢰케이스{i}", instruction=f"i{i}"), from_daepyo=False, proposed_by="기획")
            C.record_case_event(self.sdir, c["case_id"], "worked", by="예스맨")   # 항상 동의
            if i == 0:
                C.record_case_event(self.sdir, c["case_id"], "harmful", by="대표")  # 그 확인이 나중에 나쁘게 드러남
        rel = C.evaluator_reliability(self.sdir)
        self.assertLess(rel["예스맨"]["reliability"], 1.0)
        self.assertTrue(rel["예스맨"]["sycophancy_flag"])              # worked만·일부 실패

    def test_unreliable_confirmer_blocks_auto_promotion(self):
        # 신뢰도 미달 평가자는 자동승격 확인자로 불인정(사이코펀시 누적 차단).
        for i in range(2):                                                          # 예스맨이 나쁜 확인 2건
            b = C.propose_case(self.sdir, _cand(condition=f"나쁜확인{i}"), from_daepyo=False, proposed_by="기획")
            C.record_case_event(self.sdir, b["case_id"], "worked", by="예스맨")
            C.record_case_event(self.sdir, b["case_id"], "harmful", by="대표")
        target = C.propose_case(self.sdir, _cand(condition="승격대상영역"), from_daepyo=False, proposed_by="기획")
        cid = target["case_id"]
        C.record_case_event(self.sdir, cid, "worked", by="예스맨")                  # 신뢰도 1-2/3≈0.33 → 불인정
        C.record_case_event(self.sdir, cid, "worked", by="검토")                    # 검토만 신뢰 가능(1.0)
        # 신뢰 가능한 확인자 = 검토 1명 < threshold 2 → 거부
        with self.assertRaises(C.CaseLedgerError):
            C.promote_case(self.sdir, cid, by="x", rationale="사이코펀시 포함", method="worked_threshold")
        C.record_case_event(self.sdir, cid, "worked", by="감수")                    # 신뢰 가능한 독립 2명 충족
        r = C.promote_case(self.sdir, cid, by="x", rationale="신뢰확인 2", method="worked_threshold")
        self.assertEqual(r["status"], "active")

    def test_build_case_pack_avoid_tier(self):
        w = C.propose_case(self.sdir, _cand(condition="좋은 상황", instruction="해", polarity="worked"), from_daepyo=True)
        f = C.propose_case(self.sdir, _cand(condition="나쁜 상황", instruction="하지마", polarity="failed"), from_daepyo=True)
        pack = C.build_case_pack(self.sdir)
        avoid_ids = [a["case_id"] for a in pack["avoid"]]
        pos_ids = [v["case_id"] for t in ("must_apply", "may_apply", "reference_only") for v in pack[t]]
        self.assertIn(f["case_id"], avoid_ids)
        self.assertNotIn(f["case_id"], pos_ids)                   # failed는 긍정 tier에 없음
        self.assertIn(w["case_id"], pos_ids)
        self.assertEqual(pack["avoid"][0]["strength"], "must_avoid")   # 대표 failed must = 반드시 피하라


class ResourceBodyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sdir = Path(self._tmp.name) / "skill"
        _write_skill_md(self.sdir, body="# t\n\n## 절차\n1. 한다\n\n## non_overridable\n- 운영DB 직접쓰기 금지\n")
        self.mani = self.sdir / "SKILL.md"

    def tearDown(self):
        self._tmp.cleanup()

    def test_cas_stale_reject(self):
        with self.assertRaises(RB.ResourceBodyError):
            RB.revise_body(self.mani, "운영DB 직접쓰기 금지", expected_version="9",
                           by="x", rationale="r", from_case_ids=["a", "b"], regression_attestation="ok")

    def test_ncase_gate(self):
        with self.assertRaises(RB.ResourceBodyError):
            RB.revise_body(self.mani, "운영DB 직접쓰기 금지", expected_version="1",
                           by="x", rationale="r", from_case_ids=["a"], regression_attestation="ok")

    def test_regression_attestation_required(self):
        with self.assertRaises(RB.ResourceBodyError):
            RB.revise_body(self.mani, "운영DB 직접쓰기 금지", expected_version="1",
                           by="x", rationale="r", from_case_ids=["a", "b"], regression_attestation="")

    def test_nonoverridable_preserved(self):
        with self.assertRaises(RB.ResourceBodyError):
            RB.revise_body(self.mani, "안전지침 삭제된 본문", expected_version="1",
                           by="x", rationale="r", from_case_ids=["a", "b"], regression_attestation="ok")

    def test_revise_snapshot_rollback(self):
        r = RB.revise_body(self.mani, "# 개정\n\n## non_overridable\n- 운영DB 직접쓰기 금지\n",
                           expected_version="1", by="공간관리", rationale="종합",
                           from_case_ids=["c1", "c2"], regression_attestation="충돌 없음")
        self.assertEqual(r["version"], "2")
        self.assertTrue((self.sdir / ".history" / "v1.md").exists())
        rb = RB.rollback_body(self.mani, "1", by="대표", rationale="되돌림")
        self.assertEqual(rb["version"], "3")
        self.assertIn("1. 한다", self.mani.read_text(encoding="utf-8"))


class KnowledgeLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.kdir = Path(self._tmp.name) / "k"
        self.kdir.mkdir(parents=True, exist_ok=True)
        (self.kdir / "지식.md").write_text(
            "---\nknowledge_id: k1\nname: k\ndescription: d\nversion: 1\n---\n\n## 범용 사실\n- claim-A: 배포는 화요일\n",
            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dispute_verify_cycle(self):
        K.dispute_claim(self.kdir, "claim-A", by="구현자", rationale="이번주 수요일")
        self.assertEqual(K.claim_status(self.kdir, "claim-A"), "disputed")
        self.assertIn("claim-A", [x["claim_id"] for x in K.claim_review_queue(self.kdir)])
        K.verify_claim(self.kdir, "claim-A", by="대표", rationale="원래 화요일 맞음")
        self.assertEqual(K.claim_status(self.kdir, "claim-A"), "active")
        self.assertEqual(K.claim_review_queue(self.kdir), [])

    def test_rationale_required(self):
        with self.assertRaises(K.KnowledgeLedgerError):
            K.dispute_claim(self.kdir, "claim-A", by="x", rationale="")

    def test_knowledge_body_revision_no_cases(self):
        r = RB.revise_body(self.kdir / "지식.md", "## 범용 사실\n- claim-A: 화요일(예외 가능)\n",
                           expected_version="1", by="대표", rationale="명확화", require_cases=False)
        self.assertEqual(r["version"], "2")


class SkillSmithTests(unittest.TestCase):
    def _name(self):
        n = f"_selftest_{uuid4().hex[:8]}"
        self.addCleanup(shutil.rmtree, S.SKILLS / "추가" / n, ignore_errors=True)
        self.addCleanup(shutil.rmtree, S.SKILLS / "대외비" / n, ignore_errors=True)
        return n

    def test_create_and_frontmatter(self):
        from core import discovery
        n = self._name()
        r = S.create_skill(name=n, description="회의록 요약 정리 핵심: 요약")
        self.assertTrue(r["skill_id"].startswith("skill_"))
        front = discovery.parse_front(C.SKILLS / "추가" / n / "SKILL.md")
        self.assertEqual(front.get("version"), "1")
        self.assertIsNotNone(C.skill_dir(n))

    def test_skill_detail_reads_description_and_skill_md(self):
        n = self._name()
        S.create_skill(name=n, description="대시보드에 보여줄 스킬 설명", body="# 본문\n\n## 절차\n1. 읽는다")
        detail = S.skill_detail(n)
        self.assertEqual(detail["name"], n)
        self.assertEqual(detail["description"], "대시보드에 보여줄 스킬 설명")
        self.assertEqual(detail["grade"], "추가")
        self.assertIn("SKILL.md", detail["path"])
        self.assertIn("# 본문", detail["content"])
        self.assertIn("frontmatter", detail)

    def test_dup_refuse_then_overwrite(self):
        n = self._name()
        S.create_skill(name=n, description="d1")
        with self.assertRaises(S.SkillSmithError):
            S.create_skill(name=n, description="d2")
        self.assertTrue(S.create_skill(name=n, description="d3", overwrite=True)["ok"])

    def test_grade_validation_and_confidential(self):
        with self.assertRaises(S.SkillSmithError):
            S.create_skill(name="_x_", description="d", grade="없는등급")
        n = self._name()
        r = S.create_skill(name=n, description="고객 환불 절차", grade="대외비")
        self.assertIn("스킬/대외비/", r["path"])

    def test_find_similar_and_discoverable(self):
        n1 = self._name()
        S.create_skill(name=n1, description="회의록을 요약하고 액션아이템 추출 미팅 정리")
        sim = S.find_similar_skills(["회의록 요약"], top=5)
        self.assertIn(n1, [s["name"] for s in sim])
        self.assertTrue(S.check_discoverable(n1, ["회의록 요약"], top=5)["discoverable"])
        self.assertFalse(S.check_discoverable(n1, ["주식 종목 추천"], top=5)["discoverable"])


class DeployGuardTests(unittest.TestCase):
    def test_scan_text(self):
        labels = {h["label"] for h in G.scan_text("hong@a.com 010-1234-5678 박부장 3억원")}
        self.assertTrue({"email", "휴대전화", "인명+직함"} <= labels)
        self.assertEqual(G.scan_text("버튼을 크게, 회의록 요약, 공장 자동화 성장"), [])

    def test_scan_deployable_flags_basic_pii(self):
        n = f"_selftest_{uuid4().hex[:8]}"
        bdir = C.SKILLS / "기본" / n
        self.addCleanup(shutil.rmtree, bdir, ignore_errors=True)
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "SKILL.md").write_text("---\nname: {0}\ndescription: d\n---\n#본문\n".format(n), encoding="utf-8")
        (bdir / "cases.jsonl").write_text(
            json.dumps({"case_id": "x", "instruction": "김철수 부장 010-1111-2222 처리", "status": "active"},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        findings = G.scan_deployable()
        self.assertTrue(any(n in f["path"] for f in findings))


class RoomWiringTests(unittest.TestCase):
    """P-wire-B: 매니저 decision 스키마에 propose_case가 안전하게 붙었는지(게이트)."""

    def setUp(self):
        from core import room_manager
        self.rm = room_manager

    def _ok_case(self, **over):
        cand = dict(condition="모바일", instruction="버튼 크게", polarity="worked",
                    action="add_case", routing_kind="procedural",
                    judgment_rationale="작게 보임", source_quote="버튼 작아")
        cand.update(over)
        return {"action": "propose_case", "wake": "", "message": "", "reason": "절차 교훈",
                "skill": "어떤스킬", "candidate": cand}

    def test_propose_case_in_actions(self):
        self.assertIn("propose_case", self.rm.MANAGER_ACTIONS)

    def test_valid_propose_case_passes(self):
        self.assertEqual(self.rm._decision_error(self._ok_case()), "")

    def test_propose_case_rejections(self):
        d = self._ok_case(); d["wake"] = "구현자"
        self.assertTrue(self.rm._decision_error(d))                       # wake 비어야 함
        d = self._ok_case(); d["skill"] = ""
        self.assertTrue(self.rm._decision_error(d))                       # skill 필수
        d = self._ok_case(); d["candidate"] = "notdict"
        self.assertTrue(self.rm._decision_error(d))                       # candidate 객체
        d = self._ok_case(); d["candidate"].pop("source_quote")
        self.assertTrue(self.rm._decision_error(d))                       # 필드 누락

    def test_existing_actions_unbroken(self):
        # 기존 계약 회귀: pass/stop 검증이 그대로 동작
        self.assertEqual(self.rm._decision_error(
            {"action": "stop", "wake": "", "message": "", "reason": "r"}), "")
        self.assertTrue(self.rm._decision_error(
            {"action": "pass", "wake": "", "message": "", "reason": "r"}))  # pass는 wake/message 필요


class GateCaseRecordingTests(unittest.TestCase):
    """P-wire-C3: 발행게이트가 에이전트의 케이스 적용 자기보고를 record(block 아님)."""

    def _skill_with_case(self):
        n = f"_selftest_{uuid4().hex[:8]}"
        sdir = C.SKILLS / "추가" / n
        self.addCleanup(shutil.rmtree, sdir, ignore_errors=True)
        S.create_skill(name=n, description="C3 게이트 테스트")
        case = C.propose_case(sdir, _cand(), from_daepyo=False)
        return n, sdir, case["case_id"]

    def test_case_application_recorded(self):
        from core import lesson_ledger as L
        n, sdir, cid = self._skill_with_case()
        report = json.dumps({
            "schema": "LessonApplicationReport.v1", "applications": [],
            "case_applications": [{"skill": n, "case_id": cid, "applied": True, "outcome": "worked"}],
        }, ensure_ascii=False)
        res = L.audit_reply_lesson_applications(
            "_c3test_space", content="답변 본문입니다.\n" + report, context_pack={}, agent="구현자", mode="chat")
        self.assertEqual(len(res["case_applications"]), 1)
        conv = {x["case_id"]: x for x in C.case_convergence(sdir)}
        self.assertEqual(conv[cid]["worked"], 1)   # 에이전트 자기보고 → worked 자동 집계(C2 수렴)

    def test_harmful_self_report(self):
        from core import lesson_ledger as L
        n, sdir, cid = self._skill_with_case()
        report = json.dumps({"case_applications": [{"skill": n, "case_id": cid, "outcome": "harmful"}]}, ensure_ascii=False)
        L.audit_reply_lesson_applications("_c3test_space", content="본문\n" + report, context_pack={}, agent="검수자", mode="chat")
        conv = {x["case_id"]: x for x in C.case_convergence(sdir)}
        self.assertTrue(conv[cid]["needs_review"])   # harmful 자기보고 → 검토 필요 깃발

    def test_bad_case_ref_does_not_block(self):
        from core import lesson_ledger as L
        report = json.dumps({"case_applications": [{"skill": "존재안함스킬", "case_id": "x", "outcome": "worked"}]}, ensure_ascii=False)
        res = L.audit_reply_lesson_applications(
            "_c3test_space", content="본문\n" + report, context_pack={}, agent="x", mode="chat")
        self.assertEqual(res["case_applications"], [])   # 실패한 기록은 제외, 발행 안 막힘(fail-safe)


class WorkDecompositionTests(unittest.TestCase):
    """작업 분할/체크포인트: 타임아웃+진행 시 처음부터가 아니라 체크포인트에서 이어서 재실행(무한루프 방지)."""

    def setUp(self):
        from core import engine, people, spaces
        from core.paths import PEOPLE, SPACES
        self.E, self.people, self.spaces = engine, people, spaces
        self.PEOPLE, self.SPACES = PEOPLE, SPACES
        self.tok = people.create_person("_wtest_" + uuid4().hex[:6])
        self.sp = spaces.create_space("_wsp_" + uuid4().hex[:6])
        spaces.join(self.tok, self.sp)
        self._orig = engine.run_engine
        self.addCleanup(setattr, engine, "run_engine", self._orig)
        self.addCleanup(shutil.rmtree, PEOPLE / self.tok, ignore_errors=True)
        self.addCleanup(shutil.rmtree, SPACES / self.sp, ignore_errors=True)

    def _result(self):
        seat = self.PEOPLE / self.tok / "공간" / self.sp / "작업"
        wd = list(seat.iterdir()) if seat.exists() else []
        return (wd[0] / "결과.md").read_text(encoding="utf-8") if wd else ""

    def test_timeout_with_progress_continues(self):
        calls = {"n": 0}
        def fake(wdir, prompt):
            calls["n"] += 1
            rf = Path(wdir) / "결과.md"
            cur = rf.read_text(encoding="utf-8") if rf.exists() else ""
            if calls["n"] == 1:
                rf.write_text("단계1 완료\n## 다음 단계\n단계2 남음\n", encoding="utf-8")
                return "(엔진 타임아웃)"
            rf.write_text(cur + "단계2 완료\n전체 완료\n", encoding="utf-8")
            return "작업 완료"
        self.E.run_engine = fake
        self.E.work(self.tok, self.sp, "여러 단계로 나눠야 하는 큰 작업", context={})
        self.assertEqual(calls["n"], 2)            # 이어서 재실행됨
        r = self._result()
        self.assertIn("단계1 완료", r)             # 1차 체크포인트 보존
        self.assertIn("단계2 완료", r)             # 이어서 완료

    def test_timeout_no_progress_bounded_retry_then_escalates(self):
        # 무진행 타임아웃: 즉시 죽이지 않고 '체크포인트(골격)부터' 1회만 재시도(큰 작업이 읽기에 시간을
        # 다 쓰고 시작도 못 하는 경우 구제) → 그래도 무진행이면 에스컬레이션. 여전히 유한(무한루프 아님).
        calls = {"n": 0}
        def fake(wdir, prompt):
            calls["n"] += 1
            return "(엔진 타임아웃)"                # 끝까지 아무 진행도 안 남김
        self.E.run_engine = fake
        with self.assertRaises(Exception):         # 1회 재시도 후 에러로 에스컬레이션
            self.E.work(self.tok, self.sp, "멈추는 작업", context={})
        self.assertEqual(calls["n"], 1 + self.E.WORK_NO_PROGRESS_RETRY_LIMIT)  # 초기 1 + 무진행 재시도 상한


class RecoverLiveTaskTests(unittest.TestCase):
    """부팅 복구가 *살아있는*(신선 하트비트) 작업의 claim을 뺏지 않는다(긴 작업 발행 거부 버그 수정)."""

    def setUp(self):
        from core import room_manager, task_registry, manager_claim, spaces
        from core.spaces import MANAGER_DIRNAME
        from core.paths import SPACES
        self.RM, self.TR, self.MC = room_manager, task_registry, manager_claim
        self.sp = spaces.create_space("_rec_" + uuid4().hex[:6])
        (SPACES / self.sp / MANAGER_DIRNAME).mkdir(parents=True, exist_ok=True)
        (SPACES / self.sp / MANAGER_DIRNAME / "상태.json").write_text(
            json.dumps({"상태": "agent_running"}, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(shutil.rmtree, SPACES / self.sp, ignore_errors=True)
        self.addCleanup(setattr, task_registry, "snapshot", task_registry.snapshot)
        self.addCleanup(setattr, manager_claim, "expire_foreign_boot_claim", manager_claim.expire_foreign_boot_claim)
        self.addCleanup(setattr, room_manager, "tick", room_manager.tick)

    def _wire(self):
        called = {"expire": 0, "tick": 0}
        self.MC.expire_foreign_boot_claim = lambda s: called.__setitem__("expire", called["expire"] + 1)
        self.RM.tick = lambda *a, **k: (called.__setitem__("tick", called["tick"] + 1), {"ok": True})[1]
        return called

    def test_skips_recovery_when_task_alive(self):
        self.TR.snapshot = lambda s: {"active_items": [{"task_id": "t1", "heartbeat_stale": False}]}
        called = self._wire()
        res = self.RM.recover_space(self.sp)
        self.assertFalse(res["recovered"])
        self.assertIn("owner_alive", res["reason"])
        self.assertEqual(called["expire"], 0)   # 살아있는 작업의 claim을 안 뺏음
        self.assertEqual(called["tick"], 0)

    def test_recovers_when_task_stalled(self):
        self.TR.snapshot = lambda s: {"active_items": [{"task_id": "t1", "heartbeat_stale": True}]}
        called = self._wire()
        res = self.RM.recover_space(self.sp)
        self.assertTrue(res["recovered"])       # 진짜 멈춘 작업은 복구 진행
        self.assertEqual(called["expire"], 1)
        self.assertEqual(called["tick"], 1)


if __name__ == "__main__":
    unittest.main()
