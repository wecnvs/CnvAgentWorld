# -*- coding: utf-8 -*-
"""파일 탐색기 API. core.files 위의 얇은 HTTP 껍데기 (루트폴더 기준 경로)."""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
import subprocess
from pathlib import Path
from fastapi import APIRouter, Query, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import core.files as files

router = APIRouter(prefix="/api/files", tags=["files"])

# 브라우저가 그대로 미리볼 수 있는 형식(변환 불필요)
_DIRECT_PREVIEW_EXT = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "html", "htm",
                       "md", "txt", "csv", "json", "log", "yaml", "yml", "tsv", "mp4", "webm", "mp3", "wav", "ogg"}
# soffice(LibreOffice)로 PDF 변환해 미리볼 수 있는 office 형식
_OFFICE_EXT = {"pptx", "ppt", "pptm", "docx", "doc", "xlsx", "xls", "odp", "odt", "ods", "rtf"}
_SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


def _convert_office_to_pdf(src: Path) -> Path | None:
    """office 문서를 PDF로 변환(파일 mtime 기준 캐시). soffice 동시 실행 충돌은 격리 프로필로 회피."""
    if not _SOFFICE:
        return None
    cache_dir = src.parent / ".preview"
    try:
        cache_dir.mkdir(exist_ok=True)
    except Exception:
        return None
    stamp = hashlib.sha1(f"{src.name}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:12]
    out = cache_dir / f"{src.stem}.{stamp}.pdf"
    if out.exists():
        return out
    profile = cache_dir / f".soffice_profile_{stamp}"
    try:
        subprocess.run(
            [_SOFFICE, f"-env:UserInstallation=file://{profile}", "--headless",
             "--convert-to", "pdf", "--outdir", str(cache_dir), str(src)],
            timeout=90, capture_output=True,
        )
    except Exception:
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    produced = cache_dir / f"{src.stem}.pdf"
    if produced.exists():
        try:
            produced.replace(out)
            return out
        except Exception:
            return produced
    return None


@router.get("")
def list_dir(path: str = Query("")):
    return files.list_dir(path)


@router.post("/upload")
async def upload(file: UploadFile = File(...), dir: str = Form("자산/추가/업로드")):
    """파일을 dir(루트 기준) 아래 저장하고 루트 기준 경로 반환. 그 경로를 말풍선 본문에 적으면 미리보기됨."""
    try:
        content = await file.read()
        path = files.save_upload(dir, file.filename or "file", content)
        return {"ok": True, "path": path, "name": file.filename, "size": len(content)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/raw")
def raw(path: str = Query(...), download: int = Query(0)):
    f = files.resolve_file(path)
    mime, _ = mimetypes.guess_type(f.name)
    media = mime or "application/octet-stream"
    if download:
        return FileResponse(str(f), filename=f.name, media_type=media)
    return FileResponse(str(f), media_type=media)


@router.get("/preview")
def preview(path: str = Query(...)):
    """말풍선 미리보기용. 그대로 볼 수 있으면 원본을, office 문서는 PDF로 변환해 반환.
    미리보기 불가 형식(zip 등)은 415 — 프런트는 파일카드로 폴백한다."""
    f = files.resolve_file(path)
    ext = f.suffix.lower().lstrip(".")
    if ext in _DIRECT_PREVIEW_EXT:
        media = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        return FileResponse(str(f), media_type=media)
    if ext in _OFFICE_EXT:
        pdf = _convert_office_to_pdf(f)
        if pdf and pdf.exists():
            return FileResponse(str(pdf), media_type="application/pdf",
                                headers={"X-Preview-Converted": "pdf"})
        raise HTTPException(status_code=503, detail="office 미리보기 변환 실패(soffice 확인)")
    raise HTTPException(status_code=415, detail=f"미리보기 불가 형식: {ext}")


@router.get("/preview-kinds")
def preview_kinds():
    """프런트가 어떤 확장자를 어떤 방식으로 미리보기할지 알 수 있게 노출."""
    return {
        "direct": sorted(_DIRECT_PREVIEW_EXT),
        "office_to_pdf": sorted(_OFFICE_EXT) if _SOFFICE else [],
        "soffice_available": bool(_SOFFICE),
    }
