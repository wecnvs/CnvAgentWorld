#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess
import sys
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ManagerWorkHistoryTests(unittest.TestCase):
    def test_manager_instruction_requires_canonical_history(self):
        law = (ROOT / "law_manager.md").read_text(encoding="utf-8")
        self.assertIn("관리기록/", law)
        self.assertIn("대시보드·워크스페이스 작업 기록", law)
        self.assertIn("사용자 요청", law)
        self.assertIn("검증 결과", law)
        self.assertIn("관리자작업기록검사", law)
        self.assertIn("--strict-mtime", law)
        self.assertIn("누락", law)
        self.assertIn(".claude/settings.json", law)
        self.assertIn(".agents/hooks.json", law)

    def test_canonical_history_files_exist(self):
        required = [
            ROOT / "관리기록" / "README.md",
            ROOT / "관리기록" / "설계" / "공간협업_오케스트레이션_설계.md",
            ROOT / "관리기록" / "작업이력" / "대시보드_오케스트레이션_v0_작업로그.md",
            ROOT / "관리기록" / "디버깅" / "README.md",
            ROOT / "도구" / "기본" / "관리자작업기록검사" / "도구.md",
            ROOT / "도구" / "기본" / "관리자작업기록검사" / "검사.py",
            ROOT / "도구" / "기본" / "관리자작업기록검사" / "훅.py",
            ROOT / ".claude" / "settings.json",
            ROOT / ".claude" / "hooks" / "manager-history-stop.sh",
            ROOT / ".agents" / "hooks.json",
            ROOT / ".agents" / "hooks" / "manager-history-check.sh",
        ]
        for path in required:
            self.assertTrue(path.exists(), str(path))

    def test_lifecycle_hooks_are_configured(self):
        claude = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        stop_hooks = claude["hooks"]["Stop"]
        stop_commands = [
            hook["command"]
            for group in stop_hooks
            for hook in group.get("hooks", [])
            if hook.get("type") == "command"
        ]
        self.assertIn("sh .claude/hooks/manager-history-stop.sh", stop_commands)

        agy = json.loads((ROOT / ".agents" / "hooks.json").read_text(encoding="utf-8"))
        spec = agy["manager-work-history"]
        self.assertTrue(spec["enabled"])
        self.assertEqual(
            spec["PostInvocation"][0]["command"],
            "sh .agents/hooks/manager-history-check.sh agy-post-invocation",
        )
        self.assertEqual(
            spec["Stop"][0]["command"],
            "sh .agents/hooks/manager-history-check.sh agy-stop",
        )

    def test_manager_history_guard_passes(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "도구" / "기본" / "관리자작업기록검사" / "검사.py"),
                "--strict-mtime",
            ],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lifecycle_hook_wrappers_pass(self):
        commands = [
            ["sh", str(ROOT / ".claude" / "hooks" / "manager-history-stop.sh")],
            ["sh", str(ROOT / ".agents" / "hooks" / "manager-history-check.sh"), "agy-post-invocation"],
            [
                sys.executable,
                str(ROOT / "도구" / "기본" / "관리자작업기록검사" / "훅.py"),
                "--mode",
                "generic",
            ],
        ]
        for command in commands:
            result = subprocess.run(
                command,
                cwd=str(ROOT),
                input="{}",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
