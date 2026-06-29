#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""관리자에이전트 작업기록 누락 검사."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HISTORY_ROOT = ROOT / "관리기록"
REQUIRED_FILES = [
    HISTORY_ROOT / "README.md",
    HISTORY_ROOT / "설계" / "공간협업_오케스트레이션_설계.md",
    HISTORY_ROOT / "작업이력" / "대시보드_오케스트레이션_v0_작업로그.md",
    HISTORY_ROOT / "디버깅" / "README.md",
]
CANONICAL_LOGS = [
    HISTORY_ROOT / "작업이력" / "대시보드_오케스트레이션_v0_작업로그.md",
]
WATCH_ROOTS = [
    ROOT / ".agents",
    ROOT / ".claude",
    ROOT / "law.md",
    ROOT / "law_manager.md",
    ROOT / "law_space.md",
    ROOT / "law_chat.md",
    ROOT / "law_work.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "GEMINI.md",
    ROOT / "GEMMA.md",
    ROOT / "시스템",
    ROOT / "도구",
]
WATCH_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    "관리기록",
    "임시작업",
    "임시",
    "node_modules",
    ".git",
}


def _is_ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in IGNORED_PARTS for part in rel.parts):
        return True
    if "터미널" in rel.parts:
        return True
    return False


def iter_watch_files():
    for root in WATCH_ROOTS:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix in WATCH_SUFFIXES and not _is_ignored(root):
                yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in WATCH_SUFFIXES and not _is_ignored(path):
                yield path


def validate_structure(errors: list[str]) -> None:
    for path in REQUIRED_FILES:
        if not path.exists():
            errors.append(f"필수 관리기록 파일 없음: {path.relative_to(ROOT)}")
    law = (ROOT / "law_manager.md").read_text(encoding="utf-8")
    for needle in (
        "관리기록/",
        "작업이력",
        "관리자작업기록검사",
        "누락",
        "--strict-mtime",
    ):
        if needle not in law:
            errors.append(f"law_manager.md에 기록 의무 문구 누락: {needle}")


def validate_strict_mtime(errors: list[str]) -> None:
    existing_logs = [path for path in CANONICAL_LOGS if path.exists()]
    if not existing_logs:
        errors.append("mtime 검사 불가: 정본 작업로그 없음")
        return
    latest_log_mtime = max(path.stat().st_mtime for path in existing_logs)
    stale_sources = [
        path.relative_to(ROOT).as_posix()
        for path in iter_watch_files()
        if path.stat().st_mtime > latest_log_mtime + 1.0
    ]
    if stale_sources:
        preview = "\n  - ".join(stale_sources[:20])
        extra = "" if len(stale_sources) <= 20 else f"\n  ... {len(stale_sources) - 20}개 더 있음"
        errors.append(
            "관리기록 작업로그보다 나중에 수정된 감시 대상 파일이 있습니다. "
            "작업 이력을 먼저 기록하세요:\n  - "
            + preview
            + extra
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="관리자 작업기록 누락 검사")
    parser.add_argument("--strict-mtime", action="store_true", help="감시 대상 파일 수정시각이 작업로그보다 최신이면 실패")
    args = parser.parse_args()

    errors: list[str] = []
    validate_structure(errors)
    if args.strict_mtime:
        validate_strict_mtime(errors)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("OK: 관리자 작업기록 계약 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
