# -*- coding: utf-8 -*-
"""공간 API. core.spaces 위의 얇은 HTTP 껍데기."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
import core.spaces as spaces
import core.room_manager as room_manager
import core.watch as watch
from core import chat_policy
from core import lesson_ledger

router = APIRouter(prefix="/api/spaces", tags=["spaces"])


def _body_data(body: BaseModel, **kwargs):
    if hasattr(body, "model_dump"):
        return body.model_dump(**kwargs)
    return body.dict(**kwargs)


class CreateSpace(BaseModel):
    name: str
    engine: str | None = None
    model: str | None = None


class Join(BaseModel):
    person: str
    space: str


class ManagerRuntime(BaseModel):
    engine: str | None = None
    model: str | None = None


class WorkSettingsUpdate(BaseModel):
    runner_timeout_sec: int | None = None
    heartbeat_interval_sec: int | None = None
    heartbeat_stale_ms: int | None = None
    progress_report_due_ms: int | None = None
    configured_keys: list[str] | None = None


class SpacePost(BaseModel):
    text: str
    requester: str = "대표"
    run_manager: bool = True
    client_message_id: str | None = None


class TextUpdate(BaseModel):
    text: str


class ReleaseReview(BaseModel):
    actor: str = "대표"
    reason: str = ""


class ReleasePublish(BaseModel):
    actor: str = "대표"
    text: str | None = None


class TaskCancel(BaseModel):
    actor: str = "대표"
    reason: str = ""


class TaskSteer(BaseModel):
    actor: str = "대표"
    action: str
    instruction: str = ""


class TaskInstruction(BaseModel):
    actor: str = "대표"
    instruction: str = ""


class TaskWorkSettingsUpdate(WorkSettingsUpdate):
    actor: str = "대표"


class PlanReview(BaseModel):
    actor: str = "대표"
    reason: str = ""


class PromotionScan(BaseModel):
    actor: str = "공간관리"
    limit: int = 20


class PromotionReview(BaseModel):
    actor: str = "대표"
    reason: str = ""


class PromotionApply(BaseModel):
    actor: str = "대표"
    reason: str = "승인된 성장 후보를 리소스로 적용"


@router.get("")
def list_spaces():
    return spaces.list_spaces()


@router.post("")
def create_space(body: CreateSpace):
    return {"토큰": spaces.create_space(body.name, body.engine, body.model)}


@router.delete("/{space}")
def delete_space(space: str):
    return spaces.delete_space(space)


@router.post("/join")
def join(body: Join):
    return {"입장": spaces.join(body.person, body.space)}


@router.patch("/{space}/manager-runtime")
def set_manager_runtime(space: str, body: ManagerRuntime):
    return spaces.set_manager_runtime(space, body.engine, body.model)


@router.get("/{space}/work-settings")
def read_work_settings(space: str):
    return spaces.read_work_settings(space)


@router.patch("/{space}/work-settings")
def set_work_settings(space: str, body: WorkSettingsUpdate):
    result = spaces.set_work_settings(space, _body_data(body, exclude_none=True))
    room_manager.record_space_work_settings_updated(space, result)
    return result


@router.get("/{space}/members/{person}/work-settings")
def read_seat_work_settings(space: str, person: str):
    return spaces.read_seat_work_settings(space, person)


@router.patch("/{space}/members/{person}/work-settings")
def set_seat_work_settings(space: str, person: str, body: WorkSettingsUpdate):
    result = spaces.set_seat_work_settings(space, person, _body_data(body, exclude_none=True))
    room_manager.record_seat_work_settings_updated(space, person, result)
    return result


@router.get("/{space}/guide")
def read_guide(space: str):
    return {"text": spaces.read_guide(space)}


@router.put("/{space}/guide")
def write_guide(space: str, body: TextUpdate):
    return spaces.write_guide(space, body.text)


@router.get("/{space}/messages")
def messages(space: str, limit: int = 80):
    return room_manager.read(space, limit)


@router.get("/{space}/status")
def status(space: str):
    st = room_manager.status(space)
    # 감시 소견(상태칩 가시화)을 status에 얹는다 — 추가 폴링 없이 같은 주기로 전달(없으면 생략).
    try:
        report = watch.read_report(space)
        if report and isinstance(st, dict):
            st["watch_report"] = report
    except Exception:
        pass
    return st


@router.get("/{space}/handback")
def representative_handback(space: str):
    """자동 연속이 매니저 stop으로 끝나 대표에게 넘긴 핸드백 강조 마커."""
    return room_manager.read_representative_handback(space)


@router.get("/{space}/activity")
def activity(space: str, limit: int = 80):
    safe_limit = min(max(int(limit or 80), 1), 200)
    return room_manager.activity(space, safe_limit)


@router.post("/{space}/post")
def post(space: str, body: SpacePost, background_tasks: BackgroundTasks):
    requester = chat_policy.normalize_requester(body.requester)
    should_run_manager = chat_policy.should_run_space_manager(requester, body.run_manager)
    result = room_manager.post(
        space, body.text, requester, run_manager=False,
        client_message_id=body.client_message_id,
        manager_requested=should_run_manager,
    )
    ack = result.get("ack", {})
    context = result.get("orchestration") or {}
    # 대표가 말할 때마다 완료된 (비동기) 작업 결과를 먼저 대화로 흘려보낸다(Phase B reflow on activity).
    # reflow_safe: 예외를 삼켜 뒤따르는 tick 백그라운드 태스크가 끊기지 않게 한다(크로스체크 반영).
    background_tasks.add_task(room_manager.reflow_safe, space)
    if should_run_manager and (not ack.get("duplicate") or result.get("manager_recovery_needed")):
        if result.get("manager_recovery_needed"):
            event = (
                f"{requester} 메시지 중복 재시도에서 manager 미처리 감지"
                f"(event_seq={ack.get('event_seq')}, message_id={ack.get('message_id')}, "
                f"intent_id={ack.get('intent_id')}, room_generation={ack.get('room_generation')}): {body.text.strip()}"
            )
        else:
            event = (
                f"{requester}가 방에 메시지를 남김"
                f"(event_seq={ack.get('event_seq')}, message_id={ack.get('message_id')}, "
                f"intent_id={ack.get('intent_id')}, room_generation={ack.get('room_generation')}): {body.text.strip()}"
            )
        queued = room_manager.queue_manager(space, event, context)
        background_tasks.add_task(
            room_manager.tick,
            space,
            event,
            context,
            auto_continue=True,
        )
        result["events"].append({"type": queued.get("queue_event_type", "manager_queued")})
    return result


@router.post("/{space}/tick")
def tick(space: str):
    return room_manager.tick(space)


@router.get("/{space}/approvals")
def list_approvals(space: str):
    """대표 결재 대기 중인 작업계획(대화창 결재 말풍선 강조·버튼용)."""
    return room_manager.read_approval_required(space)


@router.post("/{space}/plans/{plan_id}/approve")
def approve_plan(space: str, plan_id: str, body: PlanReview, background_tasks: BackgroundTasks):
    """대표가 작업계획을 승인 → 승인·마커해제는 '동기'로 즉시(버튼 바로 반응), 실제 작업 실행만 백그라운드."""
    try:
        from core import work_plan
        plan = work_plan.get(space, plan_id)  # 존재/상태 검증(없으면 400)
        if plan.get("state") not in {work_plan.PENDING, work_plan.APPROVED}:
            raise HTTPException(status_code=409, detail=f"plan not approvable (state={plan.get('state')})")
        room_manager.approve_plan(space, plan_id, actor=body.actor)  # 동기: 승인+마커 즉시 해제
        background_tasks.add_task(room_manager.execute_approved_plan, space, plan_id, actor=body.actor)
        return {"ok": True, "approved": True, "plan_id": plan_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/plans/{plan_id}/reject")
def reject_plan(space: str, plan_id: str, body: PlanReview):
    """대표가 작업계획을 반려 → 실행하지 않고 종결."""
    try:
        record = room_manager.reject_plan(space, plan_id, actor=body.actor, reason=body.reason)
        return {"ok": True, "plan_id": plan_id, "record": record}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/reflow")
def reflow(space: str):
    """완료된 비동기 작업 결과를 대화로 회수·공개(외부 폴러가 주기 호출). 즉시 처리하고 결과 반환."""
    try:
        return room_manager.reflow(space)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/recover")
def recover(space: str, background_tasks: BackgroundTasks):
    """중단된 진행(서버 재시작 등으로 고아가 된 manager_queued/running)을 복구한다."""
    background_tasks.add_task(room_manager.recover_space, space)
    return {"ok": True, "queued": True}


@router.post("/{space}/learning/promotions/scan")
def scan_lesson_promotions(space: str, body: PromotionScan):
    try:
        return lesson_ledger.generate_promotion_candidates(space, actor=body.actor, limit=body.limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/learning/promotions/{promotion_id}/approve")
def approve_lesson_promotion(space: str, promotion_id: str, body: PromotionReview):
    try:
        return lesson_ledger.review_promotion_candidate(
            space,
            promotion_id,
            decision="approve",
            actor=body.actor,
            reason=body.reason,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/learning/promotions/{promotion_id}/reject")
def reject_lesson_promotion(space: str, promotion_id: str, body: PromotionReview):
    try:
        return lesson_ledger.review_promotion_candidate(
            space,
            promotion_id,
            decision="reject",
            actor=body.actor,
            reason=body.reason,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/learning/promotions/{promotion_id}/apply")
def apply_lesson_promotion(space: str, promotion_id: str, body: PromotionApply):
    try:
        return lesson_ledger.apply_promotion_candidate(
            space,
            promotion_id,
            actor=body.actor,
            reason=body.reason,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/releases/{release_id}/approve")
def approve_release(space: str, release_id: str, body: ReleaseReview):
    try:
        return room_manager.approve_release(space, release_id, actor=body.actor, reason=body.reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/releases/{release_id}/reject")
def reject_release(space: str, release_id: str, body: ReleaseReview):
    try:
        return room_manager.reject_release(space, release_id, actor=body.actor, reason=body.reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/releases/{release_id}/publish")
def publish_release(space: str, release_id: str, body: ReleasePublish):
    try:
        return room_manager.publish_release(space, release_id, actor=body.actor, text=body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/tasks/{task_id}/cancel")
def cancel_task(space: str, task_id: str, body: TaskCancel):
    try:
        return room_manager.request_task_cancel(space, task_id, actor=body.actor, reason=body.reason)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/tasks/{task_id}/steer")
def steer_task(space: str, task_id: str, body: TaskSteer):
    try:
        return room_manager.request_task_steering(
            space,
            task_id,
            action=body.action,
            instruction=body.instruction,
            actor=body.actor,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/tasks/{task_id}/progress")
def request_task_progress(space: str, task_id: str, body: TaskInstruction):
    try:
        return room_manager.request_task_steering(
            space,
            task_id,
            action="request_progress",
            instruction=body.instruction,
            actor=body.actor,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{space}/tasks/{task_id}/revise")
def revise_task(space: str, task_id: str, body: TaskInstruction):
    try:
        return room_manager.request_task_steering(
            space,
            task_id,
            action="revise_task",
            instruction=body.instruction,
            actor=body.actor,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{space}/tasks/{task_id}/work-settings")
def update_task_work_settings(space: str, task_id: str, body: TaskWorkSettingsUpdate):
    try:
        data = _body_data(body, exclude_none=True)
        actor = data.pop("actor", "대표")
        return room_manager.update_task_work_settings(space, task_id, data, actor=actor)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
