# -*- coding: utf-8 -*-
"""단톡방 감시모드: 관리자에이전트를 '그 방' 컨텍스트로 인터랙티브 실행하는 세션 스펙을 만든다.

대표가 방에서 [감시]를 누르면, 8686 대시보드 서버가 이 모듈로 세션 스펙(shell/cwd/title)을
만들어 8687 터미널 서버에 인터랙티브 PTY 세션을 띄운다. 그 세션은 **루트폴더(cwd)**에서
관리자에이전트(CLAUDE.md→law.md→law_manager.md)를 깨우고, "이 방을 감시하라"는 프롬프트를
초기 메시지로 주입한다.

승인(대표 결정): `--dangerously-skip-permissions` 없이 인터랙티브로 띄워, 도구 실행마다
대표가 직접 승인한다(네이티브 권한 프롬프트 = 진짜 게이트).

핵심: 8687 세션 API의 `shell` 필드는 `shells.build_posix_exec_argv`가 `shlex.split`하므로,
`/bin/zsh -lc '...'` 같은 다토큰 문자열을 주면 그 명령을 그대로 실행한다(터미널 서버 무수정).
"""
from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path

from . import runtime
from .codes import gen_code
from .spaces import MANAGER_DIRNAME

ROOT = Path(__file__).resolve().parents[2]          # 시스템/core/watch.py → 루트폴더
SPACES = ROOT / "공간"
WATCH_RUN_DIR = ROOT / "시스템" / "대시보드" / ".run" / "watch"

# 엔진 CLI(claude/agy/codex)가 흔히 설치되는 경로(절대경로 하드코딩 아님 — PATH 후보).
# 터미널 서버가 launchd 등 최소 PATH로 떠도 인터랙티브 CLI를 찾도록 보강한다(engine.py와 동일 취지).
_PATH_PREFIX = ":".join([
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".local" / "bin"),
])


