// 화면 제어 그리드 — 여러 세션(서버 호스트/원격/VM들)의 라이브 화면을 한 화면에 타일로 띄우고
// 각각 독립적으로 클릭/타이핑한다. 서로 다른 세션(target)은 백엔드 락이 독립이라 동시 제어 가능.
//
// 헛점 방지(단일패널 때와 동일, 타일별로 적용):
//  - 락 경합: 다른 액터가 그 세션을 잡고 있으면 그 타일만 '보기 전용'으로 폴백.
//  - 세션 분리/오류: 그 타일 화면에 '신호 없음' 표시, 폴링은 천천히 재시도.
//  - 좌표: 타일 표시이미지↔naturalW/H(원격 화면크기)↔화면 오프셋 절대변환.
//  - 정리 누수: 타일/그리드 닫기·페이지 이탈 시 타이머 정리 + 보유 락 전부 해제(beacon).
//  - 키보드: 포커스된 타일로 라우팅(특수키/단축키 직접, 한글/임의텍스트는 하단 입력창).
import { api } from "./api.js?v=20260702-13";

const ACTOR_NAME = "대시보드(대표)";

function actorId() {
  let id = null;
  try { id = localStorage.getItem("cnv.cuActorId"); } catch (_) {}
  if (!id) { id = "dash-" + Math.random().toString(36).slice(2, 10); try { localStorage.setItem("cnv.cuActorId", id); } catch (_) {} }
  return id;
}

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const SK_SPECIAL = {
  Enter: "{ENTER}", Tab: "{TAB}", Escape: "{ESC}", Backspace: "{BACKSPACE}", Delete: "{DELETE}", Insert: "{INSERT}",
  ArrowUp: "{UP}", ArrowDown: "{DOWN}", ArrowLeft: "{LEFT}", ArrowRight: "{RIGHT}",
  Home: "{HOME}", End: "{END}", PageUp: "{PGUP}", PageDown: "{PGDN}",
  F1: "{F1}", F2: "{F2}", F3: "{F3}", F4: "{F4}", F5: "{F5}", F6: "{F6}",
  F7: "{F7}", F8: "{F8}", F9: "{F9}", F10: "{F10}", F11: "{F11}", F12: "{F12}",
};
const skEscapeChar = (ch) => ("+^%~(){}[]".includes(ch) ? "{" + ch + "}" : ch);

let GRID = null;   // { root, gridEl, tiles: Map<target,tile>, focused, onKey, onVis, onUnload }
let STANDALONE = false;   // 별도 창(remote.html)에서 열렸으면 true → 닫기=window.close
export function setStandalone(v) { STANDALONE = !!v; }

function toggleFullscreen() {
  try {
    if (document.fullscreenElement) document.exitFullscreen();
    else (document.fullscreenEnabled ? document.documentElement : document.body).requestFullscreen();
  } catch (_) {}
}

// ── 공개 API ──────────────────────────────────────────────
export function openRemotePanel(target, label) {
  if (!target) return;
  ensureGrid();
  const exist = GRID.tiles.get(target);
  if (exist) { focusTile(exist); return; }
  const tile = makeTile(target, label || target);
  GRID.tiles.set(target, tile);
  GRID.gridEl.appendChild(tile.el);
  focusTile(tile);
  // 기본 = 보기 전용(락 미획득). 원격제어를 열자마자 락을 쥐면, 백그라운드 작업 워커가 같은
  // 세션을 조작하려다 락 경합으로 막히거나 스트랜드된다(대표 신고: c81e 벽작업이 대표 락 점유로 3D캡처 대기).
  // 그래서 열 때는 화면만 보고(스크린샷은 락 없이 누구나 읽기), 제어가 필요할 때만 상단 버튼으로 명시적으로 락을 잡는다.
  setControlling(tile, false); loadGeom(tile); startPolling(tile);
  flashMsg(tile, "👁 보기 전용으로 열렸어요 — 직접 조작하려면 상단 ‘👁 보기 전용’ 버튼을 눌러 제어를 시작하세요", 3200);
  updateMeta();
}

export function openAllSessions(list) {
  (list || []).forEach((t) => openRemotePanel(t.name || t.target, t.label));
}

