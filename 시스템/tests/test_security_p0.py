# -*- coding: utf-8 -*-
"""P0 보안 봉합 회귀 테스트 — 대외비 파일 API 가드 + 접근 토큰 가드(옵트인).

배경: 파일 API(HTTP)는 무인증이라 대외비(자격증명) 경로가 그대로 내려가는 유출 구멍이 있었다
(2026-07-02 전면 분석 S1/S2). 이 테스트는 그 부류가 다시 열리지 않게 잠근다.
"""
import os
import sys
import unicodedata
import unittest
from pathlib import Path

SYS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SYS_DIR))
sys.path.insert(0, str(SYS_DIR / "대시보드" / "서버"))

import core.files as files                     # noqa: E402
import auth_guard                              # noqa: E402


class ConfidentialFileGuardTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("CNV_FILES_ALLOW_CONFIDENTIAL", None)

    def test_raw_read_of_confidential_asset_blocked(self):
        with self.assertRaises(ValueError):
            files.resolve_file("자산/대외비/vm-targets/vm_credentials.json")

    def test_list_inside_confidential_blocked(self):
        with self.assertRaises(ValueError):
            files.list_dir("자산/대외비")

    def test_confidential_anywhere_in_path_blocked(self):
        for rel in ("도구/대외비/x.py", "스킬/대외비/foo/SKILL.md", "지식/대외비/bar/지식.md"):
            with self.assertRaises(ValueError):
                files.resolve_file(rel)

    def test_sensitive_sidecar_blocked(self):
        for f in ("cases.local.jsonl", "cases.archive.jsonl", "case_events.jsonl"):
            with self.assertRaises(ValueError):
                files.resolve_file(f"스킬/기본/앱등록/{f}")

    def test_nfd_normalized_confidential_blocked(self):
        # macOS(APFS)는 NFD/NFC를 같은 파일로 취급 — NFD로 분해한 '대외비'로 가드를 우회할 수 없어야 함.
        nfd = unicodedata.normalize("NFD", "대외비")
        self.assertNotEqual(nfd, "대외비")   # 실제로 분해됐는지 확인(우회 전제)
        with self.assertRaises(ValueError):
            files.list_dir(f"자산/{nfd}")
        with self.assertRaises(ValueError):
            files.resolve_file(f"자산/{nfd}/vm-targets/vm_credentials.json")

    def test_case_variant_sidecar_blocked(self):
        # APFS 대소문자 무관 — 대소문자 바꾼 사이드카 파일명으로 우회 불가.
        with self.assertRaises(ValueError):
            files.resolve_file("스킬/기본/앱등록/Cases.Local.JSONL")

    def test_normal_paths_still_work(self):
        listing = files.list_dir("")
        self.assertTrue(listing["항목"])
        f = files.resolve_file("law.md")
        self.assertTrue(f.is_file())
        # 대외비 폴더 '이름'이 목록에 보이는 것은 허용(들어가기만 차단)
        assets = files.list_dir("자산")
        names = [i["이름"] for i in assets["항목"]]
        self.assertIn("대외비", names)

    def test_kill_switch_restores_access(self):
        os.environ["CNV_FILES_ALLOW_CONFIDENTIAL"] = "1"
        try:
            listing = files.list_dir("자산/대외비")
            self.assertIsInstance(listing["항목"], list)
        finally:
            os.environ.pop("CNV_FILES_ALLOW_CONFIDENTIAL", None)


class _FakeConn:
    """starlette Request/WebSocket 대역 — client/cookies/query_params만 흉내낸다."""
    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, host="10.0.0.5", cookies=None, query=None):
        self.client = self._Client(host)
        self.cookies = cookies or {}
        self.query_params = query or {}


