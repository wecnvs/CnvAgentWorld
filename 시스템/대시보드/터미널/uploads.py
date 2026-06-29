# -*- coding: utf-8 -*-
"""터미널 파일 업로드 저장."""
import os
import re
import uuid

from config import MAX_UPLOAD, UPLOAD_DIR


async def save_raw_upload(request):
    ext = (request.query_params.get("ext") or ".bin").strip()[:12]
    if not ext.startswith("."):
        ext = "." + ext
    if not re.match(r"^\.[A-Za-z0-9]{1,11}$", ext):
        ext = ".bin"
    data = await request.body()
    if not data:
        return {"ok": False, "error": "빈 파일"}, 400
    if len(data) > MAX_UPLOAD:
        return {"ok": False, "error": "30MB 초과"}, 413
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(UPLOAD_DIR, uuid.uuid4().hex[:12] + ext)
    with open(dest, "wb") as f:
        f.write(data)
    return {"ok": True, "path": dest, "size": len(data)}, 200
