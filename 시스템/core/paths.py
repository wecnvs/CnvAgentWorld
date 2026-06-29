# -*- coding: utf-8 -*-
"""경로 한 곳에서만 정의한다. 절대경로 하드코딩 금지."""
from pathlib import Path

SYS = Path(__file__).resolve().parent.parent     # 시스템/ (core·엔진·대시보드 묶음)
ROOT = SYS.parent                                # CnvAgentWorld/ (루트폴더)
PEOPLE = ROOT / "에이전트"
SPACES = ROOT / "공간"
TPL = SYS / "엔진" / "templates"

# 모든 엔진이 cwd에서 자동으로 읽는 진입점 파일들
ENGINE_ENTRY = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", "GEMMA.md"]