class AuthGuardTests(unittest.TestCase):
    def setUp(self):
        self._orig = auth_guard.TOKEN_FILE
        self.tmp = Path(__file__).parent / ".test_auth_token"
        auth_guard.TOKEN_FILE = self.tmp

    def tearDown(self):
        auth_guard.TOKEN_FILE = self._orig
        self.tmp.unlink(missing_ok=True)

    def test_disabled_without_token_file(self):
        self.tmp.unlink(missing_ok=True)
        ok, set_cookie = auth_guard.check_request(_FakeConn())
        self.assertTrue(ok)
        self.assertFalse(set_cookie)

    def test_loopback_always_allowed(self):
        self.tmp.write_text("secret-token", encoding="utf-8")
        ok, _ = auth_guard.check_request(_FakeConn(host="127.0.0.1"))
        self.assertTrue(ok)

    def test_remote_without_token_rejected(self):
        self.tmp.write_text("secret-token", encoding="utf-8")
        ok, _ = auth_guard.check_request(_FakeConn())
        self.assertFalse(ok)

    def test_query_token_accepted_and_sets_cookie(self):
        self.tmp.write_text("secret-token", encoding="utf-8")
        ok, set_cookie = auth_guard.check_request(_FakeConn(query={"auth": "secret-token"}))
        self.assertTrue(ok)
        self.assertTrue(set_cookie)

    def test_cookie_accepted(self):
        self.tmp.write_text("secret-token", encoding="utf-8")
        ok, set_cookie = auth_guard.check_request(_FakeConn(cookies={"cnv_auth": "secret-token"}))
        self.assertTrue(ok)
        self.assertFalse(set_cookie)

    def test_wrong_token_rejected(self):
        self.tmp.write_text("secret-token", encoding="utf-8")
        ok, _ = auth_guard.check_request(_FakeConn(query={"auth": "wrong"}))
        self.assertFalse(ok)


class CasesRouterLocalLeakTests(unittest.TestCase):
    """cases 라우터가 대외비 사이드카(cases.local.jsonl)를 HTTP로 노출하지 않는지 (CRITICAL-2)."""

    def test_list_cases_excludes_local_sidecar(self):
        import json
        import core.case_ledger as case_ledger
        from routers import cases as cases_router

        # 임시 스킬 폴더에 공개 케이스 1 + 대외비 로컬 케이스 1을 심는다.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp)
            secret = "REDACTED_DUMMY_PII"
            (sdir / "cases.jsonl").write_text(
                json.dumps({"case_id": "pub1", "status": "active", "condition": "공개",
                            "instruction": "공개 지시", "polarity": "worked"}, ensure_ascii=False) + "\n",
                encoding="utf-8")
            (sdir / "cases.local.jsonl").write_text(
                json.dumps({"case_id": "loc1", "status": "active", "condition": secret,
                            "instruction": secret, "polarity": "worked",
                            "sensitivity": "confidential"}, ensure_ascii=False) + "\n",
                encoding="utf-8")

            # 라우터가 쓰는 _sdir를 이 임시 폴더로 돌린다.
            orig = cases_router._sdir
            cases_router._sdir = lambda skill: sdir
            try:
                res = cases_router.list_cases("테스트스킬")
            finally:
                cases_router._sdir = orig

            blob = json.dumps(res, ensure_ascii=False)
            self.assertNotIn(secret, blob)                       # 대외비 내용 미노출
            self.assertNotIn("loc1", [c.get("case_id") for c in res["cases"]])
            self.assertIn("pub1", [c.get("case_id") for c in res["cases"]])  # 공개는 정상

    def test_read_cases_internal_still_includes_local(self):
        # 내부 주입 경로는 여전히 로컬을 읽어야 한다(가드는 HTTP 노출만 막지 내부 사용은 유지).
        import json
        import tempfile
        import core.case_ledger as case_ledger
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp)
            (sdir / "cases.jsonl").write_text(
                json.dumps({"case_id": "pub1", "status": "active", "polarity": "worked"}) + "\n", encoding="utf-8")
            (sdir / "cases.local.jsonl").write_text(
                json.dumps({"case_id": "loc1", "status": "active", "polarity": "worked"}) + "\n", encoding="utf-8")
            all_ids = {c["case_id"] for c in case_ledger.read_cases(sdir)}
            pub_ids = {c["case_id"] for c in case_ledger.read_cases(sdir, include_local=False)}
            self.assertEqual(all_ids, {"pub1", "loc1"})           # 내부: 둘 다
            self.assertEqual(pub_ids, {"pub1"})                    # HTTP: 공개만


if __name__ == "__main__":
    unittest.main()
