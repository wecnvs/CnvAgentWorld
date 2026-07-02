#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""대시보드 모바일 실제 브라우저 회귀 테스트.

Playwright가 없는 환경에서는 skip된다. 설치된 환경에서는 임시 대시보드
서버를 별도 포트로 띄우고 Chromium 모바일 viewport로 실제 UI를 점검한다.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
SYS = ROOT / "시스템"
SERVER = SYS / "대시보드" / "서버"
ARTIFACTS = ROOT / "임시작업" / "playwright"

sys.path.insert(0, str(SYS))
from core import lesson_ledger, room_manager, spaces as spaces_core  # noqa: E402
from core.paths import PEOPLE, SPACES  # noqa: E402

try:
    from playwright.sync_api import expect, sync_playwright
except Exception as exc:  # pragma: no cover - 환경 의존 skip
    expect = None
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


PREFIX = "tmp_mobilepw_"


def _cleanup():
    for base in (SPACES, PEOPLE):
        for path in base.glob(PREFIX + "*"):
            shutil.rmtree(path, ignore_errors=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DashboardMobilePlaywrightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("CNV_DASHBOARD_PLAYWRIGHT") != "1":
            raise unittest.SkipTest("set CNV_DASHBOARD_PLAYWRIGHT=1 to run mobile Playwright tests")
        if PLAYWRIGHT_IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"playwright import failed: {PLAYWRIGHT_IMPORT_ERROR}")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_log_path = ARTIFACTS / "dashboard_mobile_server_latest.log"
        cls.server_log = cls.server_log_path.open("w", encoding="utf-8")
        env = dict(os.environ)
        env["HOST"] = "127.0.0.1"
        env["PORT"] = str(cls.port)
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
            ],
            cwd=str(SERVER),
            env=env,
            stdout=cls.server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 20
        last_error = ""
        while time.time() < deadline:
            if cls.server.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{cls.base_url}/api/health", timeout=0.5) as res:
                    if res.status == 200:
                        return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
        raise RuntimeError(
            f"dashboard test server did not start on :{cls.port}; "
            f"last_error={last_error}; log={cls.server_log_path}"
        )

    @classmethod
    def tearDownClass(cls):
        server = getattr(cls, "server", None)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        log = getattr(cls, "server_log", None)
        if log:
            log.close()
        _cleanup()

    def setUp(self):
        _cleanup()

    def tearDown(self):
        _cleanup()

    def _seed_space_with_growth_candidate(self) -> str:
        space_name = f"{PREFIX}{uuid4().hex[:6]}"
        space = spaces_core.create_space(space_name)
        room_manager.post(
            space,
            "모바일 회귀 테스트용 기존 메시지",
            requester="대표",
            run_manager=False,
            client_message_id=f"seed-{space}",
        )
        lesson_ledger.record_post_interaction_evaluation(
            space,
            outcome="corrected",
            source_event="mobile_playwright_seed",
            actor="대표",
            target="space",
            lesson_candidate_needed=True,
            lesson_candidate={
                "kind": "growth_rule",
                "scope": "space",
                "status": "active",
                "promotion_target": "knowledge",
                "instruction": "모바일 대시보드 변경은 실제 모바일 viewport로 observer와 입력 흐름을 검증한다.",
                "evidence_level": "user_directive",
                "confidence": 0.9,
                "applies_when": {"space_id": space, "agent_modes": ["manager"], "keywords": []},
            },
        )
        lesson_ledger.generate_promotion_candidates(space, actor="대표")
        return space

    def _seed_space_with_long_chat(self) -> str:
        space_name = f"{PREFIX}{uuid4().hex[:6]}"
        space = spaces_core.create_space(space_name)
        for idx in range(42):
            room_manager.post(
                space,
                f"모바일 긴 대화 스크롤 회귀 메시지 {idx + 1:02d}\n내용 확인용 본문입니다.",
                requester="대표" if idx % 2 == 0 else "공간관리",
                run_manager=False,
                client_message_id=f"long-chat-{space}-{idx}",
            )
        return space

    def _open_space(self, page, space: str):
        page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        expect(page.locator("#status.ok")).to_be_visible(timeout=10000)
        space_card = page.locator("#spaces-list li", has_text=space.rsplit("_", 1)[0]).first
        expect(space_card).to_be_visible(timeout=10000)
        space_card.scroll_into_view_if_needed()
        space_card.locator(".open-room-btn").click()
        expect(page.locator("#room-title")).to_have_text(space, timeout=10000)

    def test_mobile_room_chat_observer_growth_panel_and_send_button(self):
        space = self._seed_space_with_growth_candidate()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"chromium launch failed: {exc}") from exc
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                device_scale_factor=3,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
            self.assertTrue(page.evaluate("matchMedia('(max-width: 720px)').matches"))
            expect(page.locator("#status.ok")).to_be_visible(timeout=10000)

            space_card = page.locator("#spaces-list li", has_text=space.rsplit("_", 1)[0]).first
            expect(space_card).to_be_visible(timeout=10000)
            space_card.scroll_into_view_if_needed()
            space_card.locator(".open-room-btn").click()

            expect(page.locator("#room-title")).to_have_text(space, timeout=10000)
            # 모바일은 채팅 우선으로 관측패널(observer stack)이 기본 접힘이다(채팅 메시지 영역 확보 — 96px→300px+).
            # 관측 내용을 확인하려면 '상태 펼치기' 토글로 편다.
            page.locator("#room-observer-all-toggle").click()
            expect(page.locator("#room-observer-stack")).to_be_visible(timeout=10000)
            # 모바일: 스플릿바는 이제 '높이 조절' 핸들로 노출된다(row-resize) — 적층 영역 높이를 드래그로 조절(mobile-resize.js)
            for splitter in ("agents", "spaces", "chat"):
                bar = page.locator(f'.workspace-splitter[data-splitter-left="{splitter}"]')
                expect(bar).to_be_visible()
                self.assertEqual(
                    bar.evaluate("el => getComputedStyle(el).cursor"), "row-resize",
                    f"{splitter} 모바일 스플릿바가 row-resize 높이 핸들이 아님",
                )
            expect(page.locator("#room-promotion-review")).to_have_attribute("data-empty", "no", timeout=10000)
            expect(page.locator("#room-promotion-review")).to_contain_text("성장 후보")
            expect(page.locator("#room-promotion-review")).to_contain_text("검토대기")
            expect(page.locator("#room-promotion-review")).to_contain_text("승인")
            expect(page.locator("#room-promotion-review")).to_contain_text("반려")
            expect(page.locator("#room-send")).to_be_visible()
            expect(page.locator("#room-input")).to_be_visible()

            metrics = page.evaluate(
                """() => {
                  const selectors = [
                    '.topbar', '.workspace', '#viewer', '#roomView', '.room-head',
                    '#room-snapshot', '#room-observer-stack', '#room-messages', '#room-form'
                  ];
                  const rows = selectors.map((selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return { selector, missing: true };
                    const r = el.getBoundingClientRect();
                    return { selector, left: r.left, right: r.right, width: r.width, height: r.height };
                  });
                  const observer = document.querySelector('#room-observer-stack').getBoundingClientRect();
                  return {
                    innerWidth: window.innerWidth,
                    bodyScrollWidth: document.body.scrollWidth,
                    docScrollWidth: document.documentElement.scrollWidth,
                    observerHeight: observer.height,
                    rows
                  };
                }"""
            )
            self.assertLessEqual(metrics["bodyScrollWidth"], metrics["innerWidth"] + 4)
            self.assertLessEqual(metrics["docScrollWidth"], metrics["innerWidth"] + 4)
            self.assertLessEqual(metrics["observerHeight"], 322)
            for row in metrics["rows"]:
                self.assertFalse(row.get("missing"), row)
                self.assertGreaterEqual(row["left"], -2, row)
                self.assertLessEqual(row["right"], metrics["innerWidth"] + 2, row)

            page.screenshot(path=str(ARTIFACTS / "dashboard_mobile_room_latest.png"), full_page=True)

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(250)
            bottom_y = page.evaluate("window.scrollY")
            page.wait_for_timeout(1800)
            after_poll_y = page.evaluate("window.scrollY")
            self.assertGreater(bottom_y, 0)
            self.assertGreaterEqual(after_poll_y + 24, bottom_y)

            def fulfill_post(route):
                body = json.loads(route.request.post_data or "{}")
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "ack": {
                            "message_id": "msg_playwright_mobile",
                            "client_message_id": body.get("client_message_id", ""),
                            "event_seq": 999,
                            "intent_id": "intent_playwright_mobile",
                            "conversation_thread_id": "thread_playwright_mobile",
                            "room_generation": 1,
                            "duplicate": False,
                        },
                        "events": [],
                    }, ensure_ascii=False),
                )

            page.route(f"**/api/spaces/{space}/post", fulfill_post)
            send_text = "모바일 전송 회귀 확인"
            before_send_y = page.evaluate("window.scrollY")
            page.locator("#room-input").fill(send_text)
            page.locator("#room-send").click()

            expect(page.locator("#room-input")).to_have_value("", timeout=5000)
            expect(page.locator(".msg.outbox", has_text=send_text)).to_be_visible(timeout=5000)
            expect(page.locator("#room-send")).to_have_text("보내기", timeout=5000)
            after_send_y = page.evaluate("window.scrollY")
            # [모바일 하단→최상단 튐 회귀 가드] 예전엔 전송 후 window.scrollTo(0,0)으로 문서를 top으로
            # 되돌렸다(position:fixed body 전제의 iOS 완화). 그러나 모바일 body는 position:static;
            # overflow:auto라 그 리셋이 '실제 페이지'를 최상단으로 순간이동시켜 '맨 아래로 내리면(또는
            # 보내면) 최상단으로 튄다'가 됐다(대표 신고). 이제 전송 후 blur로 키보드만 닫고 스크롤은
            # 건드리지 않는다 → 보던 위치(여기선 하단)가 유지되어야 한다. top(0)으로 리셋되면 회귀다.
            self.assertGreater(after_send_y, 0, "전송 후 페이지가 최상단(0)으로 튐 — 하단→최상단 스크롤 튐 회귀")
            self.assertGreaterEqual(after_send_y + 24, before_send_y, "전송 후 스크롤 위치가 크게 위로 튐(하단→최상단 튐 회귀)")
            page.screenshot(path=str(ARTIFACTS / "dashboard_mobile_after_send_latest.png"), full_page=True)

            context.close()
            browser.close()
            self.assertEqual(page_errors, [])

    def test_webkit_busy_room_stays_responsive(self):
        # 대표 반복신고 근본원인 회귀 가드: iOS Safari(WebKit)에서 작업중(agent_running 등) 방을 열면
        # 방 안 작업콘솔 iframe이 메인스레드를 포화시켜 화면이 얼고 터치가 다 먹통이 됐다. Chromium(다른
        # 테스트)으로는 재현 안 됨 → WebKit로 busy 상태 방을 열고 폴링 몇 사이클 뒤에도 응답(터치/스크롤)
        # 하는지 확인한다. 프리즈면 evaluate/tap이 타임아웃돼 실패한다.
        space = self._seed_space_with_long_chat()
        busy = json.dumps({"상태": "agent_running", "current": "레빗전문가_ceba", "activity": [],
                           "tasks": {"running_count": 1, "latest_worker": "레빗전문가_ceba"}}, ensure_ascii=False)
        with sync_playwright() as p:
            if not getattr(p, "webkit", None):
                raise unittest.SkipTest("webkit not available")
            try:
                browser = p.webkit.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"webkit launch failed: {exc}") from exc
            context = browser.new_context(
                viewport={"width": 390, "height": 844}, has_touch=True, device_scale_factor=2,
                user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
            )
            page = context.new_page()
            page.set_default_timeout(8000)
            page.route("**/api/spaces/*/status*", lambda r: r.fulfill(status=200, content_type="application/json", body=busy))
            self._open_space(page, space)
            page.wait_for_timeout(8000)   # 폴링 ~5사이클(busy 상태 재렌더). 프리즈면 이후 호출이 막힌다.
            # 메인스레드가 살아있어야 한다(프리즈면 아래 evaluate가 타임아웃 → 테스트 실패)
            self.assertGreaterEqual(page.evaluate("() => document.querySelectorAll('#room-messages .msg').length"), 1)
            # 입력 터치가 정상이어야 한다(먹통 아님)
            inp = page.locator("#room-input").first
            ib = inp.bounding_box()
            page.touchscreen.tap(ib["x"] + ib["width"] / 2, ib["y"] + ib["height"] / 2)
            page.wait_for_timeout(200)
            self.assertEqual(page.evaluate("() => document.activeElement && document.activeElement.id"),
                             "room-input", "busy 방에서 입력 터치 먹통(WebKit 프리즈)")
            context.close()
            browser.close()

    def test_mobile_combobox_space_switch_renders_and_touch(self):
        # 대표 신고 회귀 가드: 공간대화 콤보박스로 공간을 선택하면 채팅이 안 그려지고 터치가 먹통.
        # 전환 후 (1)제목·메시지 렌더 완료, (2)JS 에러 없음, (3)입력 터치 정상, (4)스크롤 동작을 확인한다.
        space_a = self._seed_space_with_long_chat()          # 42 메시지
        space_b = self._seed_space_with_growth_candidate()   # 다른 공간(빈/성장후보)
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"chromium launch failed: {exc}") from exc
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True, has_touch=True, device_scale_factor=3,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            self._open_space(page, space_a)
            expect(page.locator("#room-messages .msg")).to_have_count(42, timeout=10000)

            # 콤보박스로 B 전환 → 제목 바뀌고 렌더 완료(에러로 반쯤 그려지다 멈추면 안 됨)
            page.select_option("#room-space-select", space_b)
            expect(page.locator("#room-title")).to_have_text(space_b, timeout=10000)
            expect(page.locator("#room-messages")).to_be_visible()

            # 다시 A로 전환 → 42개 메시지가 모두 렌더돼야 한다(렌더 중단 아님)
            page.select_option("#room-space-select", space_a)
            expect(page.locator("#room-title")).to_have_text(space_a, timeout=10000)
            expect(page.locator("#room-messages .msg")).to_have_count(42, timeout=10000)

            # 전환 직후 입력 터치가 정상(먹통 아님)
            inp = page.locator("#room-input").first
            ib = inp.bounding_box()
            page.touchscreen.tap(ib["x"] + ib["width"] / 2, ib["y"] + ib["height"] / 2)
            page.wait_for_timeout(200)
            self.assertEqual(page.evaluate("() => document.activeElement && document.activeElement.id"),
                             "room-input", "콤보박스 전환 후 입력 터치 먹통")

            # 스크롤 동작
            page.evaluate("window.scrollTo(0, 300)")
            page.wait_for_timeout(50)
            self.assertGreater(page.evaluate("() => window.scrollY"), 0, "전환 후 스크롤 먹통")

            self.assertEqual(page_errors, [], f"콤보박스 전환 중 JS 에러: {page_errors}")
            context.close()
            browser.close()

    def test_mobile_split_bar_tap_does_not_freeze_touch(self):
        # 대표 반복신고 회귀 가드: 모바일에서 공간 진입 후 스플릿바를 탭해도(과거 setPointerCapture로
        # 터치가 그 바에 갇혀 화면 전체 먹통) 이후 입력창 탭·페이지 스크롤이 정상이어야 한다.
        # (iOS 특이 프리즈 자체는 headless로 재현 불가하나, capture 누수·입력 미탭·오버레이 등 인접
        #  회귀 클래스를 가드한다. 정본 규칙: law_manager「모바일 터치·인터랙션 회귀」.)
        space = self._seed_space_with_long_chat()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"chromium launch failed: {exc}") from exc
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True, has_touch=True, device_scale_factor=3,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            self._open_space(page, space)

            # 스플릿바를 터치로 탭(과거엔 여기서 setPointerCapture로 터치가 갇힘)
            bar = page.locator('.workspace-splitter[data-splitter-left="chat"]').first
            expect(bar).to_be_visible()
            box = bar.bounding_box()
            page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(200)

            # ① 어떤 스플릿바에도 pointer capture가 남아있지 않아야 한다
            capture_leaked = page.evaluate(
                """() => [...document.querySelectorAll('.workspace-splitter')].some(
                     el => [...Array(30)].some((_, i) => { try { return el.hasPointerCapture(i); } catch (_) { return false; } }))"""
            )
            self.assertFalse(capture_leaked, "스플릿바에 pointer capture가 남음(터치 먹통 원인)")

            # ② 화면을 덮어 터치를 먹는 전면 오버레이(pointer-events 살아있음)가 없어야 한다
            blocking_overlay = page.evaluate(
                """() => { const W = innerWidth, H = innerHeight;
                     return [...document.querySelectorAll('body *')].some(el => {
                       const cs = getComputedStyle(el), r = el.getBoundingClientRect();
                       return (cs.position === 'fixed' || cs.position === 'absolute')
                         && r.width >= W * 0.9 && r.height >= H * 0.6
                         && cs.display !== 'none' && cs.visibility !== 'hidden' && cs.pointerEvents !== 'none'
                         && !el.closest('#roomView'); }); }"""
            )
            self.assertFalse(blocking_overlay, "화면을 덮는 터치차단 오버레이가 있음")

            # ③ 바 탭 이후 입력창 터치 탭이 정상 동작해야 한다(먹통 아님)
            inp = page.locator("#room-input").first
            ib = inp.bounding_box()
            page.touchscreen.tap(ib["x"] + ib["width"] / 2, ib["y"] + ib["height"] / 2)
            page.wait_for_timeout(200)
            self.assertEqual(page.evaluate("() => document.activeElement && document.activeElement.id"),
                             "room-input", "바 탭 후 입력창 터치가 먹통(포커스 안 됨)")

            # ④ 페이지 세로 스크롤이 동작해야 한다
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(50)
            page.evaluate("window.scrollTo(0, 300)")
            page.wait_for_timeout(50)
            self.assertGreater(page.evaluate("() => window.scrollY"), 0, "페이지 스크롤이 먹통")

            self.assertEqual(page_errors, [])
            context.close()
            browser.close()

    def test_mobile_room_chat_height_and_latest_button(self):
        space = self._seed_space_with_long_chat()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"chromium launch failed: {exc}") from exc
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                device_scale_factor=3,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            self._open_space(page, space)
            expect(page.locator("#room-messages .msg")).to_have_count(42, timeout=10000)
            expect(page.locator("#room-latest")).to_be_hidden()

            metrics = page.evaluate(
                """() => {
                  const chat = document.querySelector('#chat-panel').getBoundingClientRect();
                  const view = document.querySelector('#roomView').getBoundingClientRect();
                  const list = document.querySelector('#room-messages');
                  const messages = list.getBoundingClientRect();
                  const form = document.querySelector('#room-form').getBoundingClientRect();
                  return {
                    innerHeight: window.innerHeight,
                    chatHeight: chat.height,
                    viewHeight: view.height,
                    messageHeight: messages.height,
                    messageScrollHeight: list.scrollHeight,
                    messageClientHeight: list.clientHeight,
                    formBottom: form.bottom,
                    chatBottom: chat.bottom,
                    scrollWidth: document.documentElement.scrollWidth,
                    innerWidth: window.innerWidth
                  };
                }"""
            )
            self.assertLessEqual(metrics["chatHeight"], metrics["innerHeight"] + 2)
            self.assertGreater(metrics["messageScrollHeight"], metrics["messageClientHeight"] + 80)
            self.assertGreater(metrics["messageHeight"], 180)
            self.assertLessEqual(metrics["formBottom"], metrics["chatBottom"] + 2)
            self.assertLessEqual(metrics["scrollWidth"], metrics["innerWidth"] + 4)

            page.locator("#room-messages").evaluate(
                """el => {
                  el.scrollTop = 0;
                  el.dispatchEvent(new Event('scroll', { bubbles: true }));
                }"""
            )
            expect(page.locator("#room-latest")).to_be_visible(timeout=3000)

            page.locator("#room-latest").click()
            page.wait_for_function(
                """() => {
                  const el = document.querySelector('#room-messages');
                  return el && (el.scrollHeight - el.scrollTop - el.clientHeight) <= 8;
                }""",
                timeout=3000,
            )
            expect(page.locator("#room-latest")).to_be_hidden(timeout=3000)
            page.screenshot(path=str(ARTIFACTS / "dashboard_mobile_room_latest_button.png"), full_page=True)

            context.close()
            browser.close()
            self.assertEqual(page_errors, [])

    def test_desktop_room_observer_collapse_keeps_chat_readable(self):
        space = self._seed_space_with_growth_candidate()
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as exc:
                raise unittest.SkipTest(f"chromium launch failed: {exc}") from exc
            context = browser.new_context(viewport={"width": 1366, "height": 768})
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
            expect(page.locator("#status.ok")).to_be_visible(timeout=10000)
            space_card = page.locator("#spaces-list li", has_text=space.rsplit("_", 1)[0]).first
            expect(space_card).to_be_visible(timeout=10000)
            space_card.locator(".open-room-btn").click()

            expect(page.locator("#room-title")).to_have_text(space, timeout=10000)
            for selector in ("#people-panel", "#spaces-panel", "#chat-panel", "#viewer"):
                expect(page.locator(selector)).to_be_visible(timeout=10000)
            layout_metrics = page.evaluate(
                """() => {
                  const selectors = ['#people-panel', '#spaces-panel', '#chat-panel', '#viewer'];
                  const panels = selectors.map((selector) => {
                    const r = document.querySelector(selector).getBoundingClientRect();
                    return { selector, left: r.left, right: r.right, width: r.width, height: r.height };
                  });
                  return {
                    innerWidth: window.innerWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    panels
                  };
                }"""
            )
            self.assertLessEqual(layout_metrics["scrollWidth"], 1368)
            self.assertGreaterEqual(layout_metrics["panels"][0]["width"], 145)
            self.assertGreaterEqual(layout_metrics["panels"][1]["width"], 185)
            self.assertGreaterEqual(layout_metrics["panels"][2]["width"], 315)
            self.assertGreaterEqual(layout_metrics["panels"][3]["width"], 255)
            ordered_lefts = [row["left"] for row in layout_metrics["panels"]]
            self.assertEqual(ordered_lefts, sorted(ordered_lefts))
            for splitter in ("agents", "spaces", "chat"):
                expect(page.locator(f'.workspace-splitter[data-splitter-left="{splitter}"]')).to_be_visible()

            before_resize = page.evaluate(
                """() => ({
                  chat: document.querySelector('#chat-panel').getBoundingClientRect().width,
                  viewer: document.querySelector('#viewer').getBoundingClientRect().width
                })"""
            )
            splitter_box = page.locator('.workspace-splitter[data-splitter-left="chat"]').bounding_box()
            self.assertIsNotNone(splitter_box)
            page.mouse.move(splitter_box["x"] + splitter_box["width"] / 2, splitter_box["y"] + splitter_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(splitter_box["x"] + splitter_box["width"] / 2 + 70, splitter_box["y"] + splitter_box["height"] / 2)
            page.mouse.up()
            after_resize = page.evaluate(
                """() => ({
                  chat: document.querySelector('#chat-panel').getBoundingClientRect().width,
                  viewer: document.querySelector('#viewer').getBoundingClientRect().width,
                  stored: localStorage.getItem('cnv.dashboardLayoutFractions.v1'),
                  scrollWidth: document.documentElement.scrollWidth
                })"""
            )
            self.assertGreater(after_resize["chat"], before_resize["chat"] + 45)
            self.assertLess(after_resize["viewer"], before_resize["viewer"] - 45)
            self.assertIn("chat", after_resize["stored"])
            self.assertLessEqual(after_resize["scrollWidth"], 1368)

            page.locator("#toggle-agents").click()
            expect(page.locator("#people-panel")).to_be_hidden()
            expect(page.locator("#toggle-agents")).to_have_text("에이전트 펼치기")
            page.locator("#toggle-agents").click()
            expect(page.locator("#people-panel")).to_be_visible()
            page.locator("#toggle-viewer").click()
            expect(page.locator("#viewer")).to_be_hidden()
            expect(page.locator("#toggle-viewer")).to_have_text("뷰어 펼치기")
            page.locator("#toggle-viewer").click()
            expect(page.locator("#viewer")).to_be_visible()

            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "yes", timeout=10000)
            expect(page.locator("#room-observer-all-toggle")).to_have_text("상태 펼치기")
            expect(page.locator("#room-snapshot")).to_be_hidden()
            expect(page.locator("#room-observer-stack")).to_be_hidden()
            collapsed_first = page.evaluate(
                """() => {
                  const messages = document.querySelector('#room-messages').getBoundingClientRect();
                  const form = document.querySelector('#room-form').getBoundingClientRect();
                  return {
                    messageHeight: messages.height,
                    formBottom: form.bottom,
                    innerHeight: window.innerHeight,
                    scrollWidth: document.documentElement.scrollWidth
                  };
                }"""
            )
            self.assertGreaterEqual(collapsed_first["messageHeight"], 320)
            self.assertLessEqual(collapsed_first["formBottom"], collapsed_first["innerHeight"] + 2)
            self.assertLessEqual(collapsed_first["scrollWidth"], 1368)

            page.locator("#room-observer-all-toggle").click()
            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "no")
            expect(page.locator("#room-observer-all-toggle")).to_have_text("상태 접기")
            expect(page.locator("#room-promotion-review")).to_have_attribute("data-empty", "no", timeout=10000)
            expanded = page.evaluate(
                """() => {
                  const messages = document.querySelector('#room-messages').getBoundingClientRect();
                  const observer = document.querySelector('#room-observer-stack').getBoundingClientRect();
                  return {
                    messageHeight: messages.height,
                    observerHeight: observer.height,
                    scrollWidth: document.documentElement.scrollWidth
                  };
                }"""
            )
            self.assertGreater(expanded["observerHeight"], 0)
            self.assertLess(expanded["messageHeight"], collapsed_first["messageHeight"])
            self.assertLessEqual(expanded["scrollWidth"], 1368)

            page.locator("#room-observer-all-toggle").click()
            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "yes")
            expect(page.locator("#room-snapshot")).to_be_hidden()
            expect(page.locator("#room-observer-stack")).to_be_hidden()
            collapsed_again = page.evaluate(
                """() => {
                  const messages = document.querySelector('#room-messages').getBoundingClientRect();
                  const form = document.querySelector('#room-form').getBoundingClientRect();
                  return {
                    messageHeight: messages.height,
                    formBottom: form.bottom,
                    innerHeight: window.innerHeight,
                    scrollWidth: document.documentElement.scrollWidth
                  };
                }"""
            )
            self.assertGreater(collapsed_again["messageHeight"], expanded["messageHeight"])
            self.assertGreaterEqual(collapsed_again["messageHeight"], 320)
            self.assertLessEqual(collapsed_again["formBottom"], collapsed_again["innerHeight"] + 2)
            self.assertLessEqual(collapsed_again["scrollWidth"], 1368)

            page.wait_for_timeout(1800)
            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "yes")
            expect(page.locator("#room-snapshot")).to_be_hidden()
            expect(page.locator("#room-observer-stack")).to_be_hidden()

            page.locator("#room-observer-all-toggle").click()
            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "no")
            expect(page.locator("#room-promotion-review")).to_be_visible()
            page.locator('[data-observer-section="promotion"]').click()
            expect(page.locator('[data-observer-section="promotion"]')).to_have_attribute("aria-pressed", "false")
            expect(page.locator("#room-promotion-review")).to_be_hidden()
            page.wait_for_timeout(1800)
            expect(page.locator('[data-observer-section="promotion"]')).to_have_attribute("aria-pressed", "false")
            expect(page.locator("#room-promotion-review")).to_be_hidden()

            page.locator('[data-observer-section="promotion"]').click()
            expect(page.locator("#room-promotion-review")).to_be_visible()
            page.locator("#room-observer-all-toggle").click()
            expect(page.locator("#roomView")).to_have_attribute("data-observer-collapsed", "yes")
            page.screenshot(path=str(ARTIFACTS / "dashboard_desktop_observer_collapse_latest.png"), full_page=True)

            context.close()
            browser.close()
            self.assertEqual(page_errors, [])


if __name__ == "__main__":
    unittest.main()