def build_watch_prompt(space: str) -> str:
    """관리자에이전트에게 주입할 '감시' 초기 메시지(루트폴더에서 깨어난 뒤 첫 발화)."""
    return (
        f"너는 관리자에이전트다. 지금 **단톡방 감시모드**로 깨어났다.\n"
        f"방금 읽은 `law_manager.md`의 「단톡방 감시·추적 — 목표 달성과 집단지성 성장을 본다」 절의 관점으로 "
        f"아래 한 방을 감시·분석하라.\n\n"
        f"## 감시 대상 단톡방\n"
        f"- 공간 토큰: `{space}`\n"
        f"- 경로: `공간/{space}/`\n"
        f"- 먼저 볼 것: `공간/{space}/대화.jsonl`(최근), `요약.md`, `멤버.json`, "
        f"`{MANAGER_DIRNAME}/상태.json`·`상태이력.jsonl`, 그리고 진행 중인 작업·후보·성장(promotion) 관련 파일.\n\n"
        f"## 두 기준으로 본다\n"
        f"1. **목표 달성** — 대화가 작업에 막히지 않는지, 작업 결과가 대화로 돌아오는지, 대표의 말이 "
        f"최우선으로 반영되는지, 다자대화·동시작업이 잘 도는지. 에이전트들이 헛돌거나 폭주하거나 침묵에 빠지지 않는지.\n"
        f"2. **자기성장 집단지성** — 피드백마다 스킬·지식·케이스가 실제로 성장하는지, 다양성을 잃지 않는지, "
        f"회귀 없이 안전하게 누적되는지, 자기성장 루프(사회자→케이스→관리자 승격)가 끊기지 않는지.\n"
        f"3. **반드시 매번 점검할 요소** — ① **대화맥락 전달**: 공간관리에이전트(사회자)가 전체 대화 요약을 제대로 진행해 "
        f"각 에이전트가 **대화맥락·최신 대화내용·지금 해야 할 방향**을 정확히 이해한 채 일하는지(맥락을 모르면 결과물이 "
        f"어긋난다 — 맥락 이해는 모든 작업의 전제다). ② **스킬 업데이트**: 피드백·교훈이 실제로 스킬에 반영(업데이트/생성)되어 "
        f"다음에·다른 방에서 발견·적용되는지. 이 점검의 목적은 '**반드시 제대로 된 결과물**이 나오고 시스템이 **성장·발전하며 잘 운용**되는지' 확인하는 것이다.\n\n"
        f"## 어떻게 진행하나\n"
        f"- 관찰 결과를 여기(이 터미널)에서 대표에게 보고하고, 대표와 대화를 이어가라.\n"
        f"- 문제를 발견하면 원인을 짚고, 워크스페이스·대시보드·시스템·지침의 **근본 개선안**을 제안하라.\n"
        f"- 지금은 **욜로(YOLO) 모드**라 도구가 자동 실행된다(권한 프롬프트 없음). 그만큼 신중히 — 시스템/지침/대시보드를 "
        f"수정하면 **무엇을 왜 바꿨는지 이 터미널에서 대표에게 명확히 보고**하라.\n"
        f"- **[절대 원칙] 개선은 바로 하되, 이 방 `{space}`의 진행을 끊지 말고 반드시 문제없이 재개시켜라.** 네 수정·서버 재시작이 "
        f"이 방에서 진행 중이던 대화·작업을 끊거나 고아로 만들면 안 된다. 되도록 멈추지 않는 방식으로 고치고, 재시작·시스템 변경이 "
        f"불가피하면 그 뒤 **`python3 시스템/엔진/world.py recover --space {space}`**(전체는 `recover-all`)로 재개시킨 다음, "
        f"**그 방이 실제로 대화·작업을 손실 없이 잇는지 확인**하라. 살아있는 작업은 건드리지 말고 멈춘 진행만 잇는다. 방을 스트랜드로 남기고 끝내지 마라.\n"
        f"- 실제로 수정했다면 `law_manager.md`의 작업기록 의무대로 `관리기록/`에 남기고 검사를 통과시켜라.\n\n"
        f"## 소견을 상태칩으로 남겨라 (가시화)\n"
        f"진단을 마치면(그리고 상황이 바뀔 때마다) 아래 명령으로 소견을 기록하라. 대시보드가 이걸 읽어 이 방에 "
        f"**상태칩**(목표/집단지성 신호·요약·이슈)으로 표시한다. **반드시 실제 평가만 적어라(거짓 표시 금지).**\n"
        f"```\n"
        f"python3 시스템/엔진/world.py watch-report --space {space} \\\n"
        f"  --goal ok|warn|bad --goal-note \"목표 달성 한 줄\" \\\n"
        f"  --growth ok|warn|bad --growth-note \"집단지성 성장 한 줄\" \\\n"
        f"  --summary \"두 줄 요약\" \\\n"
        f"  --finding \"warn|발견한 문제\" --finding \"bad|심각한 문제\"\n"
        f"```\n"
        f"(ok=좋음, warn=주의/정체, bad=문제/단절. --finding 은 여러 번, 없으면 생략.)\n\n"
        f"우선 위 파일들을 읽고 이 방의 현재 상태를 두 기준으로 진단해 보고한 뒤, 위 명령으로 소견을 남겨라."
    )


def _interactive_exec(engine: str, model: str, prompt_path: Path) -> str:
    """엔진별 '인터랙티브 + YOLO(자동승인)' 실행 커맨드 문자열(`exec ` 뒤에 올 부분).

    대표 결정: 욜로 모드 — 권한 프롬프트 없이 도구가 자동 실행된다(승인 게이트 없음).
    프롬프트는 파일로 두고 `"$(cat <path>)"`로 한 인자로 넘겨 한글·줄바꿈·따옴표를 안전하게 전달한다.
    """
    cli_model = runtime.model_for_cli(engine, model)
    prompt_sub = f'"$(cat {shlex.quote(str(prompt_path))})"'
    model_opt = f"--model {shlex.quote(cli_model)} " if cli_model else ""
    root_q = shlex.quote(str(ROOT))
    if engine == "claude":
        # 욜로: --dangerously-skip-permissions → 도구 자동 실행(승인 프롬프트 없음).
        return f"claude --dangerously-skip-permissions {model_opt}{prompt_sub}"
    if engine == "gemini":
        # agy(Antigravity) 인터랙티브 욜로 — best-effort. --add-dir로 루트를 워크스페이스에 포함.
        return f"agy --add-dir {root_q} --dangerously-skip-permissions {model_opt}{prompt_sub}"
    if engine == "codex":
        # codex 인터랙티브 욜로 — best-effort.
        return f"codex --dangerously-bypass-approvals-and-sandbox {model_opt}{prompt_sub}"
    if engine == "gemma":
        raise ValueError("gemma 엔진은 인터랙티브 감시 실행을 지원하지 않는다.")
    raise ValueError(f"미지원 엔진: {engine}")