// ── 그리드 컨테이너 ───────────────────────────────────────
function ensureGrid() {
  if (GRID) return;
  const root = document.createElement("div");
  root.className = "rpg-overlay" + (STANDALONE ? " standalone" : "");
  root.innerHTML = `
    <div class="rpg-head">
      <span class="rpg-title">화면 제어 그리드</span>
      <span class="rpg-meta" data-rpg="meta"></span>
      <span class="rp-spacer"></span>
      <button class="rp-btn" data-rpg="fs" type="button" title="전체화면 ↔ 복귀">⛶ 전체화면</button>
      <button class="rp-btn" data-rpg="closeall" type="button">✕ ${STANDALONE ? "창 닫기" : "전체 닫기"}</button>
    </div>
    <div class="rpg-grid" data-rpg="grid"></div>
    <div class="rpg-bar">
      <span class="rpg-barlabel" data-rpg="barlabel">포커스된 세션 없음 — 타일 화면을 클릭하세요</span>
      <div class="rp-keys" data-rpg="keys">
        <button class="rp-key" data-key="{ENTER}" type="button">⏎</button>
        <button class="rp-key" data-key="{ESC}" type="button">Esc</button>
        <button class="rp-key" data-key="{TAB}" type="button">Tab</button>
        <button class="rp-key" data-key="{BACKSPACE}" type="button">⌫</button>
        <button class="rp-key" data-key="^a" type="button">^A</button>
        <button class="rp-key" data-key="^c" type="button">^C</button>
        <button class="rp-key" data-key="^v" type="button">^V</button>
      </div>
      <form class="rpg-typebar" data-rpg="typebar">
        <input class="rp-textin" data-rpg="text" type="text" placeholder="포커스 세션에 보낼 텍스트(한글 OK) — 전송" autocomplete="off">
        <button class="rp-btn" type="submit">전송</button>
      </form>
    </div>`;
  document.body.appendChild(root);
  GRID = { root, gridEl: root.querySelector('[data-rpg="grid"]'), tiles: new Map(), focused: null };

  root.querySelector('[data-rpg="closeall"]').addEventListener("click", closeGrid);
  root.querySelector('[data-rpg="fs"]').addEventListener("click", toggleFullscreen);
  root.addEventListener("mousedown", (e) => { if (!STANDALONE && e.target === root) closeGrid(); });
  root.querySelector('[data-rpg="keys"]').addEventListener("click", (e) => {
    const b = e.target.closest(".rp-key"); if (!b || !GRID.focused) return;
    sendInput(GRID.focused, "key", { keys: b.dataset.key });
  });
  root.querySelector('[data-rpg="typebar"]').addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = root.querySelector('[data-rpg="text"]'); const t = inp.value;
    if (!t || !GRID.focused) return;
    sendInput(GRID.focused, "type", { text: t }).then((ok) => { if (ok) inp.value = ""; });
  });

  GRID.onKey = (e) => {
    if (!GRID || !GRID.focused) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;     // 입력창 타이핑은 통과
    handleKeydown(GRID.focused, e);
  };
  document.addEventListener("keydown", GRID.onKey);
  GRID.onVis = () => { if (GRID && !document.hidden) GRID.tiles.forEach((t) => { if (!t.loading) tick(t); }); };
  document.addEventListener("visibilitychange", GRID.onVis);
  GRID.onUnload = () => {
    if (!GRID) return;
    GRID.tiles.forEach((t) => { if (t.controlling) releaseBeacon(t.target); });
  };
  window.addEventListener("beforeunload", GRID.onUnload);
}

function updateMeta() {
  if (!GRID) return;
  const m = GRID.root.querySelector('[data-rpg="meta"]');
  if (m) m.textContent = GRID.tiles.size ? `${GRID.tiles.size}개 세션` : "";
}

