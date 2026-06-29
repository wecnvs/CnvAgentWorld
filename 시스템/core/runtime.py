# -*- coding: utf-8 -*-
"""에이전트 실행 런타임(엔진·모델) 저장/해석."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

VALID_ENGINES = ("claude", "gemini", "codex", "gemma")

ENGINE_MODELS = {
    "claude": [
        "claude-opus-4-8",
        "claude-opus-4-8[1m]",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "opus",
        "sonnet",
        "haiku",
    ],
    "gemini": [
        "Gemini 3.1 Pro (High)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (Low)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)",
    ],
    "codex": [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "codex-auto-review",
    ],
    "gemma": [
        "gemma4:e4b",
        "gemma4",
        "gemma",
    ],
}

ENGINE_ORDER = ["claude", "gemini", "codex", "gemma"]
ENGINE_DEFAULT_MODEL = {engine: (models[0] if models else "") for engine, models in ENGINE_MODELS.items()}
DEFAULT_RUNTIME = {"engine": "claude", "model": ENGINE_DEFAULT_MODEL["claude"]}

_MODEL_EXEC_ALIASES = {
    "codex": {
        "5.3spark": "gpt-5.3-codex-spark",
        "gpt-5.3-spark": "gpt-5.3-codex-spark",
    },
}


def normalize_engine(engine: str | None) -> str:
    value = (engine or DEFAULT_RUNTIME["engine"]).strip().lower()
    if value not in VALID_ENGINES:
        raise ValueError(f"미지원 엔진: {value} (허용: {', '.join(VALID_ENGINES)})")
    return value


def normalize_model(engine: str, model: str | None) -> str:
    value = (model or "").strip()
    return value or ENGINE_DEFAULT_MODEL.get(engine, "")


def model_for_cli(engine: str, model: str | None) -> str:
    raw = (model or "").strip()
    return _MODEL_EXEC_ALIASES.get(engine, {}).get(raw, raw)


def catalog() -> dict:
    return {
        "engines": ENGINE_ORDER,
        "models": ENGINE_MODELS,
        "default": ENGINE_DEFAULT_MODEL,
    }


def read_runtime(folder: Path) -> dict:
    path = folder / "agent_runtime.json"
    if not path.exists():
        return {**DEFAULT_RUNTIME, "runtime_source": "default_missing_file", "runtime_path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            **DEFAULT_RUNTIME,
            "runtime_source": "default_read_error",
            "runtime_path": str(path),
            "runtime_error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    try:
        engine = normalize_engine(data.get("engine"))
        model = normalize_model(engine, data.get("model"))
    except Exception as exc:
        return {
            **DEFAULT_RUNTIME,
            "runtime_source": "default_invalid_runtime",
            "runtime_path": str(path),
            "runtime_error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    # 작업용 모델 분리(선택): 평소 채팅은 빠른 모델, 실제 작업(engine.work)만 강한 모델로.
    work_engine = ""
    work_model = ""
    raw_work_engine = (data.get("work_engine") or "").strip()
    raw_work_model = (data.get("work_model") or "").strip()
    if raw_work_engine or raw_work_model:
        try:
            work_engine = normalize_engine(raw_work_engine or engine)
            work_model = normalize_model(work_engine, raw_work_model or None)
        except Exception:
            work_engine = ""
            work_model = ""
    return {
        "engine": engine,
        "model": model,
        "work_engine": work_engine,
        "work_model": work_model,
        "updated": data.get("updated", ""),
        "source": data.get("source", ""),
        "runtime_source": "agent_runtime_file",
        "runtime_path": str(path),
    }


def write_runtime(
    folder: Path,
    engine: str | None = None,
    model: str | None = None,
    *,
    source: str = "",
    work_engine: str | None = None,
    work_model: str | None = None,
) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    engine_value = normalize_engine(engine)
    model_value = normalize_model(engine_value, model)
    data = {
        "engine": engine_value,
        "model": model_value,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    # 작업용 모델 분리(선택). 둘 중 하나라도 주면 기록한다.
    if work_engine or work_model:
        we = normalize_engine(work_engine or engine_value)
        data["work_engine"] = we
        data["work_model"] = normalize_model(we, work_model or None)
    if source:
        data["source"] = source
    (folder / "agent_runtime.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return data


def resolve_work_runtime(folder: Path, engine: str | None = None, model: str | None = None) -> dict:
    """작업(engine.work)이 쓸 런타임. 명시 override > 자리의 work_*(채팅과 분리) > 채팅 런타임(현행).

    → 평소 채팅은 빠른 모델로 두고, 실제 작업만 강한 모델로 돌릴 수 있다(미설정 시 무회귀).
    """
    stored = read_runtime(folder)
    if engine or model:
        eng = normalize_engine(engine or stored.get("engine"))
        return {"engine": eng, "model": normalize_model(eng, model if model is not None else stored.get("model"))}
    if stored.get("work_engine") or stored.get("work_model"):
        eng = normalize_engine(stored.get("work_engine") or stored.get("engine"))
        return {"engine": eng, "model": normalize_model(eng, stored.get("work_model") or None)}
    return {"engine": stored["engine"], "model": stored["model"]}


def copy_runtime(src: Path, dst: Path, *, source: str = "") -> dict:
    data = read_runtime(src)
    return write_runtime(dst, data["engine"], data["model"], source=source)


def resolve_runtime(folder: Path, engine: str | None = None, model: str | None = None) -> dict:
    stored = read_runtime(folder)
    engine_value = normalize_engine(engine or stored.get("engine"))
    model_value = normalize_model(engine_value, model if model is not None else stored.get("model"))
    return {"engine": engine_value, "model": model_value}
