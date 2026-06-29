# -*- coding: utf-8 -*-
"""엔진·모델 런타임 API."""
from fastapi import APIRouter
import core.runtime as runtime

router = APIRouter(prefix="/api", tags=["runtime"])


@router.get("/engine-models")
def engine_models():
    return runtime.catalog()
