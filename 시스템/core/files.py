# -*- coding: utf-8 -*-
"""파일 탐색 — 루트폴더 기준 상대경로로만 다룬다 (경로 탈출 방어)."""
import re
from datetime import datetime
from pathlib import Path
from .paths import ROOT


def _safe(rel: str) -> Path:
    rel = (rel or "").strip().lstrip("/")
    target = (ROOT / rel).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError("루트폴더 밖 경로는 허용되지 않음")
    return target


def list_dir(rel: str = ""):
    target = _safe(rel)
    if not target.exists():
        raise ValueError(f"없는 경로: {rel}")
    if target.is_file():
        raise ValueError("폴더 경로를 지정하라")
    items = []
    for e in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            st = e.stat()
        except OSError:
            continue
        items.append({
            "이름": e.name,
            "종류": "dir" if e.is_dir() else "file",
            "크기": (st.st_size if e.is_file() else None),
            "수정": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "경로": str(e.relative_to(ROOT)),
        })
    rel_path = "" if target == ROOT else str(target.relative_to(ROOT))
    parent = None if target == ROOT else ("" if target.parent == ROOT else str(target.parent.relative_to(ROOT)))
    return {"경로": rel_path, "상위": parent, "항목": items}


def resolve_file(rel: str) -> Path:
    target = _safe(rel)
    if not target.is_file():
        raise ValueError(f"파일 아님: {rel}")
    return target


def save_upload(rel_dir: str, filename: str, content: bytes) -> str:
    """업로드 파일을 rel_dir(루트 기준) 아래에 저장하고 루트 기준 상대경로를 반환한다.

    파일명은 경로 구분자/특수문자를 제거(traversal 방어), 중복 시 _N 으로 회피.
    """
    d = _safe(rel_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = Path(filename or "file").name          # 디렉터리 성분 제거
    name = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", name).strip("_") or "file"
    target = d / name
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        i = 1
        while (d / f"{stem}_{i}{ext}").exists():
            i += 1
        target = d / f"{stem}_{i}{ext}"
    target.write_bytes(content)
    return str(target.relative_to(ROOT))
