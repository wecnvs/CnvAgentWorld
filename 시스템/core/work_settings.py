# -*- coding: utf-8 -*-
"""작업 실행/감시 정책 저장과 해석."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .paths import PEOPLE, SPACES


SETTINGS_FILENAME = "작업실행설정.json"

DEFAULT_WORK_SETTINGS = {
    # 작업(work)은 채팅보다 무겁고 느린 엔진(예: Gemini)이 한 호출에 여러 파일을 읽고 쓰며 마지막 정리·
    # release까지 한다. 300s는 좋은 산출을 내고도 막판에 타임아웃→error로 끝나기 일쑤였다(라이브 실증).
    # 600s로 둬 단계 청크가 한 호출에 깨끗이 끝나게 한다. 무한이 아니라 유한 — 무진행 재시도·이어가기·
    # 상한이 총 비용을 가둔다. (작업 단위는 law_work.md '큰 작업은 쪼갠다'로 작게 유지하는 게 우선.)
    "runner_timeout_sec": 600,
    "heartbeat_interval_sec": 10,
    "heartbeat_stale_ms": 60_000,
    "progress_report_due_ms": 60_000,
}

SETTING_KEYS = tuple(DEFAULT_WORK_SETTINGS.keys())

BOUNDS = {
    "runner_timeout_sec": (30, 7200),
    "heartbeat_interval_sec": (1, 300),
    "heartbeat_stale_ms": (5_000, 3_600_000),
    "progress_report_due_ms": (5_000, 3_600_000),
}


def _settings_path(folder: Path) -> Path:
    return folder / SETTINGS_FILENAME


def _coerce_int(value, fallback: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = fallback
    return max(minimum, min(maximum, number))


def normalize_settings(raw: dict | None) -> dict:
    raw = raw or {}
    data = dict(DEFAULT_WORK_SETTINGS)
    for key, fallback in DEFAULT_WORK_SETTINGS.items():
        minimum, maximum = BOUNDS[key]
        data[key] = _coerce_int(raw.get(key, fallback), fallback, minimum=minimum, maximum=maximum)
    # report due는 heartbeat stale보다 짧으면 작업자가 계속 부분보고만 받게 되므로 최소 stale 기준에 맞춘다.
    data["progress_report_due_ms"] = max(data["progress_report_due_ms"], data["heartbeat_stale_ms"])
    data["schema"] = "WorkExecutionSettings.v1"
    if raw.get("source_chain") is not None:
        data["source_chain"] = raw.get("source_chain")
    if raw.get("settings_source") is not None:
        data["settings_source"] = raw.get("settings_source")
    if raw.get("source") is not None:
        data["source"] = raw.get("source")
    if raw.get("configured_keys") is not None:
        keys = raw.get("configured_keys")
        data["configured_keys"] = [key for key in keys if key in DEFAULT_WORK_SETTINGS] if isinstance(keys, list) else []
    return data


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_folder_settings(folder: Path) -> dict:
    path = _settings_path(folder)
    data = normalize_settings(_read_file(path))
    data["settings_path"] = str(path)
    data["settings_source"] = "file" if path.exists() else "default_missing_file"
    return data


def write_folder_settings(folder: Path, settings: dict | None = None, *, source: str = "") -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    path = _settings_path(folder)
    current = _read_file(path)
    settings = settings or {}
    explicit_configured_keys = "configured_keys" in settings and isinstance(settings.get("configured_keys"), list)
    if explicit_configured_keys:
        configured_keys = {key for key in settings.get("configured_keys", []) if key in DEFAULT_WORK_SETTINGS}
    elif "configured_keys" in current and isinstance(current.get("configured_keys"), list):
        configured_keys = {key for key in current.get("configured_keys", []) if key in DEFAULT_WORK_SETTINGS}
    elif path.exists():
        configured_keys = {key for key in DEFAULT_WORK_SETTINGS if key in current}
    else:
        configured_keys = set()
    if "configured_keys" not in settings:
        configured_keys.update(key for key in settings if key in DEFAULT_WORK_SETTINGS and settings.get(key) is not None)
    if explicit_configured_keys:
        configured_values = {
            key: settings.get(key, current.get(key))
            for key in configured_keys
            if settings.get(key, current.get(key)) is not None
        }
        data = normalize_settings({**configured_values, "configured_keys": sorted(configured_keys)})
    else:
        data = normalize_settings({**current, **(settings or {})})
    data["configured_keys"] = sorted(configured_keys)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    if source:
        data["source"] = source
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**data, "settings_path": str(path), "settings_source": "file"}


def read_space_settings(space: str) -> dict:
    return read_folder_settings(SPACES / space)


def write_space_settings(space: str, settings: dict | None = None, *, source: str = "") -> dict:
    return write_folder_settings(SPACES / space, settings, source=source or f"space-work-settings:{space}")


def read_person_settings(person: str) -> dict:
    return read_folder_settings(PEOPLE / person)


def write_person_settings(person: str, settings: dict | None = None, *, source: str = "") -> dict:
    return write_folder_settings(PEOPLE / person, settings, source=source or f"person-work-settings:{person}")


def read_seat_settings(person: str, space: str) -> dict:
    return read_folder_settings(PEOPLE / person / "공간" / space)


def write_seat_settings(person: str, space: str, settings: dict | None = None, *, source: str = "") -> dict:
    return write_folder_settings(
        PEOPLE / person / "공간" / space,
        settings,
        source=source or f"seat-work-settings:{person}->{space}",
    )


def resolve_work_settings(space: str, worker: str = "", work_dir: Path | None = None) -> dict:
    merged = dict(DEFAULT_WORK_SETTINGS)
    source_chain = ["defaults"]
    candidates = [(SPACES / space, f"space:{space}")]
    if worker:
        candidates.append((PEOPLE / worker, f"person:{worker}"))
        candidates.append((PEOPLE / worker / "공간" / space, f"seat:{worker}->{space}"))
    if work_dir is not None:
        candidates.append((work_dir, f"work:{work_dir.name}"))
    for folder, label in candidates:
        path = _settings_path(folder)
        if path.exists():
            raw = _read_file(path)
            configured_keys = raw.get("configured_keys")
            if isinstance(configured_keys, list):
                keys = [key for key in configured_keys if key in DEFAULT_WORK_SETTINGS]
            else:
                keys = [key for key in DEFAULT_WORK_SETTINGS if key in raw]
            merged.update({key: raw[key] for key in keys if key in raw})
            if keys:
                source_chain.append(label)
    data = normalize_settings(merged)
    data["source_chain"] = source_chain
    data["settings_source"] = source_chain[-1]
    return data
