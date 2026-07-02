# -*- coding: utf-8 -*-
"""엔진 실행: 폴더(cwd)를 워크스페이스로 채팅/작업 에이전트를 깨운다."""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path
from .paths import PEOPLE, ENGINE_ENTRY
from .codes import gen_code
from . import discovery, injection_log, runtime, task_registry, templates, work_settings
from .transcript import now_iso


# 엔진 CLI(claude/agy/codex 등)가 설치되는 흔한 경로. 서버가 launchd 등 최소 PATH로
# 구동돼도 subprocess가 CLI를 찾도록 PATH를 보강한다. (절대경로 하드코딩 아님 — PATH 후보일 뿐)
_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    str(Path.home() / ".local" / "bin"),
)


def _engine_env() -> dict:
    """엔진 subprocess용 환경: 기존 환경 + 흔한 bin 경로를 PATH에 보강."""
    env = os.environ.copy()
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for d in _EXTRA_BIN_DIRS:
        if d and d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


def discovery_context(query: str, top: int = 5) -> str:
    hits = discovery.find(query, "all", top)
    return discovery.render_context(query, hits)


def prompt_with_discovery(query: str, prompt: str, *, log_space: str = "", log_ref: str = "",
                          log_kind: str = "chat", log_context: dict | None = None) -> str:
    """발견 컨텍스트를 프롬프트 앞에 붙인다. log_space+log_ref가 주어지면 주입된 케이스를 기록한다(P1').

    (기존 2-인자 호출은 그대로 동작 — 로깅은 옵트인이라 무회귀.)
    """
    hits = discovery.find(query, "all", 5)
    context = discovery.render_context(query, hits)
    if log_space and log_ref:
        injection_log.record_injection(log_space, kind=log_kind, ref=log_ref,
                                        injected=discovery.injected_case_refs(hits),
                                        context=log_context)
    return f"{context}\n\n# 실제 요청\n\n{prompt}"


def _engine_command(cwd, prompt: str, engine_name: str, model: str, *, continue_session: bool = False) -> list[str]:
    cli_model = runtime.model_for_cli(engine_name, model)
    if engine_name == "claude":
        cmd = ["claude", "--dangerously-skip-permissions"]
        # 세션 연속성: 이어가기/재개 호출은 같은 cwd의 직전 세션을 --continue로 잇는다.
        # 종전엔 매 호출이 세션 없는 일회성이라, 타임아웃 이어가기 때 작업 기억이 통째로 리셋돼
        # 체크포인트 파일만 보고 처음부터 다시 파악해야 했다(직접 시킬 때 대비 다운그레이드의 2순위 원흉).
        # claude는 대화 세션을 cwd 단위로 저장하므로, 격리된 작업 폴더에서 --continue는 곧
        # '그 작업 자신의 직전 세션'이다(프로세스가 죽었어도 이어진다).
        if continue_session:
            cmd.append("--continue")
        cmd += ["-p", prompt]
        if cli_model:
            cmd += ["--model", cli_model]
        return cmd
    if engine_name == "gemini":
        agy_cmd = shutil.which("agy")
        if not agy_cmd:
            default_agy = Path.home() / ".local/bin/agy"
            agy_cmd = str(default_agy if default_agy.exists() else "agy")
        # --add-dir로 '작업폴더'를 agy 워크스페이스에 넣는다. 없으면 agy(Antigravity)가 산출물을
        # 자기 scratch(~/.gemini/...)에 써서 워크스페이스 밖→대시보드가 serve 못함→미리보기 불가.
        cmd = [agy_cmd, f"--print={prompt}", "--dangerously-skip-permissions"]
        if cwd:
            cmd += ["--add-dir", str(cwd)]
        if cli_model:
            cmd += ["--model", cli_model]
        return cmd
    if engine_name == "codex":
        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(cwd),
        ]
        if cli_model:
            cmd += ["--model", cli_model]
        cmd.append(prompt)
        return cmd
    if engine_name == "gemma":
        raise RuntimeError("gemma 런타임 실행은 아직 미구현이다. 카탈로그 선택은 가능하지만 실행은 지원하지 않는다.")
    raise ValueError(f"미지원 엔진: {engine_name}")


_AGY_TIMEOUT_PHRASE = "timed out waiting for response"
_AGY_TIMEOUT_RE = re.compile(
    r"[\x00-\x09\x0b\x0c\x0e-\x1f]*"
    r"(?:error[ \t]*:?[ \t]*)?"
    r"timed out waiting for response"
    r"[\x00-\x09\x0b\x0c\x0e-\x1f \t\r]*",
    re.IGNORECASE,
)
_CTRL_STRIP_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _scrub_agy_status_line(text: str) -> str:
    if not text or _AGY_TIMEOUT_PHRASE not in text.lower():
        return text
    cleaned = _AGY_TIMEOUT_RE.sub("", text)
    cleaned = re.sub(r"(?im)^[ \t]*error[ \t]*:?[ \t]*$", "", cleaned)
    return _CTRL_STRIP_RE.sub("", cleaned).strip()


def _clean_engine_output(engine_name: str, text: str) -> str:
    if engine_name == "gemini":
        return _scrub_agy_status_line(text)
    return text


# ── 엔진 호출 일시 오류 재시도 (터미널 CLI 에이전트의 API 재시도 방식 차용) ──
# 레이트리밋(429)·서버 오류(5xx)·네트워크 단절·빈 응답처럼 '다시 부르면 대개 성공하는' 실패를
# 호출 지점마다 흩어 처리하지 않고 이 레이어에서 분류해 짧은 백오프 후 재호출한다.
# 타임아웃은 여기서 재시도하지 않는다 — 시간 예산을 이미 소진했으므로, 상위의 진행 기반
# 이어가기(WORK_TIMEOUT_CONTINUE_LIMIT)·매니저 재시도 루프가 담당한다.
ENGINE_TRANSIENT_RETRY_LIMIT = 2
ENGINE_TRANSIENT_RETRY_DELAYS = (4.0, 12.0)
# 재시도해도 똑같은 결정적 실패(인증·권한·설정) — 이 마커가 보이면 즉시 실패 보고가 맞다.
_ENGINE_PERMANENT_MARKERS = (
    "api key", "apikey", "unauthorized", "not logged in", "login required",
    "invalid_api_key", "permission denied", "forbidden", "인증되지 않", "권한이 없",
    "no conversation found",  # --continue인데 이 cwd에 세션 없음 — 재시도 무의미(러너가 무-continue로 폴백)
)


