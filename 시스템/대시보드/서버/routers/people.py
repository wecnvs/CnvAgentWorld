# -*- coding: utf-8 -*-
"""에이전트 API. core.people 위의 얇은 HTTP 껍데기."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
import core.people as people

router = APIRouter(prefix="/api/people", tags=["people"])


def _body_data(body: BaseModel, **kwargs):
    if hasattr(body, "model_dump"):
        return body.model_dump(**kwargs)
    return body.dict(**kwargs)


class CreatePerson(BaseModel):
    name: str
    engine: str | None = None
    model: str | None = None


class RuntimeUpdate(BaseModel):
    engine: str | None = None
    model: str | None = None


class WorkSettingsUpdate(BaseModel):
    runner_timeout_sec: int | None = None
    heartbeat_interval_sec: int | None = None
    heartbeat_stale_ms: int | None = None
    progress_report_due_ms: int | None = None
    progress_bubble_after_ms: int | None = None
    progress_bubble_interval_ms: int | None = None
    configured_keys: list[str] | None = None


class TextUpdate(BaseModel):
    text: str


@router.get("")
def list_people():
    return people.list_people()


@router.post("")
def create_person(body: CreatePerson):
    return {"토큰": people.create_person(body.name, body.engine, body.model)}


@router.delete("/{person}")
def delete_person(person: str):
    return people.delete_person(person)


@router.patch("/{person}/runtime")
def set_runtime(person: str, body: RuntimeUpdate):
    return people.set_runtime(person, body.engine, body.model)


@router.get("/{person}/work-settings")
def read_work_settings(person: str):
    return people.read_work_settings(person)


@router.patch("/{person}/work-settings")
def set_work_settings(person: str, body: WorkSettingsUpdate):
    return people.set_work_settings(person, _body_data(body, exclude_none=True))


@router.get("/{person}/role")
def read_role(person: str):
    return {"text": people.read_role(person)}


@router.put("/{person}/role")
def write_role(person: str, body: TextUpdate):
    return people.write_role(person, body.text)
