# -*- coding: utf-8 -*-
"""서버 상태 확인."""
from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health():
    return {"ok": True, "app": "CnvAgentWorld"}
