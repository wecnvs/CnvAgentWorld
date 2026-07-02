// 대시보드 4패널 레이아웃 접기/펴기와 패널 너비 조절만 책임진다.
const LAYOUT_STORAGE_KEY = "cnv.dashboardLayoutCollapsed.v1";
const LAYOUT_WIDTH_STORAGE_KEY = "cnv.dashboardLayoutFractions.v1";
const SPLITTER_WIDTH = "10px";

const panels = [
  { key: "agents", label: "에이전트", id: "people-panel", min: 150, fraction: 0.62 },
  { key: "spaces", label: "공간", id: "spaces-panel", min: 190, fraction: 0.78 },
  { key: "chat", label: "채팅", id: "chat-panel", min: 320, fraction: 1.35 },
  { key: "viewer", label: "뷰어", id: "viewer", min: 260, fraction: 1 },
];

const panelByKey = new Map(panels.map((panel) => [panel.key, panel]));

function readStoredJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (_) {
    return fallback;
  }
}

function normalizedFractions(stored = {}) {
  const result = {};
  panels.forEach((panel) => {
    const value = Number(stored[panel.key]);
    result[panel.key] = Number.isFinite(value) && value > 0.05 ? value : panel.fraction;
  });
  return result;
}

const collapsedPanels = new Set(readStoredJson(LAYOUT_STORAGE_KEY, []));
const panelFractions = normalizedFractions(readStoredJson(LAYOUT_WIDTH_STORAGE_KEY, {}));

function saveCollapsedPanels() {
  try {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify([...collapsedPanels]));
  } catch (_) {}
}

function savePanelFractions() {
  try {
    localStorage.setItem(LAYOUT_WIDTH_STORAGE_KEY, JSON.stringify(panelFractions));
  } catch (_) {}
}

function visiblePanelKeys() {
  return panels
    .map((panel) => panel.key)
    .filter((key) => !collapsedPanels.has(key));
}

function updateSplitters(visibleKeys) {
  const nextVisibleByLeft = new Map();
  for (let i = 0; i < visibleKeys.length - 1; i += 1) {
    nextVisibleByLeft.set(visibleKeys[i], visibleKeys[i + 1]);
  }
  const axis = window.matchMedia("(max-width: 720px)").matches ? "높이" : "너비";
  document.querySelectorAll(".workspace-splitter").forEach((splitter) => {
    const homeLeft = splitter.dataset.homeLeft || splitter.dataset.splitterLeft;
    splitter.dataset.homeLeft = homeLeft;
    const right = nextVisibleByLeft.get(homeLeft);
    if (!right) {
      splitter.dataset.hidden = "yes";
      splitter.setAttribute("aria-hidden", "true");
      splitter.tabIndex = -1;
      return;
    }
    splitter.dataset.splitterLeft = homeLeft;
    splitter.dataset.splitterRight = right;
    splitter.dataset.hidden = "no";
    splitter.setAttribute("aria-hidden", "false");
    splitter.setAttribute("aria-label", `${panelByKey.get(homeLeft)?.label || homeLeft}와 ${panelByKey.get(right)?.label || right} 패널 ${axis} 조절`);
    splitter.tabIndex = 0;
  });
}

function panelColumn(key) {
  const panel = panelByKey.get(key);
  return `minmax(${panel.min}px, ${panelFractions[key]}fr)`;
}

function applyLayoutPanels() {
  const visibleKeys = visiblePanelKeys();
  const columns = [];
  visibleKeys.forEach((key, index) => {
    columns.push(panelColumn(key));
    const nextKey = visibleKeys[index + 1];
    if (nextKey) columns.push(SPLITTER_WIDTH);
  });
  panels.forEach((panel) => {
    const collapsed = collapsedPanels.has(panel.key);
    document.body.classList.toggle(`panel-${panel.key}-collapsed`, collapsed);
    document.querySelectorAll(`[data-layout-panel="${panel.key}"]`).forEach((el) => {
      if (!el.matches("button")) return;
      el.setAttribute("aria-pressed", collapsed ? "false" : "true");
      el.setAttribute("aria-expanded", collapsed ? "false" : "true");
      el.textContent = collapsed ? `${panel.label} 펼치기` : `${panel.label} 접기`;
    });
  });
  updateSplitters(visibleKeys);
  const workspace = document.querySelector(".workspace");
  if (workspace) workspace.style.gridTemplateColumns = columns.length ? columns.join(" ") : "1fr";
}

