# -*- coding: utf-8 -*-
"""공간 오케스트레이션 v0 식별자와 세대 관리."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .paths import SPACES
from .transcript import now_iso

DEFAULT_ROOM_GENERATION = 1
INGRESS_TYPES = {"message", "cancel_replan"}


class OrchestrationStaleError(RuntimeError):
    """실행 context가 현재 room_generation과 맞지 않는다."""


def _space_dir(space: str) -> Path:
    return SPACES / space


def _state_path(space: str) -> Path:
    return _space_dir(space) / "orchestration_state.json"


def _lock_path(space: str) -> Path:
    return _space_dir(space) / ".orchestration.lock"


def _intent_ledger_path(space: str) -> Path:
    return _space_dir(space) / "intent_ledger.jsonl"


def _effect_ledger_path(space: str) -> Path:
    return _space_dir(space) / "effect_ledger.jsonl"


def _load_json(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def _atomic_write_json(path: Path, data: dict):
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _with_lock(space: str, fn):
    lock = _lock_path(space)
    lock.touch(exist_ok=True)
    with lock.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _state_without_lock(space: str) -> dict:
    state = _load_json(_state_path(space), {})
    generation = state.get("current_room_generation")
    try:
        generation = int(generation)
    except Exception:
        generation = DEFAULT_ROOM_GENERATION
    if generation < DEFAULT_ROOM_GENERATION:
        generation = DEFAULT_ROOM_GENERATION
    return {
        **state,
        "current_room_generation": generation,
    }


def read_state(space: str) -> dict:
    return _state_without_lock(space)


def current_generation(space: str) -> int:
    return int(read_state(space).get("current_room_generation") or DEFAULT_ROOM_GENERATION)


def _write_state_without_lock(space: str, state: dict) -> dict:
    state = {
        "current_room_generation": DEFAULT_ROOM_GENERATION,
        **state,
        "updated": now_iso(),
    }
    _atomic_write_json(_state_path(space), state)
    return state


def advance_generation(space: str, reason: str, *, source_event_seq=None, source_message_id: str = "") -> dict:
    def mutate():
        state = _state_without_lock(space)
        generation = int(state.get("current_room_generation") or DEFAULT_ROOM_GENERATION) + 1
        event = {
            "type": "room_generation_advanced",
            "room_generation": generation,
            "previous_room_generation": generation - 1,
            "reason": reason,
            "source_event_seq": source_event_seq,
            "source_message_id": source_message_id,
            "at": now_iso(),
        }
        next_state = {
            **state,
            "current_room_generation": generation,
            "last_generation_advance": event,
        }
        _write_state_without_lock(space, next_state)
        _append_jsonl(_effect_ledger_path(space), {
            "effect_id": effect_id("room_generation", space, generation, reason, source_event_seq, source_message_id),
            "effect_type": "room_generation_advanced",
            "space_id": space,
            **event,
        })
        return next_state

    return _with_lock(space, mutate)


def advance_generation_if_current(
    space: str,
    expected_generation: int,
    reason: str,
    *,
    source_event_seq=None,
    source_message_id: str = "",
) -> dict:
    def mutate():
        state = _state_without_lock(space)
        current = int(state.get("current_room_generation") or DEFAULT_ROOM_GENERATION)
        if current != int(expected_generation):
            return {
                **state,
                "advanced": False,
                "expected_room_generation": int(expected_generation),
                "current_room_generation": current,
                "reason": "generation_changed",
            }
        generation = current + 1
        event = {
            "type": "room_generation_advanced",
            "room_generation": generation,
            "previous_room_generation": current,
            "reason": reason,
            "source_event_seq": source_event_seq,
            "source_message_id": source_message_id,
            "at": now_iso(),
        }
        next_state = {
            **state,
            "current_room_generation": generation,
            "last_generation_advance": event,
        }
        _write_state_without_lock(space, next_state)
        _append_jsonl(_effect_ledger_path(space), {
            "effect_id": effect_id("room_generation", space, generation, reason, source_event_seq, source_message_id),
            "effect_type": "room_generation_advanced",
            "space_id": space,
            **event,
        })
        return {**next_state, "advanced": True}

    return _with_lock(space, mutate)


def new_intent_id() -> str:
    return f"intent_{uuid4().hex[:12]}"


def new_thread_id() -> str:
    return f"thread_{uuid4().hex[:12]}"


def effect_id(kind: str, *parts) -> str:
    payload = json.dumps([kind, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    safe_kind = "".join(ch if ch.isalnum() else "_" for ch in kind)[:24] or "effect"
    return f"effect_{safe_kind}_{digest}"


def prepare_ingress(
    space: str,
    text: str,
    requester: str,
    client_message_id: str | None = None,
    *,
    ingress_type: str = "message",
) -> dict:
    ingress_type = str(ingress_type or "message").strip()
    if ingress_type not in INGRESS_TYPES:
        raise ValueError(f"지원하지 않는 ingress_type: {ingress_type}")
    ingress_effect_id = effect_id("ingress", space, client_message_id or uuid4().hex, requester, text)
    ingress_key = f"client:{client_message_id}" if client_message_id else f"effect:{ingress_effect_id}"

    def mutate():
        state = _state_without_lock(space)
        generation = int(state.get("current_room_generation") or DEFAULT_ROOM_GENERATION)
        if ingress_type == "cancel_replan":
            keys = state.get("cancel_replan_ingress_keys") if isinstance(state.get("cancel_replan_ingress_keys"), dict) else {}
            previous = keys.get(ingress_key)
            if previous:
                generation = int(previous.get("room_generation") or generation)
            else:
                generation += 1
                event = {
                    "type": "room_generation_advanced",
                    "room_generation": generation,
                    "previous_room_generation": generation - 1,
                    "reason": "cancel_replan_ingress",
                    "ingress_key": ingress_key,
                    "at": now_iso(),
                }
                keys = {**keys, ingress_key: {
                    "room_generation": generation,
                    "effect_id": ingress_effect_id,
                    "at": event["at"],
                }}
                if len(keys) > 200:
                    keys = dict(list(keys.items())[-200:])
                _write_state_without_lock(space, {
                    **state,
                    "current_room_generation": generation,
                    "cancel_replan_ingress_keys": keys,
                    "last_generation_advance": event,
                })
                _append_jsonl(_effect_ledger_path(space), {
                    "effect_id": effect_id("room_generation", space, ingress_key, generation),
                    "effect_type": "room_generation_advanced",
                    "space_id": space,
                    **event,
                })
        return {
            "space_id": space,
            "intent_id": new_intent_id(),
            "conversation_thread_id": new_thread_id(),
            "room_generation": generation,
            "ingress_type": ingress_type,
            "cancel_replan_fence": ingress_type == "cancel_replan",
            "effect_id": ingress_effect_id,
            "ingress_key": ingress_key,
        }

    return _with_lock(space, mutate)


def record_intent(space: str, stored_message: dict):
    intent_id = stored_message.get("intent_id")
    if not intent_id:
        return
    _append_jsonl(_intent_ledger_path(space), {
        "intent_id": intent_id,
        "root_message_id": stored_message.get("message_id", ""),
        "root_event_seq": stored_message.get("event_seq"),
        "conversation_thread_id": stored_message.get("conversation_thread_id", ""),
        "intent_state": "active",
        "current_room_generation": stored_message.get("room_generation"),
        "ingress_type": stored_message.get("ingress_type", "message"),
        "cancel_replan_fence": bool(stored_message.get("cancel_replan_fence")),
        "created_at": now_iso(),
    })


def context_from_message(message: dict | None, space: str) -> dict:
    message = message or {}
    return {
        "space_id": space,
        "intent_id": message.get("intent_id", ""),
        "conversation_thread_id": message.get("conversation_thread_id", ""),
        "room_generation": message.get("room_generation") or current_generation(space),
        "source_event_seq": message.get("event_seq"),
        "source_message_id": message.get("message_id", ""),
        "reply_to_message_id": message.get("message_id", ""),
    }


def is_context_stale(space: str, context: dict | None) -> bool:
    if not context:
        return False
    try:
        seen = int(context.get("room_generation") or DEFAULT_ROOM_GENERATION)
    except Exception:
        seen = DEFAULT_ROOM_GENERATION
    return seen != current_generation(space)


def run_with_context_guard(space: str, context: dict | None, fn):
    def mutate():
        state = _state_without_lock(space)
        try:
            seen = int((context or {}).get("room_generation") or DEFAULT_ROOM_GENERATION)
        except Exception:
            seen = DEFAULT_ROOM_GENERATION
        current = int(state.get("current_room_generation") or DEFAULT_ROOM_GENERATION)
        if seen != current:
            raise OrchestrationStaleError("room_generation mismatch")
        return fn()

    return _with_lock(space, mutate)


def append_effect(space: str, effect: dict):
    data = {"space_id": space, "at": now_iso(), **effect}
    if not data.get("effect_id"):
        data["effect_id"] = effect_id(data.get("effect_type", "effect"), space, data)
    _append_jsonl(_effect_ledger_path(space), data)


def effect_exists(space: str, wanted_effect_id: str) -> bool:
    if not wanted_effect_id:
        return False
    path = _effect_ledger_path(space)
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("effect_id") == wanted_effect_id:
            return True
    return False
