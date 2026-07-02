# -*- coding: utf-8 -*-
"""앱 레지스트리 API — core.apps 위의 얇은 HTTP 껍데기.

앱 폴더(`앱/<등급>/<이름>/`) 목록을 주고, 실행형 앱을 실행/중지한다.
- web-app: `run`이 포트를 열고, 응답의 port를 프런트가 host:port로 브라우저에 연다(Tailscale).
- standalone/external: 서버 호스트에서 실행.
설치파일/실행파일 다운로드는 기존 `/api/files/raw?download=1`을 그대로 쓴다(중복 구현 안 함).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import core.apps as apps

router = APIRouter(prefix="/api/apps", tags=["apps"])


class AppRef(BaseModel):
    dir: str   # 루트 기준 앱 폴더 경로(앱.md가 있는 폴더). run 명령은 서버가 매니페스트에서 읽는다.


class AppInstanceRef(BaseModel):
    dir: str
    pid: int   # 자동감지로 드러난 실행 중 인스턴스의 PID(대시보드 밖 기동 포함)


@router.get("")
def list_apps():
    return apps.list_apps()


@router.post("/run")
def run_app(body: AppRef):
    try:
        return apps.run_app(body.dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stop")
def stop_app(body: AppRef):
    try:
        return apps.stop_app(body.dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/stop-instance")
def stop_instance(body: AppInstanceRef):
    # 자동감지된 특정 PID 인스턴스를 종료(중복 Revit 등을 앱 탭 버튼으로 골라 끄기).
    try:
        return apps.stop_instance(body.dir, body.pid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