function closeGrid() {
  if (!GRID) return;
  const g = GRID; GRID = null;
  g.tiles.forEach((t) => {
    t.destroyed = true;
    if (t.pollTimer) clearTimeout(t.pollTimer);
    if (t.hbTimer) clearInterval(t.hbTimer);
    if (t.curObjUrl) { try { URL.revokeObjectURL(t.curObjUrl); } catch (_) {} }
    if (t.controlling) { try { api.cuRelease(actorId(), t.target); } catch (_) {} }
  });
  document.removeEventListener("keydown", g.onKey);
  document.removeEventListener("visibilitychange", g.onVis);
  window.removeEventListener("beforeunload", g.onUnload);
  if (g.root.parentNode) g.root.parentNode.removeChild(g.root);
  if (STANDALONE) { try { window.close(); } catch (_) {} }
}

function closeTile(tile) {
  if (tile.destroyed) return;
  tile.destroyed = true;
  if (tile.pollTimer) clearTimeout(tile.pollTimer);
  if (tile.hbTimer) clearInterval(tile.hbTimer);
  if (tile.curObjUrl) { try { URL.revokeObjectURL(tile.curObjUrl); } catch (_) {} }
  if (tile.controlling) { try { api.cuRelease(actorId(), tile.target); } catch (_) {} }
  if (tile.el.parentNode) tile.el.parentNode.removeChild(tile.el);
  if (GRID) {
    GRID.tiles.delete(tile.target);
    if (GRID.focused === tile) GRID.focused = null;
    updateMeta();
    if (GRID.tiles.size === 0) closeGrid();
  }
}

// ── 타일 ──────────────────────────────────────────────────
function makeTile(target, label) {
  const el = document.createElement("div");
  el.className = "rpg-tile"; el.tabIndex = 0;
  el.innerHTML = `
    <div class="rpg-tile-head">
      <span class="rpg-tile-name">${esc(label)}</span>
      <span class="rp-host" data-t="host"></span>
      <span class="rp-spacer"></span>
      <button class="rp-modebtn" data-t="mode" type="button" title="보기 전용 ↔ 제어 전환">연결 중…</button>
      <button class="rp-btn" data-t="close" type="button" title="이 타일 닫기">✕</button>
    </div>
    <div class="rpg-tile-screen">
      <img class="rp-screen" data-t="img" alt="${esc(label)}" draggable="false">
      <div class="rp-msg" data-t="msg" hidden></div>
    </div>`;
  const tile = {
    target, label, el, controlling: false, geom: null, destroyed: false,
    pollTimer: null, hbTimer: null, loading: false, flashT: null,
    img: el.querySelector('[data-t="img"]'), msg: el.querySelector('[data-t="msg"]'),
    hostBadge: el.querySelector('[data-t="host"]'), modeBtn: el.querySelector('[data-t="mode"]'),
  };
  el.querySelector('[data-t="close"]').addEventListener("click", (e) => { e.stopPropagation(); closeTile(tile); });
  tile.modeBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMode(tile); });
  el.addEventListener("mousedown", () => focusTile(tile));
  tile.img.addEventListener("click", (e) => { focusTile(tile); onClick(tile, e, "left", false); });
  tile.img.addEventListener("dblclick", (e) => onClick(tile, e, "left", true));
  tile.img.addEventListener("contextmenu", (e) => { e.preventDefault(); focusTile(tile); onClick(tile, e, "right", false); });
  tile.img.addEventListener("wheel", (e) => onWheel(tile, e), { passive: false });
  tile.img.addEventListener("dragstart", (e) => e.preventDefault());
  return tile;
}

function focusTile(tile) {
  if (!GRID || tile.destroyed || GRID.focused === tile) return;
  GRID.focused = tile;
  GRID.tiles.forEach((t) => t.el.classList.toggle("focused", t === tile));
  const bl = GRID.root.querySelector('[data-rpg="barlabel"]');
  if (bl) bl.textContent = "포커스: " + tile.label;
}

function setMode(tile, mode, holder) {
  const el = tile.modeBtn; if (!el) return;
  el.classList.remove("ok", "ro", "err");
  if (mode === "control") { el.textContent = "🎮 제어 중"; el.classList.add("ok"); el.title = "제어 중 — 클릭하면 보기 전용으로"; }
  else if (mode === "viewonly") { el.textContent = holder ? `👁 보기 (${holder})` : "👁 보기 전용"; el.classList.add("ro"); el.title = "보기 전용 — 클릭하면 제어 시도"; }
  else if (mode === "err") { el.textContent = "오류"; el.classList.add("err"); }
  else { el.textContent = "연결 중…"; }
}

