// 파일 탐색기: breadcrumb + 리스트 + 미리보기. 모든 경로는 루트폴더 기준.
import { api } from "./api.js?v=20260702-08";

const ICONS = {
  md: "📝", txt: "📄", py: "🐍", js: "⭐", ts: "⭐", json: "🧾", jsonl: "🧾",
  css: "🎨", html: "🌐", sh: "🐚", yml: "⚙️", yaml: "⚙️", log: "📋",
  png: "🖼️", jpg: "🖼️", jpeg: "🖼️", gif: "🖼️", webp: "🖼️", svg: "🖼️", ico: "🖼️",
  pdf: "📕", zip: "📦", mp3: "🎵", mp4: "🎬",
};
const IMG = ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico"];
const TEXT = ["md", "txt", "py", "js", "ts", "json", "jsonl", "css", "html", "sh", "yml", "yaml", "log"];
const FILE_SPLIT_STORAGE_KEY = "cnv.filesSplitListPx.v1";
const FILE_SPLIT_MIN_LIST = 120;
const FILE_SPLIT_MIN_PREVIEW = 160;

const extOf = (n) => n.split(".").pop().toLowerCase();
const iconFor = (it) => (it.종류 === "dir" ? "📁" : (ICONS[extOf(it.이름)] || "📄"));
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

// 현재 보고 있는 폴더와 마지막 내용 시그니처 — 자동 갱신의 기준.
let currentPath = "";
let lastSig = "";
let refreshing = false;
let filesSplitListPx = readStoredNumber(FILE_SPLIT_STORAGE_KEY, 220);

function readStoredNumber(key, fallback) {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch (_) {
    return fallback;
  }
}

function saveFilesSplit() {
  try {
    localStorage.setItem(FILE_SPLIT_STORAGE_KEY, String(Math.round(filesSplitListPx)));
  } catch (_) {}
}

function applyFilesSplit() {
  const split = document.getElementById("filesView")?.querySelector(".files-split");
  const list = document.getElementById("files-list");
  if (!split || !list) return;
  const splitter = document.getElementById("files-splitter");
  const total = split.clientHeight - (splitter?.offsetHeight || 7);
  const max = Math.max(FILE_SPLIT_MIN_LIST, total - FILE_SPLIT_MIN_PREVIEW);
  const next = Math.min(Math.max(filesSplitListPx, FILE_SPLIT_MIN_LIST), max);
  filesSplitListPx = next;
  list.style.flexBasis = `${Math.round(next)}px`;
}

function resizeFilesSplit(delta) {
  filesSplitListPx += delta;
  applyFilesSplit();
  saveFilesSplit();
}

function startFilesSplitDrag(event) {
  const splitter = event.currentTarget;
  if (!splitter) return;
  event.preventDefault();
  const startY = event.clientY;
  const startPx = filesSplitListPx;
  splitter.dataset.active = "yes";
  document.body.classList.add("files-resizing");
  splitter.setPointerCapture?.(event.pointerId);
  const onMove = (moveEvent) => {
    filesSplitListPx = startPx + (moveEvent.clientY - startY);
    applyFilesSplit();
  };
  const onEnd = () => {
    splitter.dataset.active = "no";
    document.body.classList.remove("files-resizing");
    saveFilesSplit();
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onEnd);
    window.removeEventListener("pointercancel", onEnd);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onEnd);
  window.addEventListener("pointercancel", onEnd);
}

// 항목의 경로·종류·크기·수정시각을 묶은 지문. 추가·삭제·이름변경·내용수정(mtime)까지 잡는다.
function sigOf(data) {
  return (data.항목 || [])
    .map((it) => `${it.경로}|${it.종류}|${it.크기}|${it.수정}`)
    .join("\n");
}

export async function openDir(path = "") {
  currentPath = path;                 // 즉시 반영해 이벤트 레이스를 막는다
  const data = await api.listFiles(path);
  currentPath = data.경로;            // 서버 정규화 경로로 확정
  lastSig = sigOf(data);
  renderBreadcrumb(data.경로);
  renderList(data);
  applyFilesSplit();
}

// 현재 폴더를 다시 읽어, 디스크가 바뀌었을 때만 목록을 갱신한다.
async function refreshCurrent() {
  if (refreshing) return;             // 중복 호출 합치기
  refreshing = true;
  try {
    const data = await api.listFiles(currentPath);
    const sig = sigOf(data);
    if (sig !== lastSig) {            // 바뀐 게 있을 때만 다시 그린다 (깜빡임 방지)
      lastSig = sig;
      renderBreadcrumb(data.경로);
      renderList(data);
      applyFilesSplit();
    }
  } catch (_) {
    // 폴더가 사라졌을 수 있음 — 다음 사용자 이동에서 정리된다. 조용히 넘어간다.
  } finally {
    refreshing = false;
  }
}

