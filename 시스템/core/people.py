# -*- coding: utf-8 -*-
"""에이전트(독립 캐릭터) 생성/조회. 한 폴더 = 한 에이전트."""
from __future__ import annotations

import json
import shutil

from .paths import PEOPLE, SPACES
from .codes import gen_code, split_token
from . import runtime, templates, work_settings
from .transcript import with_space_lock


def _safe_person_dir(person: str):
    token = str(person or "").strip()
    if not token or "/" in token or "\\" in token or token in {".", ".."}:
        raise ValueError(f"에이전트 토큰이 올바르지 않음: {person}")
    return PEOPLE / token


def create_person(name: str, engine: str = None, model: str = None) -> str:
    name = name.strip().replace(" ", "")
    if not name:
        raise ValueError("이름이 비었음")
    runtime.normalize_engine(engine)
    token = f"{name}_{gen_code()}"
    d = PEOPLE / token
    if d.exists():
        raise ValueError(f"이미 존재: {token}")
    (d / "공간").mkdir(parents=True)
    nm, cd = split_token(token)
    (d / "role.md").write_text(
        templates.fill(templates.load("role.md"), 이름=nm, 코드=cd), encoding="utf-8")
    runtime.write_runtime(d, engine, model, source="person")
    work_settings.write_person_settings(token, {}, source=f"person-create:{token}")
    return token


def delete_person(person: str) -> dict:
    pdir = _safe_person_dir(person)
    if not pdir.exists() or not pdir.is_dir():
        raise ValueError(f"에이전트 없음: {person}")
    _nm, code = split_token(pdir.name)
    removed_spaces = []

    for sdir in sorted(SPACES.iterdir()) if SPACES.exists() else []:
        if not sdir.is_dir():
            continue

        def mutate(space_dir=sdir):
            mf = space_dir / "멤버.json"
            if not mf.exists():
                return False
            try:
                members = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                return False
            if not isinstance(members, list):
                return False
            kept = []
            for member in members:
                if not isinstance(member, dict):
                    kept.append(member)
                    continue
                member_token = member.get("토큰", "")
                legacy_code_match = not member_token and code and member.get("코드") == code
                if member_token == pdir.name or legacy_code_match:
                    continue
                kept.append(member)
            if len(kept) == len(members):
                return False
            mf.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
            return True

        try:
            if with_space_lock(sdir.name, mutate):
                removed_spaces.append(sdir.name)
        except Exception:
            continue

    shutil.rmtree(pdir)
    return {"ok": True, "deleted": pdir.name, "removed_from_spaces": removed_spaces}


def list_people():
    if not PEOPLE.exists():
        return []
    out = []
    for p in sorted(PEOPLE.iterdir()):
        if not p.is_dir():
            continue
        nm, cd = split_token(p.name)
        sp = [s.name for s in (p / "공간").iterdir() if s.is_dir()] if (p / "공간").exists() else []
        rt = runtime.read_runtime(p)
        ws = work_settings.read_person_settings(p.name)
        out.append({"토큰": p.name, "이름": nm, "코드": cd, "공간": sp,
                    "engine": rt["engine"], "model": rt["model"], "작업설정": ws})
    return out


def set_runtime(person: str, engine: str | None = None, model: str | None = None) -> dict:
    p = _safe_person_dir(person)
    if not p.exists():
        raise ValueError(f"에이전트 없음: {person}")
    data = runtime.write_runtime(p, engine, model, source=f"person-update:{person}")
    for seat in (p / "공간").iterdir() if (p / "공간").exists() else []:
        if seat.is_dir() and (seat / "agent_runtime.json").exists():
            runtime.write_runtime(seat, data["engine"], data["model"], source=f"person-update-seat:{person}")
    return data


def read_role(person: str) -> str:
    p = _safe_person_dir(person) / "role.md"
    if not p.exists():
        raise ValueError(f"role 없음: {person}")
    return p.read_text(encoding="utf-8")


def write_role(person: str, text: str) -> dict:
    pdir = _safe_person_dir(person)
    if not pdir.exists():
        raise ValueError(f"에이전트 없음: {person}")
    (pdir / "role.md").write_text(text or "", encoding="utf-8")
    return {"ok": True}


def read_work_settings(person: str) -> dict:
    pdir = _safe_person_dir(person)
    if not pdir.exists():
        raise ValueError(f"에이전트 없음: {person}")
    return work_settings.read_person_settings(person)


def set_work_settings(person: str, settings: dict | None = None) -> dict:
    pdir = _safe_person_dir(person)
    if not pdir.exists():
        raise ValueError(f"에이전트 없음: {person}")
    return work_settings.write_person_settings(person, settings, source=f"person-work-settings:{person}")
