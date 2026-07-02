# -*- coding: utf-8 -*-
"""앱 실행 타깃 레지스트리 — 앱이 '어디서' 실행되는지(서버/원격 윈도우/VM)를 해석한다.

앱.md의 `target`(별칭)을 이 레지스트리로 풀어 채널·연결정보를 얻는다. 서버 컴퓨터를
중앙 컨트롤센터로 삼아, 한 앱 탭에서 로컬·원격·VM 앱을 구분해 실행하기 위한 토대다.

채널(channel):
- local      : 서버 컴퓨터(이 호스트)에서 subprocess로 실행. (항상 존재)
- cu-helper  : VM/원격 윈도우에서 도는 cu_helper.ps1 HTTP 데몬에 /run·/stop·/ps로 디스패치.
- ssh        : OpenSSH가 있는 원격에 `ssh <ssh> <cmd>`로 실행(추적 제한).
- parallels  : 이 Mac의 Parallels 게스트에 `prlctl exec <vm> <cmd>`로 실행(추적 제한).

보안(law.md §7): 실제 host/IP·포트·VM명·자격증명은 대외비다. 공개 경로엔 스키마/예시만,
실값은 `자산/대외비/앱실행대상/targets.json`(gitignore)에 둔다. 절대경로 하드코딩 금지.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .paths import ROOT

# 실 타깃 레지스트리(대외비). 없으면 local만 존재.
_REGISTRY_CANDIDATES = [
    os.environ.get("APP_TARGETS_JSON", ""),
    str(ROOT / "자산" / "대외비" / "앱실행대상" / "targets.json"),
]


def _local_label() -> str:
    """서버 컴퓨터 라벨에 OS를 자동반영(워크스페이스가 mac↔Win 이전돼도 자동 — 하드코딩 금지)."""
    if os.name == "nt":
        return "서버 컴퓨터 (Windows)"
    if sys.platform == "darwin":
        return "서버 컴퓨터 (macOS)"
    return "서버 컴퓨터"


# 서버 컴퓨터(이 호스트) — 항상 존재하는 local 타깃. 원격제어(cu_remote local 채널) 대상이기도 하다.
LOCAL = {"name": "local", "channel": "local", "label": _local_label()}
VALID_CHANNELS = {"local", "cu-helper", "ssh", "parallels"}

# UI 표시용(채널→아이콘/한글)
CHANNEL_META = {
    "local":     {"icon": "🖥️", "ko": "서버"},
    "cu-helper": {"icon": "🪟", "ko": "원격/VM"},
    "ssh":       {"icon": "🔌", "ko": "원격(SSH)"},
    "parallels": {"icon": "📦", "ko": "VM"},
}


def _load_registry() -> list[dict]:
    for cand in _REGISTRY_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            targets = data.get("targets") if isinstance(data, dict) else data
            if isinstance(targets, list):
                return [t for t in targets if isinstance(t, dict) and t.get("name")]
    return []


def resolve(name: str | None) -> dict:
    """타깃 별칭 → 설정 dict. 빈/`local`은 항상 LOCAL. 미등록이면 unknown 채널로 표시.

    반환 dict는 항상 `name`·`channel`을 가진다. 원격은 host/port/vm/ssh 등 채널별 필드 포함.
    """
    name = (name or "").strip()
    if not name or name.lower() == "local":
        return dict(LOCAL)
    for t in _load_registry():
        if t.get("name") == name:
            cfg = dict(t)
            ch = (cfg.get("channel") or "").strip().lower()
            cfg["channel"] = ch if ch in VALID_CHANNELS else "unknown"
            cfg.setdefault("label", name)
            return cfg
    # 앱.md엔 target이 적혔는데 레지스트리에 없음 → 미구성으로 표시(실행 시 안내)
    return {"name": name, "channel": "unknown", "label": name, "unconfigured": True}


def list_targets() -> list[dict]:
    """알려진 타깃 목록(local + 레지스트리) — **공개 안전 필드만**.

    ★ host/IP·port·ssh·vm 같은 연결정보는 대외비(law.md §7)라 절대 응답에 싣지 않는다.
    대시보드/스킬엔 name·channel·label만 노출한다(연결정보는 서버 내부 resolve에서만 쓴다)."""
    out = [{"name": LOCAL["name"], "channel": LOCAL["channel"], "label": LOCAL["label"]}]
    for t in _load_registry():
        out.append({
            "name": t.get("name", ""),
            "channel": (t.get("channel") or "").strip().lower() or "unknown",
            "label": t.get("label", t.get("name", "")),
        })
    return out


def display(cfg: dict) -> dict:
    """카드 배지용 표시 정보."""
    ch = cfg.get("channel", "local")
    meta = CHANNEL_META.get(ch, {"icon": "❔", "ko": ch})
    name = cfg.get("name", "local")
    label = cfg.get("label", name)
    is_local = ch == "local"
    text = "서버" if is_local else (label or name)
    return {
        "name": name,
        "channel": ch,
        "icon": meta["icon"],
        "text": text,
        "is_local": is_local,
        "unconfigured": bool(cfg.get("unconfigured")),
    }
