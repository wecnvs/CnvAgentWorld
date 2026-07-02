# -*- coding: utf-8 -*-
"""공간 생성/조회와 입장(join). 공간이 없으면 대화가 일어나지 않는다."""
from __future__ import annotations

import json
import shutil
from .paths import PEOPLE, SPACES, ENGINE_ENTRY
from .codes import gen_code, split_token
from . import runtime, templates, work_settings
from .transcript import state as transcript_state, now_iso, with_space_lock


def _write_entry(folder, body):
    for fn in ENGINE_ENTRY:
        (folder / fn).write_text(body, encoding="utf-8")


MANAGER_DIRNAME = "관리자"
PROJECTION_BASELINE_FILENAME = "projection_baseline.json"


def _safe_space_dir(space: str):
    token = str(space or "").strip()
    if not token or "/" in token or "\\" in token or token in {".", ".."}:
        raise ValueError(f"공간 토큰이 올바르지 않음: {space}")
    return SPACES / token


def _safe_person_dir(person: str):
    token = str(person or "").strip()
    if not token or "/" in token or "\\" in token or token in {".", ".."}:
        raise ValueError(f"에이전트 토큰이 올바르지 않음: {person}")
    return PEOPLE / token


def _ensure_manager(space_dir, space_token: str, engine: str | None = None, model: str | None = None):
    manager = space_dir / MANAGER_DIRNAME
    manager.mkdir(parents=True, exist_ok=True)
    _write_entry(manager, templates.fill(templates.load("공간관리_진입점.md"), 공간표시=space_token))
    if engine or model or not (manager / "agent_runtime.json").exists():
        runtime.write_runtime(manager, engine, model, source=f"space-manager:{space_token}")
    state = manager / "상태.json"
    if not state.exists():
        state.write_text(json.dumps({"상태": "idle"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log = manager / "진행기록.jsonl"
    if not log.exists():
        log.write_text("", encoding="utf-8")
    return manager


def create_space(name: str, engine: str | None = None, model: str | None = None) -> str:
    name = name.strip().replace(" ", "")
    if not name:
        raise ValueError("이름이 비었음")
    token = f"{name}_{gen_code()}"
    d = SPACES / token
    if d.exists():
        raise ValueError(f"이미 존재: {token}")
    (d / "공유파일").mkdir(parents=True)
    (d / "공간지침.md").write_text(
        templates.fill(templates.load("공간지침.md"), 공간표시=token, 공간이름=name), encoding="utf-8")
    (d / "멤버.json").write_text(json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "대화.jsonl").write_text("", encoding="utf-8")
    (d / "요약.md").write_text(f"# {token} 요약\n\n(아직 요약 없음)\n", encoding="utf-8")
    work_settings.write_space_settings(token, {}, source=f"space-create:{token}")
    _ensure_manager(d, token, engine, model)
    return token


def delete_space(space: str) -> dict:
    sdir = _safe_space_dir(space)
    if not sdir.exists() or not sdir.is_dir():
        raise ValueError(f"공간 없음: {space}")
    removed_seats = []

    def mutate():
        for pdir in sorted(PEOPLE.iterdir()) if PEOPLE.exists() else []:
            if not pdir.is_dir():
                continue
            seat = pdir / "공간" / sdir.name
            if seat.exists() and seat.is_dir():
                shutil.rmtree(seat)
                removed_seats.append(pdir.name)
        shutil.rmtree(sdir)
        return True

    with_space_lock(sdir.name, mutate)
    return {"ok": True, "deleted": sdir.name, "removed_seats": removed_seats}


def list_spaces():
    if not SPACES.exists():
        return []
    out = []
    for s in sorted(SPACES.iterdir()):
        if not s.is_dir():
            continue
        try:
            nm, cd = split_token(s.name)
            mf = s / "멤버.json"
            members = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else []
            enriched_members = []
            for member in members:
                if not isinstance(member, dict):
                    continue
                item = dict(member)
                token = item.get("토큰", "")
                if token:
                    item["작업설정"] = work_settings.resolve_work_settings(s.name, token)
                    item["좌석작업설정"] = work_settings.read_seat_settings(token, s.name)
                enriched_members.append(item)
            manager = _ensure_manager(s, s.name)
            rt = runtime.read_runtime(manager)
            out.append({"토큰": s.name, "이름": nm, "코드": cd, "멤버": enriched_members})
            out[-1]["관리자"] = {"engine": rt["engine"], "model": rt["model"]}
            out[-1]["작업설정"] = work_settings.read_space_settings(s.name)
        except Exception as exc:
            # 깨진 공간 하나가 목록 전체를 500으로 죽이지 않게 건너뛰되, 흔적은 남긴다.
            out.append({"토큰": s.name, "이름": s.name, "코드": "", "멤버": [],
                        "오류": f"{type(exc).__name__}: {str(exc)[:120]}"})
    return out


def set_manager_runtime(space: str, engine: str | None = None, model: str | None = None) -> dict:
    sdir = _safe_space_dir(space)
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    return runtime.write_runtime(sdir / MANAGER_DIRNAME, engine, model, source=f"space-manager:{space}")


def read_work_settings(space: str) -> dict:
    sdir = _safe_space_dir(space)
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    return work_settings.read_space_settings(space)


def set_work_settings(space: str, settings: dict | None = None) -> dict:
    sdir = _safe_space_dir(space)
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    return work_settings.write_space_settings(space, settings, source=f"space-work-settings:{space}")


def _seat_dir(space: str, person: str):
    pdir = _safe_person_dir(person)
    sdir = _safe_space_dir(space)
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    if not pdir.exists():
        raise ValueError(f"에이전트 없음: {person}")
    seat = pdir / "공간" / sdir.name
    if not seat.exists():
        raise ValueError(f"입장 안 됨: {person} -> {space}")
    return seat


def read_seat_work_settings(space: str, person: str) -> dict:
    _seat_dir(space, person)
    seat_settings = work_settings.read_seat_settings(person, space)
    effective_settings = work_settings.resolve_work_settings(space, person)
    return {
        **effective_settings,
        "space": space,
        "person": person,
        "seat_settings": seat_settings,
        "effective_settings": effective_settings,
    }


def set_seat_work_settings(space: str, person: str, settings: dict | None = None) -> dict:
    _seat_dir(space, person)
    seat_settings = work_settings.write_seat_settings(
        person,
        space,
        settings,
        source=f"seat-work-settings:{person}->{space}",
    )
    effective_settings = work_settings.resolve_work_settings(space, person)
    return {
        **effective_settings,
        "space": space,
        "person": person,
        "seat_settings": seat_settings,
        "effective_settings": effective_settings,
    }


def _seat_projection_baseline(space: str, person: str) -> dict:
    delivery = transcript_state(space)
    return {
        "schema": "SeatProjectionBaseline.v1",
        "space": space,
        "person": person,
        "baseline_event_seq": int(delivery.get("last_event_seq") or 0),
        "baseline_message_id": delivery.get("last_message_id", ""),
        "baseline_message_count": int(delivery.get("message_count") or 0),
        "created_at": now_iso(),
        "reason": "late_join_projection_baseline",
    }


def read_guide(space: str) -> str:
    p = SPACES / space / "공간지침.md"
    if not p.exists():
        raise ValueError(f"공간지침 없음: {space}")
    return p.read_text(encoding="utf-8")


def write_guide(space: str, text: str) -> dict:
    sdir = _safe_space_dir(space)
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")
    (sdir / "공간지침.md").write_text(text or "", encoding="utf-8")
    return {"ok": True}


def read_summary(space: str) -> dict:
    """방 누적 요약(요약.md) 읽기 — 대시보드 '방요약' 패널용(조회 전용).

    요약.md는 사회자·시스템이 유지하는 방 맥락의 정본인데 종전엔 UI 어디에서도 보여주지 않아,
    대표가 '방이 맥락을 제대로 요약·이해하고 있는지'를 확인할 길이 없었다(목표.md 요구 9의 가시화)."""
    sdir = _safe_space_dir(space)
    p = sdir / "요약.md"
    if not p.exists():
        return {"summary": "", "updated_at": ""}
    try:
        from datetime import datetime
        mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        mtime = ""
    return {"summary": p.read_text(encoding="utf-8"), "updated_at": mtime}


def join(person: str, space: str) -> bool:
    pdir = _safe_person_dir(person)
    sdir = _safe_space_dir(space)
    if not pdir.exists():
        raise ValueError(f"에이전트 없음: {person}")
    if not sdir.exists():
        raise ValueError(f"공간 없음: {space}")

    def mutate():
        seat = pdir / "공간" / sdir.name
        if seat.exists():
            return False  # 이미 입장
        projection_baseline = _seat_projection_baseline(sdir.name, pdir.name)
        (seat / "작업").mkdir(parents=True)
        _write_entry(seat, templates.fill(templates.load("채팅_진입점.md"), 사람표시=pdir.name, 공간표시=sdir.name))
        runtime.copy_runtime(pdir, seat, source=f"seat:{person}->{space}")
        work_settings.write_seat_settings(pdir.name, sdir.name, {}, source=f"seat-create:{person}->{space}")
        (seat / PROJECTION_BASELINE_FILENAME).write_text(
            json.dumps(projection_baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (seat / "대화.jsonl").write_text("", encoding="utf-8")
        (seat / "요약.md").write_text(f"# {sdir.name} 요약\n\n(아직 요약 없음)\n", encoding="utf-8")
        mf = sdir / "멤버.json"
        members = json.loads(mf.read_text(encoding="utf-8"))
        nm, cd = split_token(pdir.name)
        if not any(m.get("토큰") == pdir.name for m in members if isinstance(m, dict)):
            members.append({
                "이름": nm,
                "코드": cd,
                "토큰": pdir.name,
                "projection_baseline_event_seq": projection_baseline["baseline_event_seq"],
                "projection_baseline_message_count": projection_baseline["baseline_message_count"],
                "joined_at": projection_baseline["created_at"],
            })
            mf.write_text(json.dumps(members, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    return with_space_lock(sdir.name, mutate)
