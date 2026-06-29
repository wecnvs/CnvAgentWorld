#!/opt/homebrew/bin/python3.13
"""
오버레이 로그 기록
사용법:
  log.py "메시지"          → 로그 기록 + 오버레이 표시
  log.py --think "내용"   → AI 판단/분석 (💭 prefix)
  log.py --done "요약"    → 완료 표시 (오버레이는 유지 — 사용자가 직접 닫음)
"""
import sys
import time
from pathlib import Path

LOG_FILE    = "/tmp/cu_overlay.log"
ACTIVE_FILE = "/tmp/cu_active"
MAX_LINES   = 300

def _write(line: str):
    p = Path(LOG_FILE)
    existing = p.read_text("utf-8", errors="replace").splitlines() if p.exists() else []
    existing.append(line)
    if len(existing) > MAX_LINES:
        existing = existing[-MAX_LINES:]
    p.write_text("\n".join(existing) + "\n", "utf-8")
    print(line)

args = sys.argv[1:]
ts   = time.strftime("%H:%M:%S")

if not args:
    sys.exit(0)

# --think: AI 판단/분석 내용 (오버레이에 💭 로 표시)
if args[0] == "--think":
    msg  = " ".join(args[1:])
    Path(ACTIVE_FILE).touch()
    _write(f"[{ts}] 💭 {msg}")

# --done: 완료 표시 + 마커 제거 → 오버레이 자동 숨김
elif args[0] == "--done":
    summary = " ".join(args[1:]) if len(args) > 1 else "작업 완료"
    _write(f"[{ts}] ✅ {summary}")
    Path(ACTIVE_FILE).unlink(missing_ok=True)

# 일반 메시지
else:
    msg = " ".join(args)
    Path(ACTIVE_FILE).touch()
    _write(f"[{ts}] {msg}")