function setControlling(tile, on, holder) {
  tile.controlling = !!on;
  setMode(tile, on ? "control" : "viewonly", holder);
  tile.el.classList.toggle("controlling", !!on);
  if (on) startHeartbeat(tile); else stopHeartbeat(tile);
}

// 보기 ↔ 제어 토글: 제어 중이면 락 풀어 보기 전용으로, 보기면 제어 시도(락 획득).
function toggleMode(tile) {
  if (tile.destroyed) return;
  if (tile.controlling) {
    try { api.cuRelease(actorId(), tile.target); } catch (_) {}
    setControlling(tile, false, "");
    flashMsg(tile, "보기 전용으로 전환", 1100);
  } else {
    acquireLock(tile, true);
  }
}

async function acquireLock(tile, manual) {
  const r = await api.cuAcquire(actorId(), tile.target, ACTOR_NAME, "대시보드 원격제어 그리드");
  if (tile.destroyed) return;
  if (r.ok && r.data && r.data.acquired) setControlling(tile, true);
  else {
    const holder = (r.data && r.data.holder_name) || "";
    setControlling(tile, false, holder);
    if (manual && holder) flashMsg(tile, `'${holder}'가 사용 중 — 제어 불가`, 2000);
  }
}

function startHeartbeat(tile) {
  stopHeartbeat(tile);
  tile.hbTimer = setInterval(() => { if (!tile.destroyed && tile.controlling) api.cuHeartbeat(actorId(), tile.target); }, 120000);
}
function stopHeartbeat(tile) { if (tile.hbTimer) { clearInterval(tile.hbTimer); tile.hbTimer = null; } }

async function loadGeom(tile) {
  try {
    const st = await api.cuViewStatus(tile.target);
    if (tile.destroyed) return;
    if (st && st.screen) tile.geom = st.screen;
    if (st && st.hostname && tile.hostBadge) tile.hostBadge.textContent = `🪟 ${st.hostname}`;
  } catch (_) {}
}

function scheduleNext(tile, ms) { if (!tile.destroyed) tile.pollTimer = setTimeout(() => tick(tile), ms); }

function startPolling(tile) {
  // 여러 타일 동시 오픈 시 첫 요청이 한꺼번에 몰리지 않게 타일마다 살짝 시차.
  const idx = GRID ? GRID.tiles.size : 1;
  tile.pollTimer = setTimeout(() => tick(tile), Math.min(1200, Math.max(0, (idx - 1) * 280)));
}

async function screenshotError(r) {
  let msg = String(r.status || "오류");
  try {
    const ct = r.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const data = await r.json();
      msg = data.detail || data.message || msg;
    } else {
      msg = (await r.text()) || msg;
    }
  } catch (_) {}
  return String(msg).replace(/\s+/g, " ").slice(0, 220);
}

function screenFailureMessage(message) {
  const msg = String(message || "");
  if (msg.includes("화면 녹화 권한") || msg.includes("Screen Recording")) {
    return "macOS 화면 녹화 권한 필요 — 시스템 설정에서 권한을 허용한 뒤 대시보드 서버를 재시작하세요.";
  }
  return "화면 표시 실패 — " + (msg || "세션 분리/무응답. 재시도 중…");
}

// blob 더블버퍼: 프레임을 fetch→blob→objectURL로 받아 한 번에 교체 → 재로딩 중 빈 화면(깜빡임) 없음.
function tick(tile) {
  if (tile.destroyed || tile.loading) return;
  tile.loading = true;
  const url = api.cuScreenshotUrl(tile.target) + "&_=" + Date.now();
  fetch(url)
    .then(async (r) => { if (!r.ok) throw new Error(await screenshotError(r)); return r.blob(); })
    .then((blob) => {
      if (tile.destroyed) { return; }
      const ou = URL.createObjectURL(blob);
      const prev = tile.curObjUrl; tile.curObjUrl = ou;
      tile.img.onload = () => { if (prev) { try { URL.revokeObjectURL(prev); } catch (_) {} } };
      tile.img.src = ou;
      tile.loading = false; hideMsg(tile);
      // 최대한 빠르게: 보이면 즉시 다음 프레임(체인 속도만큼 — 호스트는 ~5fps). 숨김 탭은 절약.
      scheduleNext(tile, document.hidden ? 2000 : 0);
    })
    .catch((err) => {
      if (tile.destroyed) return;
      tile.loading = false; showMsg(tile, screenFailureMessage(err && err.message));
      scheduleNext(tile, 2200);
    });
}

