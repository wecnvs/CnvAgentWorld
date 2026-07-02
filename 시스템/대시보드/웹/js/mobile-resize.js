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
const DEFAULTS = { agents: 30, spaces: 30, chat: 88 };

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
  if (e.pointerType === "mouse" && e.button !== 0) return;
  // [모바일 터치 먹통 근본수정] 예전엔 pointerdown에서 곧바로 e.preventDefault() + setPointerCapture를
  // 했다. iOS Safari에서 이 조합은 스플릿바를 살짝 탭/스치기만 해도 터치를 그 바에 '가둬' 화면 전체가
  // 먹통이 되는 대표적 원인이다(setPointerCapture 미해제 + touch-action:none). 그래서:
  //  ① setPointerCapture를 아예 쓰지 않는다 — 아래 window 리스너가 드래그 좌표를 전부 받으므로 불필요.
  //  ② preventDefault·active 표시는 '진짜 세로 드래그가 확정된 뒤'에만 한다(threshold) — 단순 탭이나
  //     스크롤 스침은 브라우저 기본 동작(탭·스크롤)을 그대로 두어 하이재킹/프리즈가 없다.
  const vhPx = window.innerHeight / 100;
  const startY = e.clientY;
  const startDvh = heights[key];
  const DRAG_THRESHOLD = 6;   // px — 이만큼 세로로 움직여야 '리사이즈'로 간주
  let engaged = false;

  const onMove = (ev) => {
    const dy = ev.clientY - startY;
    if (!engaged) {
      if (Math.abs(dy) < DRAG_THRESHOLD) return;   // 아직 탭/미세이동 — 관여하지 않음(터치 통과)
      engaged = true;
      bar.dataset.active = "yes";
    }
    if (ev.cancelable) ev.preventDefault();          // 리사이즈 확정 후에만 스크롤 억제
    heights[key] = clamp(startDvh + dy / vhPx);
    applyHeight(key, heights[key]);
  };
  const onEnd = (ev) => {
    if (engaged) {
      bar.dataset.active = "no";
      try { localStorage.setItem(STORE_KEY, JSON.stringify(heights)); } catch (_) {}
    }
    try { bar.releasePointerCapture?.(ev && ev.pointerId); } catch (_) {}   // 방어: 혹시 잡혔으면 해제
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onEnd);
    window.removeEventListener("pointercancel", onEnd);
  };
  window.addEventListener("pointermove", onMove, { passive: false });
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
