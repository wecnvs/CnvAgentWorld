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
            self.assertGreater(after_send_y, 0)
            self.assertGreaterEqual(after_send_y + 80, before_send_y)
            page.screenshot(path=str(ARTIFACTS / "dashboard_mobile_after_send_latest.png"), full_page=True)

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