def _engine_transient_failure(text: str, returncode: int | None = None) -> bool:
    """'다시 부르면 대개 성공하는' 실패인지 분류한다.
    - 빈 응답 + 비정상 종료(rc≠0): 프로세스가 죽으며 완성이 유실된 것 → 재시도 가치 있음.
      (빈 응답이라도 rc=0 정상 종료면 재시도하지 않는다 — 파일 부수효과만 내고 침묵하는
       작업 러너의 정상 패턴을 오분류하면 안 된다. rc를 모르면 보수적으로 재시도하지 않는다.)
    - (stderr)만 남은 출력 + 비정상 종료(rc≠0): API/네트워크 오류가 대부분. 단 인증·권한류
      결정적 실패 마커가 보이면 재시도하지 않는다. rc=0인 stderr 출력은 기존 계약(stdout 우선·
      stderr 폴백)대로 그대로 반환한다(재시도 없음 — 회귀 금지).
    - 타임아웃·취소·정상 출력: 여기 해당 없음.
    """
    if returncode is None or returncode == 0:
        return False
    value = (text or "").strip()
    if not value:
        return True
    if value.startswith("(엔진 타임아웃)") or value.startswith("(엔진 취소됨"):
        return False
    if not value.startswith("(stderr)"):
        return False
    low = value.lower()
    return not any(marker in low for marker in _ENGINE_PERMANENT_MARKERS)


def _transient_retry_delay(attempt: int) -> float:
    idx = min(max(attempt - 1, 0), len(ENGINE_TRANSIENT_RETRY_DELAYS) - 1)
    return ENGINE_TRANSIENT_RETRY_DELAYS[idx]


def _run_engine_once(cwd, prompt: str, engine: str = None, model: str = None, timeout: int = 300) -> tuple[str, int | None]:
    rt = runtime.resolve_runtime(cwd, engine, model)
    cmd = _engine_command(cwd, prompt, rt["engine"], rt["model"])
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=_engine_env())
    except FileNotFoundError:
        raise RuntimeError(f"EngineError: '{cmd[0]}' CLI를 찾을 수 없음 (engine={rt['engine']}, PATH 확인)")
    except subprocess.TimeoutExpired:
        return "(엔진 타임아웃)", None
    out = (r.stdout or "").strip()
    if not out and r.stderr:
        out = "(stderr) " + r.stderr.strip()
    return _clean_engine_output(rt["engine"], out), r.returncode


def run_engine(cwd, prompt: str, engine: str = None, model: str = None, timeout: int = 300) -> str:
    out = ""
    for attempt in range(ENGINE_TRANSIENT_RETRY_LIMIT + 1):
        out, returncode = _run_engine_once(cwd, prompt, engine, model, timeout)
        if not _engine_transient_failure(out, returncode):
            return out
        if attempt < ENGINE_TRANSIENT_RETRY_LIMIT:
            time.sleep(_transient_retry_delay(attempt + 1))
    return out


_ORIGINAL_RUN_ENGINE = run_engine
WORK_STEERING_RESTART_LIMIT = 3
# 타임아웃 + 부분 진행이 있을 때 체크포인트에서 이어서 재실행하는 횟수 상한(작업 분할/누적).
# 전략(터미널 CLI 에이전트 방식 차용): '진행이 있는 동안만' 이어가고, '진행이 없으면 즉시 멈춤'(가짜
# 무한루프 차단). 즉 비용을 결정하는 건 시간이 아니라 '진행 여부'다 — 계속 나아가는 무거운 작업은
# 끝까지 갈 기회를 주되(runner_timeout_sec×(1+이 값) ≈ 상한 예산), 멈춘 작업은 한 번에 에스컬레이션.
# 무거운 분석(다수 파일 정독+긴 산출)이 상한에 걸려 헛되이 error 나던 걸 줄이려 3→6으로 올린다(여전히 유한).
WORK_TIMEOUT_CONTINUE_LIMIT = 6
# 무진행 타임아웃(첫 호출에서 결과.md에 아무것도 못 남김 — 보통 작업이 커서 읽기에 시간을 다 씀) 재시도 상한.
# 즉시 에스컬레이션 대신 '체크포인트(골격)부터 써라' 1회 nudge를 줘서 큰 작업도 시작은 하게 한다.
# 그래도 무진행이면 에스컬레이션(유한). 진짜 멈춘 작업이 예산을 끝없이 먹지 않게 1회로 제한.
WORK_NO_PROGRESS_RETRY_LIMIT = 1

# 진행 감지에서 제외할 '발판/계약/런타임' 파일들 — 이건 워커의 산출물이 아니다.
_WORK_SCAFFOLD_FILES = {
    "task_pack.json", "task_handoff_pack.json", "runtime_capabilities.json",
    "execution_strategy.json", "발견후보.md", "지시.md", "discovery_manifest.json",
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", "GEMMA.md", "agent_runtime.json",
    "work_status.json", "상태.json", "작업실행설정.json",
}


def _work_output_sig(wdir) -> tuple:
    """워커가 만든 산출물(슬라이드·html·이미지·대본 등)의 지문 (파일수, 총바이트).
    결과.md 길이만으로는 못 잡는 진행(파일은 만들었는데 체크포인트 미갱신)을 감지하기 위함."""
    count = 0
    total = 0
    try:
        for f in wdir.rglob("*"):
            try:
                if not f.is_file():
                    continue
                rel = f.relative_to(wdir)
                if rel.parts and rel.parts[0] == "steering":
                    continue
                if f.name in _WORK_SCAFFOLD_FILES:
                    continue
                count += 1
                total += f.stat().st_size
            except Exception:
                continue
    except Exception:
        pass
    return (count, total)


def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
        return
    except ProcessLookupError:
        return
    except AttributeError:
        pass
    except Exception:
        pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        return


