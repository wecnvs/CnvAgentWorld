# -*- coding: utf-8 -*-
"""앱 레지스트리 — 루트폴더 `앱/` 아래 앱(매니페스트 `앱.md`)을 스캔·실행·중지한다.

한 앱 = 한 폴더(`앱/<등급>/<이름>/`). 그 안에 소스·설치파일·실행기가 함께 있다.
대시보드 앱 탭이 이 모듈로 목록을 받아, 종류(kind)와 **실행 위치(target)**에 맞는 액션을 그린다.

실행 위치(target) — 서버 컴퓨터를 중앙 컨트롤센터로:
- local      : 서버 컴퓨터에서 subprocess (Windows/mac/Linux 크로스플랫폼)
- cu-helper  : VM/원격 윈도우의 cu_helper.ps1 HTTP 데몬에 /run·/stop·/ps 디스패치(완전 추적)
- ssh        : `ssh <ssh> <cmd>` 로 원격 실행(런처 추적 — 원격 종료는 제한)
- parallels  : `prlctl exec <vm> <cmd>` 로 게스트 실행(런처 추적 — 제한)

종류(kind): web-app(포트+브라우저) · standalone/external(실행) · revit-addin/install-only(다운로드만).
발견 정본은 frontmatter `description`이다(태그 없음 — law.md §7).
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .paths import ROOT
from .discovery import parse_front
from . import app_targets

IS_WIN = os.name == "nt"

APPS = ROOT / "앱"
MANIFEST = "앱.md"
GRADES = ("기본", "추가", "고급", "대외비")
RUN_DIR = ".run"
RUN_FILE = "app.json"

_SKIP_NAMES = {MANIFEST, RUN_DIR, ".preview", "__pycache__", ".DS_Store", ".gitkeep"}
_INSTALL_EXT = {"msi", "exe", "dmg", "pkg", "zip", "vsix", "addin", "msix", "appimage", "deb"}
_DOWNLOAD_ONLY = {"revit-addin", "install-only"}
# 원격이라 런처 pid만 잡혀(중지 시 원격 프로세스까지는 보장 못 함) 상태추적이 제한적인 채널
_LIMITED_TRACK = {"ssh", "parallels"}


# ═══════════════════════ 로컬 프로세스 계층 (크로스플랫폼) ═══════════════════════

def _popen_kwargs() -> dict:
    """detach 실행 옵션 — POSIX는 새 세션, Windows는 새 프로세스그룹+detach."""
    if IS_WIN:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _local_pid_alive(pid) -> bool:
    if not pid:
        return False
    pid = int(pid)
    if IS_WIN:
        # ★ Windows에서 os.kill(pid,0)은 '신호 확인'이 아니라 프로세스를 죽인다 → 절대 쓰지 않는다.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        except Exception:
            # ctypes 실패 시 tasklist 폴백
            try:
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                     capture_output=True, text=True, timeout=5).stdout
                return str(pid) in out
            except Exception:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 다른 사용자 소유 — 살아있음(좀비 아님)
    except OSError:
        return False
    # 좀비(defunct)는 죽은 것으로 본다 — os.kill(0)은 미회수 좀비에도 성공하므로, 종료 후 '실행 중'
    # 오표시를 막기 위해 프로세스 상태(ps state='Z…')를 확인한다.
    try:
        st = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=3).stdout.strip()
        if st and st[0].upper() == "Z":
            return False
    except Exception:
        pass
    return True


def _posix_descendants(pid: int) -> list:
    """pid와 그 자손 pid들(BFS, pgrep -P로 트리 순회). 프로세스 '그룹 전체'가 아니라 실제 자손만
    모은다 — 그룹째 종료(killpg)는 대상이 셸/터미널과 같은 그룹일 때 무관 프로세스까지 죽인다."""
    out = [pid]
    seen = {pid}
    frontier = [pid]
    while frontier:
        nxt = []
        for p in frontier:
            try:
                r = subprocess.run(["pgrep", "-P", str(p)], capture_output=True, text=True, timeout=3)
            except Exception:
                continue
            for tok in r.stdout.split():
                try:
                    c = int(tok)
                except ValueError:
                    continue
                if c not in seen:
                    seen.add(c)
                    out.append(c)
                    nxt.append(c)
        frontier = nxt
    return out


def _local_kill_tree(pid) -> None:
    if not pid:
        return
    pid = int(pid)
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    # POSIX: 자기+자손만 SIGTERM(그룹 전체 killpg 금지 — 터미널서 띄운 앱 종료 시 터미널까지 죽는 위험).
    import signal
    targets = _posix_descendants(pid)
    for t in reversed(targets):          # 리프(자손)부터 종료
        try:
            os.kill(t, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(10):
        if not _local_pid_alive(pid):
            break
        time.sleep(0.2)
    for t in reversed(targets):          # 안 죽은 것 SIGKILL
        try:
            if _local_pid_alive(t):
                os.kill(t, signal.SIGKILL)
        except OSError:
            pass


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(port: int, deadline_sec: float = 6.0) -> bool:
    end = time.time() + deadline_sec
    while time.time() < end:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


# ═══════════════════════ 원격 채널: cu-helper (HTTP) ═══════════════════════

def _cuhelper_url(cfg: dict, path: str) -> str:
    host = cfg.get("host", "")
    port = cfg.get("port", 8599)
    return f"http://{host}:{port}{path}"


def _http_json(url: str, payload: dict | None = None, timeout: float = 6.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body) if body else {}
    except Exception:
        return {"_raw": body}


def _cuhelper_run(cfg: dict, cmd: str, cwd: str = "") -> dict:
    """VM/원격 윈도우 헬퍼에 프로그램 실행을 요청 → 원격 pid를 받는다."""
    try:
        out = _http_json(_cuhelper_url(cfg, "/run"), {"cmd": cmd, "cwd": cwd})
    except Exception as exc:
        raise ValueError(f"원격 헬퍼 연결 실패({cfg.get('label', cfg.get('name'))}): {exc}")
    pid = out.get("pid")
    if not pid:
        raise ValueError(f"원격 실행 실패: {out.get('error') or out}")
    return {"pid": pid}


def _cuhelper_alive(cfg: dict, pid) -> bool:
    try:
        out = _http_json(_cuhelper_url(cfg, "/ps"), {"pid": pid}, timeout=2.0)
        return bool(out.get("alive"))
    except Exception:
        return False


def _cuhelper_stop(cfg: dict, pid) -> bool:
    try:
        out = _http_json(_cuhelper_url(cfg, "/stop"), {"pid": pid}, timeout=6.0)
        return bool(out.get("ok", True))
    except Exception:
        return False


# ═══════════════════════ 실행 중 인스턴스 감지 (자동감지·PID 타깃팅) ═══════════════════════
# pidfile은 '대시보드가 띄운' 앱만 안다. 사용자가 대시보드 밖에서 켠 인스턴스(예: 원격 Revit이
# 이미 열려 있음)는 놓쳐서 launch_app이 중복 실행을 한다(실증: 레빗_bcd7 8600 바인딩 충돌).
# 그래서 앱의 프로세스 시그니처(manifest process, 없으면 run 실행파일명)로 target에서 실제 실행
# 중 인스턴스를 조회한다 → 중복 실행 방지 + PID를 surface 해 '어느 인스턴스'를 대상으로 작업할지
# 에이전트에 지정 가능. 폴링 비용은 짧은 TTL 캐시로 억제(채널 미응답 결과도 캐시해 반복 stall 방지).

_PROCLIST_TTL_SEC = 12.0
_proclist_cache: dict = {}
_APP_EXE_SUFFIXES = (".exe", ".app", ".bat", ".cmd", ".com")
# 이 이름들이 시그니처면 감지를 건너뛴다(범용 인터프리터/런처 — 무관 프로세스 오탐·오종료 방지).
# 이런 앱은 manifest에 explicit `process:`를 두어 실제 앱명으로 감지하게 한다.
_GENERIC_PROC_SIGNATURES = {
    "python", "python3", "python2", "py", "pythonw", "node", "nodejs", "deno", "bun",
    "java", "javaw", "ruby", "perl", "php", "sh", "bash", "zsh", "cmd", "powershell",
    "pwsh", "dotnet", "mono", "electron", "open", "cmd.exe", "wscript", "cscript",
}


def _process_signature(front: dict) -> str:
    """실행 중 인스턴스를 매칭할 프로세스명(확장자 제외). manifest의 `process`가 있으면 우선,
    없으면 run에서 '실제 프로세스'를 유도한다. 런처 명령 주의:
      - `...\\Revit.exe`      → 'Revit'
      - `open -a TextEdit`   → 'TextEdit'  (macOS `open`은 런처일 뿐, 실제 프로세스는 그 앱)
      - `open Foo.app`       → 'Foo'
    (런처 토큰 'open'을 그대로 시그니처로 쓰면 pgrep이 'OpenGL/opendirectoryd' 같은 무관 프로세스를
     오탐한다 — 실증: 메모장-맥 종료가 엉뚱한 시스템 프로세스를 겨눠 실패.)"""
    cand = str(front.get("process") or "").strip()
    if not cand:
        run = str(front.get("run") or "").strip()
        if not run:
            return ""
        try:
            parts = shlex.split(run, posix=True)
        except ValueError:
            parts = run.split()
        if parts and Path(parts[0]).name.lower() in ("open", "open.exe"):
            for i, tok in enumerate(parts):
                if tok == "-a" and i + 1 < len(parts):
                    cand = parts[i + 1]
                    break
                if tok.lower().endswith(".app"):
                    cand = Path(tok).name
                    break
        if not cand:
            if run[0] in ("\"", "'"):
                end = run.find(run[0], 1)
                exe = run[1:end] if end > 0 else run[1:]
            else:
                exe = parts[0] if parts else run
            cand = Path(exe.replace("\\", "/")).name
    low = cand.lower()
    for suf in _APP_EXE_SUFFIXES:
        if low.endswith(suf):
            return cand[: -len(suf)].strip()
    return cand.strip()


def _cuhelper_proclist(cfg: dict, name: str) -> dict:
    try:
        out = _http_json(_cuhelper_url(cfg, "/proclist"), {"name": name}, timeout=3.0)
    except Exception:
        return {"detected": False, "instances": [], "reason": "cuhelper_unreachable"}
    inst = []
    for p in (out.get("procs") if isinstance(out, dict) else None) or []:
        if isinstance(p, dict) and p.get("pid"):
            inst.append({"pid": int(p["pid"]), "title": str(p.get("title") or ""), "start": str(p.get("start") or "")})
    return {"detected": True, "instances": inst}


def _local_proclist(name: str) -> dict:
    inst = []
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=4)
            for line in out.stdout.splitlines():
                cells = [c.strip().strip("\"") for c in line.split("\",\"")]
                if len(cells) >= 2 and cells[0].lower().startswith(name.lower()):
                    try:
                        inst.append({"pid": int(cells[1]), "title": "", "start": ""})
                    except Exception:
                        pass
        else:
            # -f(명령줄 매칭) 금지: 'open' 같은 시그니처가 OpenGL/opendirectoryd 등 무관 프로세스를
            # 오탐한다(실증). 프로세스 '이름'으로만 매칭하고, comm(실행파일명)으로 재확인해 오탐 제거.
            out = subprocess.run(["pgrep", "-i", name], capture_output=True, text=True, timeout=4)
            for tok in out.stdout.split():
                try:
                    pid = int(tok)
                except ValueError:
                    continue
                try:
                    comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                                          capture_output=True, text=True, timeout=3).stdout.strip()
                except Exception:
                    comm = ""
                base = Path(comm).name if comm else ""
                # comm 기준 재확인: 실행파일명에 시그니처가 들어가야 진짜 그 앱(pgrep 이름매칭 오탐 차단)
                if base and name.lower() in base.lower():
                    inst.append({"pid": pid, "title": base, "start": ""})
    except Exception:
        return {"detected": False, "instances": [], "reason": "local_query_failed"}
    return {"detected": True, "instances": inst}


def _detect_instances_uncached(rel_dir: str) -> dict:
    try:
        app_dir = _safe_app_dir(rel_dir)
        front = parse_front(app_dir / MANIFEST)
    except Exception:
        return {"detected": False, "instances": [], "reason": "no_manifest"}
    # 웹앱(포트 서버)은 프로세스명 감지 대상이 아니다 — 인터프리터(python3/node 등)로 떠서
    # 시그니처가 범용이라 무관 프로세스를 오탐하고 종료버튼이 그걸(대시보드 서버 등!) 죽일 위험이
    # 있다. 웹앱은 pidfile/port로 판단한다.
    kind = str(front.get("kind") or "").strip().lower()
    if kind == "web-app" or _int_or_none(front.get("port")) is not None:
        return {"detected": False, "instances": [], "reason": "web_app_uses_pidfile"}
    sig = _process_signature(front)
    if not sig:
        return {"detected": False, "instances": [], "reason": "no_signature"}
    # 범용 인터프리터/런처 시그니처는 감지 제외(오탐·오종료 방지). 그런 앱은 manifest에 explicit
    # `process:`를 두면 그 이름으로 감지된다.
    if not str(front.get("process") or "").strip() and sig.lower() in _GENERIC_PROC_SIGNATURES:
        return {"detected": False, "instances": [], "reason": f"signature_too_generic:{sig}"}
    cfg = app_targets.resolve(front.get("target"))
    ch = cfg.get("channel")
    if ch == "cu-helper":
        res = _cuhelper_proclist(cfg, sig)
    elif ch == "local":
        res = _local_proclist(sig)
    else:
        res = {"detected": False, "instances": [], "reason": f"channel_{ch}_unsupported"}
    res["signature"] = sig
    return res


def detect_running_instances(rel_dir: str, *, fresh: bool = False) -> dict:
    """등록앱 target에서 프로세스 시그니처로 실행 중 인스턴스 조회 →
    {detected, instances:[{pid,title,start}], signature}. pidfile 밖(외부 기동) 인스턴스도 잡는다.
    fresh=False면 짧은 TTL 캐시(폴링 비용·미응답 stall 억제)."""
    key = str(rel_dir)
    if not fresh:
        c = _proclist_cache.get(key)
        if c and (time.time() - c[0]) < _PROCLIST_TTL_SEC:
            return c[1]
    res = _detect_instances_uncached(rel_dir)
    _proclist_cache[key] = (time.time(), res)
    return res


# ═══════════════════════ 디스패치 (채널 추상화) ═══════════════════════

def _wrap_remote_cmd(cfg: dict, cmd: str) -> list[str]:
    """ssh/parallels: 로컬 런처 명령으로 감싼다(런처 pid만 추적 — 제한)."""
    ch = cfg["channel"]
    if ch == "ssh":
        ssh_target = cfg.get("ssh") or cfg.get("host")
        return ["ssh", ssh_target] + shlex.split(cmd)
    if ch == "parallels":
        vm = cfg.get("vm") or cfg.get("name")
        return ["prlctl", "exec", vm] + shlex.split(cmd)
    raise ValueError(f"감쌀 수 없는 채널: {ch}")


def _dispatch_alive(cfg: dict, pid) -> bool:
    if cfg["channel"] == "cu-helper":
        return _cuhelper_alive(cfg, pid)
    return _local_pid_alive(pid)   # local/ssh/parallels는 로컬 런처 pid


def _dispatch_stop(cfg: dict, pid) -> None:
    if cfg["channel"] == "cu-helper":
        _cuhelper_stop(cfg, pid)
    else:
        _local_kill_tree(pid)


# ═══════════════════════ 스캔 / 표시 ═══════════════════════

def _grade_of(app_dir: Path) -> str:
    try:
        rel = app_dir.relative_to(APPS)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if parts and parts[0] in GRADES else ""


def _rel_in_app(app_dir: Path, value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    target = (app_dir / value).resolve()
    if app_dir.resolve() not in target.parents and target != app_dir.resolve():
        return None
    if not target.is_file():
        return None
    return str(target.relative_to(ROOT))


def _list_files(app_dir: Path) -> list[dict]:
    out = []
    for f in sorted(app_dir.rglob("*"), key=lambda p: str(p.relative_to(app_dir)).lower()):
        if not f.is_file():
            continue
        if any(part in _SKIP_NAMES for part in f.relative_to(app_dir).parts):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        ext = f.suffix.lower().lstrip(".")
        out.append({"이름": f.name, "경로": str(f.relative_to(ROOT)),
                    "크기": size, "설치파일": ext in _INSTALL_EXT})
    return out


def _body_excerpt(manifest: Path, limit: int = 600) -> str:
    try:
        text = manifest.read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()[:limit]


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _runfile(app_dir: Path) -> Path:
    return app_dir / RUN_DIR / RUN_FILE


def _running_state(app_dir: Path) -> dict:
    """pidfile을 읽어 현재 실행 상태를 채널에 맞게 판단한다. 죽은 pidfile은 청소한다."""
    rf = _runfile(app_dir)
    try:
        info = json.loads(rf.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "pid": None, "port": None}
    pid = info.get("pid")
    cfg = app_targets.resolve(info.get("target"))
    if _dispatch_alive(cfg, pid):
        return {"running": True, "pid": pid, "port": info.get("port")}
    try:
        rf.unlink()
    except OSError:
        pass
    return {"running": False, "pid": None, "port": None}


def _app_from(manifest: Path) -> dict:
    front = parse_front(manifest)
    app_dir = manifest.parent
    kind = (front.get("kind") or "").strip().lower() or "기타"
    run = (front.get("run") or "").strip()
    port = _int_or_none(front.get("port"))
    is_web = kind == "web-app" or port is not None
    download_only = kind in _DOWNLOAD_ONLY
    tcfg = app_targets.resolve(front.get("target"))
    tdisp = app_targets.display(tcfg)
    state = _running_state(app_dir)
    # 자동감지: 대시보드 밖에서 켜진 인스턴스까지 포함해 실제 실행 중 인스턴스(+PID)를 붙인다(캐시됨).
    det = detect_running_instances(str(app_dir.relative_to(ROOT)))
    instances = det.get("instances") or []
    return {
        "name": front.get("name", app_dir.name),
        "description": front.get("description", ""),
        "kind": kind,
        "platform": front.get("platform", ""),
        "version": front.get("version", ""),
        "status": front.get("status", ""),
        "grade": _grade_of(app_dir),
        "dir": str(app_dir.relative_to(ROOT)),
        "web": is_web,
        "port": port,
        "run": run,
        "runnable": bool(run) and not download_only,
        "download_only": download_only,
        # 실행 위치(target)
        "target": tdisp["name"],
        "target_channel": tdisp["channel"],
        "target_icon": tdisp["icon"],
        "target_text": tdisp["text"],
        "target_local": tdisp["is_local"],
        "target_unconfigured": tdisp["unconfigured"],
        "target_limited": tdisp["channel"] in _LIMITED_TRACK,
        "install_path": _rel_in_app(app_dir, front.get("install", "")),
        "download_path": _rel_in_app(app_dir, front.get("download", "")),
        "files": _list_files(app_dir),
        "body": _body_excerpt(manifest),
        "running": state["running"] or bool(instances),
        "pid": state["pid"] or (instances[0]["pid"] if instances else None),
        "running_port": state["port"],
        "instances": instances,                       # [{pid,title,start}] — 외부 기동 포함
        "instance_count": len(instances),
        "instances_detected": bool(det.get("detected")),
    }


def list_apps() -> dict:
    apps = []
    if APPS.exists():
        for manifest in APPS.rglob(MANIFEST):
            front = parse_front(manifest)
            if not (front.get("description") or front.get("name")):
                continue
            apps.append(_app_from(manifest))
    order = {g: i for i, g in enumerate(GRADES)}
    apps.sort(key=lambda a: (order.get(a["grade"], 99), a["name"].lower()))
    return {"apps": apps, "count": len(apps), "targets": app_targets.list_targets()}


# ═══════════════════════ 실행 / 중지 ═══════════════════════

def _safe_app_dir(rel_dir: str) -> Path:
    rel = (rel_dir or "").strip().lstrip("/")
    target = (ROOT / rel).resolve()
    apps_root = APPS.resolve()
    if target != apps_root and apps_root not in target.parents:
        raise ValueError("앱 폴더 밖 경로는 허용되지 않음")
    if not (target / MANIFEST).is_file():
        raise ValueError(f"앱 매니페스트 없음: {rel_dir}")
    return target


def _log_tail(app_dir: Path, n: int = 600) -> str:
    try:
        return (app_dir / RUN_DIR / "app.log").read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except Exception:
        return ""


def _launch_local(app_dir: Path, parts: list[str]) -> subprocess.Popen:
    run_dir = app_dir / RUN_DIR
    run_dir.mkdir(exist_ok=True)
    logf = open(run_dir / "app.log", "ab")
    return subprocess.Popen(
        parts, cwd=str(app_dir),
        stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
        **_popen_kwargs(),
    )


def run_app(rel_dir: str) -> dict:
    """매니페스트의 `run`만 그 앱의 target(서버/원격/VM)에서 실행한다(클라이언트 임의 명령 차단)."""
    app_dir = _safe_app_dir(rel_dir)
    front = parse_front(app_dir / MANIFEST)
    kind = (front.get("kind") or "").strip().lower()
    if kind in _DOWNLOAD_ONLY:
        raise ValueError(f"{kind}은 설치파일 다운로드만 — 실행 대상이 아닙니다")
    cmd = (front.get("run") or "").strip()
    if not cmd:
        raise ValueError("이 앱에는 실행(run) 명령이 없습니다")
    port = _int_or_none(front.get("port"))
    is_web = kind == "web-app" or port is not None

    cfg = app_targets.resolve(front.get("target"))
    channel = cfg["channel"]
    if channel == "unknown":
        raise ValueError(f"실행 위치(target='{cfg.get('name')}')가 레지스트리에 없습니다 — 자산/대외비/앱실행대상/targets.json 확인")

    state = _running_state(app_dir)
    if state["running"]:
        return {"ok": True, "already": True, "running": True, "pid": state["pid"],
                "port": state["port"], "web": is_web, "kind": kind, "channel": channel,
                "target": cfg["name"], "dir": str(app_dir.relative_to(ROOT))}

    # 중복 실행 방지: pidfile 밖(사용자가 대시보드 밖에서 켠) 인스턴스도 프로세스 시그니처로 감지한다.
    # 이미 실행 중이면 또 띄우지 않고 실행 중 PID들을 보고한다(실증: 레빗_bcd7의 중복 Revit → 8600 충돌).
    # 굳이 새 인스턴스가 필요하면 매니페스트에 allow_multi:true를 둔다.
    if str(front.get("allow_multi") or "").strip().lower() not in ("true", "1", "yes", "y"):
        det = detect_running_instances(rel_dir, fresh=True)
        insts = det.get("instances") or []
        if insts:
            return {"ok": True, "already": True, "running": True, "pid": insts[0]["pid"],
                    "instances": insts, "instance_count": len(insts), "detected_external": True,
                    "port": None, "web": is_web, "kind": kind, "channel": channel,
                    "target": cfg["name"], "dir": str(app_dir.relative_to(ROOT))}

    # ── 채널별 실행 ──
    if channel == "cu-helper":
        res = _cuhelper_run(cfg, cmd, cwd=front.get("run_cwd", ""))
        pid = res["pid"]
        ready = None
    else:
        # local / ssh / parallels — 로컬 런처 subprocess
        if channel == "local":
            try:
                parts = shlex.split(cmd, posix=not IS_WIN)
            except ValueError as exc:
                raise ValueError(f"run 명령 파싱 실패: {exc}")
        else:
            parts = _wrap_remote_cmd(cfg, cmd)
        if not parts:
            raise ValueError("빈 run 명령")
        try:
            proc = _launch_local(app_dir, parts)
        except FileNotFoundError:
            raise ValueError(f"실행 파일을 찾을 수 없음: {parts[0]}")
        except Exception as exc:
            raise ValueError(f"실행 실패: {exc}")
        pid = proc.pid
        ready = None
        # 실행 직후 점검(조용한 실패 방지)
        if is_web and port:
            ready = _wait_port(port)
            if not ready and proc.poll() is not None:
                _rm_runfile(app_dir)
                raise ValueError(f"web-app 서버가 시작 직후 종료됨(포트 {port} 안 열림). 로그: {_log_tail(app_dir) or '(없음)'}")
        elif channel == "local":
            time.sleep(0.5)
            rc = proc.poll()
            if rc is not None and rc != 0:
                _rm_runfile(app_dir)
                raise ValueError(f"실행기가 비정상 종료(코드 {rc}) — 프로그램 설치/run 명령 확인. 로그: {_log_tail(app_dir) or '(없음)'}")

    info = {"channel": channel, "target": cfg["name"], "pid": pid, "port": port,
            "web": is_web, "kind": kind, "cmd": cmd,
            "started_at": datetime.now().isoformat(timespec="seconds")}
    _write_runfile(app_dir, info)
    # web-app을 브라우저로 열 때: 로컬이면 내가 접속한 호스트(프런트의 location.hostname),
    # 원격이면 그 타깃 host로 연다.
    open_host = cfg.get("host") if (is_web and channel != "local") else None
    return {"ok": True, "already": False, "running": True, "pid": pid, "port": port,
            "web": is_web, "kind": kind, "channel": channel, "target": cfg["name"],
            "ready": ready, "limited": channel in _LIMITED_TRACK, "open_host": open_host,
            "dir": str(app_dir.relative_to(ROOT))}


def stop_app(rel_dir: str) -> dict:
    app_dir = _safe_app_dir(rel_dir)
    rf = _runfile(app_dir)
    try:
        info = json.loads(rf.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": True, "running": False, "stopped": False, "reason": "실행 중 아님"}
    pid = info.get("pid")
    cfg = app_targets.resolve(info.get("target"))
    if not _dispatch_alive(cfg, pid):
        _rm_runfile(app_dir)
        return {"ok": True, "running": False, "stopped": False, "reason": "이미 종료됨"}
    _dispatch_stop(cfg, pid)
    for _ in range(10):
        if not _dispatch_alive(cfg, pid):
            break
        time.sleep(0.2)
    _rm_runfile(app_dir)
    _proclist_cache.pop(str(rel_dir), None)   # 감지 캐시 무효화(다음 조회에 즉시 반영)
    return {"ok": True, "running": False, "stopped": True, "pid": pid,
            "channel": cfg["channel"], "target": cfg["name"], "dir": str(app_dir.relative_to(ROOT))}


def stop_instance(rel_dir: str, pid) -> dict:
    """앱의 target에서 **특정 PID 인스턴스**를 종료한다(대시보드 밖에서 켠 것 포함). 자동감지로 드러난
    실행 중 인스턴스를 대표가 앱 탭 버튼으로 골라 끄기 위한 경로. cu-helper→/stop, local→taskkill."""
    app_dir = _safe_app_dir(rel_dir)
    front = parse_front(app_dir / MANIFEST)
    cfg = app_targets.resolve(front.get("target"))
    if cfg.get("channel") == "unknown":
        raise ValueError(f"실행 위치(target='{cfg.get('name')}')가 레지스트리에 없습니다")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise ValueError("유효한 pid가 아닙니다")
    if not _dispatch_alive(cfg, pid):
        _proclist_cache.pop(str(rel_dir), None)
        return {"ok": True, "stopped": False, "pid": pid, "reason": "이미 종료됨",
                "channel": cfg["channel"], "target": cfg["name"], "dir": str(app_dir.relative_to(ROOT))}
    _dispatch_stop(cfg, pid)
    for _ in range(10):
        if not _dispatch_alive(cfg, pid):
            break
        time.sleep(0.2)
    stopped = not _dispatch_alive(cfg, pid)
    # 이 pid가 pidfile을 가리키면 청소
    try:
        info = json.loads(_runfile(app_dir).read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == pid:
            _rm_runfile(app_dir)
    except Exception:
        pass
    _proclist_cache.pop(str(rel_dir), None)   # 감지 캐시 무효화
    return {"ok": True, "stopped": stopped, "pid": pid,
            "channel": cfg["channel"], "target": cfg["name"], "dir": str(app_dir.relative_to(ROOT))}


def _write_runfile(app_dir: Path, info: dict) -> None:
    try:
        (app_dir / RUN_DIR).mkdir(exist_ok=True)
        _runfile(app_dir).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _rm_runfile(app_dir: Path) -> None:
    try:
        _runfile(app_dir).unlink()
    except OSError:
        pass
