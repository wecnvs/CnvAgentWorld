# -*- coding: utf-8 -*-
"""지식 dispute 채널(P3'/P4) — claim id 파생·dispute·audit 보고 파싱 회귀 테스트."""
import sys
import tempfile
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))

import core.knowledge_ledger as kl              # noqa: E402


def _mk_knowledge(kroot: Path, name: str, claims: list) -> Path:
    """지식/추가/{name}/지식.md를 직접 생성(create_knowledge는 relative_to(ROOT)라 temp 밖에서 못 씀)."""
    kdir = kroot / "추가" / name
    kdir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\ndescription: \"테스트\"\n---\n\n# {name}\n\n## 범용 사실\n"
    body += "".join(f"- {c}\n" for c in claims)
    (kdir / "지식.md").write_text(body, encoding="utf-8")
    return kdir


class KnowledgeDisputeTests(unittest.TestCase):
    def setUp(self):
        self._K = kl.KNOWLEDGE
        self.tmp = Path(tempfile.mkdtemp())
        kl.KNOWLEDGE = self.tmp
        _mk_knowledge(self.tmp, "배포정책", ["배포는 금요일 금지", "핫픽스는 예외로 허용"])

    def tearDown(self):
        kl.KNOWLEDGE = self._K
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_claims_derives_ids(self):
        claims = kl.list_claims("배포정책")
        self.assertEqual(len(claims), 2)
        texts = [c["text"] for c in claims]
        self.assertIn("배포는 금요일 금지", texts)
        for c in claims:
            self.assertTrue(c["claim_id"].startswith("claim_"))
            self.assertEqual(c["status"], "provisional")

    def test_dispute_by_text_marks_disputed(self):
        kl.dispute("배포정책", claim_text="배포는 금요일 금지", by="구현자", rationale="실제론 금요일도 배포함")
        claims = {c["text"]: c for c in kl.list_claims("배포정책")}
        self.assertEqual(claims["배포는 금요일 금지"]["status"], "disputed")
        self.assertEqual(claims["핫픽스는 예외로 허용"]["status"], "provisional")  # 다른 claim 영향 없음
        rq = kl.claim_review_queue("배포정책")
        self.assertEqual(len(rq), 1)

    def test_dispute_by_id(self):
        cid = kl.list_claims("배포정책")[0]["claim_id"]
        kl.dispute("배포정책", claim_id=cid, by="검토가", rationale="틀림")
        self.assertEqual(kl.claim_status("배포정책", cid), "disputed")

    def test_dispute_requires_target(self):
        with self.assertRaises(kl.KnowledgeLedgerError):
            kl.dispute("배포정책", by="x", rationale="y")

    def test_claim_id_stable(self):
        self.assertEqual(kl.claim_id_for("배포는 금요일 금지"), kl.claim_id_for(" 배포는  금요일 금지 "))


class AuditKnowledgeDisputeTests(unittest.TestCase):
    """audit_reply_lesson_applications가 knowledge_disputes 보고를 파싱해 dispute하는지."""
    def setUp(self):
        self._K = kl.KNOWLEDGE
        self.tmp = Path(tempfile.mkdtemp())
        kl.KNOWLEDGE = self.tmp
        _mk_knowledge(self.tmp, "배포정책", ["배포는 금요일 금지"])

    def tearDown(self):
        kl.KNOWLEDGE = self._K
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_audit_parses_knowledge_disputes(self):
        import json
        import core.lesson_ledger as ll
        cid = kl.list_claims("배포정책")[0]["claim_id"]
        report = ("작업 결과입니다.\n"
                  + json.dumps({"knowledge_disputes": [
                      {"knowledge": "배포정책", "claim_id": cid, "why": "실제론 금요일도 배포"}]}, ensure_ascii=False))
        res = ll.audit_reply_lesson_applications(
            "테스트공간", content=report,
            context_pack={"context_pack_id": "x", "lesson_pack": {}}, agent="구현자", mode="work")
        self.assertEqual(len(res.get("knowledge_disputes", [])), 1)
        self.assertEqual(kl.claim_status("배포정책", cid), "disputed")


if __name__ == "__main__":
    unittest.main()
