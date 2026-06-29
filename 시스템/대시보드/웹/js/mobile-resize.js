// 모바일 전용: 적층된 영역의 높이를 스플릿바로 드래그 조절한다.
// 데스크탑 컬럼 '너비' 조절은 viewer.js가 담당(거기 핸들러는 모바일이면 early-return).
// 이 모듈은 모바일 세로 '높이'만 책임진다 — 같은 .workspace-splitter에 핸들러를 얹되
// 뷰포트로 갈라(데스크탑이면 early-return) 서로 안 부딪힌다.

const MOBILE_MQ = window.matchMedia("(max-width: 720px)");
const STORE_KEY = "cnv.mobilePanelHeights";
const MIN_DVH = 12;
const MAX_DVH = 88;
// 스플릿바의 '위쪽(left)' 패널 키 → 높이 CSS 변수. 마지막 패널(viewer)은 아래 바가 없어 비조절.
const PANEL_VAR = { agents: "--m-h-agents", spaces: "--m-h-spaces", chat: "--m-h-chat" };
const DEFAULTS = { agents: 30, spaces: 30, chat: 70 };

function loadHeights() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORE_KEY) || "{}") };
  } catch (_) {
    return { ...DEFAULTS };
  }
}

function applyHeight(key, dvh) {
  document.documentElement.style.setProperty(PANEL_VAR[key], `${dvh}dvh`);
}

function clamp(v) {
  return Math.min(MAX_DVH, Math.max(MIN_DVH, Math.round(v)));
}

function startDrag(bar, heights, e) {
  if (!MOBILE_MQ.matches) return;            // 데스크탑은 viewer.js(너비)가 처리
  const key = bar.dataset.splitterLeft;      // 바 '위쪽' 패널을 조절
  if (!(key in PANEL_VAR)) return;
  e.preventDefault();
  const vhPx = window.innerHeight / 100;
  const startY = e.clientY;
  const startDvh = heights[key];
  bar.dataset.active = "yes";
  bar.setPointerCapture?.(e.pointerId);

  const onMove = (ev) => {
    heights[key] = clamp(startDvh + (ev.clientY - startY) / vhPx);
    applyHeight(key, heights[key]);
  };
  const onEnd = () => {
    bar.dataset.active = "no";
    try { localStorage.setItem(STORE_KEY, JSON.stringify(heights)); } catch (_) {}
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onEnd);
    window.removeEventListener("pointercancel", onEnd);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onEnd);
  window.addEventListener("pointercancel", onEnd);
}

export function setupMobileResize() {
  const heights = loadHeights();
  for (const key in PANEL_VAR) applyHeight(key, heights[key]);
  document.querySelectorAll(".workspace-splitter").forEach((bar) => {
    bar.addEventListener("pointerdown", (e) => startDrag(bar, heights, e));
    // 접근성: 키보드 위/아래로도 조절
    bar.addEventListener("keydown", (e) => {
      if (!MOBILE_MQ.matches) return;
      const key = bar.dataset.splitterLeft;
      if (!(key in PANEL_VAR)) return;
      const step = e.key === "ArrowUp" ? -3 : e.key === "ArrowDown" ? 3 : 0;
      if (!step) return;
      e.preventDefault();
      heights[key] = clamp(heights[key] + step);
      applyHeight(key, heights[key]);
      try { localStorage.setItem(STORE_KEY, JSON.stringify(heights)); } catch (_) {}
    });
  });
}
