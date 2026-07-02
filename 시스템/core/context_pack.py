# -*- coding: utf-8 -*-
"""공간 wake에 전달할 최소 ContextPack/TurnHandoffPack."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import lesson_ledger, space_memory
from .paths import PEOPLE, SPACES
from .transcript import now_iso, read


MAX_GUIDE_CHARS = 4000
MAX_SUMMARY_CHARS = 4000
MAX_MESSAGE_CHARS = 1000
# 최신 몇 건은 잘림 없이 전문으로 전달한다(대표 지시: 최근 대화는 원본으로 봐야 함). 나머지 최근대화는
# recent_messages(1000자)·active_context(700자 미리보기)로 충분. 병적으로 긴 단일 메시지가 프롬프트를
# 폭주시키지 않도록 전문에도 안전 상한만 둔다.
MAX_VERBATIM_RECENT = 5
VERBATIM_CHAR_CAP = 6000
MAX_HANDOFF_PREVIEW_CHARS = 1400
MAX_HANDOFF_BRIEF_PREVIEW_CHARS = 1800


def _space_dir(space: str) -> Path:
    return SPACES / space


def _north_star_path(space: str) -> Path:
    return _space_dir(space) / "north_star_goal_ledger.jsonl"


def _context_pack_path(space: str) -> Path:
    return _space_dir(space) / "context_packs.jsonl"


def _wake_manifest_path(space: str) -> Path:
    return _space_dir(space) / "wake_pack_manifest.jsonl"


def _read_text(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return text[:limit]


def _append_jsonl(path: Path, data: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


# 전달 원장(context_packs.jsonl·wake_pack_manifest.jsonl)의 무한 성장 방지. 실증: 레빗_bcd7 에서
# 23시간 만에 context_packs.jsonl 18MB(팩 전문 append) → status 폴링·매 tick 전량 재파싱으로 O(n²).
# 상한 초과 시 .1 아카이브로 밀어내고 새로 시작한다(아카이브 1개 유지 — 디버깅용 직전 세대 보존).
LEDGER_ROTATE_MAX_BYTES = 4 * 1024 * 1024


def _append_jsonl_rotating(path: Path, data: dict, *, max_bytes: int = LEDGER_ROTATE_MAX_BYTES):
    # flock: parallel_pass 는 후보 스레드 여럿이 동시에 이 원장에 append 한다. 큰 라인의 동시 append 는
    # 원자성이 보장되지 않아 라인이 섞여 ledger_corrupt 가 될 수 있고, 로테이션(rename)과 append 가
    # 겹치면 레코드가 유실될 수 있어 직렬화한다.
    lock = path.with_name("." + path.name + ".lock")
    try:
        lock.touch(exist_ok=True)
        with lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                if path.exists() and path.stat().st_size > max_bytes:
                    path.replace(path.with_name(path.name + ".1"))
                _append_jsonl(path, data)
                return
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception:
        pass  # 락/로테이션 실패가 wake를 막으면 안 된다 — 무락 append 로 폴백.
    _append_jsonl(path, data)


def _count_lines(path: Path) -> int:
    """전량 json 파싱 없이 원장 레코드 수만 센다(바이너리 개행 카운트)."""
    if not path.exists():
        return 0
    count = 0
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
    except Exception:
        return 0
    return count


def _tail_rows_with_error(path: Path, limit: int, *, tail_bytes: int = 512 * 1024) -> tuple[list[dict], str]:
    """파일 끝 tail_bytes 만 읽어 마지막 limit 개 레코드를 파싱한다 — 큰 원장 전량 재읽기 방지."""
    if not path.exists():
        return [], ""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # 잘린 첫 줄 버림
            data = f.read()
    except Exception as exc:
        return [], f"{path.name}: {type(exc).__name__}"
    out = []
    bad_lines = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
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
    error = f"{path.name}: invalid_json_lines={bad_lines}" if bad_lines else ""
    return out[-limit:], error


def _rows_with_error(path: Path) -> tuple[list[dict], str]:
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


def _rows(path: Path) -> list[dict]:
    return _rows_with_error(path)[0]


def _stable_id(prefix: str, *parts) -> str:
    payload = json.dumps([prefix, *parts], ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _checksum(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _preview(value, limit: int = MAX_HANDOFF_PREVIEW_CHARS) -> str:
    text = str(value or "").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def append_north_star_goal(
    space: str,
    text: str,
    *,
    source: str = "manual",
    source_message_id: str = "",
    priority: str = "normal",
) -> dict:
    clean = (text or "").strip()
    if not clean:
        raise ValueError("north star goal text required")
    row = {
        "schema": "NorthStarGoalLedger.v1",
        "goal_id": _stable_id("goal", space, clean, source_message_id),
        "state": "active",
        "text": clean,
        "source": source,
        "source_message_id": source_message_id,
        "priority": priority,
        "created_at": now_iso(),
    }
    _append_jsonl(_north_star_path(space), row)
    return row


def _active_goals_from_rows(rows: list[dict], limit: int = 8) -> list[dict]:
    by_id = {}
    for row in rows:
        goal_id = row.get("goal_id")
        if goal_id:
            by_id[goal_id] = row
    active = [row for row in by_id.values() if row.get("state", "active") == "active"]
    return active[-limit:]


def active_north_star_goals(space: str, limit: int = 8) -> list[dict]:
    return _active_goals_from_rows(_rows(_north_star_path(space)), limit)


def _recent_messages(space: str, limit: int = 32) -> list[dict]:
    out = []
    for row in read(space, limit):
        out.append({
            "event_seq": row.get("event_seq"),
            "message_id": row.get("message_id", ""),
            "speaker": row.get("화자", ""),
            "speaker_code": row.get("코드", ""),
            "role": row.get("역할", ""),
            "content": str(row.get("내용", ""))[:MAX_MESSAGE_CHARS],
            "intent_id": row.get("intent_id", ""),
            "conversation_thread_id": row.get("conversation_thread_id", ""),
            "room_generation": row.get("room_generation"),
        })
    return out


def _recent_verbatim_messages(space: str, n: int = MAX_VERBATIM_RECENT) -> list[dict]:
    """최신 n건을 (안전 상한 내) 전문으로. 700/1000자 미리보기와 달리 잘림 없이 원문 맥락을 준다."""
    out = []
    for row in read(space, n):
        out.append({
            "event_seq": row.get("event_seq"),
            "speaker": row.get("화자", ""),
            "role": row.get("역할", ""),
            "content": str(row.get("내용", ""))[:VERBATIM_CHAR_CAP],
        })
    return out


def _source_message(space: str, context: dict | None) -> dict:
    context = context or {}
    source_message_id = str(context.get("source_message_id") or "")
    source_event_seq = context.get("source_event_seq")
    for row in reversed(read(space, None)):
        if source_message_id and row.get("message_id") == source_message_id:
            return row
        try:
            if source_event_seq is not None and int(row.get("event_seq") or 0) == int(source_event_seq):
                return row
        except Exception:
            continue
    return {}


def _memory_snapshot(space: str) -> dict:
    try:
        return space_memory.ensure_projection(space)
    except Exception as exc:
        legacy = _read_text(_space_dir(space) / "요약.md", MAX_SUMMARY_CHARS)
        return {
            "schema": "SpaceMemorySnapshot.v1",
            "space_id": space,
            "memory_source": "legacy_summary",
            "projection_available": False,
            "projection_corrupt": True,
            "projection_errors": [f"{type(exc).__name__}: {str(exc)[:180]}"],
            "projection_id": "",
            "projection_version": 0,
            "projection_checksum": "",
            "projection_state": "failed",
            "source": "",
            "projection_method": {},
            "applied_event_seq": 0,
            "latest_event_seq": 0,
            "projection_lag": 0,
            "active_context_summary": legacy[:MAX_MESSAGE_CHARS],
            "active_context": [],
            "representative_requests": [],
            "user_directive_items": [],
            "relevant_past": [],
            "topic_threads": [],
            "active_topic_threads": [],
            "dormant_topic_threads": [],
            "precedence_policy": {},
            "conflict_hints": {},
            "source_refs": [],
            "legacy_summary_hint": {
                "status": "fallback",
                "char_count": len(legacy),
                "summary_hash": _stable_id("summary", legacy),
                "text": legacy[:1200],
            },
        }


def _completed_result_preview(work_dir: str) -> str:
    """완료작업 work_dir의 결과.md에서 '무엇을 만들었나/결론' 한 줄을 가볍게 뽑는다.
    objective(무엇을 하려 했나)만으론 부족 — 산출·판정을 보여 자기 결과 위에 쌓게 한다."""
    if not work_dir:
        return ""
    try:
        p = Path(work_dir)
        if not p.is_absolute():
            p = SPACES.parent / work_dir
        rp = p / "결과.md"
        if not rp.exists():
            return ""
        for line in rp.read_text(encoding="utf-8").splitlines():
            s = line.strip().lstrip("#").strip()
            if s and not s.startswith(("-", "*", "[")):
                return s[:140]
    except Exception:
        return ""
    return ""


def _role_one_liner(token: str) -> str:
    """role.md에서 그 에이전트가 누군지 한 줄(전문성)을 가볍게 뽑는다."""
    try:
        text = (PEOPLE / token / "role.md").read_text(encoding="utf-8")
    except Exception:
        return ""
    for line in text.splitlines():
        raw = line.strip()
        # 헤더(# …)·빈 줄·머리표는 건너뛰고 첫 서술 문장을 쓴다(제목에 토큰이 박혀 있어도 헤더라 스킵).
        if not raw or raw.startswith(("#", "-", "*")):
            continue
        return raw[:90]
    return ""


def _room_roster(space: str, target_agent: str = "") -> list[dict]:
    """이 방의 멤버(동료) 명단 — 이름·전문성 한 줄. 에이전트가 '옆에 누가 있고 뭘 잘하는지'를 알고
    서로에게 말 걸거나 알맞은 동료에게 넘기게 한다(대표만 보고 독백하지 않게)."""
    try:
        members = json.loads((SPACES / space / "멤버.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    roster = []
    for m in members[:12]:
        if not isinstance(m, dict):
            continue
        token = str(m.get("토큰") or "").strip()
        roster.append({
            "이름": m.get("이름", ""),
            "토큰": token,
            "is_you": bool(target_agent) and token == target_agent,
            "전문성": _role_one_liner(token),
        })
    return roster


def _build_work_situation(space: str, target_agent: str = "") -> dict:
    """이 방의 '작업 상황판' — 진행 중·결재 대기 작업을 모아 에이전트가 상황을 보고 판단하게 한다.

    종전엔 ContextPack에 작업 상태가 없어, 에이전트가 이미 진행/대기 중인 작업을 못 보고 같은 일을 중복
    요청하는 헛돎이 났다(실증 2026-06-29: 게이트된 작업 6번 반복). law(에이전트는 상황을 이해하고 일한다)가
    명령하는 '상황 판단'의 재료를 여기서 공급한다. 늦은 임포트로 순환참조를 피한다.
    """
    from . import task_registry, work_plan
    try:
        active = task_registry.snapshot(space).get("active_items") or []
    except Exception:
        active = []
    try:
        unstarted = work_plan.list_plans(space, states={work_plan.PENDING, work_plan.APPROVED})
    except Exception:
        unstarted = []
    pending = [p for p in unstarted if p.get("state") == work_plan.PENDING]

    def _active_objective(work_dir: str) -> str:
        # task_registry 스냅샷엔 objective가 없어(작업 식별자만), 작업폴더의 task_pack.json에서 가볍게 읽는다
        # — 진행중 작업이 '무슨 일'인지 보여 중복을 막기 위함. 작업 수가 적고 예외안전이라 hot-path 부담 작다.
        if not work_dir:
            return ""
        try:
            p = Path(work_dir)
            if not p.is_absolute():
                p = SPACES.parent / work_dir  # work_dir은 루트폴더 기준 상대경로
            tp = p / "task_pack.json"
            if tp.exists():
                return str(json.loads(tp.read_text(encoding="utf-8")).get("objective") or "")[:120]
        except Exception:
            return ""
        return ""

    active_rows = [{
        "task_id": t.get("task_id", ""),
        "worker": t.get("worker_agent", ""),
        "state": t.get("state", ""),
        "heartbeat_stale": bool(t.get("heartbeat_stale")),
        "objective_preview": _active_objective(t.get("work_dir", "")),
    } for t in active][:8]
    pending_rows = [{
        "plan_id": p.get("plan_id", ""),
        "worker": p.get("worker", ""),
        "state": p.get("state", ""),
        "objective_preview": str(p.get("objective") or "")[:120],
    } for p in pending][:8]
    # 개인 작업기록: 이 에이전트가 '무엇을 어디까지 완료했는지'(완료 이력·진척). active/pending만으로는
    # '내가 지금까지 한 일'을 알 수 없어, 종료된 자기 작업을 최신순으로 보여 자기 작업 위에 쌓게 한다.
    your_completed_rows = []
    your_completed_count = 0
    if target_agent:
        try:
            closed = task_registry.recent_closed_items(space, worker=target_agent, limit=20)
        except Exception:
            closed = []
        your_completed_count = len(closed)
        for t in closed[:6]:
            your_completed_rows.append({
                "task_id": t.get("task_id", ""),
                "state": t.get("state", ""),  # done/error/partial_ready/cancelled — 어디까지 갔는지
                "objective_preview": _active_objective(t.get("work_dir", "")),
                "result_preview": _completed_result_preview(t.get("work_dir", "")),  # 무엇을 만들었나/결론
            })
    note = ""
    if active_rows or pending_rows:
        note = (
            "착수·요청 전에 이 작업 상황을 확인하라. 이미 진행 중(active)이거나 결재 대기 중(pending_approval)인 "
            "같은 작업을 다시 시작·요청하지 마라(중복·헛돎 금지). 네가 맡은 일이 이미 진행 중이면 이어가거나 "
            "보고만 하고, 구조적으로 막혔으면(권한·환경·자격증명·네트워크 부재 등) 재시도 말고 막힌 사유와 필요한 것을 알려라."
        )
    return {
        "active_task_count": len(active),
        "active_tasks": active_rows,
        "pending_approval_count": len(pending),
        "pending_approval_plans": pending_rows,
        "your_active_tasks": [r for r in active_rows if target_agent and r.get("worker") == target_agent],
        "your_pending_plans": [r for r in pending_rows if target_agent and r.get("worker") == target_agent],
        "your_recent_completed": your_completed_rows,
        "your_completed_count": your_completed_count,
        "note": note,
    }


def build_context_pack(
    space: str,
    *,
    mode: str,
    event: str,
    context: dict | None,
    target_agent: str = "",
    recent_limit: int = 32,
) -> dict:
    sdir = _space_dir(space)
    context = context or {}
    guide = _read_text(sdir / "공간지침.md", MAX_GUIDE_CHARS)
    summary = _read_text(sdir / "요약.md", MAX_SUMMARY_CHARS)
    memory = _memory_snapshot(space)
    recent = _recent_messages(space, recent_limit)
    recent_verbatim = _recent_verbatim_messages(space, MAX_VERBATIM_RECENT)
    source = _source_message(space, context)
    goals = active_north_star_goals(space)
    lesson_pack = lesson_ledger.build_lesson_pack(
        space,
        mode=mode,
        context=context,
        event=event,
        target_agent=target_agent,
    )
    latest_event_seq = recent[-1].get("event_seq") if recent else None
    identity = {
        "space_id": space,
        "mode": mode,
        "target_agent": target_agent,
        "intent_id": context.get("intent_id", ""),
        "conversation_thread_id": context.get("conversation_thread_id", ""),
        "room_generation": context.get("room_generation"),
        "source_event_seq": context.get("source_event_seq"),
        "source_message_id": context.get("source_message_id", ""),
        "latest_event_seq": latest_event_seq,
        "guide_hash": _stable_id("guide", guide),
        "summary_hash": _stable_id("summary", summary),
        "memory_source": memory.get("memory_source", "legacy_summary"),
        "memory_projection_id": memory.get("projection_id", ""),
        "memory_projection_version": memory.get("projection_version", 0),
        "memory_projection_checksum": memory.get("projection_checksum", ""),
        "memory_applied_event_seq": memory.get("applied_event_seq", 0),
        "memory_projection_lag": memory.get("projection_lag", 0),
        "goal_ids": [g.get("goal_id", "") for g in goals],
        "lesson_ids": lesson_pack.get("included_lessons", []),
        "lesson_pack_hash": _checksum(lesson_pack),
        "lesson_pack_status": lesson_pack.get("lesson_pack_status", ""),
    }
    pack = {
        "schema": "ContextPack.compat_minimal.v1",
        "context_pack_id": _stable_id("ctx", identity),
        "created_at": now_iso(),
        **identity,
        "event": event,
        "north_star_goals": goals,
        "lesson_pack": lesson_pack,
        "space_guide_excerpt": guide,
        "space_summary_excerpt": summary,
        # space_memory_projection 이 이 필드들의 단일 진실원천이다. 예전엔 동일한 리스트
        # (active_context·representative_requests·user_directive_items·relevant_past·
        #  topic_threads·active_topic_threads·dormant_topic_threads·source_refs)를 최상위에도
        # 그대로 복사해 팩이 ~2배(160KB→300KB)로 비대해졌다 — 모든 소비자(room_manager 프롬프트
        # 스냅샷, 대시보드 room-chat.js, 렌더러)는 space_memory_projection 쪽을 읽으므로 최상위
        # 복사는 순수 잉여였다. 매 wake 비용을 절반으로 줄이려 제거. (읽는 코드는 아래 단일 출처를 본다.)
        "space_memory_projection": memory,
        # active_context_summary·precedence_policy·conflict_hints 는 tiny(<0.5KB)하고 렌더러가
        # 최상위 폴백으로 읽으므로 유지(비대와 무관).
        "active_context_summary": memory.get("active_context_summary", ""),
        "precedence_policy": memory.get("precedence_policy", {}),
        "conflict_hints": memory.get("conflict_hints", {}),
        "room_roster": _room_roster(space, target_agent),
        "work_situation": _build_work_situation(space, target_agent),
        "current_user_request": {
            "event_seq": source.get("event_seq"),
            "message_id": source.get("message_id", ""),
            "speaker": source.get("화자", ""),
            "content": str(source.get("내용", ""))[:MAX_MESSAGE_CHARS],
            "ingress_type": source.get("ingress_type", ""),
            "cancel_replan_fence": bool(source.get("cancel_replan_fence")),
        },
        "recent_messages": recent,
        "recent_messages_verbatim": recent_verbatim,
        "fallback_policy": {
            "legacy_message_fallback_allowed": True,
            "managed_v2_side_effects_allowed_without_pack": False,
            "if_pack_missing_or_mismatch": "stop_or_return_to_manager",
        },
    }
    pack["context_pack_checksum"] = _checksum(pack)
    return pack


def turn_handoff_brief(pack: dict, target_agent: str, manager_message: str, reason: str) -> str:
    req = pack.get("current_user_request") or {}
    goals = pack.get("north_star_goals") or []
    goal_lines = "\n".join(f"- {g.get('text', '')}" for g in goals[:5]) or "- 등록된 장기 목표 없음"
    lesson_pack = pack.get("lesson_pack") or {}
    must_lessons = lesson_pack.get("must_apply") or []
    may_lessons = lesson_pack.get("may_apply") or []
    ref_lessons = lesson_pack.get("reference_only") or []
    memory = pack.get("space_memory_projection") or {}
    active_context_summary = str(memory.get("active_context_summary") or pack.get("active_context_summary") or "").strip()
    work_situation = pack.get("work_situation") or {}

    def work_situation_block() -> str:
        ws = work_situation
        act = ws.get("active_tasks") or []
        pend = ws.get("pending_approval_plans") or []
        mine_done = ws.get("your_recent_completed") or []
        mine_done_count = ws.get("your_completed_count", 0)
        lines = []
        if act or pend:
            lines.append(f"- 진행 중 작업: {ws.get('active_task_count', 0)}건 / 결재 대기 계획: {ws.get('pending_approval_count', 0)}건")
            for t in act:
                stale = " (하트비트 끊김=죽었을 수 있음)" if t.get("heartbeat_stale") else ""
                lines.append(f"  · [진행중] worker={t.get('worker','')} task={t.get('task_id','')}{stale}: {t.get('objective_preview','')}")
            for p in pend:
                lines.append(f"  · [결재대기] worker={p.get('worker','')} plan={p.get('plan_id','')}: {p.get('objective_preview','')}")
        else:
            lines.append("- 진행 중이거나 결재 대기 중인 작업 없음.")
        # 네가 지금까지 완료/종료한 작업 — '어디까지 했는지'를 보고 자기 작업 위에 쌓아라(중복 착수 금지).
        if mine_done:
            lines.append(f"- 너의 완료/종료 작업 누적 {mine_done_count}건 (최근 {len(mine_done)}건 — 네가 어디까지·무엇을 했는지):")
            for d in mine_done:
                res = d.get("result_preview")
                res_line = f" → 결과: {res}" if res else ""
                lines.append(f"  · [{d.get('state','')}] task={d.get('task_id','')}: {d.get('objective_preview','')}{res_line}")
        elif not act and not pend:
            lines.append("- 너의 완료/진행/대기 작업 없음 — 깨끗한 상태에서 시작한다.")
        if ws.get("note"):
            lines.append(f"- ⚠️ {ws.get('note')}")
        return "\n".join(lines) + "\n"

    def roster_block() -> str:
        roster = pack.get("room_roster") or []
        if not roster:
            return "- (멤버 정보 없음)\n"
        out = []
        for m in roster:
            you = " ← 너" if m.get("is_you") else ""
            spec = f" — {m['전문성']}" if m.get("전문성") else ""
            out.append(f"- {m.get('이름','')}({m.get('토큰','')}){you}{spec}")
        return "\n".join(out) + "\n"

    def verbatim_block() -> str:
        items = pack.get("recent_messages_verbatim") or []
        if not items:
            return "- 없음\n"
        out = []
        for it in items:
            seq = it.get("event_seq", "")
            spk = it.get("speaker") or it.get("role") or "?"
            body = str(it.get("content") or "").strip()
            out.append(f"- event #{seq} {spk}:\n{body}")
        return "\n".join(out) + "\n"

    def context_lines(items: list[dict], empty: str, limit: int = 32) -> str:
        lines = []
        for item in items[:limit]:
            seq = item.get("event_seq", "")
            speaker = item.get("speaker") or item.get("role") or "?"
            preview = str(item.get("content_preview") or item.get("content") or "").replace("\n", " ").strip()
            lines.append(f"- event #{seq} {speaker}: {preview[:220]}")
        return "\n".join(lines) if lines else empty

    def lesson_lines(items: list[dict], empty: str) -> str:
        lines = []
        for lesson in items[:5]:
            lesson_id = lesson.get("lesson_id", "")
            instruction = str(lesson.get("instruction") or "").replace("\n", " ").strip()
            level = lesson.get("application_level", "")
            lines.append(f"- {lesson_id} [{level}] {instruction}")
        return "\n".join(lines) if lines else empty

    def directive_lines(items: list[dict], empty: str, limit: int = 40) -> str:
        # 대표 지시는 시간순 전부 전달한다(누락 금지). 나중 지시가 이전과 충돌하면 나중 것 우선,
        # 충돌하지 않는 이전 지시는 계속 유효 — 에이전트가 현재 방향을 종합해 판단하도록 한다.
        lines = []
        for item in items[-limit:]:
            seq = item.get("event_seq", "")
            rank = item.get("precedence_rank", "")
            preview = str(item.get("content_preview") or "").replace("\n", " ").strip()
            lines.append(f"- event #{seq} rank {rank}: {preview[:260]}")
        return "\n".join(lines) if lines else empty

    def topic_lines(items: list[dict], empty: str, limit: int = 8) -> str:
        lines = []
        for item in items[:limit]:
            latest = item.get("latest_event_seq", "")
            thread_id = item.get("thread_id", "")
            status = item.get("status", "")
            preview = str(item.get("latest_user_request") or item.get("latest_assistant_reply") or "").replace("\n", " ").strip()
            lines.append(f"- {status} {thread_id} @event #{latest}: {preview[:180]}")
        return "\n".join(lines) if lines else empty

    must_ids = [lesson.get("lesson_id", "") for lesson in must_lessons if lesson.get("lesson_id")]
    precedence = memory.get("precedence_policy") or pack.get("precedence_policy") or {}
    conflict_hints = memory.get("conflict_hints") or pack.get("conflict_hints") or {}
    report_template = ""
    if must_ids:
        applications = [{
            "lesson_id": lesson_id,
            "applied": True,
            "not_applicable_reason": "",
            "how": "이번 답변에 어떻게 반영했는지 한 줄로 기록",
            "outcome": "success",
            "needs_lesson_update": False,
        } for lesson_id in must_ids]
        report_template = (
            "\n## LessonApplicationReport\n"
            "아래 must_apply 레슨은 답변 마지막에 시스템용 JSON으로 적용 여부를 보고한다. "
            "이 JSON은 공개 전 제거된다.\n"
            "```json\n"
            f"{json.dumps({'schema': 'LessonApplicationReport.v1', 'applications': applications}, ensure_ascii=False, indent=2)}\n"
            "```\n"
        )
    return (
        "# TurnHandoffBrief\n\n"
        f"- 너는 이 공간의 채팅에이전트로 턴을 받았다: `{target_agent}`\n"
        f"- space_id: {pack.get('space_id', '')}\n"
        f"- intent_id: {pack.get('intent_id', '')}\n"
        f"- conversation_thread_id: {pack.get('conversation_thread_id', '')}\n"
        f"- room_generation: {pack.get('room_generation')}\n"
        f"- response_target: source message `{req.get('message_id', '')}`에 이어서 방에 답할 후보를 만든다.\n"
        f"- 공간관리 전달 메시지: {manager_message}\n"
        f"- 턴 전달 이유: {reason}\n\n"
        "## 이 방 멤버(동료) — 너는 이 중 하나이고, 나머지는 같이 일하는 동료다\n"
        "- 대표에게만 보고하지 말고, 동료의 말에 이어 말하고(동의·보완·이견) 알맞은 동료에게 넘겨라.\n"
        f"{roster_block()}\n"
        "## 대표/공간 장기 목표\n"
        f"{goal_lines}\n\n"
        "## 현재 맥락 projection\n"
        f"- memory_source: {memory.get('memory_source', pack.get('memory_source', ''))}\n"
        f"- applied_event_seq: {memory.get('applied_event_seq', pack.get('memory_applied_event_seq', 0))}\n"
        f"- active_context_summary: {active_context_summary or '등록된 현재 맥락 없음'}\n"
        "- 최근 핵심 대화\n"
        f"{context_lines(memory.get('active_context') or pack.get('recent_messages') or [], '- 없음')}\n"
        "- 관련 과거/대표 요청\n"
        f"{context_lines((memory.get('representative_requests') or [])[-12:] or memory.get('relevant_past') or [], '- 없음')}\n\n"
        f"## 최근 대화 원문(최신 {MAX_VERBATIM_RECENT}건 — 잘림 없음)\n"
        "- 위 '최근 핵심 대화'는 미리보기(잘림)다. 아래는 가장 최근 메시지들의 원문이니 정확한 맥락은 여기서 본다.\n"
        f"{verbatim_block()}\n"
        "## 이 방의 작업 상황 (착수·요청 전 반드시 확인 — 기계적 반복/중복 금지)\n"
        f"{work_situation_block()}\n"
        "## 대표 지시 누적(시간순 전부) · 현재 방향 종합\n"
        "- 아래는 대표가 지금까지 남긴 지시를 event 순서대로 누적한 것이다. 빠짐없이 읽는다.\n"
        "- **입장 변경 규칙**: 나중 지시가 이전 지시와 충돌하면 나중 것을 따른다(대표의 입장이 바뀐 것). "
        "충돌하지 않는 이전 지시는 계속 유효하다. 이 규칙으로 **이전 요청과 모순 없이 현재 추구하는 방향**을 스스로 종합해 판단한다.\n"
        f"- precedence_rule: {precedence.get('rule', 'confirmed conflict만 최신 발언 우선')}\n"
        f"- conflict_candidate_count: {conflict_hints.get('candidate_count', 0)}\n"
        f"{directive_lines(memory.get('user_directive_items') or pack.get('user_directive_items') or [], '- 없음')}\n\n"
        "## 주제 상태\n"
        "- active topics\n"
        f"{topic_lines(memory.get('active_topic_threads') or pack.get('active_topic_threads') or [], '- 없음')}\n"
        "- dormant topics\n"
        f"{topic_lines(memory.get('dormant_topic_threads') or pack.get('dormant_topic_threads') or [], '- 없음')}\n\n"
        "## 이번 wake 레슨\n"
        f"- lesson_pack_status: {lesson_pack.get('lesson_pack_status', 'unknown')}\n"
        "- must_apply\n"
        f"{lesson_lines(must_lessons, '- 없음')}\n"
        "- may_apply\n"
        f"{lesson_lines(may_lessons, '- 없음')}\n"
        "- reference_only\n"
        f"{lesson_lines(ref_lessons, '- 없음')}\n\n"
        "## 응답 원칙\n"
        "- 지금 턴의 맥락과 공간지침에 맞춰 한 번 답한다. **진짜 단톡방의 사람처럼** — 길게 다 읊지 말고 사람이 말하듯 자연스럽게.\n"
        "- **남이 이미 충분히 말했으면 반복하지 마라.** 같은 말을 길게 다시 쓰는 대신, 짧게 동의(한 줄·👍)하거나 **너만의 다른 관점/보탤 것**만 더한다. 정말 보탤 게 없으면 그 동료 말에 동의한다고 한 줄로만 남겨라(중복 장문 금지 — 방이 같은 내용으로 도배되지 않게).\n"
        "- 작업이 필요하면 직접 작업 폴더를 만들거나 결과를 공개하지 말고, `ChatAgentResult.v1` JSON으로 `action=request_work`와 `work_request.objective`를 반환한다.\n"
        "- `public_reply`는 방에 네 말풍선으로 공개된다. 보탤 게 있을 때 채운다(짧아도 좋다). 작업을 넘길 때도 한 줄로 무엇을 맡는지 남겨 협업이 보이게 한다.\n"
        "- `request_work`의 작업 상세(objective/constraints)는 TaskRegistry로 가고, `public_reply`는 방에 공개된다 — 둘 다 채우면 협업이 방에서 보이면서 작업도 진행된다.\n"
        "- `suggested_worker`에는 작업을 맡길 방 멤버를 적는다. 표시이름(예: 구현자)이나 토큰(예: 구현자_2a79) 모두 쓸 수 있고, 시스템이 토큰으로 해석한다.\n"
        "- 다른 공간이나 다른 intent의 기억과 섞지 않는다.\n"
        "- ChatAgentResult.v1 예시: "
        '{"schema":"ChatAgentResult.v1","action":"request_work","public_reply":"요구사항을 이렇게 정리했고, 구현은 구현자에게 넘깁니다: add/list/done 3기능, JSON 저장.","work_request":{"objective":"해야 할 작업","constraints":[],"suggested_worker":"'
        f"{target_agent}"
        '"},"manager_requests":[]}\n'
        f"{report_template}"
    )


def build_turn_handoff_pack(
    space: str,
    *,
    target_agent: str,
    manager_message: str,
    reason: str,
    context: dict | None,
    manager_claim_context: dict | None,
    context_pack: dict,
) -> dict:
    context = context or {}
    claim = manager_claim_context or {}
    wake_id = _stable_id(
        "wake",
        space,
        target_agent,
        context.get("intent_id", ""),
        context.get("source_event_seq"),
        context.get("source_message_id", ""),
        claim.get("claim_token", ""),
    )
    pack = {
        "schema": "TurnHandoffPack.compat_minimal.v1",
        "wake_id": wake_id,
        "turn_handoff_id": _stable_id("turn", wake_id, context_pack.get("context_pack_id", "")),
        "created_at": now_iso(),
        "space_id": space,
        "target_agent": target_agent,
        "mode": "chat",
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "response_target": {
            "type": "space_thread",
            "reply_to_message_id": context.get("reply_to_message_id") or context.get("source_message_id", ""),
            "source_event_seq": context.get("source_event_seq"),
            "conversation_thread_id": context.get("conversation_thread_id", ""),
            "intent_id": context.get("intent_id", ""),
        },
        "why_you": reason,
        "manager_message": manager_message,
        "allowed_actions": ["reply_candidate_to_manager", "request_work_via_manager"],
        "disallowed_actions": ["direct_transcript_write", "direct_task_wake", "direct_public_publish"],
        "return_contract": {
            "kind": "single_chat_reply_candidate",
            "published_by": "space_manager_publish_ledger",
            "must_echo_space_id": space,
            "must_remain_in_thread": True,
            "structured_request_schema": "ChatAgentResult.v1",
            "request_work_route": "space_manager_task_registry",
            "must_report_lessons": [
                lesson.get("lesson_id", "")
                for lesson in (context_pack.get("lesson_pack") or {}).get("must_apply", [])
                if lesson.get("lesson_id")
            ],
            "lesson_application_report_schema": "LessonApplicationReport.v1",
        },
        "lesson_pack_status": (context_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
        "memory_source": context_pack.get("memory_source", ""),
        "memory_projection_id": context_pack.get("memory_projection_id", ""),
        "memory_projection_version": context_pack.get("memory_projection_version", 0),
        "memory_projection_checksum": context_pack.get("memory_projection_checksum", ""),
        "memory_applied_event_seq": context_pack.get("memory_applied_event_seq", 0),
        "memory_projection_lag": context_pack.get("memory_projection_lag", 0),
        "included_lessons": (context_pack.get("lesson_pack") or {}).get("included_lessons", []),
    }
    pack["turn_handoff_brief"] = turn_handoff_brief(context_pack, target_agent, manager_message, reason)
    pack["turn_handoff_checksum"] = _checksum(pack)
    return pack


def _turn_handoff_observation(
    *,
    space: str,
    manifest_id: str,
    state: str,
    recipient: str,
    delivery_type: str,
    context_pack: dict,
    turn_handoff_pack: dict,
    manager_claim_context: dict | None,
    delivered_at: str,
) -> dict:
    claim = manager_claim_context or {}
    lesson_pack = context_pack.get("lesson_pack") or {}
    must_apply = [
        lesson.get("lesson_id", "")
        for lesson in lesson_pack.get("must_apply", [])
        if lesson.get("lesson_id")
    ]
    recent_messages = context_pack.get("recent_messages") or []
    current_request = context_pack.get("current_user_request") or {}
    return_contract = turn_handoff_pack.get("return_contract") or {}
    response_target = turn_handoff_pack.get("response_target") or {}
    return {
        "schema": "TurnHandoffObservation.v1",
        "manifest_id": manifest_id,
        "state": state,
        "delivered_at": delivered_at,
        "turn_handoff_created_at": turn_handoff_pack.get("created_at", ""),
        "space_id": space,
        "recipient": recipient,
        "target_agent": turn_handoff_pack.get("target_agent") or recipient,
        "delivery_type": delivery_type,
        "wake_id": turn_handoff_pack.get("wake_id", ""),
        "turn_handoff_id": turn_handoff_pack.get("turn_handoff_id", ""),
        "turn_handoff_checksum": turn_handoff_pack.get("turn_handoff_checksum", ""),
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "intent_id": context_pack.get("intent_id", ""),
        "conversation_thread_id": context_pack.get("conversation_thread_id", ""),
        "room_generation": context_pack.get("room_generation"),
        "source_event_seq": context_pack.get("source_event_seq"),
        "source_message_id": context_pack.get("source_message_id", ""),
        "response_target": response_target,
        "why_you": _preview(turn_handoff_pack.get("why_you", ""), 700),
        "manager_message_preview": _preview(turn_handoff_pack.get("manager_message", ""), 900),
        "turn_handoff_brief_preview": _preview(
            turn_handoff_pack.get("turn_handoff_brief", ""),
            MAX_HANDOFF_BRIEF_PREVIEW_CHARS,
        ),
        "current_user_request_preview": _preview(current_request.get("content", ""), 700),
        "recent_message_count": len(recent_messages),
        "allowed_actions": list(turn_handoff_pack.get("allowed_actions") or []),
        "disallowed_actions": list(turn_handoff_pack.get("disallowed_actions") or []),
        "return_contract": return_contract,
        "lesson_pack_status": lesson_pack.get("lesson_pack_status", ""),
        "included_lesson_count": len(lesson_pack.get("included_lessons") or []),
        "must_apply_lesson_count": len(must_apply),
        "must_apply_lessons": must_apply,
    }


def record_pack_delivery(
    space: str,
    *,
    recipient: str,
    delivery_type: str,
    context_pack: dict,
    turn_handoff_pack: dict | None = None,
    manager_claim_context: dict | None = None,
) -> dict:
    claim = manager_claim_context or {}
    manifest_id = f"manifest_{uuid4().hex[:12]}"
    state = "context_delivered"
    delivered_at = now_iso()
    turn_handoff_observation = {}
    if turn_handoff_pack:
        turn_handoff_observation = _turn_handoff_observation(
            space=space,
            manifest_id=manifest_id,
            state=state,
            recipient=recipient,
            delivery_type=delivery_type,
            context_pack=context_pack,
            turn_handoff_pack=turn_handoff_pack,
            manager_claim_context=manager_claim_context,
            delivered_at=delivered_at,
        )
    row = {
        "schema": "WakePackManifest.v1",
        "manifest_id": manifest_id,
        "state": state,
        "delivered_at": delivered_at,
        "space_id": space,
        "recipient": recipient,
        "delivery_type": delivery_type,
        "mode": context_pack.get("mode", ""),
        "context_pack_id": context_pack.get("context_pack_id", ""),
        "context_pack_checksum": context_pack.get("context_pack_checksum", ""),
        "wake_id": (turn_handoff_pack or {}).get("wake_id", ""),
        "turn_handoff_id": (turn_handoff_pack or {}).get("turn_handoff_id", ""),
        "turn_handoff_checksum": (turn_handoff_pack or {}).get("turn_handoff_checksum", ""),
        "manager_claim_token": claim.get("claim_token", ""),
        "manager_fencing_token": claim.get("fencing_token", ""),
        "owner_boot_id": claim.get("owner_boot_id", ""),
        "intent_id": context_pack.get("intent_id", ""),
        "conversation_thread_id": context_pack.get("conversation_thread_id", ""),
        "room_generation": context_pack.get("room_generation"),
        "source_event_seq": context_pack.get("source_event_seq"),
        "source_message_id": context_pack.get("source_message_id", ""),
        "lesson_pack_status": (context_pack.get("lesson_pack") or {}).get("lesson_pack_status", ""),
        "included_lessons": (context_pack.get("lesson_pack") or {}).get("included_lessons", []),
        "must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in (context_pack.get("lesson_pack") or {}).get("must_apply", [])
            if lesson.get("lesson_id")
        ],
    }
    if turn_handoff_observation:
        row["turn_handoff_observation"] = turn_handoff_observation
    # 디스크 원장에는 슬림 팩만 남긴다. 종전엔 팩 전문(projection·recent·roster·work_situation 포함,
    # 팩당 최대 414KB 실측)을 통째로 append 해 원장이 하루 18MB까지 폭주했다. 원장의 소비자는
    # snapshot(메타·lesson_pack·최신 id들만 읽음)뿐이므로 큰 본문 리스트는 기록할 이유가 없다 —
    # 전달된 팩 실물은 checksum 으로 식별되고, 본문은 프롬프트로 이미 전달됐다.
    _append_jsonl_rotating(_context_pack_path(space), {
        "schema": context_pack.get("schema", "ContextPack.compat_minimal.v1"),
        "recorded_at": row["delivered_at"],
        "recipient": recipient,
        "delivery_type": delivery_type,
        **_slim_context_pack_for_ledger(context_pack),
    })
    _append_jsonl_rotating(_wake_manifest_path(space), row)
    return row


# 원장 기록에서 제외할 대용량 본문 필드 — snapshot 이 읽는 필드(lesson_pack·context_pack_id·
# memory_* 스칼라·mode 등)는 모두 보존된다.
_LEDGER_DROP_FIELDS = (
    "space_memory_projection", "recent_messages", "recent_messages_verbatim",
    "space_guide_excerpt", "space_summary_excerpt", "room_roster",
    "work_situation", "north_star_goals",
)


def _slim_context_pack_for_ledger(context_pack: dict) -> dict:
    slim = {k: v for k, v in context_pack.items() if k not in _LEDGER_DROP_FIELDS}
    memory = context_pack.get("space_memory_projection")
    if isinstance(memory, dict):
        slim["space_memory_projection"] = {
            k: memory[k]
            for k in (
                "schema", "space_id", "memory_source", "projection_available",
                "projection_state", "projection_id", "projection_version",
                "projection_checksum", "source", "applied_event_seq",
                "latest_event_seq", "projection_lag",
            )
            if k in memory
        }
    event = slim.get("event")
    if isinstance(event, str) and len(event) > 500:
        slim["event"] = event[:500] + "..."
    return slim


def render_manager_context_prompt(context_pack: dict) -> str:
    return (
        "## ContextPack.compat_minimal.v1\n"
        "아래 JSON은 이번 판단에 사용할 정본 맥락이다. pack이 현재 이벤트와 맞지 않는다고 판단되면 pass하지 말고 stop하라.\n"
        "```json\n"
        f"{json.dumps(context_pack, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def _slim_context_pack_for_handoff_dump(context_pack: dict) -> dict:
    """에이전트 턴핸드오프 프롬프트의 raw ContextPack 덤프용 슬림 사본.

    render_turn_handoff_prompt는 맨 앞에 turn_handoff_brief(사람이 읽는 포맷)를 이미 싣는다.
    그 브리프가 active_context(최근 핵심 대화)·user_directive_items(대표 지시 누적)·
    active/dormant 주제스레드·representative_requests를 전부 담으므로, 뒤따르는 raw ContextPack
    덤프에 space_memory_projection의 같은 큰 목록을 또 실으면 같은 맥락을 두 번 보내는 것이다
    (에이전트 프롬프트가 ~13만 토큰까지 부푼 주범 — C층 중복). 큰 목록은 빼고 projection
    메타데이터만 남긴다(키는 유지 — 계약·테스트 보존). 사회자 프롬프트
    (render_manager_context_prompt)는 브리프가 없어 raw 덤프가 유일한 맥락이므로 건드리지 않는다.
    """
    memory = context_pack.get("space_memory_projection")
    if not isinstance(memory, dict):
        return context_pack
    keep = (
        "schema", "space_id", "memory_source", "projection_available",
        "projection_state", "projection_id", "projection_version",
        "projection_checksum", "source", "projection_method",
        "applied_event_seq", "latest_event_seq", "projection_lag",
        "active_context_summary", "precedence_policy", "conflict_hints",
        "projection_corrupt", "projection_errors",
    )
    slim_memory = {k: memory[k] for k in keep if k in memory}
    slim_memory["_omitted_lists"] = (
        "active_context·topic_threads·active_topic_threads·dormant_topic_threads·"
        "user_directive_items·representative_requests·source_refs·relevant_past 는 위 "
        "TurnHandoffBrief의 '최근 핵심 대화 / 대표 지시 누적 / 주제 상태' 섹션에 사람이 읽는 "
        "형태로 이미 있다. 토큰 절약 위해 raw 중복 덤프를 생략했다 — 맥락은 브리프를 보라."
    )
    slim = dict(context_pack)
    slim["space_memory_projection"] = slim_memory
    # recent_messages_verbatim 은 브리프 '최근 대화 원문' 섹션에 이미 전문으로 들어간다 — raw 덤프에서
    # 또 싣지 않는다(중복 방지). recent_messages(1000자)는 6번째 이후 중간 fidelity용으로 덤프에 둔다.
    slim.pop("recent_messages_verbatim", None)
    return slim


def render_turn_handoff_prompt(context_pack: dict, turn_handoff_pack: dict) -> str:
    return (
        f"{turn_handoff_pack.get('turn_handoff_brief', '')}\n"
        "## TurnHandoffPack.compat_minimal.v1\n"
        "```json\n"
        f"{json.dumps(turn_handoff_pack, ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "## ContextPack.compat_minimal.v1\n"
        "위 TurnHandoffBrief가 정리된 맥락이고, 아래 JSON은 그 정본(큰 목록은 브리프와 중복이라 생략).\n"
        "```json\n"
        f"{json.dumps(_slim_context_pack_for_handoff_dump(context_pack), ensure_ascii=False, indent=2)}\n"
        "```\n\n"
        "# 실제 요청\n\n"
        f"{turn_handoff_pack.get('manager_message', '')}"
    )


def snapshot(space: str) -> dict:
    # 팩 원장은 tail 만 읽는다 — 이 snapshot 은 status 폴링·매 매니저 tick 마다 호출되는데,
    # 종전엔 원장 전량(실측 18MB)을 매번 read_text + json.loads 해 방이 살수록 모든 턴이 느려졌다.
    # 여기서 쓰는 것은 레코드 수와 최신 몇 개뿐이므로 개행 카운트 + tail 파싱으로 충분하다.
    packs, packs_error = _tail_rows_with_error(_context_pack_path(space), 8)
    pack_count = _count_lines(_context_pack_path(space))
    manifests, manifests_error = _tail_rows_with_error(_wake_manifest_path(space), 400)
    manifest_count = _count_lines(_wake_manifest_path(space))
    goal_rows, goals_error = _rows_with_error(_north_star_path(space))
    counts = {}
    for row in manifests:
        key = row.get("delivery_type", "unknown")
        counts[key] = counts.get(key, 0) + 1
    latest_manifest = manifests[-1] if manifests else {}
    latest_pack = packs[-1] if packs else {}
    latest_lesson_pack = latest_pack.get("lesson_pack") or {}
    handoff_manifests = [row for row in manifests if row.get("turn_handoff_id")]
    latest_handoff_manifest = handoff_manifests[-1] if handoff_manifests else {}
    latest_handoff = latest_handoff_manifest.get("turn_handoff_observation") or {}
    if latest_handoff_manifest and not latest_handoff:
        latest_handoff = {
            "schema": "TurnHandoffObservation.legacy_manifest.v1",
            "delivered_at": latest_handoff_manifest.get("delivered_at", ""),
            "space_id": latest_handoff_manifest.get("space_id", space),
            "recipient": latest_handoff_manifest.get("recipient", ""),
            "target_agent": latest_handoff_manifest.get("recipient", ""),
            "delivery_type": latest_handoff_manifest.get("delivery_type", ""),
            "wake_id": latest_handoff_manifest.get("wake_id", ""),
            "turn_handoff_id": latest_handoff_manifest.get("turn_handoff_id", ""),
            "turn_handoff_checksum": latest_handoff_manifest.get("turn_handoff_checksum", ""),
            "context_pack_id": latest_handoff_manifest.get("context_pack_id", ""),
            "context_pack_checksum": latest_handoff_manifest.get("context_pack_checksum", ""),
            "intent_id": latest_handoff_manifest.get("intent_id", ""),
            "conversation_thread_id": latest_handoff_manifest.get("conversation_thread_id", ""),
            "room_generation": latest_handoff_manifest.get("room_generation"),
            "source_event_seq": latest_handoff_manifest.get("source_event_seq"),
            "source_message_id": latest_handoff_manifest.get("source_message_id", ""),
            "lesson_pack_status": latest_handoff_manifest.get("lesson_pack_status", ""),
            "included_lesson_count": len(latest_handoff_manifest.get("included_lessons") or []),
            "must_apply_lesson_count": len(latest_handoff_manifest.get("must_apply_lessons") or []),
            "must_apply_lessons": latest_handoff_manifest.get("must_apply_lessons") or [],
        }
    ledger_errors = [err for err in (packs_error, manifests_error, goals_error) if err]
    return {
        "context_pack_count": pack_count,
        "wake_manifest_count": manifest_count,
        # turn_handoff_count·delivery_counts 는 최근 창(tail 400 manifest) 기준 — 전량 카운트가
        # 필요했던 소비자는 없고(정보성 지표), 전량 파싱 비용이 목적을 압도해 창 기준으로 바꿨다.
        "turn_handoff_count": len(handoff_manifests),
        "delivery_counts": counts,
        "latest_manifest_id": latest_manifest.get("manifest_id", ""),
        "latest_manifest_state": latest_manifest.get("state", ""),
        "latest_delivered_at": latest_manifest.get("delivered_at", ""),
        "latest_context_pack_id": latest_pack.get("context_pack_id", ""),
        "latest_wake_id": latest_manifest.get("wake_id", ""),
        "latest_turn_handoff_id": latest_handoff_manifest.get("turn_handoff_id", ""),
        "latest_turn_handoff_checksum": latest_handoff_manifest.get("turn_handoff_checksum", ""),
        "latest_turn_handoff": latest_handoff,
        "latest_recipient": latest_manifest.get("recipient", ""),
        "latest_delivery_type": latest_manifest.get("delivery_type", ""),
        "latest_lesson_pack_status": latest_lesson_pack.get("lesson_pack_status", ""),
        "latest_memory_source": latest_pack.get("memory_source", ""),
        "latest_memory_projection_id": latest_pack.get("memory_projection_id", ""),
        "latest_memory_projection_version": latest_pack.get("memory_projection_version", 0),
        "latest_memory_applied_event_seq": latest_pack.get("memory_applied_event_seq", 0),
        "latest_memory_projection_lag": latest_pack.get("memory_projection_lag", 0),
        "latest_included_lessons": latest_lesson_pack.get("included_lessons", []),
        "latest_must_apply_lessons": [
            lesson.get("lesson_id", "")
            for lesson in latest_lesson_pack.get("must_apply", [])
            if lesson.get("lesson_id")
        ],
        "north_star_goal_count": len(_active_goals_from_rows(goal_rows, limit=1000)),
        "ledger_corrupt": bool(ledger_errors),
        "ledger_errors": ledger_errors,
    }