def _terminate_process(proc: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    _signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return


def _read_stream(stream, chunks: list[str]) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _start_readers(proc: subprocess.Popen) -> tuple[list[str], list[str], list[threading.Thread]]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    readers: list[threading.Thread] = []
    if proc.stdout is not None:
        reader = threading.Thread(target=_read_stream, args=(proc.stdout, stdout_chunks), daemon=True)
        reader.start()
        readers.append(reader)
    if proc.stderr is not None:
        reader = threading.Thread(target=_read_stream, args=(proc.stderr, stderr_chunks), daemon=True)
        reader.start()
        readers.append(reader)
    return stdout_chunks, stderr_chunks, readers


def _join_readers(readers: list[threading.Thread], timeout: float = 1.0) -> None:
    for reader in readers:
        reader.join(timeout=timeout)


def _process_output(stdout_chunks: list[str], stderr_chunks: list[str], engine_name: str = "") -> str:
    out = "".join(stdout_chunks).strip()
    err = "".join(stderr_chunks).strip()
    if not out and err:
        out = "(stderr) " + err
    return _clean_engine_output(engine_name, out)


def run_engine_polling(
    cwd,
    prompt: str,
    engine: str = None,
    model: str = None,
    timeout: int = 300,
    *,
    cancel_check=None,
    cancel_reason=None,
    heartbeat=None,
    heartbeat_interval: float = 10.0,
    work_policy_loader=None,
    terminate_grace_seconds: float = 5.0,
    continue_session: bool = False,
) -> str:
    rt = runtime.resolve_runtime(cwd, engine, model)
    try:
        cmd = _engine_command(cwd, prompt, rt["engine"], rt["model"], continue_session=continue_session)
    except TypeError:
        # 테스트가 _engine_command를 4-인자 fake로 몽키패치하는 기존 계약 호환(세션 연속성 미지원 fake)
        cmd = _engine_command(cwd, prompt, rt["engine"], rt["model"])

    def emit(phase: str, note: str = "") -> None:
        if not heartbeat:
            return
        try:
            heartbeat(phase, note)
        except Exception:
            pass

    started = time.monotonic()
    current_timeout = timeout
    current_heartbeat_interval = heartbeat_interval
    poll_sleep = min(0.25, max(0.05, current_heartbeat_interval / 4.0))

    def refresh_work_policy():
        nonlocal current_timeout, current_heartbeat_interval, poll_sleep
        if not work_policy_loader:
            return
        try:
            policy = work_settings.normalize_settings(work_policy_loader() or {})
        except Exception:
            return
        current_timeout = policy["runner_timeout_sec"]
        current_heartbeat_interval = policy["heartbeat_interval_sec"]
        poll_sleep = min(0.25, max(0.05, current_heartbeat_interval / 4.0))

    def _cancelled_reason() -> str | None:
        if not cancel_check:
            return None
        try:
            if cancel_check():
                reason = "cancel_requested"
                if cancel_reason:
                    try:
                        reason = str(cancel_reason() or reason)[:120]
                    except Exception:
                        reason = "cancel_requested"
                return reason
        except Exception as exc:
            emit("engine_cancel_check_error", f"{type(exc).__name__}: {str(exc)[:240]}")
        return None

    # 일시 오류(레이트리밋·5xx·빈 응답) 재시도 루프 — 전체 시간 예산(started 기준)은 시도를
    # 가로질러 공유한다(재시도가 timeout 상한을 늘리지 않음). 취소·타임아웃은 재시도하지 않는다.
    attempt = 0
    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=_engine_env(),
            )
        except FileNotFoundError:
            raise RuntimeError(f"EngineError: '{cmd[0]}' CLI를 찾을 수 없음 (engine={rt['engine']}, PATH 확인)")

        stdout_chunks, stderr_chunks, readers = _start_readers(proc)
        last_heartbeat = time.monotonic()
        emit("engine_process_started", f"pid={proc.pid}" + (f" retry={attempt}" if attempt else ""))
        output = None
        while output is None:
            if proc.poll() is not None:
                _join_readers(readers)
                output = _process_output(stdout_chunks, stderr_chunks, rt["engine"])
                break
            now = time.monotonic()
            refresh_work_policy()
            if current_heartbeat_interval and now - last_heartbeat >= current_heartbeat_interval:
                emit("engine_poll", f"elapsed={int(now - started)}s")
                last_heartbeat = now
            reason = _cancelled_reason()
            if reason is not None:
                _terminate_process(proc, grace_seconds=terminate_grace_seconds)
                _join_readers(readers)
                emit("engine_cancelled", reason)
                return f"(엔진 취소됨: {reason})"
            if current_timeout is not None and now - started >= current_timeout:
                _terminate_process(proc, grace_seconds=terminate_grace_seconds)
                _join_readers(readers)
                return "(엔진 타임아웃)"
            time.sleep(poll_sleep)

        if not _engine_transient_failure(output, proc.returncode) or attempt >= ENGINE_TRANSIENT_RETRY_LIMIT:
            return output
        attempt += 1
        delay = _transient_retry_delay(attempt)
        emit(
            "engine_transient_retry",
            f"일시 오류 감지(빈 응답/stderr) — {delay:.0f}s 백오프 후 재호출 {attempt}/{ENGINE_TRANSIENT_RETRY_LIMIT}: "
            + str(output or "")[:160],
        )
        # 백오프 대기 중에도 취소·타임아웃 예산에 반응한다. 예산이 다하면 타임아웃으로 둔갑시키지
        # 말고 마지막 실제 출력을 반환한다(실패의 원문 보존).
        wait_until = time.monotonic() + delay
        while time.monotonic() < wait_until:
            reason = _cancelled_reason()
            if reason is not None:
                emit("engine_cancelled", reason)
                return f"(엔진 취소됨: {reason})"
            if current_timeout is not None and time.monotonic() - started >= current_timeout:
                return output
            time.sleep(min(0.25, poll_sleep))


def _chat_direct_diagnostic(person: str, space: str, text: str, engine: str = None, model: str = None) -> str:
    seat = PEOPLE / person / "공간" / space
    if not seat.exists():
        raise ValueError(f"입장 안 됨: {person} -> {space} (먼저 join)")
    return run_engine(seat, prompt_with_discovery(text, text), engine, model)


def chat(
    person: str,
    space: str,
    text: str,
    requester: str = "대표",
    engine: str = None,
    model: str = None,
    *,
    record_request: bool = True,
    direct_diagnostic: bool = False,
    client_message_id: str | None = None,
) -> str:
    if direct_diagnostic:
        if record_request:
            raise ValueError("direct diagnostic chat cannot write transcript; pass record_request=False")
        return _chat_direct_diagnostic(person, space, text, engine, model)

    from . import room_manager

    result = room_manager.post(
        space,
        text,
        requester=requester,
        run_manager=True,
        client_message_id=client_message_id,
        manager_requested=True,
    )
    return json.dumps(
        {
            "ok": True,
            "managed_by": "space_manager",
            "direct_target_ignored": person,
            "ack": result.get("ack", {}),
            "events": result.get("events", []),
        },
        ensure_ascii=False,
        default=str,
    )


