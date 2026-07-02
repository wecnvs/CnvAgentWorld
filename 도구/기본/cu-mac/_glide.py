#!/opt/homebrew/bin/python3.13
"""사람처럼 보이는 커서 글라이드 — 명시적 보간(현재→목표를 여러 스텝으로) 이동.

왜 (2026-07-02 실증):
- 원격 화면제어 그리드는 ~5fps로 캡처된다. cliclick `-e easing`(기본 5)은 이동이 0.4초 안에 끝나
  1~2프레임 만에 사라져 **순간이동처럼** 보인다 → 대표가 "제어 중인 게 안 보인다"고 지적.
- easing의 시간 매핑이 불투명해 값 추측이 위험하므로, 여기서 **현재 커서 위치→목표**를 N스텝으로
  직접 보간해 `m: … w: …` 체인으로 내보낸다. duration을 우리가 정확히 제어 → 그리드에 부드럽게 보인다.
- 이동+클릭을 한 cliclick 호출로 묶으면 원자적이라, move 후 배경폴링 사이 커서가 드리프트하지 않는다.

기본 duration은 '지켜보는 CU'에 맞춰 사람 속도(≈1.0초). 빠른 무관전 조작이 필요하면 호출부가 duration을
낮춘다(--duration). 접근성 권한이 없어 현재 위치를 못 읽으면 cliclick 내장 easing으로 폴백한다.
"""
from __future__ import annotations

import subprocess

CLICLICK = "/opt/homebrew/bin/cliclick"
# 사람이 마우스 옮기는 자연스러운 속도(≈0.7초). 1.8초는 너무 느려 답답했음(대표 피드백).
# 그리드가 부드럽게 보이는 건 이동시간을 늘리는 게 아니라 그리드 fps를 올려 해결한다(cu_remote 내부캡처).
# 빠른 무관전 조작은 호출부가 --duration 낮추고, 아주 천천히 보여줄 땐 높인다.
DEFAULT_DURATION = 0.7
STEP_SECONDS = 0.02             # 스텝 간격(≈50스텝/초) — 물리 화면에서 부드럽게(그리드는 fps개선으로 커버)


def current_pos():
    """현재 커서 좌표 (x, y). 접근성 권한 없거나 실패 시 None."""
    try:
        out = subprocess.run([CLICLICK, "p:"], capture_output=True, text=True, timeout=3)
        txt = (out.stdout or "").strip().splitlines()[-1]
        x, y = txt.split(",")
        return int(x), int(y)
    except Exception:
        return None


def glide_tokens(x: int, y: int, duration: float = DEFAULT_DURATION) -> list[str]:
    """현재 위치→(x,y)를 사람처럼 보간 이동하는 cliclick 커맨드 토큰들.

    현재 위치를 못 읽으면 내장 easing 한 방 이동으로 폴백(['-e','N','m:x,y']는 아니고 여기선 m:만 —
    호출부가 -e를 붙이도록 fallback_easing()을 제공).
    """
    pos = current_pos()
    if pos is None:
        return [f"m:{x},{y}"]                       # 폴백(호출부에서 -e easing 부여)
    cx, cy = pos
    if cx == x and cy == y:
        return [f"m:{x},{y}"]
    steps = max(8, int(duration / STEP_SECONDS))
    dt = max(10, int(duration * 1000 / steps))
    toks: list[str] = []
    for i in range(1, steps + 1):
        ix = int(round(cx + (x - cx) * i / steps))
        iy = int(round(cy + (y - cy) * i / steps))
        toks.append(f"m:{ix},{iy}")
        if i < steps:
            toks.append(f"w:{dt}")
    return toks


def fallback_easing(duration: float) -> int:
    """현재위치 못 읽을 때 쓸 cliclick 내장 easing 값(그리드 가시성 위해 종전보다 크게)."""
    return max(20, int(duration * 30))


def is_fallback(tokens: list[str]) -> bool:
    """glide_tokens가 폴백(단일 m:)을 냈는지 — 호출부가 -e를 붙일지 판단."""
    return len(tokens) == 1 and tokens[0].startswith("m:")