function mapToScreen(tile, ev) {
  const img = tile.img;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  // 실화면크기(geom) 우선 — 서버가 이미지를 축소(JPEG)해 보내도 클릭 좌표는 원격 실해상도로 매핑.
  const nx = (tile.geom && tile.geom.w) || img.naturalWidth || rect.width;
  const ny = (tile.geom && tile.geom.h) || img.naturalHeight || rect.height;
  let ox = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
  let oy = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
  const px = Math.round(ox * (nx / rect.width));
  const py = Math.round(oy * (ny / rect.height));
  const sx = tile.geom && Number.isFinite(tile.geom.x) ? tile.geom.x : 0;
  const sy = tile.geom && Number.isFinite(tile.geom.y) ? tile.geom.y : 0;
  return { x: sx + px, y: sy + py };
}

function onClick(tile, ev, button, dbl) { const p = mapToScreen(tile, ev); if (p) sendInput(tile, "click", { x: p.x, y: p.y, button, double: dbl }); }
function onWheel(tile, ev) { ev.preventDefault(); const p = mapToScreen(tile, ev); if (p) sendInput(tile, "scroll", { x: p.x, y: p.y, amount: ev.deltaY > 0 ? -1 : 1 }); }

function handleKeydown(tile, e) {
  if (e.isComposing || e.keyCode === 229) return;     // IME → 입력창 사용
  let keys = null;
  const mod = e.ctrlKey || e.altKey || e.metaKey;
  if (mod) {
    const base = SK_SPECIAL[e.key] || (e.key.length === 1 ? e.key.toLowerCase() : null);
    if (!base) return;
    keys = (e.ctrlKey || e.metaKey ? "^" : "") + (e.altKey ? "%" : "") + (e.shiftKey ? "+" : "") + base;
  } else if (SK_SPECIAL[e.key]) keys = SK_SPECIAL[e.key];
  else if (e.key.length === 1) keys = skEscapeChar(e.key);
  else return;
  e.preventDefault();
  sendInput(tile, "key", { keys });
}

async function sendInput(tile, action, params) {
  if (tile.destroyed) return false;
  if (!tile.controlling) { flashMsg(tile, "👁 보기 전용이라 조작이 안 돼요 — 상단 ‘👁 보기 전용’ 버튼을 눌러 제어를 시작하세요", 2000); return false; }
  const r = await api.cuInput({ agent_id: actorId(), target: tile.target, action, ...params });
  if (tile.destroyed) return false;
  if (r.ok) return true;
  if (r.status === 409) { setControlling(tile, false, (r.data && r.data.holder_name) || ""); flashMsg(tile, "제어 권한 상실(타인 사용/만료)", 2000); }
  else flashMsg(tile, "입력 실패: " + ((r.data && (r.data.detail || r.data.message)) || r.status), 1600);
  return false;
}

function showMsg(tile, t) { if (tile.msg) { tile.msg.textContent = t; tile.msg.hidden = false; } }
function hideMsg(tile) { if (tile.msg && !tile.msg.dataset.sticky) tile.msg.hidden = true; }
function flashMsg(tile, t, ms) {
  if (!tile.msg) return;
  tile.msg.textContent = t; tile.msg.hidden = false; tile.msg.dataset.sticky = "1";
  clearTimeout(tile.flashT);
  tile.flashT = setTimeout(() => { if (tile.msg) { delete tile.msg.dataset.sticky; tile.msg.hidden = true; } }, ms || 1500);
}

function releaseBeacon(target) {
  try {
    const blob = new Blob([JSON.stringify({ agent_id: actorId(), target })], { type: "application/json" });
    navigator.sendBeacon("/api/cu/release", blob);
  } catch (_) {}
}