def build_watch_session(space: str, engine: str | None = None, model: str | None = None) -> dict:
    """8687 `POST /api/sessions`에 보낼 세션 스펙을 만든다.

    엔진/모델: 명시값 > 그 방 관리자(사회자) 런타임 > 기본(claude/opus).
    """
    sdir = SPACES / space
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    resolved = runtime.resolve_runtime(sdir / MANAGER_DIRNAME, engine, model)
    eng, mdl = resolved["engine"], resolved["model"]

    WATCH_RUN_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = WATCH_RUN_DIR / f"{space}.{gen_code()}.md"
    prompt_path.write_text(build_watch_prompt(space), encoding="utf-8")

    inner = (
        f'export PATH={shlex.quote(_PATH_PREFIX)}:"$PATH"; '
        f"exec {_interactive_exec(eng, mdl, prompt_path)}"
    )
    shell = shlex.join(["/bin/zsh", "-lc", inner])
    return {
        "shell": shell,
        "cwd": str(ROOT),
        "title": f"감시:{space}",
        "cols": 120,
        "rows": 30,
        "engine": eng,
        "model": mdl,
        "prompt_path": str(prompt_path),
    }


# ── 감시 소견(상태칩 가시화) ── 감시 에이전트가 두 기준 평가를 구조화해 남기고, 대시보드가 칩으로 표시 ──
REPORT_FILENAME = "감시소견.json"
_STATUS_ALIASES = {
    "ok": "ok", "good": "ok", "좋음": "ok", "정상": "ok", "양호": "ok",
    "warn": "warn", "warning": "warn", "주의": "warn", "정체": "warn",
    "bad": "bad", "critical": "bad", "문제": "bad", "위험": "bad", "단절": "bad",
}


def _norm_status(value: str | None) -> str:
    return _STATUS_ALIASES.get((value or "").strip().lower(), "unknown")


def report_path(space: str) -> Path:
    return SPACES / space / MANAGER_DIRNAME / REPORT_FILENAME


def read_report(space: str) -> dict | None:
    """방의 최신 감시 소견을 읽는다(없거나 깨졌으면 None)."""
    path = report_path(space)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_report(
    space: str,
    *,
    goal: str,
    growth: str,
    goal_note: str = "",
    growth_note: str = "",
    summary: str = "",
    findings: list | None = None,
    by: str = "관리자에이전트(감시)",
) -> dict:
    """감시 소견을 기록한다. 상태칩이 이 결과를 반영한다(거짓 표시 금지 — 실제 평가만 남길 것).

    findings: ["sev|텍스트", ...] 또는 [{"severity","text"}, ...] 모두 허용.
    """
    sdir = SPACES / space
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    norm_findings = []
    for f in (findings or []):
        if isinstance(f, dict):
            sev, text = f.get("severity"), str(f.get("text", "")).strip()
        else:
            raw = str(f)
            sev, _, text = raw.partition("|")
            if not text:
                sev, text = "info", raw.strip()
        text = text.strip()
        if text:
            norm_findings.append({"severity": _norm_status(sev) if _norm_status(sev) != "unknown" else "info", "text": text})
    data = {
        "space": space,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "by": by,
        "goal": {"status": _norm_status(goal), "note": goal_note.strip()},
        "growth": {"status": _norm_status(growth), "note": growth_note.strip()},
        "summary": summary.strip(),
        "findings": norm_findings,
    }
    path = report_path(space)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)   # 원자적 교체
    return data