export function setLayoutPanelCollapsed(key, collapsed, persist = true) {
  if (!panelByKey.has(key)) return;
  if (collapsed) collapsedPanels.add(key);
  else collapsedPanels.delete(key);
  if (persist) saveCollapsedPanels();
  applyLayoutPanels();
}

function panelElement(key) {
  const panel = panelByKey.get(key);
  return panel ? document.getElementById(panel.id) : null;
}

function currentPanelWidthTotal(keys) {
  return keys
    .map((key) => panelElement(key)?.getBoundingClientRect().width || 0)
    .reduce((sum, width) => sum + width, 0);
}

function currentFractionTotal(keys) {
  return keys
    .map((key) => Number(panelFractions[key]) || panelByKey.get(key).fraction)
    .reduce((sum, fraction) => sum + fraction, 0);
}

function resizePanelPair(leftKey, rightKey, deltaPx, start = null) {
  const left = panelByKey.get(leftKey);
  const right = panelByKey.get(rightKey);
  const leftEl = panelElement(leftKey);
  const rightEl = panelElement(rightKey);
  if (!left || !right || !leftEl || !rightEl) return false;
  const visibleKeys = visiblePanelKeys();
  const totalWidth = start?.totalWidth || currentPanelWidthTotal(visibleKeys);
  const totalFraction = start?.totalFraction || currentFractionTotal(visibleKeys);
  if (!totalWidth || !totalFraction) return false;
  const leftStart = start?.leftWidth ?? leftEl.getBoundingClientRect().width;
  const rightStart = start?.rightWidth ?? rightEl.getBoundingClientRect().width;
  const pairWidth = leftStart + rightStart;
  const minLeft = left.min;
  const minRight = right.min;
  const nextLeft = Math.max(minLeft, Math.min(pairWidth - minRight, leftStart + deltaPx));
  const nextRight = pairWidth - nextLeft;
  const fractionPerPixel = totalFraction / totalWidth;
  panelFractions[leftKey] = Math.max(0.05, nextLeft * fractionPerPixel);
  panelFractions[rightKey] = Math.max(0.05, nextRight * fractionPerPixel);
  applyLayoutPanels();
  return true;
}

function startSplitterDrag(e, splitter) {
  if (window.matchMedia("(max-width: 720px)").matches) return;
  const leftKey = splitter.dataset.splitterLeft;
  const rightKey = splitter.dataset.splitterRight;
  if (collapsedPanels.has(leftKey) || collapsedPanels.has(rightKey)) return;
  const leftEl = panelElement(leftKey);
  const rightEl = panelElement(rightKey);
  if (!leftEl || !rightEl) return;
  e.preventDefault();
  const visibleKeys = visiblePanelKeys();
  const start = {
    x: e.clientX,
    leftWidth: leftEl.getBoundingClientRect().width,
    rightWidth: rightEl.getBoundingClientRect().width,
    totalWidth: currentPanelWidthTotal(visibleKeys),
    totalFraction: currentFractionTotal(visibleKeys),
  };
  splitter.dataset.active = "yes";
  document.body.classList.add("layout-resizing");
  splitter.setPointerCapture?.(e.pointerId);

  const onMove = (moveEvent) => {
    resizePanelPair(leftKey, rightKey, moveEvent.clientX - start.x, start);
  };
  const onEnd = () => {
    splitter.dataset.active = "no";
    document.body.classList.remove("layout-resizing");
    savePanelFractions();
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onEnd);
    window.removeEventListener("pointercancel", onEnd);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onEnd);
  window.addEventListener("pointercancel", onEnd);
}

function wireSplitters() {
  document.querySelectorAll(".workspace-splitter").forEach((splitter) => {
    splitter.addEventListener("pointerdown", (e) => startSplitterDrag(e, splitter));
    splitter.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const direction = e.key === "ArrowLeft" ? -1 : 1;
      if (resizePanelPair(splitter.dataset.splitterLeft, splitter.dataset.splitterRight, direction * 24)) {
        savePanelFractions();
      }
    });
  });
}

export function wireViewer() {
  document.querySelectorAll("[data-layout-panel]").forEach((btn) => {
    if (!btn.matches("button")) return;
    btn.onclick = () => {
      const key = btn.dataset.layoutPanel;
      setLayoutPanelCollapsed(key, !collapsedPanels.has(key));
    };
  });
  wireSplitters();
  applyLayoutPanels();
}