// 파일 탭 자동 갱신: 서버의 변경 감시(SSE)를 구독한다. 상시 폴링이 아니라,
// 디스크가 실제로 바뀐 순간에만 이벤트가 오고 — 그중 '지금 보는 폴더'가 바뀐 경우만 다시 읽는다.
// EventSource는 연결이 끊기면 자동 재연결한다.
let _watchES = null;
export function startFilesAutoRefresh() {
  if (_watchES) return;
  _watchES = new EventSource("/api/watch");
  _watchES.onmessage = (e) => {
    let dirs;
    try { dirs = JSON.parse(e.data); } catch (_) { return; }
    if (dirs.includes(currentPath)) refreshCurrent();   // 내 폴더가 바뀐 경우만
  };
  // onerror 시 EventSource가 알아서 재연결하므로 별도 처리 불필요.
}

// 파일 선택기가 열린 동안 SSE 연결을 끊는다(iOS에서 지속 연결이 선택기를 흔드는 경우 대비).
export function pauseFileWatch() {
  if (_watchES) { _watchES.close(); _watchES = null; }
}
export function resumeFileWatch() {
  startFilesAutoRefresh();
}

function renderBreadcrumb(path) {
  const bc = document.getElementById("breadcrumb");
  let acc = "", html = `<a data-path="" class="crumb">루트폴더</a>`;
  for (const p of (path ? path.split("/") : [])) {
    acc = acc ? acc + "/" + p : p;
    html += ` / <a data-path="${acc}" class="crumb">${p}</a>`;
  }
  bc.innerHTML = html;
}

function renderList(data) {
  const ul = document.getElementById("files-list");
  let rows = "";
  if (data.상위 !== null) {
    rows += `<li class="frow dir" data-path="${data.상위}" data-type="dir"><span class="ic">⬆️</span><span class="fn">..</span></li>`;
  }
  rows += data.항목.map((it) => `
    <li class="frow ${it.종류}" data-path="${it.경로}" data-type="${it.종류}">
      <span class="ic">${iconFor(it)}</span>
      <span class="fn">${it.이름}</span>
      <span class="fsz">${fmtSize(it.크기)}</span>
      ${it.종류 === "file" ? `<a class="dl" href="/api/files/raw?path=${encodeURIComponent(it.경로)}&download=1" title="다운로드">⤓</a>` : ""}
    </li>`).join("");
  ul.innerHTML = rows || `<li class="empty">비어 있음</li>`;
}

async function preview(path) {
  const box = document.getElementById("files-preview");
  const ext = extOf(path);
  const url = `/api/files/raw?path=${encodeURIComponent(path)}`;
  const head = `<div class="pv-head"><span>${path}</span><a class="dl" href="${url}&download=1">다운로드</a></div>`;
  if (IMG.includes(ext)) {
    box.innerHTML = head + `<div class="pv-body"><img src="${url}"></div>`;
  } else if (ext === "pdf") {
    box.innerHTML = head + `<iframe class="pv-frame" src="${url}"></iframe>`;
  } else if (TEXT.includes(ext)) {
    try {
      const txt = (await (await fetch(url)).text()).slice(0, 500000);
      const esc = txt.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      box.innerHTML = head + `<pre class="pv-code">${esc}</pre>`;
    } catch (_) {
      box.innerHTML = `<div class="empty">미리보기 불가</div>`;
    }
  } else {
    box.innerHTML = head + `<div class="empty">미리보기 불가 — 다운로드로 확인하세요</div>`;
  }
}

export function wireFiles() {
  document.getElementById("files-list").onclick = (e) => {
    if (e.target.closest(".dl")) return;          // 다운로드 링크는 그대로
    const row = e.target.closest(".frow");
    if (!row) return;
    if (row.dataset.type === "dir") openDir(row.dataset.path);
    else preview(row.dataset.path);
  };
  document.getElementById("breadcrumb").onclick = (e) => {
    const c = e.target.closest(".crumb");
    if (c) openDir(c.dataset.path);
  };
  const splitter = document.getElementById("files-splitter");
  if (splitter) {
    splitter.addEventListener("pointerdown", startFilesSplitDrag);
    splitter.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      resizeFilesSplit(e.key === "ArrowDown" ? 24 : -24);
    });
  }
  window.addEventListener("resize", applyFilesSplit);
  applyFilesSplit();
}
