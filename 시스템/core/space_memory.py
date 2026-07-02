# -*- coding: utf-8 -*-
"""공간 대화 맥락 projection.

LLM 요약을 붙이기 전의 v0 정본이다. 전체 대화를 매 wake에 읽히지 않도록
event_seq 기준으로 최신 대화, 대표 요청, 관련 source ref를 bounded JSON으로 만든다.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .paths import SPACES
from .transcript import now_iso, read


PROJECTION_SCHEMA = "SpaceMemoryProjection.v1"
MAX_TEXT_CHARS = 700
# 에이전트가 최근 대화·대표 지시를 충분히 보고 판단하도록 누적 컨텍스트 폭을 넓게 잡는다.
# (대표 요구: 최근 대화 20개 더 전달, 대표 발언은 전부 누적 전달)
MAX_ACTIVE_CONTEXT = 32
MAX_REPRESENTATIVE_REQUESTS = 20
MAX_RELEVANT_PAST = 12
MAX_SOURCE_REFS = 40
MAX_USER_DIRECTIVES = 40
MAX_TOPIC_THREADS = 16
MAX_THREAD_ITEMS = 4
# 토픽 active/dormant 판정 폭. 에이전트에 전달하는 최근대화 폭(MAX_ACTIVE_CONTEXT)과
# 분리한다 — 최근대화는 넓게 주되, 토픽 dormancy 임계는 별도로 좁게 유지한다.
MAX_ACTIVE_TOPIC_SPAN = 10


def _space_dir(space: str) -> Path:
    return SPACES / space


def projection_path(space: str) -> Path:
    return _space_dir(space) / "memory" / "projection.json"


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _checksum(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _row_event_seq(row: dict, fallback: int = 0) -> int:
    seq = _as_int(row.get("event_seq"))
    return seq if seq > 0 else fallback


def _preview(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    clean = str(text or "").replace("\r", "\n").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def _message_item(row: dict, fallback: int) -> dict:
    return {
        "event_seq": _row_event_seq(row, fallback),
        "message_id": row.get("message_id", ""),
        "speaker": row.get("화자", ""),
        "role": row.get("역할", ""),
        "content_preview": _preview(row.get("내용", "")),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
    }


def _thread_id(row: dict, fallback: int) -> str:
    thread = str(row.get("conversation_thread_id") or "").strip()
    if thread:
        return thread
    intent = str(row.get("intent_id") or "").strip()
    if intent:
        return intent
    seq = _row_event_seq(row, fallback)
    return f"event:{seq}"


def _user_directive_items(user_rows: list[tuple[int, dict]]) -> list[dict]:
    items = []
    selected = user_rows[-MAX_USER_DIRECTIVES:]
    total = len(selected)
    for offset, (idx, row) in enumerate(selected):
        event_seq = _row_event_seq(row, idx)
        item = _message_item(row, idx)
        item.update({
            "directive_id": _stable_id("directive", item.get("message_id") or event_seq, item.get("content_preview", "")),
            "thread_id": _thread_id(row, idx),
            "status": "active_until_superseded_or_archived",
            "precedence_rank": total - offset,
            "precedence_hint": "later_user_message_takes_precedence_only_when_conflict_is_confirmed",
        })
        items.append(item)
    return items


def _topic_threads(indexed_rows: list[tuple[int, dict]], latest_seq: int) -> list[dict]:
    groups: dict[str, dict] = {}
    for idx, row in indexed_rows:
        thread_id = _thread_id(row, idx)
        group = groups.setdefault(thread_id, {
            "thread_id": thread_id,
            "intent_id": row.get("intent_id", ""),
            "conversation_thread_id": row.get("conversation_thread_id", ""),
            "first_event_seq": _row_event_seq(row, idx),
            "latest_event_seq": _row_event_seq(row, idx),
            "message_count": 0,
            "user_message_count": 0,
            "assistant_message_count": 0,
            "latest_user_request": "",
            "latest_assistant_reply": "",
            "recent_items": [],
        })
        seq = _row_event_seq(row, idx)
        group["latest_event_seq"] = max(_as_int(group.get("latest_event_seq")), seq)
        group["message_count"] = _as_int(group.get("message_count")) + 1
        if row.get("역할") == "user":
            group["user_message_count"] = _as_int(group.get("user_message_count")) + 1
            group["latest_user_request"] = _preview(row.get("내용", ""), 360)
        elif row.get("역할") == "assistant":
            group["assistant_message_count"] = _as_int(group.get("assistant_message_count")) + 1
            group["latest_assistant_reply"] = _preview(row.get("내용", ""), 360)
        group["recent_items"].append(_message_item(row, idx))
        group["recent_items"] = group["recent_items"][-MAX_THREAD_ITEMS:]

    topics = []
    active_floor = max(0, latest_seq - MAX_ACTIVE_TOPIC_SPAN + 1)
    for group in groups.values():
        latest = _as_int(group.get("latest_event_seq"))
        event_gap = max(0, latest_seq - latest)
        status = "active" if latest >= active_floor else "dormant"
        topics.append({
            **group,
            "status": status,
            "freshness_clock": "event_seq",
            "event_gap": event_gap,
        })
    topics.sort(key=lambda item: (_as_int(item.get("latest_event_seq")), _as_int(item.get("message_count"))), reverse=True)
    return topics[:MAX_TOPIC_THREADS]


# active/dormant_topic_threads 뷰가 유지할 필드 — 소비처 전수 조사 결과
# (room_manager._prompt_room_status_snapshot 의 _slim_rows 필드 목록, context_pack.turn_handoff_brief 의
#  topic_lines 가 읽는 필드)의 합집합. recent_items(스레드당 4건 × 700자 미리보기)는 어느 소비처도
# 안 읽으므로 뷰에서 제외한다 — 전문은 topic_threads 에만 남는다.
_TOPIC_VIEW_KEYS = (
    "thread_id", "status", "latest_event_seq", "first_event_seq", "event_gap",
    "message_count", "user_message_count", "assistant_message_count",
    "latest_user_request", "latest_assistant_reply",
    "freshness_clock", "intent_id", "conversation_thread_id",
)


def _topic_view(item: dict) -> dict:
    return {key: item[key] for key in _TOPIC_VIEW_KEYS if key in item}


def _source_ref(row: dict, fallback: int) -> dict:
    return {
        "event_seq": _row_event_seq(row, fallback),
        "message_id": row.get("message_id", ""),
        "speaker": row.get("화자", ""),
        "role": row.get("역할", ""),
        "intent_id": row.get("intent_id", ""),
        "conversation_thread_id": row.get("conversation_thread_id", ""),
        "room_generation": row.get("room_generation"),
    }


# ── 자동 누적요약 ─────────────────────────────────────────────────────────────
# 요약.md 갱신은 원래 사회자 LLM의 update_summary 재량에 100% 의존했는데, 실방(레빗_bcd7)에서
# 23시간·대화 66건 동안 단 한 번도 호출되지 않아 요약이 빈 채 방치됐다(장기 맥락 소실 → 재질문·모순).
# projection 재빌드 때마다 결정적(비-LLM) 자동 요약 블록을 요약.md에 유지해 바닥을 깐다.
# - 대표 지시 이력은 projection 창 밖으로 밀린 것도 이전 블록에서 물려받아 계속 누적한다(event_seq 병합).
# - 사회자 update_summary가 쓴 수동 요약 본문은 건드리지 않는다(마커 블록만 교체).
_AUTO_SUMMARY_BEGIN = "<!-- 자동누적요약:시작 (시스템이 관리 — 손으로 고치지 말 것) -->"
_AUTO_SUMMARY_END = "<!-- 자동누적요약:끝 -->"
_SUMMARY_PLACEHOLDER = "(아직 요약 없음)"
MAX_AUTO_SUMMARY_DIRECTIVES = 100
_AUTO_DIRECTIVE_PREVIEW_CHARS = 200
_AUTO_DIRECTIVE_LINE_RE = re.compile(r"^- \[#(\d+)\] ")


def summary_path(space: str) -> Path:
    return _space_dir(space) / "요약.md"


def _auto_directive_line(item: dict) -> str:
    seq = _as_int(item.get("event_seq"))
    speaker = str(item.get("speaker") or "대표").strip()
    preview = str(item.get("content_preview") or "").replace("\n", " ").strip()
    return f"- [#{seq}] {speaker}: {preview[:_AUTO_DIRECTIVE_PREVIEW_CHARS]}"


def _merge_directive_lines(previous_block: str, projection: dict) -> list[str]:
    """이전 자동 블록의 지시 라인과 현재 projection 지시를 event_seq 로 병합(중복 제거)."""
    merged: dict[int, str] = {}
    for line in previous_block.splitlines():
        match = _AUTO_DIRECTIVE_LINE_RE.match(line.strip())
        if match:
            merged[int(match.group(1))] = line.strip()
    for item in projection.get("user_directive_items") or []:
        seq = _as_int(item.get("event_seq"))
        if seq > 0:
            merged[seq] = _auto_directive_line(item)
    ordered = [merged[seq] for seq in sorted(merged, reverse=True)]
    return ordered[:MAX_AUTO_SUMMARY_DIRECTIVES]


def _render_auto_summary_block(projection: dict, previous_block: str) -> str:
    topics = projection.get("topic_threads") or []
    topic_lines = []
    for item in topics[:12]:
        preview = str(item.get("latest_user_request") or item.get("latest_assistant_reply") or "").replace("\n", " ").strip()
        topic_lines.append(
            f"- [{item.get('status', '')}] {item.get('thread_id', '')} @#{_as_int(item.get('latest_event_seq'))}: {preview[:160]}"
        )
    directive_lines = _merge_directive_lines(previous_block, projection)
    parts = [
        _AUTO_SUMMARY_BEGIN,
        f"## 자동 누적요약 (기준 event #{_as_int(projection.get('applied_event_seq'))}, {projection.get('updated_at', '')})",
        "",
        f"**현재 방향:** {str(projection.get('active_context_summary') or '').strip() or '(없음)'}",
        "",
        "### 주제 흐름",
        *(topic_lines or ["- 없음"]),
        "",
        "### 대표 지시 누적 (최신순 — 오래된 지시도 창 밖으로 밀리기 전 여기 보존됨)",
        *(directive_lines or ["- 없음"]),
        _AUTO_SUMMARY_END,
    ]
    return "\n".join(parts)


def refresh_auto_summary(space: str, projection: dict) -> None:
    path = summary_path(space)
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        existing = ""
    begin = existing.find(_AUTO_SUMMARY_BEGIN)
    end = existing.find(_AUTO_SUMMARY_END)
    previous_block = existing[begin:end] if (begin != -1 and end > begin) else ""
    block = _render_auto_summary_block(projection, previous_block)
    if begin != -1 and end > begin:
        new_text = existing[:begin] + block + existing[end + len(_AUTO_SUMMARY_END):]
    elif _SUMMARY_PLACEHOLDER in existing:
        new_text = existing.replace(_SUMMARY_PLACEHOLDER, block, 1)
    else:
        base = existing.rstrip()
        new_text = (base + "\n\n" if base else "") + block + "\n"
    if new_text != existing:
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(path)


def _read_legacy_summary(space: str) -> dict:
    path = _space_dir(space) / "요약.md"
    if not path.exists():
        return {"status": "missing", "text": "", "char_count": 0, "summary_hash": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {
            "status": "read_error",
            "text": "",
            "char_count": 0,
            "summary_hash": "",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    return {
        "status": "ok",
        "text": text[:4000],
        "char_count": len(text),
        "summary_hash": _stable_id("legacy_summary", text),
    }


def _read_projection(space: str) -> tuple[dict, str]:
    path = projection_path(space)
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"{path.name}: {type(exc).__name__}"
    if not isinstance(data, dict):
        return {}, f"{path.name}: root_not_object"
    return data, ""


def _write_projection(space: str, data: dict):
    path = projection_path(space)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _latest_event_seq(rows: list[dict]) -> int:
    latest = 0
    for idx, row in enumerate(rows, start=1):
        latest = max(latest, _row_event_seq(row, idx))
    return latest


def _build_projection(space: str, rows: list[dict], *, previous: dict | None = None) -> dict:
    previous = previous or {}
    latest_seq = _latest_event_seq(rows)
    indexed_rows = list(enumerate(rows, start=1))
    user_rows = [(idx, row) for idx, row in indexed_rows if row.get("역할") == "user"]
    assistant_rows = [(idx, row) for idx, row in indexed_rows if row.get("역할") == "assistant"]
    legacy_summary = _read_legacy_summary(space)
    active_context = [_message_item(row, idx) for idx, row in indexed_rows[-MAX_ACTIVE_CONTEXT:]]
    representative_requests = [
        _message_item(row, idx)
        for idx, row in user_rows[-MAX_REPRESENTATIVE_REQUESTS:]
    ]
    relevant_past_source = user_rows[:-MAX_REPRESENTATIVE_REQUESTS] or assistant_rows[:-MAX_RELEVANT_PAST]
    relevant_past = [
        _message_item(row, idx)
        for idx, row in relevant_past_source[-MAX_RELEVANT_PAST:]
    ]
    source_refs = [
        _source_ref(row, idx)
        for idx, row in indexed_rows[-MAX_SOURCE_REFS:]
    ]
    user_directives = _user_directive_items(user_rows)
    topic_threads = _topic_threads(indexed_rows, latest_seq)
    # active/dormant 는 topic_threads 의 부분집합이라 전문 복사하면 같은 데이터가 projection 에
    # 3중 저장된다(실증: 레빗_bcd7 팩당 topic 39KB + dormant 30KB + active 9KB — 팩 폭주 주범).
    # 소비처(room_manager 프롬프트 스냅샷 _slim_rows, context_pack topic_lines 렌더러)는
    # recent_items 등 무거운 필드를 전혀 읽지 않으므로, 뷰 필드만 남긴 슬림 사본으로 대체한다.
    active_topic_threads = [_topic_view(item) for item in topic_threads if item.get("status") == "active"]
    dormant_topic_threads = [_topic_view(item) for item in topic_threads if item.get("status") == "dormant"]
    latest_user = representative_requests[-1] if representative_requests else {}
    latest_assistant = _message_item(assistant_rows[-1][1], assistant_rows[-1][0]) if assistant_rows else {}
    version = _as_int(previous.get("version")) + 1 if previous.get("schema") == PROJECTION_SCHEMA else 1
    projection = {
        "schema": PROJECTION_SCHEMA,
        "space_id": space,
        "projection_id": _stable_id("memproj", space, latest_seq, len(rows)),
        "version": version,
        "expected_previous_version": _as_int(previous.get("version")),
        "state": "clean",
        "source": "event_log_deterministic_v1",
        "applied_event_seq": latest_seq,
        "applied_message_count": len(rows),
        "updated_at": now_iso(),
        "active_context_summary": (
            latest_user.get("content_preview")
            or latest_assistant.get("content_preview")
            or legacy_summary.get("text", "").strip()
            or "아직 누적된 대화 맥락이 없음"
        )[:MAX_TEXT_CHARS],
        "active_context": active_context,
        "representative_requests": representative_requests,
        "user_directive_items": user_directives,
        "relevant_past": relevant_past,
        "topic_threads": topic_threads,
        "active_topic_threads": active_topic_threads,
        "dormant_topic_threads": dormant_topic_threads,
        "precedence_policy": {
            "clock": "event_seq",
            "rule": "later_user_message_takes_precedence_only_for_confirmed_conflicts",
            "non_conflicting_older_user_directives_remain_active": True,
            "semantic_conflict_detection": "not_performed_by_deterministic_projection",
        },
        "conflict_hints": {
            "semantic_conflicts_detected": False,
            "candidate_count": 0,
            "items": [],
            "note": "이 projection은 대화 원장을 층화할 뿐 의미 모순을 추정하지 않는다. 공간관리가 확인한 충돌만 최신 발언 우선으로 처리한다.",
        },
        "source_refs": source_refs,
        "legacy_summary_hint": {
            "status": legacy_summary.get("status", ""),
            "char_count": legacy_summary.get("char_count", 0),
            "summary_hash": legacy_summary.get("summary_hash", ""),
            "text": legacy_summary.get("text", "")[:1200],
        },
        "projection_method": {
            "kind": "bounded_event_projection_v1",
            "llm_summary_used": False,
            "layers": [
                "active_context",
                "representative_requests",
                "user_directive_items",
                "topic_threads",
                "legacy_summary_hint",
            ],
            "note": "요약.md는 legacy hint이며, 정본 freshness는 applied_event_seq/source_refs/topic_threads로 판단한다.",
        },
    }
    projection["projection_checksum"] = _checksum(projection)
    return projection


def ensure_projection(space: str) -> dict:
    rows = read(space, None)
    latest_seq = _latest_event_seq(rows)
    existing, error = _read_projection(space)
    existing_kind = ((existing or {}).get("projection_method") or {}).get("kind", "")
    existing_source = (existing or {}).get("source", "")
    if (
        existing
        and not error
        and _as_int(existing.get("applied_event_seq")) >= latest_seq
        and existing_kind == "bounded_event_projection_v1"
        and existing_source == "event_log_deterministic_v1"
    ):
        return snapshot(space, latest_event_seq=latest_seq, projection=existing, read_error="")
    projection = _build_projection(space, rows, previous=existing)
    _write_projection(space, projection)
    # 새 이벤트로 projection이 실제 재빌드될 때만 자동 누적요약을 갱신한다(위 조기반환 경로는 손대지 않음
    # — status 폴링마다 파일을 다시 쓰지 않는다). 요약 실패가 wake를 막으면 안 되므로 삼킨다.
    try:
        refresh_auto_summary(space, projection)
    except Exception:
        pass
    return snapshot(space, latest_event_seq=latest_seq, projection=projection, read_error=error)


def snapshot(
    space: str,
    *,
    latest_event_seq: int | None = None,
    projection: dict | None = None,
    read_error: str = "",
) -> dict:
    if latest_event_seq is None:
        latest_event_seq = _latest_event_seq(read(space, None))
    if projection is None:
        projection, read_error = _read_projection(space)
    applied = _as_int((projection or {}).get("applied_event_seq"))
    lag = max(0, _as_int(latest_event_seq) - applied)
    source = "space_memory_projection" if projection and not read_error else "legacy_summary"
    legacy = _read_legacy_summary(space)
    return {
        "schema": "SpaceMemorySnapshot.v1",
        "space_id": space,
        "memory_source": source,
        "projection_available": bool(projection and not read_error),
        "projection_corrupt": bool(read_error),
        "projection_errors": [read_error] if read_error else [],
        "projection_id": (projection or {}).get("projection_id", ""),
        "projection_version": _as_int((projection or {}).get("version")),
        "projection_checksum": (projection or {}).get("projection_checksum", ""),
        "projection_state": (projection or {}).get("state", ""),
        "source": (projection or {}).get("source", ""),
        "projection_method": (projection or {}).get("projection_method", {}),
        "applied_event_seq": applied,
        "latest_event_seq": _as_int(latest_event_seq),
        "projection_lag": lag,
        "active_context_summary": (projection or {}).get("active_context_summary", legacy.get("text", ""))[:MAX_TEXT_CHARS],
        "active_context": (projection or {}).get("active_context", []),
        "representative_requests": (projection or {}).get("representative_requests", []),
        "user_directive_items": (projection or {}).get("user_directive_items", []),
        "relevant_past": (projection or {}).get("relevant_past", []),
        "topic_threads": (projection or {}).get("topic_threads", []),
        "active_topic_threads": (projection or {}).get("active_topic_threads", []),
        "dormant_topic_threads": (projection or {}).get("dormant_topic_threads", []),
        "precedence_policy": (projection or {}).get("precedence_policy", {}),
        "conflict_hints": (projection or {}).get("conflict_hints", {}),
        "source_refs": (projection or {}).get("source_refs", []),
        "legacy_summary_hint": (projection or {}).get("legacy_summary_hint", {
            "status": legacy.get("status", ""),
            "char_count": legacy.get("char_count", 0),
            "summary_hash": legacy.get("summary_hash", ""),
            "text": legacy.get("text", "")[:1200],
        }),
    }
