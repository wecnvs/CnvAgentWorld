# -*- coding: utf-8 -*-
"""파일 탐색 — 루트폴더 기준 상대경로로만 다룬다 (경로 탈출 방어)."""
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from .paths import ROOT


def _safe(rel: str) -> Path:
    rel = (rel or "").strip().lstrip("/")
    target = (ROOT / rel).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError("루트폴더 밖 경로는 허용되지 않음")
    return target


# --- 대외비 읽기 가드 (law.md §7 보안 불변식) ---
# 파일 API(HTTP)는 무인증이라, 자격증명·개인정보가 담기는 대외비 등급과 민감 사이드카는
# 이 경로(list/read)로 내보내지 않는다. 쓰기(업로드)는 막지 않는다 — 유출 방향만 차단.
# 비상 복구용 킬스위치: CNV_FILES_ALLOW_CONFIDENTIAL=1
#
# [주의] macOS(APFS)는 유니코드 정규화·대소문자 무관이라, 비교 전 반드시 NFC 정규화 + casefold 한다.
# NFD로 분해한 "대외비"나 대소문자 바꾼 사이드카 파일명으로 가드를 우회하는 걸 막는다(크로스체크 CRITICAL-1/3).
_CONFIDENTIAL_DIR = unicodedata.normalize("NFC", "대외비")
_CONFIDENTIAL_FILES = {"cases.local.jsonl", "cases.archive.jsonl", "case_events.jsonl", "claim_events.jsonl"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").casefold()


def _confidential_allowed() -> bool:
    return os.environ.get("CNV_FILES_ALLOW_CONFIDENTIAL", "") == "1"


def _guard_confidential(target: Path) -> None:
    if _confidential_allowed():
        return
    rel_parts = target.relative_to(ROOT).parts if target != ROOT else ()
    if _norm(_CONFIDENTIAL_DIR) in {_norm(p) for p in rel_parts}:
        raise ValueError("대외비 경로는 파일 API로 열람할 수 없음 (로컬에서 직접 확인)")
    if _norm(target.name) in {_norm(f) for f in _CONFIDENTIAL_FILES}:
        raise ValueError("민감 사이드카 파일은 파일 API로 열람할 수 없음")


def list_dir(rel: str = ""):
    target = _safe(rel)
    _guard_confidential(target)
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
    _guard_confidential(target)
    if not target.is_file():
        raise ValueError(f"파일 아님: {rel}")
    return target


# 업로드 최대 용량 가드 — 실수(수십 GB 통짜 첨부)로 디스크/전송이 마비되는 것만 막는 넉넉한 상한.
# 스트리밍 저장이라 메모리는 청크(4MB)만 쓴다(종전 read() 통짜 적재는 수백 MB zip에서 서버 메모리 위험).
MAX_UPLOAD_BYTES = 4 * 1024 ** 3   # 4GB


def _sanitize_component(name: str) -> str:
    name = Path(name or "file").name              # 디렉터리 성분 제거(traversal 방어)
    return re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", name).strip("_") or "file"


def _unique_target(d: Path, name: str) -> Path:
    target = d / name
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        i = 1
        while (d / f"{stem}_{i}{ext}").exists():
            i += 1
        target = d / f"{stem}_{i}{ext}"
    return target


def save_upload(rel_dir: str, filename: str, content: bytes) -> str:
    """업로드 파일을 rel_dir(루트 기준) 아래에 저장하고 루트 기준 상대경로 반환(소용량·하위호환 래퍼)."""
    import io
    return save_upload_stream(rel_dir, filename, io.BytesIO(content))


def save_upload_stream(rel_dir: str, filename: str, fileobj, *, relpath: str = "",
                       max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    """업로드 파일을 스트리밍으로 저장한다(대용량 zip·영상 대응 — 통짜 메모리 적재 금지).

    relpath: 폴더 첨부(webkitRelativePath) — 하위 폴더 구조를 보존해 저장한다.
    각 경로 성분은 개별 소독(traversal 방어), 파일명 중복 시 _N 회피, max_bytes 초과 시 중단·삭제.
    """
    d = _safe(rel_dir)
    if relpath:
        parts = [p for p in Path(relpath).parts if p not in ("..", ".", "/", "\\")]
        if parts:
            filename = parts[-1]
            for comp in parts[:-1]:
                d = d / _sanitize_component(comp)
    d.mkdir(parents=True, exist_ok=True)
    target = _unique_target(d, _sanitize_component(filename))
    written = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = fileobj.read(4 * 1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"업로드 최대 용량 초과 — {max_bytes // 1024 ** 3}GB까지 가능")
                out.write(chunk)
    except Exception:
        try:
            target.unlink()
        except Exception:
            pass
        raise
    return str(target.relative_to(ROOT))
