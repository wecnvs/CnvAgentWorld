#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code/Antigravity lifecycle hook for manager work-history guard."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CHECKER = Path(__file__).with_name("검사.py")
MAX_REASON_CHARS = 5000


def _checker_command() -> list[str]:
    return [sys.executable, str(CHECKER), "--strict-mtime"]


def run_checker() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _checker_command(),
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def build_reason(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > MAX_REASON_CHARS:
        detail = detail[:MAX_REASON_CHARS].rstrip() + "\n...생략됨"
    command = "python3 도구/기본/관리자작업기록검사/검사.py --strict-mtime"
    return (
        "관리자 작업기록 검사가 실패했습니다.\n"
        "대시보드/워크스페이스/지침/시스템/도구 변경을 완료로 보고하기 전에 "
        "`관리기록/작업이력/대시보드_오케스트레이션_v0_작업로그.md`를 최신화하고 "
        f"`{command}`를 다시 통과시켜야 합니다.\n\n"
        f"{detail}"
    ).strip()


def emit_block(mode: str, reason: str) -> int:
    if mode.startswith("agy-"):
        print(reason)
        return 2
    if mode == "generic":
        print(reason, file=sys.stderr)
        return 1
    print(reason, file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="관리자 작업기록 훅")
    parser.add_argument(
        "--mode",
        choices=("claude-stop", "agy-post-invocation", "agy-stop", "generic"),
        default="generic",
        help="호출 런타임에 맞춘 실패 신호 방식",
    )
    args = parser.parse_args()

    # Hook payload is delivered via stdin. The current guard only needs the
    # workspace state, but stdin must be drained so callers do not block.
    _ = sys.stdin.read()

    result = run_checker()
    if result.returncode == 0:
        return 0
    return emit_block(args.mode, build_reason(result))


if __name__ == "__main__":
    raise SystemExit(main())