def _engine_timeout_text(text: str) -> bool:
    return str(text or "").strip().startswith("(엔진 타임아웃)")


def _engine_failure_text(text: str) -> bool:
    value = (text or "").strip()
    return value.startswith("(엔진 타임아웃)") or value.startswith("(stderr)")


def _engine_cancel_text(text: str) -> bool:
    return (text or "").strip().startswith("(엔진 취소됨")


def _engine_cancel_reason(text: str) -> str:
    value = (text or "").strip()
    prefix = "(엔진 취소됨:"
    if not value.startswith(prefix):
        return ""
    return value[len(prefix):].strip().rstrip(")").strip()


def _read_json(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return data if isinstance(data, dict) else fallback


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _steering_events(work_dir: Path, *, after_seq: int = 0) -> list[dict]:
    events = []
    steering_dir = work_dir / "steering"
    if not steering_dir.exists():
        return events
    for path in sorted(steering_dir.glob("*.json")):
        data = _read_json(path, {})
        if not data:
            continue
        try:
            seq = int(data.get("steering_seq") or path.name.split("_", 1)[0])
        except Exception:
            seq = 0
        if seq <= int(after_seq or 0):
            continue
        events.append({**data, "steering_seq": seq})
    return sorted(events, key=lambda item: int(item.get("steering_seq") or 0))


def _latest_steering_summary(events: list[dict]) -> str:
    lines = []
    for event in events[-8:]:
        lines.append(
            "- seq {seq} action={action} reason_code={reason_code} instruction={instruction}".format(
                seq=event.get("steering_seq", 0),
                action=event.get("action", ""),
                reason_code=event.get("reason_code", ""),
                instruction=str(event.get("instruction") or event.get("reason") or "")[:500],
            )
        )
    return "\n".join(lines)


def _merge_steering_events(*groups: list[dict]) -> list[dict]:
    by_seq = {}
    for group in groups:
        for event in group or []:
            try:
                seq = int(event.get("steering_seq") or 0)
            except Exception:
                seq = 0
            if seq:
                by_seq[seq] = event
    return [by_seq[seq] for seq in sorted(by_seq)]


def _ack_work_steering(work_dir: Path, events: list[dict], *, phase: str, note: str) -> None:
    if not events:
        return
    latest = events[-1]
    seq = int(latest.get("steering_seq") or 0)

    # flock: 매니저 측 request_steering·워커 heartbeat는 space 락 안에서 work_status.json을 갱신하는데
    # 여기 ack만 무락 read-modify-write였다 — 겹치면 last_seen_steering_seq/pending_steering_ack 갱신이
    # 유실돼 revise가 반영됐는데도 steering_unacknowledged로 결과가 갇힐 수 있다. 파일 옆 락으로 직렬화.
    lock = work_dir / ".work_status.lock"

    def mutate():
        status = _read_json(work_dir / "work_status.json", {})
        current_seen = int(status.get("last_seen_steering_seq") or 0)
        updated = {
            **status,
            "last_seen_steering_seq": max(current_seen, seq),
            "pending_steering_ack": False,
            "heartbeat_phase": phase,
            "heartbeat_note": note[:500],
            "updated_at": now_iso(),
        }
        if seq >= current_seen:
            updated.update({
                "latest_steering_seq": seq,
                "latest_steering_action": latest.get("action", ""),
                "latest_steering_instruction": str(latest.get("instruction") or latest.get("reason") or "")[:1000],
                "latest_steering_requested_at": latest.get("created_at", ""),
                "latest_steering_requested_by": latest.get("requested_by", ""),
                "latest_steering_reason_code": latest.get("reason_code", ""),
                "latest_steering_dedupe_key": latest.get("dedupe_key", ""),
            })
        if "schema" not in updated:
            updated["schema"] = "WorkStatus.v1"
        _write_json(work_dir / "work_status.json", updated)

    try:
        lock.touch(exist_ok=True)
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                mutate()
                return
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception:
        mutate()  # 락 실패가 ack 자체를 막으면 안 된다(종전 무락 동작으로 폴백)


def work(
    person: str,
    space: str,
    task: str,
    engine: str = None,
    model: str = None,
    *,
    context: dict | None = None,
    requested_by: str = "legacy_engine_work",
    approved_by: str = "task_registry_v0_adapter",
    task_id: str | None = None,
) -> dict:
    seat = PEOPLE / person / "공간" / space
    if not seat.exists():
        raise ValueError(f"입장 안 됨: {person} -> {space}")
    # task_id 사전 지정: 호출자(_execute_work_plan)가 실행 '전에' plan↔task를 결속(mark_executing)해
    # 중복 기동을 계획 원장 수준에서 차단할 수 있게 한다(실증 2026-07-02: 15분 넘는 작업이
    # redispatch에 의해 중복 기동 — plan이 실행 내내 approved+task 없음으로 보였다).
    wcode = str(task_id or "").strip() or gen_code()
    wdir = seat / "작업" / wcode
    wdir.mkdir(parents=True)
    _work_entry = templates.fill(templates.load("작업_진입점.md"),
                                 사람표시=person, 공간표시=space, 작업코드=wcode)
    for fn in ENGINE_ENTRY:
        (wdir / fn).write_text(_work_entry, encoding="utf-8")
    if engine or model:
        runtime_info = runtime.write_runtime(wdir, engine, model, source="work-override")
    else:
        # 작업용 모델 분리: 자리에 work_model이 있으면 그걸로(채팅=빠른 모델, 작업=강한 모델),
        # 없으면 채팅 런타임 그대로(현행 동작·무회귀).
        wr = runtime.resolve_work_runtime(seat)
        runtime_info = runtime.write_runtime(wdir, wr["engine"], wr["model"], source=f"work:{person}->{space}")
    discovery_hits = discovery.find(task, "all", 5)
    task_contract = task_registry.create_task(
        space,
        worker=person,
        task_id=wcode,
        objective=task,
        work_dir=wdir,
        runtime_info=runtime_info,
        context=context,
        discovery_hits=discovery_hits,
        requested_by=requested_by,
        approved_by=approved_by,
    )
    task_pack = task_contract["task_pack"]

    _current_work_policy = _work_policy_loader(wdir, task_pack)
    work_policy = _current_work_policy()
    (wdir / "발견후보.md").write_text(discovery.render_context(task, discovery_hits), encoding="utf-8")
    # 주입 로그: 이 작업에 노출된 케이스를 기록(harmful 역추적·음승률 감지의 데이터 기반, P1' 안전판).
    injection_log.record_injection(space, kind="work", ref=wcode,
                                   injected=discovery.injected_case_refs(discovery_hits),
                                   context=context if isinstance(context, dict) else None)
    try:
        rel_wdir = wdir.relative_to(PEOPLE.parent).as_posix()   # 루트기준 작업 폴더 경로(미리보기 보고용)
    except Exception:
        rel_wdir = str(wdir)
    (wdir / "지시.md").write_text(
        f"# 지시\n\n{task}\n\n"
        f"# 너의 작업 폴더 (루트기준)\n\n`{rel_wdir}/`\n"
        "- 산출 파일은 **반드시 이 폴더에 저장**한다. 미리보기가 필요하면 결과·public_summary에 "
        f"그 파일의 루트기준 경로(`{rel_wdir}/<파일명>`)를 **한 줄로** 적는다. **경로를 지어내지 말고 실제 저장한 경로만** — "
        "대시보드가 그 경로를 말풍선 미리보기로 자동 렌더한다.\n\n"
        "# TaskPack v0 계약\n\n"
        "- 이 작업은 `task_pack.json`과 `task_handoff_pack.json` 범위 안에서만 수행한다.\n"
        "- **네 엔진의 능력을 전부 발휘해 끝까지 결론을 내라.** `runtime_capabilities.json`에 선언된 능력"
        "(셸 실행, 웹 리서치/네트워크, 서브에이전트 병렬 탐색, 이미지 판독 등)은 아끼지 말고 적극 사용한다 — "
        "직접 대화로 시켰을 때와 똑같은 수준으로 일해야 한다. 필요하면 서브에이전트로 조사·검증을 병렬로 돌리고, "
        "모르는 것은 웹으로 리서치하라. 제한은 능력이 아니라 **범위**다: 산출물 저장·수정은 allowed_paths(이 작업 폴더) "
        "안에서만, 결과 공개는 release 경로로만, 위험한 일(외부 발송·대량 변경)은 결재로.\n"
        f"- 이 작업의 runner_timeout_sec={work_policy['runner_timeout_sec']}, heartbeat_interval_sec={work_policy['heartbeat_interval_sec']}, "
        f"heartbeat_stale_ms={work_policy['heartbeat_stale_ms']}, progress_report_due_ms={work_policy['progress_report_due_ms']} 정책을 인지한다.\n"
        "- **작업 분할·체크포인트(가장 중요 — 클로드코드/코덱스처럼 일하라):** 아무리 큰 작업도 한 호출에 끝내려 하지 마라. 그게 타임아웃의 1순위 원인이다. 순서는 항상 이렇다:\n"
        "  (1) **깊이 읽기 전에 먼저** `결과.md` 상단에 **단계 TODO 체크리스트**를 박는다(골격 우선 — 첫 호출에서 이것조차 못 쓰고 시간 다 쓰면 안 된다).\n"
        "  (2) **한 번에 '한 단계'만** 완료하고 즉시 `결과.md`에 결과를 저장(체크)한다. 그다음 단계로.\n"
        "  (3) **한 단계는 '한 runner 호출 안에 확실히 끝낼 만큼' 작아야 한다.** 여러 산출물을 한 단계에 묶지 마라 — 예: 스킬 9개 이식이면 **'스킬 1개 이식'이 한 단계**다(9개를 한 호출에 X). 파일이 크면 '읽기 → 1개만 변환 → 저장 → 체크'로 더 잘게 쪼갠다. 한 단계가 호출보다 커 보이면 그 단계를 다시 하위 단계로 나눠라.\n"
        "  (4) 통과한 단계는 다시 건드리지 않는다. 시간이 부족하면 무리하지 말고 **깨끗한 단계 경계에서 멈추고** '## 다음 단계'를 남겨라 — 시스템이 그 체크포인트에서 다음 단계부터 **이어서 재실행**한다(끝낸 단계 반복 금지).\n"
        "  핵심: **진행을 잘게 쪼개 매 단계 저장하면 절대 통째로 실패하지 않는다.** 한 호출에 1단계라도 완료·저장하면 누적으로 끝까지 간다.\n"
        "- 긴 작업은 주요 단계 사이에 `steering/`과 `취소요청.json`을 확인한다.\n"
        "- `steering/`에 새 파일이 있으면 `steering_seq`, `action`, `instruction`을 반영하고 work_status.json의 `last_seen_steering_seq`를 해당 seq 이상으로 갱신한다.\n"
        "- `request_progress`는 현재 진행/막힌 점/다음 단계/부분 결과를 체크포인트로 남기라는 요청이고, `revise_task`는 반영 전 결과가 자동 공개되지 않는 재지시다. 시스템 runner가 실행 중 revise를 감지하면 이 작업을 새 지시와 함께 재실행할 수 있다.\n"
        "- `취소요청.json`이 있으면 현재까지의 결과를 `결과.md`에 체크포인트로 남기고 `상태.json`을 `{\"상태\":\"cancelled\",\"사유\":\"취소 요청 반영\"}` 형태로 갱신한 뒤 멈춘다.\n"
        "- **완료하면 '먼저' `상태.json`에 `{\"상태\":\"done\"}`을 기록하라(중요).** 산출물(결과.md·파일)이 사실상 완성되면 "
        "요약·정리 같은 마지막 단계로 미루지 말고 done부터 박아라 — 마지막 단계에서 타임아웃돼도 '완료'가 보존돼 "
        "결과가 방에 정상 공개된다(done 표시를 맨 끝으로 미루면, 막판 타임아웃 시 완료한 작업이 error로 처리될 수 있다).\n"
        "- 결과는 방에 직접 공개하지 않는다. `결과.md`, `상태.json`, 필요 시 `release_request.json` 초안으로 남긴다.\n"
        "- `task_pack.json`의 `lesson_pack.must_apply`가 비어 있지 않으면 `레슨적용보고.json`을 반드시 작성한다.\n"
        "- `레슨적용보고.json` 형식: "
        '{"schema":"LessonApplicationReport.v1","applications":[{"lesson_id":"...","applied":true,"not_applicable_reason":"","how":"...","outcome":"success","needs_lesson_update":false}]}\n\n'
        "# 시스템 주입 발견 후보\n\n"
        "이 작업 폴더의 `발견후보.md`를 먼저 읽고, 필요한 스킬·지식·도구·자산만 선택해 활용한다.\n",
        encoding="utf-8",
    )
    (wdir / "결과.md").write_text("", encoding="utf-8")
    return _run_task_runner(person, space, wcode, wdir, task_pack, task)


def _work_policy_loader(wdir: Path, task_pack: dict):
    def _load() -> dict:
        stored = work_settings.read_folder_settings(wdir)
        if stored.get("settings_source") == "default_missing_file":
            return work_settings.normalize_settings(task_pack.get("work_runtime_policy") or {})
        return work_settings.normalize_settings(stored)
    return _load


def resume_work(work_dir) -> dict:
    """죽은 워커의 작업을 '같은 작업 폴더'에서 체크포인트부터 이어서 재실행한다(교차 프로세스 재개).

    engine.work의 이어가기(WORK_TIMEOUT_CONTINUE_LIMIT)는 러너 프로세스가 살아있을 때만 돈다.
    러너 자체가 죽으면(하드킬·서버 재시작·기계 슬립) 종전엔 reaper가 error로 박고 '실패 보고'로
    끝났다 — 체크포인트(결과.md·산출물)가 멀쩡히 남아 있는데도. 이 함수는 그 체크포인트에서
    같은 작업을 이어가는 재개 진입점이다(run_work --resume가 detached로 호출).
    결과.md·산출물은 보존되고, 종료 시 동일하게 finalize_task → release로 공개된다.
    """
    wdir = Path(work_dir)
    task_pack = _read_json(wdir / "task_pack.json", {})
    space = str(task_pack.get("space_id") or "")
    person = str(task_pack.get("worker_agent") or "")
    wcode = str(task_pack.get("task_id") or wdir.name)
    objective = str(task_pack.get("objective") or "")
    if not space or not person or not wdir.exists():
        raise ValueError(f"resume_work: task_pack.json에 space/worker 없음 또는 폴더 없음: {wdir}")
    initial = (
        "\n\n# 이어서 수행(자동 재개 — 이전 러너 중단)\n"
        "이전 실행 프로세스가 중단됐다(타임아웃/종료). `결과.md`의 체크포인트와 '## 다음 단계'를 읽고 "
        "**이미 끝낸 단계는 반복하지 말고 다음 단계부터** 이어서 수행하라. "
        "이 폴더에 이미 있는 산출물·캡처는 다시 만들지 말고 재활용한다. "
        "다시 시간이 부족하면 깨끗한 단계 경계에서 멈추고 `결과.md`에 '## 다음 단계'를 갱신하라.\n"
    )
    return _run_task_runner(person, space, wcode, wdir, task_pack, objective, initial_continue_prompt=initial)


def _run_task_runner(
    person: str,
    space: str,
    wcode: str,
    wdir: Path,
    task_pack: dict,
    task: str,
    *,
    initial_continue_prompt: str = "",
) -> dict:
    _current_work_policy = _work_policy_loader(wdir, task_pack)
    run_error = None
    run_cancelled = False
    steering_context_events: list[dict] = []
    runtime_seen_steering_seq = int(_read_json(wdir / "work_status.json", {}).get("last_seen_steering_seq") or 0)
    # 러너 프로세스 pid를 기록한다 — reaper의 자동재개가 '러너가 정말 죽었는지'를 확인해
    # 살아있는(락대기 등으로 느린) 러너 위에 두 번째 러너를 겹쳐 띄우는 이중 실행을 막는 근거.
    try:
        _status = _read_json(wdir / "work_status.json", {})
        _write_json(wdir / "work_status.json", {**_status, "runner_pid": os.getpid(), "runner_pid_at": now_iso()})
    except Exception:
        pass

    def _work_cancel_requested(control_state: dict) -> bool:
        nonlocal runtime_seen_steering_seq
        if (wdir / "취소요청.json").exists():
            control_state["reason"] = "cancel_requested"
            return True
        status = _read_json(wdir / "work_status.json", {})
        try:
            last_seen = int(status.get("last_seen_steering_seq") or 0)
        except Exception:
            last_seen = 0
        events = _steering_events(wdir, after_seq=max(last_seen, runtime_seen_steering_seq))
        if not events:
            return False
        actions = {str(event.get("action") or "") for event in events}
        if "cancel_requested" in actions:
            control_state["reason"] = "cancel_requested"
            control_state["events"] = events
            return True
        if "revise_task" in actions:
            latest = events[-1]
            note = (
                f"revise_task steering 감지 seq={latest.get('steering_seq', 0)} "
                f"instruction={str(latest.get('instruction') or '')[:180]}"
            )
            _work_heartbeat("steering_revise_detected", note)
            control_state["reason"] = "revise_task"
            control_state["events"] = events
            runtime_seen_steering_seq = max(runtime_seen_steering_seq, int(latest.get("steering_seq") or 0))
            return True
        progress_events = [event for event in events if event.get("action") == "request_progress"]
        if progress_events:
            latest = progress_events[-1]
            note = (
                f"request_progress steering 감지 seq={latest.get('steering_seq', 0)}; "
                "엔진 실행 중이라 상세 결과는 반환/체크포인트 시 갱신"
            )
            _ack_work_steering(wdir, events, phase="steering_progress_seen", note=note)
            _work_heartbeat("steering_progress_seen", note)
            runtime_seen_steering_seq = max(runtime_seen_steering_seq, int(latest.get("steering_seq") or 0))
        return False

    def _work_heartbeat(phase: str, note: str = "") -> None:
        task_registry.record_heartbeat(
            space,
            task_id=wcode,
            worker=person,
            work_dir=wdir,
            task_pack=task_pack,
            phase=phase,
            note=note,
        )

    base_engine_prompt = (
        "이 폴더의 task_pack.json, task_handoff_pack.json, runtime_capabilities.json, "
        "execution_strategy.json, 발견후보.md, 지시.md를 읽고 실제로 수행해라. "
        "긴 단계 전후에는 steering/과 취소요청.json을 확인하고, steering을 반영하면 "
        "work_status.json의 last_seen_steering_seq를 해당 seq 이상으로 갱신해라. 취소요청이 있으면 "
        "결과.md에 체크포인트를 남긴 뒤 상태.json을 cancelled로 갱신하고 멈춰라. "
        "시스템 runner가 revise_task를 감지해 재실행했다면 아래 steering 요약을 원래 목표보다 후순위가 아니라 최신 보완 지시로 반영해라. "
        "완료/실패/취소 상태에 맞게 결과.md와 상태.json을 갱신해라. "
        "must_apply 레슨이 있으면 레슨적용보고.json도 남겨라."
    )
    engine_attempt = 0
    revise_restarts = 0
    continue_restarts = 0      # 타임아웃 후 체크포인트에서 이어서 재실행한 횟수
    no_progress_restarts = 0   # 무진행 타임아웃에서 '체크포인트부터' nudge로 재시도한 횟수
    continue_prompt = initial_continue_prompt  # 이어서 재실행 시 주입할 안내(재개 진입 시 사전 주입)
    while True:
        engine_attempt += 1
        work_policy = _current_work_policy()
        control_state = {"reason": "", "events": []}
        pre_run_result_len = len((wdir / "결과.md").read_text(encoding="utf-8")) if (wdir / "결과.md").exists() else 0
        pre_run_output_sig = _work_output_sig(wdir)
        try:
            _work_heartbeat(
                phase="engine_start" if engine_attempt == 1 else "engine_restart",
                note=f"engine runner 호출 직전 attempt={engine_attempt}",
            )
            steering_prompt = ""
            if steering_context_events:
                steering_prompt = (
                    "\n\n# 이번 재실행에 반드시 반영할 steering\n"
                    "이전 엔진 실행은 아래 steering을 반영하기 위해 중단됐다. "
                    "아래 지시를 현재 작업 목표와 함께 반영하고, 결과/상태/레슨 보고를 다시 점검하라.\n"
                    f"{_latest_steering_summary(steering_context_events)}"
                )
            engine_prompt = base_engine_prompt + steering_prompt + continue_prompt
            # 세션 연속성: 2회차 이후(revise/타임아웃 이어가기) 또는 자동재개 진입이면 같은 cwd의
            # 직전 엔진 세션을 잇는다 — 체크포인트 파일만이 아니라 '작업 기억' 자체가 이어진다.
            _continue_session = engine_attempt > 1 or bool(initial_continue_prompt)
            if run_engine is _ORIGINAL_RUN_ENGINE:
                def _call_engine(cont: bool) -> str:
                    return run_engine_polling(
                        wdir,
                        engine_prompt,
                        timeout=work_policy["runner_timeout_sec"],
                        cancel_check=lambda: _work_cancel_requested(control_state),
                        cancel_reason=lambda: control_state.get("reason") or "cancel_requested",
                        heartbeat=_work_heartbeat,
                        heartbeat_interval=work_policy["heartbeat_interval_sec"],
                        work_policy_loader=_current_work_policy,
                        continue_session=cont,
                    )
                engine_output = _call_engine(_continue_session)
                # 세션 부재 폴백: --continue인데 이 cwd에 이어질 세션이 없으면(세션 파일 유실 등)
                # 즉시 무-continue로 한 번 다시 부른다. 체크포인트 파일 기반 이어가기는 그대로 유효하다.
                if (
                    _continue_session
                    and str(engine_output or "").strip().startswith("(stderr)")
                    and "no conversation found" in str(engine_output or "").lower()
                ):
                    _work_heartbeat("engine_continue_fallback", "직전 세션 없음 — 무-continue로 재호출(체크포인트 기반 이어가기)")
                    engine_output = _call_engine(False)
            else:
                engine_output = run_engine(wdir, engine_prompt)
            if _engine_cancel_text(engine_output):
                cancel_reason = _engine_cancel_reason(engine_output)
                if cancel_reason == "revise_task":
                    if revise_restarts >= WORK_STEERING_RESTART_LIMIT:
                        run_error = RuntimeError("revise_task steering restart limit exceeded")
                        break
                    revise_restarts += 1
                    steering_context_events = _merge_steering_events(
                        steering_context_events,
                        control_state.get("events") or [],
                    )
                    _work_heartbeat(
                        phase="engine_restarting_for_revise",
                        note=f"revise_task 반영 재실행 {revise_restarts}/{WORK_STEERING_RESTART_LIMIT}",
                    )
                    continue
                run_cancelled = True
                _work_heartbeat(
                    phase="engine_cancelled",
                    note=str(engine_output or "")[:240],
                )
            else:
                post_events = _steering_events(wdir, after_seq=runtime_seen_steering_seq)
                post_actions = {str(event.get("action") or "") for event in post_events}
                if (wdir / "취소요청.json").exists() or "cancel_requested" in post_actions:
                    run_cancelled = True
                    _work_heartbeat(
                        phase="engine_cancelled",
                        note="engine_return 후 취소 요청 감지",
                    )
                    break
                if "revise_task" in post_actions:
                    if revise_restarts >= WORK_STEERING_RESTART_LIMIT:
                        run_error = RuntimeError("revise_task steering restart limit exceeded")
                        break
                    revise_restarts += 1
                    latest_post = post_events[-1]
                    steering_context_events = _merge_steering_events(steering_context_events, post_events)
                    runtime_seen_steering_seq = max(runtime_seen_steering_seq, int(latest_post.get("steering_seq") or 0))
                    _work_heartbeat(
                        phase="steering_revise_detected_after_return",
                        note=(
                            f"engine_return 직후 revise_task 감지 seq={latest_post.get('steering_seq', 0)}; "
                            f"재실행 {revise_restarts}/{WORK_STEERING_RESTART_LIMIT}"
                        ),
                    )
                    continue
                progress_events = [event for event in post_events if event.get("action") == "request_progress"]
                if progress_events:
                    latest_progress = progress_events[-1]
                    _ack_work_steering(
                        wdir,
                        progress_events,
                        phase="steering_progress_seen",
                        note=f"engine_return 직후 request_progress 감지 seq={latest_progress.get('steering_seq', 0)}",
                    )
                    runtime_seen_steering_seq = max(runtime_seen_steering_seq, int(latest_progress.get("steering_seq") or 0))
                    _work_heartbeat(
                        "steering_progress_seen",
                        f"engine_return 직후 request_progress 감지 seq={latest_progress.get('steering_seq', 0)}",
                    )
                if steering_context_events:
                    latest_seq = int(steering_context_events[-1].get("steering_seq") or 0)
                    _ack_work_steering(
                        wdir,
                        steering_context_events,
                        phase="steering_revise_applied",
                        note=f"revise_task steering 반영 실행 완료 seq={latest_seq}",
                    )
                    runtime_seen_steering_seq = max(runtime_seen_steering_seq, latest_seq)
                _work_heartbeat(
                    phase="engine_returned",
                    note=str(engine_output or "")[:240],
                )
            if _engine_failure_text(engine_output):
                # 작업 분할/체크포인트(연구 최적): 타임아웃 + 이번 실행에서 결과.md가 늘었으면(=진행 있음)
                # 처음부터 다시가 아니라 체크포인트에서 *이어서* 재실행한다. 진행이 없거나(멈춤) 상한 초과면 에스컬레이션.
                if _engine_timeout_text(engine_output):
                    post_len = len((wdir / "결과.md").read_text(encoding="utf-8")) if (wdir / "결과.md").exists() else 0
                    post_output_sig = _work_output_sig(wdir)
                    # 진행 = 결과.md가 늘었거나 OR 산출물(슬라이드·html·이미지 등)이 바뀐 경우.
                    # (워커가 파일은 만들었는데 결과.md 체크포인트를 안 갱신해도 진행으로 인정 → 이어서 마무리)
                    made_progress = (post_len > pre_run_result_len) or (post_output_sig != pre_run_output_sig)
                    # 워커가 이미 done/완료를 명시했으면 더 이어가지 않는다(불필요한 재가동으로 방을 점유 방지).
                    try:
                        _ws = json.loads((wdir / "상태.json").read_text(encoding="utf-8")) if (wdir / "상태.json").exists() else {}
                        _worker_done = str(_ws.get("상태") or _ws.get("state") or "").lower() in {"done", "completed", "complete", "cancelled", "canceled"}
                    except Exception:
                        _worker_done = False
                    if made_progress and not _worker_done and continue_restarts < WORK_TIMEOUT_CONTINUE_LIMIT:
                        continue_restarts += 1
                        continue_prompt = (
                            "\n\n# 이어서 수행(체크포인트 재개)\n"
                            "이전 실행이 시간제한으로 중단됐다. `결과.md`에 남긴 체크포인트와 '다음 단계'를 읽고 "
                            "**이미 끝낸 단계는 다시 하지 말고 다음 단계부터 이어서** 수행하라. "
                            "다시 시간이 부족하면 무리하게 끝내려 말고 깨끗한 단계 경계에서 멈추고, "
                            "`결과.md`에 '완료한 단계'와 '## 다음 단계'를 갱신해 남겨라.\n"
                            f"(이어가기 {continue_restarts}/{WORK_TIMEOUT_CONTINUE_LIMIT})\n"
                        )
                        _work_heartbeat(
                            phase="engine_restarting_for_continue",
                            note=f"타임아웃+진행 감지(결과 {pre_run_result_len}->{post_len}자), 체크포인트에서 이어서 {continue_restarts}/{WORK_TIMEOUT_CONTINUE_LIMIT}",
                        )
                        continue
                    # 무진행 타임아웃: 즉시 포기 대신 '체크포인트(골격)부터 써라' 1회 재시도(큰 작업 시작 유도).
                    if (not made_progress) and (not _worker_done) and no_progress_restarts < WORK_NO_PROGRESS_RETRY_LIMIT:
                        no_progress_restarts += 1
                        continue_prompt = (
                            "\n\n# 즉시 체크포인트부터(무진행 재시도)\n"
                            "이전 실행이 제한시간 안에 `결과.md`에 아무것도 남기지 못했다(작업이 크거나 읽기에 시간을 다 씀). "
                            "**무거운 읽기·탐색을 더 하기 전에, 가장 먼저** `결과.md`에 단계 체크리스트와 지금 당장 만들 수 있는 "
                            "가장 작은 첫 산출(골격)을 써서 저장하라. 한 번에 다 하려 하지 말고 가장 작은 한 조각만 끝낸 뒤 "
                            "`## 다음 단계`에 나머지를 남겨라(다음 실행이 거기서 이어간다).\n"
                            f"(무진행 재시도 {no_progress_restarts}/{WORK_NO_PROGRESS_RETRY_LIMIT})\n"
                        )
                        _work_heartbeat(
                            phase="engine_restarting_no_progress_checkpoint",
                            note=f"타임아웃 무진행 — 체크포인트 우선 1회 재시도 {no_progress_restarts}/{WORK_NO_PROGRESS_RETRY_LIMIT}",
                        )
                        continue
                    _work_heartbeat(
                        phase="engine_timeout_no_progress" if not made_progress else "engine_timeout_continue_limit",
                        note=(f"타임아웃, 진행 없음(결과 {post_len}자) — 에스컬레이션" if not made_progress
                              else f"타임아웃 이어가기 상한 도달 {continue_restarts}/{WORK_TIMEOUT_CONTINUE_LIMIT} — 에스컬레이션"),
                    )
                run_error = RuntimeError(engine_output)
            break
        except Exception as exc:
            run_error = exc
            try:
                task_registry.record_heartbeat(
                    space,
                    task_id=wcode,
                    worker=person,
                    work_dir=wdir,
                    task_pack=task_pack,
                    phase="engine_error",
                    note=f"{type(exc).__name__}: {str(exc)[:240]}",
                )
            except Exception:
                pass
            break
    if run_cancelled:
        if not (wdir / "결과.md").read_text(encoding="utf-8").strip():
            (wdir / "결과.md").write_text("취소 요청으로 작업 실행이 중단됨", encoding="utf-8")
        existing_status = {}
        if (wdir / "상태.json").exists():
            try:
                existing_status = json.loads((wdir / "상태.json").read_text(encoding="utf-8"))
            except Exception:
                existing_status = {}
            if not isinstance(existing_status, dict):
                existing_status = {}
        (wdir / "상태.json").write_text(
            json.dumps({
                **existing_status,
                "상태": "cancelled",
                "사유": existing_status.get("사유") or "엔진 실행 중 취소 요청 감지",
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    if run_error is not None:
        if not (wdir / "결과.md").read_text(encoding="utf-8").strip():
            (wdir / "결과.md").write_text("", encoding="utf-8")
        (wdir / "상태.json").write_text(
            json.dumps({"상태": "error", "사유": f"{type(run_error).__name__}: {str(run_error)[:240]}"}, ensure_ascii=False),
            encoding="utf-8",
        )
    final = task_registry.finalize_task(
        space,
        task_id=wcode,
        worker=person,
        work_dir=wdir,
        task_pack=task_pack,
        objective=task,
    )
    if run_error is not None:
        raise run_error
    return {
        "작업코드": wcode,
        "상태": final["state"],
        "결과": final["result"],
    }
