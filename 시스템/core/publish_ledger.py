# -*- coding: utf-8 -*-
"""공개 말풍선 publish ledger와 append gate."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .paths import SPACES, ROOT

_ROOT_STR = str(ROOT)


def _to_root_relative_paths(content: str) -> str:
    """공개 말풍선의 파일 경로를 '루트기준 상대경로'로 정규화한다(공개 단일 지점).

    에이전트(특히 Gemini)가 산출물을 `file:///Users/.../CnvAgentWorld/<rel>` 같은 절대경로·file:// URL로
    적으면 (1) 대시보드 미리보기가 동작 안 하고(raw API는 루트상대만 serve) (2) /Users/<이름> 사용자명이
    공개로 노출된다. ROOT 접두사를 떼어 상대경로로 바꾼다 — 스킬 가이드를 모델이 어겨도 시스템이 보정.
    """
    if not content or _ROOT_STR not in content:
        return content
    root = _ROOT_STR
    return (content
            .replace("file://" + root + "/", "")
            .replace("file://" + root, "")
            .replace(root + "/", "")
            .replace(root, ""))
from .transcript import now_iso, record
from . import manager_claim, orchestration


class PublishLedgerError(RuntimeError):
    """publish ledger 계약을 만족하지 못해 공개 append를 거절했다."""


def _ledger_path(space: str) -> Path:
    return SPACES / space / "publish_ledger.jsonl"


def _lock_path(space: str) -> Path:
    return SPACES / space / ".publish_ledger.lock"


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _rows_with_error(space: str) -> tuple[list[dict], str]:
    path = _ledger_path(space)
    if not path.exists():
        return [], ""
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return [], f"{path.name}: {type(exc).__name__}"
    bad_lines = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            bad_lines += 1
            continue
        if isinstance(row, dict):
            out.append(row)
        else:
            bad_lines += 1
    if bad_lines:
        return out, f"{path.name}: invalid_json_lines={bad_lines}"
    return out, ""


def _rows(space: str) -> list[dict]:
    rows, error = _rows_with_error(space)
    if error:
        raise PublishLedgerError(error)
    return rows


def _latest_by_effect(space: str, publish_effect_id: str) -> dict:
    for row in reversed(_rows(space)):
        if row.get("publish_effect_id") == publish_effect_id:
            return row
    return {}


def _payload_hash(*, speaker_name: str, speaker_code: str, role: str, content: str, context: dict, extra: dict | None) -> str:
    payload = {
        "speaker_name": speaker_name,
        "speaker_code": speaker_code,
        "role": role,
        "content": content,
        "context": _context_fields(context),
        "extra": extra or {},
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _with_lock(space: str, fn):
    lock = _lock_path(space)
    lock.touch(exist_ok=True)
    with lock.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def deterministic_published_message_id(space: str, publish_effect_id: str) -> str:
    payload = json.dumps(["published_message", space, publish_effect_id], ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"msg_pub_{digest}"


def _claim_contract(manager_claim_token: str, claim: dict | None = None) -> dict:
    claim = dict(claim or {})
    if manager_claim_token and not claim.get("claim_token"):
        claim["claim_token"] = manager_claim_token
    return {
        "claim_token": claim.get("claim_token", ""),
        "fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
    }


def _validate_current_manager(space: str, claim: dict):
    missing = [k for k in ("claim_token", "fencing_token", "owner_boot_id") if not claim.get(k)]
    if missing:
        raise PublishLedgerError("manager_claim contract missing: " + ", ".join(missing))
    if not manager_claim.is_current(space, claim):
        raise PublishLedgerError("manager_claim_not_current")


def claim_publish(
    space: str,
    *,
    publish_effect_id: str,
    manager_claim_token: str,
    manager_claim_context: dict | None = None,
    context: dict,
    publisher: str,
    speaker: str,
) -> dict:
    if not publish_effect_id:
        raise PublishLedgerError("publish_effect_id required")
    if not manager_claim_token:
        raise PublishLedgerError("manager_claim_token required")
    published_message_id = deterministic_published_message_id(space, publish_effect_id)
    claim_contract = _claim_contract(manager_claim_token, manager_claim_context)

    def mutate():
        latest = _latest_by_effect(space, publish_effect_id)
        if latest.get("published_message_id") and latest.get("published_message_id") != published_message_id:
            raise PublishLedgerError("published_message_id is not deterministic")
        if latest.get("state") == "committed":
            _validate_current_manager(space, claim_contract)
            return {**latest, "already_committed": True}
        _validate_current_manager(space, claim_contract)
        claim = {
            "state": "claimed",
            "publish_ledger_claim": f"pledger_{uuid4().hex[:12]}",
            "publish_effect_id": publish_effect_id,
            "published_message_id": published_message_id,
            "manager_claim_token": claim_contract["claim_token"],
            "manager_fencing_token": claim_contract["fencing_token"],
            "owner_boot_id": claim_contract["owner_boot_id"],
            "publisher": publisher,
            "speaker": speaker,
            "claimed_at": now_iso(),
            **_context_fields(context),
        }
        _append_jsonl(_ledger_path(space), claim)
        return claim

    return _with_lock(space, mutate)


def _context_fields(context: dict | None) -> dict:
    context = context or {}
    return {
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "reply_to_message_id": context.get("reply_to_message_id", ""),
    }


def _validate_required(
    *,
    publish_effect_id: str,
    publish_ledger_claim: str,
    manager_claim_token: str,
    published_message_id: str,
    intent_stale_guard_passed: bool,
):
    missing = []
    if not publish_effect_id:
        missing.append("publish_effect_id")
    if not publish_ledger_claim:
        missing.append("publish_ledger_claim")
    if not manager_claim_token:
        missing.append("manager_claim_token")
    if not published_message_id:
        missing.append("published_message_id")
    if not intent_stale_guard_passed:
        missing.append("intent_stale_guard_passed")
    if missing:
        raise PublishLedgerError("append_public_message missing: " + ", ".join(missing))


def append_public_message(
    space: str,
    *,
    publish_effect_id: str,
    publish_ledger_claim: str,
    manager_claim_token: str,
    manager_claim_context: dict | None = None,
    published_message_id: str,
    intent_stale_guard_passed: bool,
    speaker_name: str,
    speaker_code: str,
    role: str,
    content: str,
    context: dict,
    extra: dict | None = None,
) -> dict:
    _validate_required(
        publish_effect_id=publish_effect_id,
        publish_ledger_claim=publish_ledger_claim,
        manager_claim_token=manager_claim_token,
        published_message_id=published_message_id,
        intent_stale_guard_passed=intent_stale_guard_passed,
    )
    expected_message_id = deterministic_published_message_id(space, publish_effect_id)
    if published_message_id != expected_message_id:
        raise PublishLedgerError("published_message_id is not deterministic")
    content = _to_root_relative_paths(content)   # 절대경로/file:// → 루트상대(미리보기 동작 + 사용자명 노출 차단)
    claim_contract = _claim_contract(manager_claim_token, manager_claim_context)
    publish_payload_hash = _payload_hash(
        speaker_name=speaker_name,
        speaker_code=speaker_code,
        role=role,
        content=content,
        context=context,
        extra=extra,
    )

    def mutate():
        latest = _latest_by_effect(space, publish_effect_id)
        if latest.get("state") == "committed":
            if latest.get("publish_payload_hash") and latest.get("publish_payload_hash") != publish_payload_hash:
                raise PublishLedgerError("idempotency_payload_mismatch")
            if latest.get("publish_ledger_claim") != publish_ledger_claim:
                raise PublishLedgerError("publish_ledger_claim mismatch")
            if latest.get("manager_claim_token") != manager_claim_token:
                raise PublishLedgerError("manager_claim_token mismatch")
            if latest.get("published_message_id") != published_message_id:
                raise PublishLedgerError("published_message_id mismatch")
            if latest.get("manager_fencing_token") and latest.get("manager_fencing_token") != claim_contract.get("fencing_token"):
                raise PublishLedgerError("manager_fencing_token mismatch")
            if latest.get("owner_boot_id") and latest.get("owner_boot_id") != claim_contract.get("owner_boot_id"):
                raise PublishLedgerError("owner_boot_id mismatch")
            _validate_current_manager(space, claim_contract)
            return {
                "ok": True,
                "duplicate": True,
                "record": {
                    "message_id": latest.get("published_message_id", published_message_id),
                    "event_seq": latest.get("event_seq"),
                },
                "ledger": latest,
            }
        if latest.get("publish_ledger_claim") != publish_ledger_claim:
            raise PublishLedgerError("publish_ledger_claim mismatch")
        if latest.get("manager_claim_token") != manager_claim_token:
            raise PublishLedgerError("manager_claim_token mismatch")
        if latest.get("published_message_id") != published_message_id:
            raise PublishLedgerError("published_message_id mismatch")
        if latest.get("manager_fencing_token") and latest.get("manager_fencing_token") != claim_contract.get("fencing_token"):
            raise PublishLedgerError("manager_fencing_token mismatch")
        if latest.get("owner_boot_id") and latest.get("owner_boot_id") != claim_contract.get("owner_boot_id"):
            raise PublishLedgerError("owner_boot_id mismatch")

        def guarded_commit():
            _validate_current_manager(space, claim_contract)
            rec = record(space, {
                "시각": now_iso(),
                "공간": space,
                "화자": speaker_name,
                "코드": speaker_code,
                "역할": role,
                "내용": content,
                "message_id": published_message_id,
                "effect_id": publish_effect_id,
                "publish_effect_id": publish_effect_id,
                "publish_ledger_claim": publish_ledger_claim,
                "manager_claim_token": claim_contract["claim_token"],
                "manager_fencing_token": claim_contract["fencing_token"],
                "owner_boot_id": claim_contract["owner_boot_id"],
                "published_by_manager_effect_id": publish_effect_id,
                "intent_stale_guard_passed": True,
                "publish_payload_hash": publish_payload_hash,
                **_context_fields(context),
                **(extra or {}),
            }, dedupe_effect_id=True)
            stored = rec.get("record") or {}
            if stored.get("message_id") != published_message_id:
                raise PublishLedgerError("existing published message id mismatch")
            if rec.get("duplicate"):
                stored_hash = stored.get("publish_payload_hash")
                if stored_hash and stored_hash != publish_payload_hash:
                    raise PublishLedgerError("idempotency_payload_mismatch")
                if not stored_hash and (
                    stored.get("내용") != content
                    or stored.get("화자") != speaker_name
                    or stored.get("코드") != speaker_code
                    or stored.get("역할") != role
                ):
                    raise PublishLedgerError("idempotency_payload_mismatch")
            committed = {
                **latest,
                "state": "committed",
                "committed_at": now_iso(),
                "event_seq": stored.get("event_seq"),
                "published_message_id": stored.get("message_id", published_message_id),
                "publish_payload_hash": publish_payload_hash,
                "duplicate": bool(rec.get("duplicate")),
                "duplicate_by": rec.get("duplicate_by", ""),
            }
            _append_jsonl(_ledger_path(space), committed)
            return {"ok": True, "duplicate": bool(rec.get("duplicate")), "record": stored, "ledger": committed}

        try:
            return orchestration.run_with_context_guard(space, context, guarded_commit)
        except orchestration.OrchestrationStaleError as exc:
            raise PublishLedgerError("intent_stale_guard_failed") from exc

    return _with_lock(space, mutate)


def snapshot(space: str) -> dict:
    rows, error = _rows_with_error(space)
    latest_by_effect = {}
    for row in rows:
        effect = row.get("publish_effect_id")
        if effect:
            latest_by_effect[effect] = row
    counts = {}
    for row in latest_by_effect.values():
        state = row.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
    latest = rows[-5:]
    return {
        "counts": counts,
        "effect_count": len(latest_by_effect),
        "latest": latest,
        "last_publish_effect_id": latest[-1].get("publish_effect_id", "") if latest else "",
        "ledger_corrupt": bool(error),
        "ledger_errors": [error] if error else [],
    }
