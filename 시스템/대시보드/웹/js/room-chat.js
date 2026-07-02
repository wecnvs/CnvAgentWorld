// 공간 채팅방 보기와 입력.
import { api } from "./api.js?v=20260702-08";
import { openWorkSettingsModal, openRuntimeModal } from "./people.js?v=20260702-08";
import { setLayoutPanelCollapsed } from "./viewer.js?v=20260702-08";
import { pauseFileWatch, resumeFileWatch } from "./files.js?v=20260702-08";

let currentSpace = "";
let refreshTimer = null;
let statusTimer = null;
let lastAck = null;
let openSeq = 0;
let activityFilter = "all";
let latestActivityRows = [];
let lastMessageRows = [];
let latestRoomMembers = [];
let latestSpaceRows = [];
let handbackMessageId = "";
let handbackReason = "";
let approvalsByMsgId = {};   // 결재 대기 계획: message_id -> {plan_id, approval_reason, worker, objective}
let latestRoomStatus = {};
const HEAVY_PANEL_MIN_INTERVAL_MS = 4000;   // 무거운 상태패널 재렌더 최소 간격(Safari 메인스레드 포화 방지)
let lastHeavyRenderMs = 0;
let latestWatchReport = null;
let lastActivityFetchMs = 0;
let lastActivitySpace = "";
let outbox = [];
let outboxProcessing = false;
let latestButtonWired = false;
const ACTIVITY_FULL_REFRESH_MS = 10000;
const MESSAGE_REFRESH_MS = 1500;
const STATUS_REFRESH_MS = 1500;
const OBSERVER_COLLAPSE_STORAGE_KEY = "cnv.roomObserverCollapsed.v1";
const OBSERVER_SECTIONS_STORAGE_KEY = "cnv.roomObserverSectionsCollapsed.v1";
const ROOM_HEAD_COLLAPSE_STORAGE_KEY = "cnv.roomHeadCollapsed.v1";
const WORK_CONSOLE_COLLAPSE_STORAGE_KEY = "cnv.roomWorkConsoleCollapsed.v1";
const observerSections = [
  ["watch", "room-watch-report"],
  ["snapshot", "room-snapshot"],
  ["status", "room-status-detail"],
  ["flow", "room-chat-flow"],
  ["obligation", "room-obligation-panel"],
  ["handoff", "room-turn-handoff"],
  ["task", "room-task-panel"],
  ["candidate", "room-candidate-panel"],
  ["promotion", "room-promotion-review"],
  ["activity", "room-activity"],
];

function readStoredJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (_) {
    return fallback;
  }
}

function defaultObserverCollapsed() {
  try {
    return window.matchMedia("(min-width: 721px)").matches;
  } catch (_) {
    return false;
  }
}

// [모바일 채팅영역 근본수정] 모바일에선 진단패널(observer/snapshot ~184px)이 화면을 먹어 채팅 메시지
// 영역이 96px로 눌렸다(대표 신고: 채팅이 눌려 스크롤·조작이 다 엉망). 그래서 모바일은 **진단패널 접힘을
// 기본값으로 강제**해 채팅에 화면을 몰아준다(저장된 펼침 상태도 모바일에선 무시 — 채팅 우선). 필요하면
// 상단 '상태 펼치기' 토글로 펼칠 수 있다. (방 헤더는 접지 않는다 — 거기에 공간 선택 콤보박스가 있어서
// 접으면 콤보박스가 사라진다.) 데스크톱은 종전대로 저장/기본값을 따른다.
const _mobileChatFirst = (() => { try { return window.matchMedia("(max-width: 720px)").matches; } catch (_) { return false; } })();
let observerCollapsed = _mobileChatFirst ? true : Boolean(readStoredJson(OBSERVER_COLLAPSE_STORAGE_KEY, defaultObserverCollapsed()));
let collapsedObserverSections = new Set(readStoredJson(OBSERVER_SECTIONS_STORAGE_KEY, []));
let roomHeadCollapsed = Boolean(readStoredJson(ROOM_HEAD_COLLAPSE_STORAGE_KEY, false));
let workConsoleCollapsed = Boolean(readStoredJson(WORK_CONSOLE_COLLAPSE_STORAGE_KEY, true));
let latestWorkConsoleUrl = "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}

function switchView(id) {
  if (id === "roomView") {
    setLayoutPanelCollapsed("chat", false);
    return;
  }
  if (id === "filesView") setLayoutPanelCollapsed("viewer", false);
  document.querySelectorAll(".vtab").forEach((b) => b.classList.toggle("active", b.dataset.view === id));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === id));
}

function messageClass(row) {
  if (row.코드 === "boss") return "boss";
  if (row.코드 === "manager") return "manager";
  return row.역할 === "assistant" ? "agent" : "user";
}

// 한 턴이 진행되는 동안 상태는 여러 중간값을 연속으로 거친다:
//   posted → manager_queued → manager_running → manager_decision
//   → agent_running → (request_progress|chat_request_work_*|work_dispatched) → agent_replied → idle
// 예전엔 이 중 4개(manager_queued/running/retrying, agent_running)만 말풍선으로 매핑해서,
// 폴링이 매핑 안 된 찰나 상태(manager_decision·posted·agent_replied·request_progress·
// chat_request_work_*·work_dispatched)를 잡으면 말풍선이 사라졌다 다시 나타나며 '깜빡'였다.
// → '아직 처리 중'인 모든 상태를 매니저/에이전트 두 부류로 묶어 연속된 말풍선을 유지하고,
//   진짜 종료(idle)·오류·대표결재대기에서만 말풍선을 없앤다(깜빡임 근본 제거).
const NO_BUBBLE_STATES = new Set([
  "idle", "manager_closed", "manager_stop",
  "wake_failed", "manager_failed", "manager_recovery_needed",
  "manager_generation_stale", "manager_stale_result", "manager_read_lag",
  "manager_claim_corrupt", "manager_claim_busy", "manager_redrive_limit_reached",
  "lesson_application_missing", "manager_handback_to_representative",
  "work_plan_pending_approval", "work_plan_pending_representative_approval",
  "work_plan_registered_pending_approval", "work_plan_rejected",
]);

function transientStatusBubble(st = {}) {
  const state = st.상태 || "";
  if (!state || NO_BUBBLE_STATES.has(state)) return null;

  // 재요청(JSON 형식) — 기존 문구 유지
  if (state === "manager_retrying") {
    return { kind: "manager", speaker: "공간관리", code: "manager", text: "JSON 형식 재요청 중..." };
  }
  // 매니저(사회자)가 판단 중인 국면 (manager_queued/running/decision/tick/auto_continue …)
  if (state === "manager_queued") {
    return { kind: "manager", speaker: "공간관리", code: "manager", text: "대기 중..." };
  }
  if (state.startsWith("manager")) {
    return { kind: "manager", speaker: "공간관리", code: "manager", text: st.label || "판단 중" };
  }
  // 그 외 진행 상태(agent_*, chat_request_*, work_*, request_progress, posted, task_created_*)
  // = 에이전트가 턴을 받아 일하는 중 → '생각 중' 말풍선을 끊김 없이 유지
  const who = st.current || st.target || st.last_target || "에이전트";
  const elapsed = formatDuration(st.staleness_ms);
  return { kind: "agent", speaker: who, code: "typing", text: `턴을 받아 생각 중${elapsed ? ` · ${elapsed}` : ""}` };
}

// ── 말풍선 파일 미리보기 (일반적인 단톡 임베드 방식 참고, 우리에 맞춤) ──
// 메시지 스키마 변경 없음: 본문 텍스트의 '워크스페이스 경로(슬래시 포함 + 알려진 확장자)'를 스캔해
// 이미지=인라인, pdf/html=지연 iframe, 영상/오디오=플레이어, 그 외=파일카드로 치환한다.
// 서버 /api/files/raw 는 ROOT 밖 경로를 거부(보안)하므로 워크스페이스 내부 파일만 미리보기된다.
const EMBED_EXT = {
  img: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"],
  vid: ["mp4", "webm", "mov", "m4v"],
  aud: ["mp3", "wav", "ogg", "m4a", "aac", "flac"],
  frame: ["pdf", "html", "htm"],
  text: ["md", "txt", "csv", "json", "log", "yaml", "yml", "tsv"],     // 인라인 텍스트/마크다운 미리보기
  doc: ["pptx", "ppt", "pptm", "docx", "doc", "xlsx", "xls", "odp", "odt", "ods", "rtf"],  // soffice→PDF 변환 미리보기
  file: ["hwp", "hwpx", "zip", "tar", "gz", "key", "7z", "rar"],       // 미리보기 불가 → 파일카드(다운로드)
};
const EMBED_ALL = [].concat(...Object.values(EMBED_EXT));
const EMBED_KIND = (() => {
  const m = {};
  for (const [k, exts] of Object.entries(EMBED_EXT)) exts.forEach((e) => (m[e] = k));
  return m;
})();
// esc() 적용 후 텍스트에서 '슬래시 포함 경로 + 확장자'를 찾는다(공백/꺾쇠 없는 토큰).
// 백틱(`)·별표(*)·괄호([]())도 제외한다 — 에이전트가 경로를 `경로`/**경로**/[라벨](경로)로 감싸면
// 그 문자가 토큰 앞뒤에 붙어 raw 조회가 400으로 깨지던 버그(미리보기 안 뜸)를 막는다.
const EMBED_RE = new RegExp(`([^\\s<>"'\`*()\\[\\]]*\\/[^\\s<>"'\`*()\\[\\]]*\\.(${EMBED_ALL.join("|")}))`, "gi");

function fileRawURL(p) {
  if (/^\/api\/files\/raw/.test(p) || /^https?:\/\//.test(p)) return p;
  return "/api/files/raw?path=" + encodeURIComponent(p.replace(/&amp;/g, "&"));
}
function filePreviewURL(p) {
  return "/api/files/preview?path=" + encodeURIComponent(p.replace(/&amp;/g, "&"));
}
function embedFor(path, ext) {
  const url = fileRawURL(path);
  const name = esc(path.split("/").pop());
  const kind = EMBED_KIND[(ext || "").toLowerCase()] || "file";
  if (kind === "img") return `<img class="msg-embed-img" loading="lazy" src="${url}" alt="${name}" title="${name} (클릭하면 크게)">`;
  if (kind === "vid") return `<video class="msg-embed-media" controls preload="metadata" src="${url}"></video>`;
  if (kind === "aud") return `<audio class="msg-embed-media" controls preload="none" src="${url}"></audio>`;
  if (kind === "frame" || kind === "doc") {
    // frame=원본 그대로(pdf/html), doc=office는 /preview가 PDF로 변환해 같은 iframe에 띄움
    const src = kind === "doc" ? filePreviewURL(path) : url;
    const icon = kind === "doc" ? "📑" : "📄";
    return (
      `<div class="msg-embed-card"><div class="embed-bar"><span class="embed-name">${icon} ${name}</span>` +
      `<a class="embed-act" href="${url}" target="_blank" rel="noopener" title="원본">↗</a>` +
      `<a class="embed-act" href="${url}&download=1" title="다운로드">⬇</a></div>` +
      `<div class="embed-frame" data-src="${src}"></div></div>`
    );
  }
  if (kind === "text") return (
    `<div class="msg-embed-card"><div class="embed-bar"><span class="embed-name">📄 ${name}</span>` +
    `<a class="embed-act" href="${url}" target="_blank" rel="noopener" title="원본">↗</a>` +
    `<a class="embed-act" href="${url}&download=1" title="다운로드">⬇</a></div>` +
    `<div class="embed-text" data-text-src="${url}" data-md="${(ext || "").toLowerCase() === "md" ? "1" : "0"}">로딩…</div></div>`
  );
  return (
    `<div class="msg-file-card"><span class="embed-name">📎 ${name}</span>` +
    `<a class="embed-act" href="${url}" target="_blank" rel="noopener">열기</a>` +
    `<a class="embed-act" href="${url}&download=1">다운로드</a></div>`
  );
}

// 안전한 경량 마크다운 렌더(입력을 먼저 escape한 뒤 제한된 서식만 적용 → XSS 없음).
function _escText(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function _mdInline(s) { return s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>"); }
function _renderMarkdown(src) {
  const lines = _escText(src).split("\n");
  const out = []; let inList = false;
  for (const ln of lines) {
    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)$/))) { if (inList) { out.push("</ul>"); inList = false; } out.push(`<div class="md-h md-h${m[1].length}">${_mdInline(m[2])}</div>`); continue; }
    if ((m = ln.match(/^\s*[-*]\s+(.*)$/))) { if (!inList) { out.push("<ul class='md-ul'>"); inList = true; } out.push(`<li>${_mdInline(m[1])}</li>`); continue; }
    if (inList) { out.push("</ul>"); inList = false; }
    if (ln.trim() === "") { out.push("<div class='md-sp'></div>"); continue; }
    out.push(`<div class="md-p">${_mdInline(ln)}</div>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}
let _textObserver = null;
async function _loadText(el) {
  if (el.dataset.loaded) return; el.dataset.loaded = "1";
  const src = el.getAttribute("data-text-src"); const isMd = el.getAttribute("data-md") === "1";
  try {
    const r = await fetch(src);
    if (!r.ok) { el.textContent = "(미리보기 불가)"; return; }
    let t = await r.text();
    const truncated = t.length > 20000; if (truncated) t = t.slice(0, 20000);
    el.innerHTML = isMd ? _renderMarkdown(t) : `<pre class="embed-pre">${_escText(t)}</pre>`;
    if (truncated) { const more = document.createElement("div"); more.className = "embed-more"; more.textContent = "… (이하 생략 — ↗로 전체 보기)"; el.appendChild(more); }
  } catch (_) { el.textContent = "(미리보기 로드 실패)"; }
}
function _observeText(el) {
  if (!("IntersectionObserver" in window)) { _loadText(el); return; }
  if (!_textObserver) {
    _textObserver = new IntersectionObserver((entries, obs) => {
      entries.forEach((e) => { if (e.isIntersecting) { _loadText(e.target); obs.unobserve(e.target); } });
    }, { rootMargin: "200px" });
  }
  _textObserver.observe(el);
}
// 말풍선 인라인 서식 — 입력은 '이미 escape된' 텍스트. 경로 미리보기(embed-pending)를 먼저 잡고
// (경로엔 *·` 없음 → 이후 서식과 충돌 안 함) 굵게/코드/링크를 적용한다. XSS 안전(escape 선행).
function _inlineFmt(s) {
  s = s.replace(EMBED_RE, (m, path, ext) =>
    `<span class="embed-pending" data-path="${path}" data-ext="${(ext || "").toLowerCase()}">${path}</span>`);
  s = s.replace(/&lt;br\s*\/?&gt;/gi, "<br>");   // 의도적 줄바꿈만 허용(escape 후 — br은 속성 없어 안전)
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/`([^`]+)`/g, "<code class=\"md-ic\">$1</code>");
  // [텍스트](http…) 링크만 허용(스킴 제한 — javascript: 등 차단)
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    "<a href=\"$2\" target=\"_blank\" rel=\"noopener noreferrer\">$1</a>");
  return s;
}

// 표(테이블) 판별: 파이프 행 + 구분행(|---|:--:|).
function _isTableRow(s) { return /\|/.test(s) && /\S/.test(s); }
function _isTableSep(s) { return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(s) && /\|/.test(s); }
function _splitTableCells(s) {
  let t = s.trim().replace(/^\|/, "").replace(/\|$/, "");   // 양끝 파이프 제거
  return t.split("|").map((c) => c.trim());
}
function _tableAligns(sepCells) {
  return sepCells.map((c) => {
    const l = c.startsWith(":"), r = c.endsWith(":");
    return r && l ? "center" : r ? "right" : l ? "left" : "";
  });
}
function _renderTable(header, sep, bodyRows) {
  const aligns = _tableAligns(_splitTableCells(sep));
  const cell = (txt, i, tag) => {
    const a = aligns[i] ? ` style="text-align:${aligns[i]}"` : "";
    return `<${tag}${a}>${_inlineFmt(esc(txt))}</${tag}>`;
  };
  const head = _splitTableCells(header).map((c, i) => cell(c, i, "th")).join("");
  const body = bodyRows.map((r) => `<tr>${_splitTableCells(r).map((c, i) => cell(c, i, "td")).join("")}</tr>`).join("");
  return `<table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// 말풍선 본문을 '마크다운 렌더링'한다(블록: 제목·목록·표·인용·구분선·코드블록·문단).
// 파일 경로는 embed-pending으로 남겨 scanEmbeds가 미리보기로 승격(기존 동작 유지).
function renderMessageBody(text) {
  try {
    const lines = String(text ?? "").split("\n");
    const out = [];
    let inList = false, inCode = false, codeBuf = [];
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
    let i = 0;
    while (i < lines.length) {
      const raw = lines[i];
      if (/^\s*```/.test(raw)) {                                   // 코드블록 펜스
        if (inCode) { out.push(`<pre class="md-code">${esc(codeBuf.join("\n"))}</pre>`); codeBuf = []; inCode = false; }
        else { closeList(); inCode = true; }
        i++; continue;
      }
      if (inCode) { codeBuf.push(raw); i++; continue; }            // 코드블록 내부는 그대로(서식·경로감지 안 함)
      // 표: 헤더행 + 다음 줄이 구분행이면 표 블록을 소비한다.
      if (_isTableRow(raw) && i + 1 < lines.length && _isTableSep(lines[i + 1])) {
        closeList();
        const header = raw, sep = lines[i + 1];
        let j = i + 2; const bodyRows = [];
        while (j < lines.length && _isTableRow(lines[j]) && lines[j].trim() !== "") { bodyRows.push(lines[j]); j++; }
        out.push(_renderTable(header, sep, bodyRows));
        i = j; continue;
      }
      let m;
      if ((m = raw.match(/^(#{1,6})\s+(.*)$/))) {                  // 제목
        closeList(); out.push(`<div class="md-h md-h${Math.min(m[1].length, 4)}">${_inlineFmt(esc(m[2]))}</div>`); i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(raw)) {                // 구분선 ---, ***
        closeList(); out.push('<hr class="md-hr">'); i++; continue;
      }
      if ((m = raw.match(/^\s*>\s?(.*)$/))) {                      // 인용
        closeList(); out.push(`<div class="md-quote">${_inlineFmt(esc(m[1]))}</div>`); i++; continue;
      }
      if ((m = raw.match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/))) {        // 목록(-, *, +, 1.)
        if (!inList) { out.push("<ul class=\"md-ul\">"); inList = true; }
        out.push(`<li>${_inlineFmt(esc(m[1]))}</li>`); i++; continue;
      }
      closeList();
      if (raw.trim() === "") { out.push('<div class="md-sp"></div>'); i++; continue; }
      out.push(`<div class="md-p">${_inlineFmt(esc(raw))}</div>`); i++;
    }
    if (inCode) out.push(`<pre class="md-code">${esc(codeBuf.join("\n"))}</pre>`);
    closeList();
    return out.join("");
  } catch (_) {
    return esc(text || "").replace(/\n/g, "<br>");
  }
}
let _embedObserver = null;
function injectEmbedFrame(el) {
  const src = el.getAttribute("data-src");
  if (!src || el.dataset.loaded) return;
  el.dataset.loaded = "1";
  const f = document.createElement("iframe");
  f.className = "embed-iframe";
  f.loading = "lazy";
  f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups");
  f.src = src;
  el.appendChild(f);
}
function _observeFrame(el) {
  if (!("IntersectionObserver" in window)) { injectEmbedFrame(el); return; }
  if (!_embedObserver) {
    _embedObserver = new IntersectionObserver((entries, obs) => {
      entries.forEach((e) => { if (e.isIntersecting) { injectEmbedFrame(e.target); obs.unobserve(e.target); } });
    }, { rootMargin: "200px" });
  }
  _embedObserver.observe(el);
}
// 대기 경로가 실제 워크스페이스 파일(ROOT 기준)인지 확인하고, 맞을 때만 미리보기로 승격한다.
async function _upgradeEmbed(span) {
  const rawPath = (span.getAttribute("data-path") || "").replace(/&amp;/g, "&");
  const ext = span.getAttribute("data-ext") || "";
  if (!rawPath) { span.classList.remove("embed-pending"); return; }
  let ok = false;
  try {
    const r = await fetch(fileRawURL(rawPath), { headers: { Range: "bytes=0-0" } });
    const ct = (r.headers.get("content-type") || "").toLowerCase();
    ok = r.ok && !ct.includes("application/json");      // 없는 경로는 raw가 400+JSON을 준다
    try { await (r.body && r.body.cancel()); } catch (_) {}   // 본문은 받지 않음(검증만)
  } catch (_) { ok = false; }
  if (!span.isConnected) return;
  if (!ok) { span.classList.remove("embed-pending"); return; }   // 실제 파일 아님 → plain text 유지
  const tmp = document.createElement("div");
  tmp.innerHTML = embedFor(rawPath, ext);
  const node = tmp.firstElementChild;
  if (!node) { span.classList.remove("embed-pending"); return; }
  span.replaceWith(node);
  if (node.matches && node.matches("img.msg-embed-img")) {
    node.addEventListener("error", () => {                 // 승격 후에도 깨지면 텍스트로 복귀
      const t = document.createElement("span");
      t.textContent = rawPath;
      node.replaceWith(t);
    }, { once: true });
  }
  if (node.querySelectorAll) {
    node.querySelectorAll(".embed-frame[data-src]").forEach(_observeFrame);
    node.querySelectorAll(".embed-text[data-text-src]").forEach(_observeText);
  }
}
function scanEmbeds(container) {
  container.querySelectorAll(".embed-pending").forEach((span) => { _upgradeEmbed(span); });
  container.querySelectorAll(".embed-frame[data-src]:not([data-loaded])").forEach(_observeFrame);
  container.querySelectorAll(".embed-text[data-text-src]:not([data-loaded])").forEach(_observeText);
}

// iOS 파일(문서) 선택기를 띄우면 페이지가 백그라운드로 가고, 메모리가 빠듯하면 iOS가
// 웹 프로세스를 리로드해 선택기가 반복적으로 다시 뜬다(WebKit #172533, 사진보관함은 가벼워서 OK).
// → 선택기 직전에 가장 무거운 PDF/이미지/영상 리소스를 떼어 메모리를 낮추고, 끝나면 복원한다.
function shedEmbedMemory() {
  const list = document.getElementById("room-messages");
  if (!list) return;
  list.querySelectorAll("iframe.embed-iframe").forEach((f) => f.remove());     // PDF/HTML iframe = 최대 소비원
  list.querySelectorAll(".embed-frame[data-loaded]").forEach((el) => { delete el.dataset.loaded; });
  list.querySelectorAll("img.msg-embed-img[src]").forEach((img) => {
    img.dataset.osrc = img.getAttribute("src"); img.removeAttribute("src");
  });
  list.querySelectorAll("video.msg-embed-media[src],audio.msg-embed-media[src]").forEach((m) => {
    m.dataset.osrc = m.getAttribute("src"); m.removeAttribute("src"); try { m.load(); } catch (_) {}
  });
}
function restoreEmbedMemory() {
  const list = document.getElementById("room-messages");
  if (!list) return;
  list.querySelectorAll("[data-osrc]").forEach((el) => { el.setAttribute("src", el.dataset.osrc); delete el.dataset.osrc; });
  scanEmbeds(list);                                                            // iframe 지연 재주입
}

// 이미지 클릭 확대 오버레이(라이트박스). 이벤트 위임이라 메시지 innerHTML 교체에도 동작 유지.
let _lightboxWired = false;
function setupImageLightbox() {
  if (_lightboxWired) return;
  _lightboxWired = true;
  const ov = document.createElement("div");
  ov.className = "img-lightbox";
  ov.id = "img-lightbox";
  ov.innerHTML = `<button class="img-lightbox-close" type="button" aria-label="닫기">✕</button><img class="img-lightbox-img" alt="확대 이미지">`;
  ov.addEventListener("click", () => closeImageLightbox());
  document.body.appendChild(ov);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeImageLightbox(); });
  const list = document.getElementById("room-messages");
  if (list) list.addEventListener("click", (e) => {
    const img = e.target.closest && e.target.closest(".msg-embed-img");
    if (img) { e.preventDefault(); openImageLightbox(img.getAttribute("src")); return; }
    const btn = e.target.closest && e.target.closest(".approval-btn");
    if (btn) { e.preventDefault(); handleApprovalClick(btn); }
  });
}

// 대화창 결재 말풍선의 [진행]/[반려] 처리.
async function handleApprovalClick(btn) {
  const planId = btn.getAttribute("data-plan-id");
  const space = currentSpace;
  if (!planId || !space) return;
  const isApprove = btn.classList.contains("approve");
  if (!isApprove && !window.confirm("이 작업계획을 반려할까요? (실행하지 않음)")) return;
  const siblings = btn.parentElement ? btn.parentElement.querySelectorAll(".approval-btn") : [btn];
  siblings.forEach((b) => { b.disabled = true; });
  try {
    if (isApprove) {
      await api.approvePlan(space, planId);
    } else {
      const reason = window.prompt("반려 사유(선택):", "") || "";
      await api.rejectPlan(space, planId, reason);
    }
    await refreshRoomChat();   // 마커가 서버에서 해제됨 → 재조회로 버튼/하이라이트 제거
  } catch (err) {
    siblings.forEach((b) => { b.disabled = false; });
    alert("결재 처리 실패: " + (err && err.message ? err.message : err));
  }
}
// 폴링 일시정지: 파일 선택기가 열린 동안 채팅/상태 폴링과 재렌더를 멈춘다.
// (iOS 홈화면 앱에서 선택기가 열린 채 주기적 폴링이 돌면 선택기가 계속 새로고침되어 첨부가 안 됨)
let pollPaused = false;
let _pollResumeTimer = 0;
function pausePolling() {
  pollPaused = true;
  try { pauseFileWatch(); } catch (_) {}          // SSE 연결도 끊어 선택기 방해 제거
  try { shedEmbedMemory(); } catch (_) {}         // 무거운 미리보기 떼어 메모리 낮춤(선택기 리로드 방지)
  clearTimeout(_pollResumeTimer);
  _pollResumeTimer = setTimeout(resumePolling, 60000);     // 안전장치: 오래 멈춰있지 않게 자동 복구
  // 선택기가 닫히고 사용자가 페이지를 다시 터치하면 재개(취소/선택 모두 커버). once라 중복 안 쌓임.
  document.addEventListener("pointerdown", resumePolling, { once: true });
}
function resumePolling() {
  pollPaused = false;
  clearTimeout(_pollResumeTimer);
  document.removeEventListener("pointerdown", resumePolling);
  try { resumeFileWatch(); } catch (_) {}
  try { restoreEmbedMemory(); } catch (_) {}      // 떼어냈던 미리보기 복원
}

// 📎 첨부 업로드: 파일을 공간 inbox에 올리고 반환된 경로를 입력창에 붙인다(보내면 미리보기됨).
function setupAttachUpload() {
  const btn = document.getElementById("room-attach");
  const fileInput = document.getElementById("room-file");
  const input = document.getElementById("room-input");
  if (!btn || !fileInput || !input) return;
  // 📎는 <label for="room-file"> — iOS는 JS의 input.click()보다 네이티브 label 연결을
  // 훨씬 안정적으로 처리한다(JS로 hidden input을 click하면 홈화면 앱에서 선택기가
  // 깜빡/리로드되는 알려진 WebKit 버그가 있음). 그래서 클릭에선 default를 막지 않고
  // 폴링/SSE만 멈춘다 — 선택기는 브라우저가 네이티브로 연다.
  btn.addEventListener("click", (e) => {
    if (!currentSpace) { e.preventDefault(); alert("먼저 공간을 여세요."); return; }
    pausePolling();        // 선택기 동안 폴링/SSE 정지 — 네이티브 label이 선택기를 연다(별도페이지 없음)
  });
  // 키보드 접근성(데스크톱): label은 기본적으로 Enter/Space로 안 열리므로 직접 연다.
  btn.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && currentSpace) {
      e.preventDefault(); pausePolling(); fileInput.click();
    }
  });
  fileInput.onchange = async () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (!currentSpace || !files.length) { resumePolling(); return; }
    const prev = btn.textContent;
    btn.disabled = true; btn.textContent = "⏳";
    for (const f of files) {
      try {
        const r = await api.uploadFile(`공간/${currentSpace}/inbox`, f);
        input.value = (input.value ? input.value.replace(/\s*$/, "") + "\n" : "") + r.path;
      } catch (e) {
        alert(`업로드 실패(${f.name}): ${e.message}`);
      }
    }
    btn.disabled = false; btn.textContent = prev;
    resumePolling();
    input.focus();
  };
}

function openImageLightbox(src) {
  const ov = document.getElementById("img-lightbox");
  if (!ov || !src) return;
  ov.querySelector(".img-lightbox-img").src = src;
  ov.classList.add("open");
}
function closeImageLightbox() {
  const ov = document.getElementById("img-lightbox");
  if (ov) ov.classList.remove("open");
}

function isRoomMessagesNearBottom(threshold = 80) {
  const list = document.getElementById("room-messages");
  if (!list) return true;
  return list.scrollHeight - Math.ceil(list.scrollTop) - list.clientHeight <= threshold + 2;
}

function updateLatestButton() {
  const list = document.getElementById("room-messages");
  const btn = document.getElementById("room-latest");
  if (!list || !btn) return;
  const canScroll = list.scrollHeight > list.clientHeight + 12;
  btn.hidden = !canScroll || isRoomMessagesNearBottom(96);
  const form = document.getElementById("room-form");
  btn.style.bottom = `${(form?.offsetHeight || 56) + 12}px`;
}

function scrollRoomToLatest({ smooth = true } = {}) {
  const list = document.getElementById("room-messages");
  if (!list) return;
  list.scrollTo({ top: list.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  requestAnimationFrame(updateLatestButton);
}

function wireLatestButton() {
  if (latestButtonWired) return;
  latestButtonWired = true;
  const list = document.getElementById("room-messages");
  const btn = document.getElementById("room-latest");
  if (!list || !btn) return;
  btn.onclick = () => scrollRoomToLatest();
  list.addEventListener("scroll", updateLatestButton, { passive: true });
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(updateLatestButton);
    ro.observe(list);
    const form = document.getElementById("room-form");
    if (form) ro.observe(form);
  }
}

// 메시지 한 줄의 '내용 서명' — 이게 그대로면 DOM을 다시 만들지 않는다(이미지/iframe 재로드=번쩍임 방지).
let lastMsgSig = null;
function _msgRowSig(r) {
  const id = r.message_id || r.client_message_id || "";
  const hb = (!r.__outbox && r.message_id && r.message_id === handbackMessageId) ? "H" : "";
  const ap = (!r.__outbox && r.message_id && approvalsByMsgId[r.message_id]) ? "A" : "";
  return `${id}|${r.event_seq || ""}|${r.state || ""}|${r.코드 || ""}|${(r.내용 || "").length}|${hb}${ap}`;
}

// 말풍선 깜빡임 근본 방지 — 히스테리시스(debounce).
// 상태는 상태폴(1.5s)로 갱신되는데 ① 폴이 간헐 실패하면 catch에서 latestRoomStatus={}가 되고,
// ② 한 응답 흐름(특히 작업 중 auto-continue)에서 manager↔agent 전이 사이에 찰나 idle이 낀다.
// 그때마다 transientStatusBubble이 null이 되어 말풍선이 '떴다 사라졌다' 반복(대표 신고).
// → 마지막으로 busy(사회자/에이전트 처리 중)를 본 시점 이후 이 유예 동안은 직전 말풍선을 유지한다.
// 유예를 넘겨 busy가 계속 안 보이면(진짜 idle·서버 다운) 그때 없앤다(스틱 방지·자기교정).
const TRANSIENT_HYSTERESIS_MS = 4000;
let _lastTransientBubble = null;
let _transientBusyUntil = 0;
function effectiveTransientBubble() {
  const t = transientStatusBubble(latestRoomStatus);
  if (t) {
    _lastTransientBubble = t;
    _transientBusyUntil = Date.now() + TRANSIENT_HYSTERESIS_MS;
    return t;
  }
  if (_lastTransientBubble && Date.now() < _transientBusyUntil) {
    return _lastTransientBubble;   // 유예 안: 직전 말풍선 유지(찰나 null/idle blip을 무시 → 깜빡임 제거)
  }
  _lastTransientBubble = null;
  return null;
}

function renderMessages(rows) {
  const list = document.getElementById("room-messages");
  const prevScrollTop = list.scrollTop;
  const nearBottom = list.scrollHeight - prevScrollTop - list.clientHeight < 80;
  lastMessageRows = Array.isArray(rows) ? rows : [];
  markRecordedOutbox(lastMessageRows);
  const serverClientIds = new Set(lastMessageRows.map((r) => r.client_message_id).filter(Boolean));
  const localRows = outbox
    .filter((item) => item.space === currentSpace && item.state !== "recorded" && !serverClientIds.has(item.clientMessageId))
    .map((item) => ({
      __outbox: true,
      화자: "대표",
      코드: outboxStateLabel(item),
      역할: "user",
      내용: item.text,
      시각: item.createdAt,
      client_message_id: item.clientMessageId,
      event_seq: item.ack?.event_seq,
      state: item.state,
      error: item.error,
    }));
  const allRows = [...lastMessageRows, ...localRows];
  const transient = effectiveTransientBubble();
  if (!allRows.length && !transient) {
    list.innerHTML = `<div class="room-empty">아직 대화가 없습니다</div>`;
    lastMsgSig = null;
    updateLatestButton();
    return;
  }

  // 메시지 내용이 직전과 같으면 메시지 DOM을 재생성하지 않는다 → 폴링마다 이미지/PDF가 다시 로드되며
  // 번쩍이는 현상 제거. 변하는 transient(생각 중 등)는 별도 슬롯에서만 갱신.
  const sig = (currentSpace || "") + "::" + allRows.map(_msgRowSig).join("~");
  const rebuilt = sig !== lastMsgSig;
  if (rebuilt) {
    lastMsgSig = sig;
    const messages = allRows.map((r) => {
      const isHandback = !r.__outbox && r.message_id && r.message_id === handbackMessageId;
      const approval = (!r.__outbox && r.message_id) ? approvalsByMsgId[r.message_id] : null;
      const cls = [
        "msg",
        r.__outbox ? `outbox ${esc(outboxClass(r.state))}` : messageClass(r),
        isHandback ? "handback-highlight" : "",
        approval ? "approval-highlight" : "",
      ].filter(Boolean).join(" ");
      return `
    <article class="${cls}">
      <header>
        <strong>${esc(r.화자 || "?")}</strong>
        <span>${esc(r.코드 || "")}</span>
        ${isHandback ? `<span class="handback-badge" title="${esc(handbackReason || "대표 확인 필요")}">확인 필요</span>` : ""}
        ${approval ? `<span class="approval-badge" title="${esc(approval.approval_reason || "대표 결재 필요")}">결재 필요</span>` : ""}
        <time>${esc((r.시각 || "").replace("T", " "))}</time>
      </header>
      <div class="msg-body">${renderMessageBody(r.내용 || "")}</div>
      ${approval ? `
      <div class="approval-actions">
        <span class="approval-reason">${esc(approval.approval_reason || "")}</span>
        <button class="approval-btn approve" data-plan-id="${esc(approval.plan_id)}">진행</button>
        <button class="approval-btn reject" data-plan-id="${esc(approval.plan_id)}">반려</button>
      </div>` : ""}
      ${r.__outbox && r.error ? `<div class="msg-meta error">${esc(r.error)}</div>` : ""}
    </article>`;
    }).join("");
    list.innerHTML = messages + `<div id="transient-slot"></div>`;
    scanEmbeds(list);
  }

  // transient는 메시지 DOM을 건드리지 않고 슬롯만 갱신(임베드 없음 → 번쩍임 없음).
  let slot = document.getElementById("transient-slot");
  if (!slot) { slot = document.createElement("div"); slot.id = "transient-slot"; list.appendChild(slot); }
  slot.innerHTML = transient ? `
    <article class="msg transient ${esc(transient.kind)}">
      <header>
        <strong>${esc(transient.speaker)}</strong>
        <span>${esc(transient.code)}</span>
      </header>
      <div class="msg-body">${esc(transient.text)}</div>
    </article>` : "";

  // [스크롤 튐/자꾸 새로고침 근본수정] 예전엔 near-bottom이면 '매 폴(1.5s)마다' scrollTop을 재설정해
  // 사용자가 스크롤해도 계속 아래로 당겨지고(화면이 자꾸 새로고침되는 느낌), 위로 스크롤해 읽는 중
  // 메시지 목록이 재생성되면 브라우저가 scrollTop을 0으로 리셋해 '맨 아래로 내렸는데 다시 맨 위로 튄다'
  // (대표 신고). → **메시지가 실제로 바뀐 폴(rebuilt)에서만** 스크롤을 건드린다. 변화 없는 폴은 손대지
  // 않아 사용자의 스크롤을 방해하지 않는다.
  if (rebuilt) {
    if (nearBottom) {
      // 맨 아래 근처 + 새 메시지 → 자연스럽게 따라 내려간다.
      list.scrollTop = list.scrollHeight;
      requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; updateLatestButton(); });
    } else {
      // 위로 스크롤해 읽는 중 재생성 → 읽던 위치를 유지(맨 위로 튀지 않게).
      list.scrollTop = prevScrollTop;
    }
  }
  updateLatestButton();
}

function activityClass(row) {
  const state = row.상태 || "";
  if (state === "manager_running" || state === "manager_queued" || state === "manager_retrying") return "manager";
  if (state === "agent_running" || state === "chat_request_work_received" || state === "task_created_from_chat_request") return "agent";
  if (
    state === "wake_failed"
    || state === "manager_failed"
    || state === "lesson_application_missing"
    || state.includes("timeout")
    || row.label?.includes("시간 초과")
    || (state === "idle" && row.last_action === "wake_failed")
    || (state === "idle" && row.last_action === "lesson_application_missing")
  ) return "failed";
  if (state === "idle" && row.last_action === "stop") return "stop";
  if (state === "posted") return "posted";
  return "idle";
}

function activityKind(row) {
  const state = String(row.상태 || "");
  const label = String(row.label || "");
  if (
    state.includes("failed")
    || state.includes("failure")
    || state.includes("corrupt")
    || state.includes("timeout")
    || state === "wake_failed"
    || state === "manager_failed"
    || state === "lesson_application_missing"
    || label.includes("실패")
    || label.includes("손상")
    || label.includes("시간 초과")
  ) return "failure";
  if (
    state.startsWith("manager_")
    || state === "posted"
    || label.includes("공간관리")
    || label.includes("JSON")
  ) return "manager";
  if (
    state.includes("work_settings")
    || state.includes("runtime")
    || label.includes("설정")
    || label.includes("엔진")
  ) return "settings";
  if (
    state.startsWith("task_")
    || state.includes("task")
    || state === "request_progress"
    || state === "revise_task"
    || state === "chat_request_work_received"
  ) return "task";
  return "flow";
}

function activityFilterOptions(rows = []) {
  const counts = { all: rows.length, manager: 0, task: 0, settings: 0, failure: 0 };
  rows.forEach((row) => {
    const kind = activityKind(row);
    if (kind === "manager") counts.manager += 1;
    if (kind === "task") counts.task += 1;
    if (kind === "settings") counts.settings += 1;
    if (kind === "failure") counts.failure += 1;
  });
  return [
    ["all", "전체", counts.all],
    ["manager", "공간관리", counts.manager],
    ["task", "작업", counts.task],
    ["settings", "설정", counts.settings],
    ["failure", "실패", counts.failure],
  ];
}

function activityMatchesFilter(row) {
  if (activityFilter === "all") return true;
  return activityKind(row) === activityFilter;
}

function activityRowKey(row) {
  const stable = [
    row.message_id && `msg:${row.message_id}`,
    row.event_seq && `event:${row.event_seq}`,
    row.status_seq && `status:${row.status_seq}`,
    row.task_id && `task:${row.task_id}`,
    row.release_id && `release:${row.release_id}`,
    row.candidate_id && `candidate:${row.candidate_id}`,
    row.context_pack_id && `ctx:${row.context_pack_id}`,
    row.wake_id && `wake:${row.wake_id}`,
  ].filter(Boolean);
  if (stable.length) {
    return stable.join("|");
  }
  return [
    row.시각,
    row.상태,
    row.actor,
    row.target || row.current,
    row.label,
    row.detail || row.reason || row.event,
  ].filter((v) => v !== undefined && v !== null && v !== "").join("|");
}

function mergeActivityRows(base = [], recent = []) {
  const merged = new Map();
  [...base, ...recent].forEach((row) => {
    if (!row || typeof row !== "object") return;
    merged.set(activityRowKey(row), row);
  });
  return Array.from(merged.values()).slice(-80);
}

async function loadActivityRows(space, statusActivity = [], options = {}) {
  const now = Date.now();
  const shouldFetchFull = (
    options.forceActivity
    || lastActivitySpace !== space
    || !latestActivityRows.length
    || now - lastActivityFetchMs >= ACTIVITY_FULL_REFRESH_MS
  );
  if (shouldFetchFull) {
    try {
      const rows = await api.spaceActivity(space, 80);
      lastActivityFetchMs = now;
      lastActivitySpace = space;
      return mergeActivityRows([], rows);
    } catch (_) {
      // /status의 최신 activity로 즉시 대체한다.
    }
  }
  return mergeActivityRows(latestActivityRows, statusActivity || []);
}

function renderActivity(rows = []) {
  const box = document.getElementById("room-activity");
  if (!box) return;
  latestActivityRows = Array.isArray(rows) ? rows : [];
  const filtered = latestActivityRows.filter(activityMatchesFilter);
  const controls = activityFilterOptions(latestActivityRows).map(([key, label, count]) => `
    <button class="activity-filter" type="button" data-activity-filter="${esc(key)}" aria-pressed="${activityFilter === key ? "true" : "false"}">
      ${esc(label)} <span>${esc(count)}</span>
    </button>
  `).join("");
  const rowsHtml = filtered.length ? filtered.slice(-12).reverse().map((r) => {
    const label = r.label || r.상태 || "상태";
    const meta = [r.actor, r.target || r.current, (r.시각 || "").replace("T", " ")].filter(Boolean).join(" · ");
    const detail = r.detail || r.reason || r.event || "";
    return `
      <div class="activity-row ${activityClass(r)}">
        <span class="activity-dot"></span>
        <div class="activity-main">
          <div class="activity-label">${esc(label)}</div>
          <div class="activity-meta">${esc(meta)}</div>
          ${detail ? `<div class="activity-detail">${esc(detail)}</div>` : ""}
        </div>
      </div>`;
  }).join("") : `<div class="activity-empty">해당 이력 없음</div>`;
  box.innerHTML = `
    <div class="activity-toolbar" aria-label="진행 이력 필터">${controls}</div>
    <div class="activity-list">${rowsHtml}</div>
  `;
}

function newClientMessageId(space) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `ui:${space}:${random}`;
}

function outboxStateLabel(item = {}) {
  if (item.state === "pending") return "전송 대기";
  if (item.state === "sending") return "전송 중";
  if (item.state === "acked") {
    return item.ack?.event_seq ? `ACK #${item.ack.event_seq}` : "ACK";
  }
  if (item.state === "error") return "전송 실패";
  return "전송";
}

function outboxClass(state) {
  if (state === "error") return "error";
  if (state === "acked") return "acked";
  if (state === "sending") return "sending";
  return "pending";
}

function outboxCounts(space = currentSpace) {
  const items = outbox.filter((item) => item.space === space && item.state !== "recorded");
  return {
    pending: items.filter((item) => item.state === "pending").length,
    sending: items.filter((item) => item.state === "sending").length,
    acked: items.filter((item) => item.state === "acked").length,
    error: items.filter((item) => item.state === "error").length,
    total: items.length,
  };
}

function updateSendButton() {
  const submit = document.getElementById("room-send");
  if (!submit) return;
  const counts = outboxCounts(currentSpace);
  submit.disabled = false;
  submit.textContent = counts.pending || counts.sending ? "전송중" : "보내기";
}

function memberName(member = {}) {
  return member.이름 || member.name || member.토큰 || member.code || "";
}

function memberCode(member = {}) {
  return member.코드 || member.code || member.토큰 || "";
}

function spaceRowMatches(row = {}, space = currentSpace) {
  return Boolean(space && (row.토큰 === space || row.코드 === space || row.이름 === space));
}

function spaceOptionLabel(row = {}) {
  const name = row.이름 || row.토큰 || "이름 없음";
  const code = row.코드 ? ` (${row.코드})` : "";
  return `${name}${code}`;
}

function renderRoomSpaceSelect({ failed = false } = {}) {
  const select = document.getElementById("room-space-select");
  if (!select) return;
  const rows = Array.isArray(latestSpaceRows) ? latestSpaceRows : [];
  if (failed) {
    select.innerHTML = `<option value="">공간 목록 확인 실패</option>`;
    select.value = "";
    select.disabled = true;
    return;
  }
  if (!rows.length) {
    select.innerHTML = `<option value="">공간 없음</option>`;
    select.value = "";
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = [
    `<option value="">공간 선택</option>`,
    ...rows.map((row) => {
      const value = row.토큰 || row.이름 || row.코드 || "";
      const members = Array.isArray(row.멤버) ? row.멤버.length : 0;
      const title = `${spaceOptionLabel(row)} · 참여 ${members}명`;
      return `<option value="${esc(value)}" title="${esc(title)}">${esc(spaceOptionLabel(row))}</option>`;
    }),
  ].join("");
  select.value = currentSpace && rows.some((row) => spaceRowMatches(row)) ? currentSpace : "";
}

function renderRoomParticipants({ failed = false } = {}) {
  const sub = document.getElementById("room-sub");
  const box = document.getElementById("room-participants");
  if (!sub || !box) return;
  const keepBottom = currentSpace && isRoomMessagesNearBottom(120);
  if (!currentSpace) {
    sub.textContent = "공간 카드의 열기 버튼으로 들어갑니다";
    box.innerHTML = "";
    box.hidden = true;
    updateRoomHeadCompact();
    return;
  }
  const members = Array.isArray(latestRoomMembers) ? latestRoomMembers : [];
  if (failed && !members.length) {
    sub.textContent = "참여 목록 확인 실패";
    box.hidden = false;
    box.innerHTML = `<span class="room-participant-chip empty">참여 목록 확인 실패</span>`;
    updateRoomHeadCompact();
    if (keepBottom) requestAnimationFrame(() => scrollRoomToLatest({ smooth: false }));
    else updateLatestButton();
    return;
  }
  sub.textContent = members.length ? `참여 ${members.length}명` : "참여 에이전트 없음";
  if (!members.length) {
    box.innerHTML = "";
    box.hidden = true;
    updateRoomHeadCompact();
    if (keepBottom) requestAnimationFrame(() => scrollRoomToLatest({ smooth: false }));
    else updateLatestButton();
    return;
  }
  box.hidden = false;
  box.innerHTML = members.map((member) => {
    const name = memberName(member) || "이름 없음";
    const code = memberCode(member);
    const runtime = [member.engine || member.엔진, member.model || member.모델].filter(Boolean).join(" · ");
    const title = [name, code, runtime].filter(Boolean).join(" · ");
    return `
      <span class="room-participant-chip" title="${esc(title)}">
        <span>${esc(name)}</span>
        ${code ? `<span class="participant-code">${esc(code)}</span>` : ""}
      </span>`;
  }).join("");
  updateRoomHeadCompact();
  if (keepBottom) {
    requestAnimationFrame(() => scrollRoomToLatest({ smooth: false }));
  } else {
    updateLatestButton();
  }
}

async function refreshRoomParticipants() {
  if (!currentSpace) {
    latestRoomMembers = [];
    renderRoomParticipants();
    return;
  }
  const space = currentSpace;
  try {
    const spaces = await api.spaces();
    if (space !== currentSpace) return;
    latestSpaceRows = Array.isArray(spaces) ? spaces : [];
    renderRoomSpaceSelect();
    const row = latestSpaceRows.find((item) => spaceRowMatches(item, space));
    latestRoomMembers = Array.isArray(row?.멤버) ? row.멤버 : [];
    renderRoomParticipants();
  } catch (_) {
    if (space !== currentSpace) return;
    renderRoomSpaceSelect({ failed: true });
    renderRoomParticipants({ failed: true });
  }
}

export async function refreshCurrentRoomParticipants() {
  await refreshRoomParticipants();
}

export async function refreshRoomSpaceSelect() {
  try {
    const spaces = await api.spaces();
    latestSpaceRows = Array.isArray(spaces) ? spaces : [];
    renderRoomSpaceSelect();
    if (currentSpace) {
      const row = latestSpaceRows.find((item) => spaceRowMatches(item));
      latestRoomMembers = Array.isArray(row?.멤버) ? row.멤버 : [];
      renderRoomParticipants();
    }
  } catch (_) {
    renderRoomSpaceSelect({ failed: true });
  }
}

async function refreshRoomMemberControl() {
  const form = document.getElementById("room-member-form");
  const select = document.getElementById("room-member-select");
  const joinButton = document.getElementById("room-member-join");
  if (!form || !select || !joinButton) return;
  if (!currentSpace) {
    select.innerHTML = `<option value="">공간 선택 필요</option>`;
    select.disabled = true;
    joinButton.disabled = true;
    return;
  }
  try {
    const people = await api.people();
    const available = (people || []).filter((p) => !(p.공간 || []).includes(currentSpace));
    select.innerHTML = available.length
      ? available.map((p) => {
        const runtime = [p.engine, p.model].filter(Boolean).join(" · ");
        return `<option value="${esc(p.토큰)}">${esc(p.이름)} (${esc(p.코드)})${runtime ? ` · ${esc(runtime)}` : ""}</option>`;
      }).join("")
      : `<option value="">입장 가능 없음</option>`;
    select.disabled = !available.length;
    joinButton.disabled = !available.length;
  } catch (_) {
    select.innerHTML = `<option value="">목록 확인 실패</option>`;
    select.disabled = true;
    joinButton.disabled = true;
  }
}

function markRecordedOutbox(rows = []) {
  const serverClientIds = new Set((rows || []).map((row) => row.client_message_id).filter(Boolean));
  let changed = false;
  outbox = outbox.map((item) => {
    if (serverClientIds.has(item.clientMessageId)) {
      changed = true;
      return { ...item, state: "recorded", updatedAt: Date.now() };
    }
    return item;
  }).filter((item) => item.state !== "recorded");
  if (changed) updateSendButton();
}

function saveObserverVisibility() {
  try {
    localStorage.setItem(OBSERVER_COLLAPSE_STORAGE_KEY, JSON.stringify(observerCollapsed));
    localStorage.setItem(OBSERVER_SECTIONS_STORAGE_KEY, JSON.stringify([...collapsedObserverSections]));
  } catch (_) {}
}

function applyObserverVisibility() {
  const roomView = document.getElementById("roomView");
  if (roomView) roomView.dataset.observerCollapsed = observerCollapsed ? "yes" : "no";
  const allBtn = document.getElementById("room-observer-all-toggle");
  if (allBtn) {
    allBtn.setAttribute("aria-pressed", observerCollapsed ? "false" : "true");
    allBtn.setAttribute("aria-expanded", observerCollapsed ? "false" : "true");
    allBtn.textContent = observerCollapsed ? "상태 펼치기" : "상태 접기";
  }
  observerSections.forEach(([key, elementId]) => {
    const hidden = collapsedObserverSections.has(key);
    const el = document.getElementById(elementId);
    if (el) el.setAttribute("data-section-hidden", hidden ? "yes" : "no");
    const btn = document.querySelector(`[data-observer-section="${key}"]`);
    if (btn) {
      btn.setAttribute("aria-pressed", hidden ? "false" : "true");
      btn.setAttribute("aria-expanded", hidden ? "false" : "true");
      btn.title = hidden ? `${btn.textContent} 펼치기` : `${btn.textContent} 접기`;
    }
  });
}

function wireObserverControls() {
  const toolbar = document.getElementById("room-diagnostics-toolbar");
  if (!toolbar) return;
  if (toolbar.dataset.wired === "yes") {
    applyObserverVisibility();
    return;
  }
  toolbar.dataset.wired = "yes";
  const allBtn = document.getElementById("room-observer-all-toggle");
  if (allBtn) {
    allBtn.onclick = () => {
      observerCollapsed = !observerCollapsed;
      saveObserverVisibility();
      applyObserverVisibility();
    };
  }
  toolbar.querySelectorAll("[data-observer-section]").forEach((btn) => {
    btn.onclick = () => {
      const key = btn.dataset.observerSection;
      if (!key) return;
      if (collapsedObserverSections.has(key)) collapsedObserverSections.delete(key);
      else collapsedObserverSections.add(key);
      if (observerCollapsed) observerCollapsed = false;
      saveObserverVisibility();
      applyObserverVisibility();
    };
  });
  applyObserverVisibility();
}

function updateRoomHeadCompact() {
  const title = document.getElementById("room-compact-title");
  const status = document.getElementById("room-compact-status");
  if (title) title.textContent = currentSpace || "공간을 선택하세요";
  if (status) {
    const statusText = document.getElementById("room-status")?.textContent || "상태 없음";
    const participantCount = latestRoomMembers.length ? ` · ${latestRoomMembers.length}명` : "";
    status.textContent = `${statusText}${participantCount}`;
  }
}

function setRoomHeadCollapsed(collapsed, { persist = true } = {}) {
  roomHeadCollapsed = Boolean(collapsed);
  const view = document.getElementById("roomView");
  const compact = document.getElementById("room-head-compact");
  const collapseBtn = document.getElementById("room-head-toggle");
  const expandBtn = document.getElementById("room-head-expand");
  if (view) view.dataset.roomHeadCollapsed = roomHeadCollapsed ? "yes" : "no";
  if (compact) compact.hidden = !roomHeadCollapsed;
  if (collapseBtn) collapseBtn.setAttribute("aria-expanded", roomHeadCollapsed ? "false" : "true");
  if (expandBtn) expandBtn.setAttribute("aria-expanded", roomHeadCollapsed ? "true" : "false");
  if (persist) {
    try { localStorage.setItem(ROOM_HEAD_COLLAPSE_STORAGE_KEY, JSON.stringify(roomHeadCollapsed)); } catch (_) {}
  }
  updateRoomHeadCompact();
  updateLatestButton();
}

function wireRoomHeadCollapse() {
  const collapseBtn = document.getElementById("room-head-toggle");
  const expandBtn = document.getElementById("room-head-expand");
  if (collapseBtn) collapseBtn.onclick = () => setRoomHeadCollapsed(true);
  if (expandBtn) expandBtn.onclick = () => setRoomHeadCollapsed(false);
  setRoomHeadCollapsed(roomHeadCollapsed, { persist: false });
}

function terminalAttachUrl(sessionId) {
  const sid = String(sessionId || "").trim();
  return sid ? `${monitorBase()}/static/attach.html?sid=${encodeURIComponent(sid)}` : "";
}

function normalizeWorkConsoleUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^https?:\/\//i.test(raw)) return raw;
  if (raw.startsWith("/")) return `${monitorBase()}${raw}`;
  if (raw.includes("attach.html")) return `${monitorBase()}/${raw.replace(/^\/+/, "")}`;
  return "";
}

function workConsoleUrlFromObject(obj = {}) {
  if (!obj || typeof obj !== "object") return "";
  const directUrl = (
    obj.console_url
    || obj.terminal_url
    || obj.work_console_url
    || obj.attach_url
  );
  const url = normalizeWorkConsoleUrl(directUrl);
  if (url) return url;
  const directSession = (
    obj.console_session_id
    || obj.terminal_session_id
    || obj.work_console_session_id
    || obj.pty_session_id
    || obj.session_id
    || obj.sid
    || obj.console_sid
    || obj.terminal_sid
    || obj.latest_console_session_id
    || obj.latest_terminal_session_id
  );
  const attachUrl = terminalAttachUrl(directSession);
  if (attachUrl) return attachUrl;
  return workConsoleUrlFromObject(obj.console || obj.terminal || obj.work_console);
}

function workConsoleUrlFromTasks(tasks = {}) {
  const pools = [
    tasks,
    ...activeTaskItems(tasks),
    ...(Array.isArray(tasks.runtime_activity_items) ? tasks.runtime_activity_items : []),
    ...(Array.isArray(tasks.running_items) ? tasks.running_items : []),
    ...(Array.isArray(tasks.cancel_requested_items) ? tasks.cancel_requested_items : []),
  ];
  for (const item of pools) {
    const url = workConsoleUrlFromObject(item);
    if (url) return url;
  }
  return "";
}

function saveWorkConsoleCollapsed() {
  try { localStorage.setItem(WORK_CONSOLE_COLLAPSE_STORAGE_KEY, JSON.stringify(workConsoleCollapsed)); } catch (_) {}
}

function setWorkConsoleCollapsed(collapsed, { persist = true } = {}) {
  workConsoleCollapsed = Boolean(collapsed);
  const panel = document.getElementById("room-work-panel");
  const consoleBox = document.getElementById("room-work-console");
  const banner = document.getElementById("room-work-banner");
  const toggle = document.getElementById("room-work-toggle");
  if (panel) panel.classList.toggle("collapsed", workConsoleCollapsed);
  if (consoleBox) consoleBox.hidden = workConsoleCollapsed;
  if (banner) banner.setAttribute("aria-expanded", workConsoleCollapsed ? "false" : "true");
  if (toggle) toggle.textContent = workConsoleCollapsed ? "콘솔" : "접기";
  if (persist) saveWorkConsoleCollapsed();
  updateLatestButton();
}

let _appliedWorkFrameSrc = "";   // iframe에 마지막으로 적용한 src(재로드 thrash 방지용 추적)
let _workEmptyMode = "";
function renderWorkConsole(url) {
  const next = url || "";
  // [모바일/Safari 프리즈 근본수정] 예전엔 렌더마다 frame.src를 (재)설정했다. frame.src(프로퍼티)는 절대
  // URL로 정규화돼 저장값과의 문자열 비교(frame.src !== url)가 어긋나면 iframe이 매 폴 재네비게이션(재로드)
  // 됐고, 실제 방에선 라이브 스트리밍 작업콘솔 iframe이 iOS WebKit 메인스레드를 포화시켜 화면이 얼고 터치가
  // 다 먹통이 됐다(대표 신고 'Safari에서 방 열면 렌더 안 됨+터치 먹통' — WebKit로 재현·확증: 작업중/agent_running
  // 상태에서 방 열면 HANG, renderWorkConsole 비활성화 시 정상). 그래서:
  //  ① iframe에 '적용한 src'를 따로 추적해 실제로 바뀔 때만 건드린다(재로드 thrash 제거).
  //  ② 모바일에선 무거운 콘솔 iframe을 아예 임베드하지 않고 '새 탭으로 열기' 링크만 준다.
  latestWorkConsoleUrl = next;
  const isMobile = window.matchMedia("(max-width: 720px)").matches;
  const frame = document.getElementById("room-work-frame");
  const empty = document.getElementById("room-work-empty");
  const open = document.getElementById("room-work-open");
  const embed = Boolean(next) && !isMobile;
  const wantSrc = embed ? next : "about:blank";
  if (frame && _appliedWorkFrameSrc !== wantSrc) { frame.src = wantSrc; _appliedWorkFrameSrc = wantSrc; }
  if (frame) frame.hidden = !embed;
  if (open) {
    if (next) { if (open.getAttribute("href") !== next) open.href = next; open.hidden = false; }
    else { open.hidden = true; open.removeAttribute("href"); }
  }
  // innerHTML도 '모드'가 바뀔 때만 갱신(매 폴 innerHTML 재설정 방지).
  const mode = next ? (isMobile ? "mobilelink" : "hidden") : "none";
  if (empty && _workEmptyMode !== mode) {
    _workEmptyMode = mode;
    if (mode === "hidden") { empty.hidden = true; }
    else if (mode === "mobilelink") { empty.hidden = false; empty.innerHTML = `작업 콘솔은 <a href="${esc(next)}" target="_blank" rel="noopener">새 탭에서 열기</a> (모바일은 임베드 생략)`; }
    else { empty.hidden = false; empty.innerHTML = `연결된 작업 콘솔 없음 <a href="${esc(monitorBase())}/static/index.html" target="_blank" rel="noopener">터미널 열기</a>`; }
  }
}

function resetWorkPanel() {
  const panel = document.getElementById("room-work-panel");
  const banner = document.getElementById("room-work-banner");
  const consoleBox = document.getElementById("room-work-console");
  const toggle = document.getElementById("room-work-toggle");
  if (banner) {
    banner.textContent = "작업 상태 없음";
    banner.setAttribute("aria-expanded", "false");
    delete banner.dataset.kind;
  }
  if (panel) {
    panel.hidden = true;
    panel.classList.add("collapsed");
    delete panel.dataset.kind;
  }
  if (consoleBox) consoleBox.hidden = true;
  if (toggle) toggle.textContent = workConsoleCollapsed ? "콘솔" : "접기";
  renderWorkConsole("");
  updateLatestButton();
}

function wireWorkConsole() {
  const banner = document.getElementById("room-work-banner");
  const toggle = document.getElementById("room-work-toggle");
  const handler = () => setWorkConsoleCollapsed(!workConsoleCollapsed);
  if (banner) banner.onclick = handler;
  if (toggle) toggle.onclick = handler;
  setWorkConsoleCollapsed(workConsoleCollapsed, { persist: false });
}

// 대표가 "지금 뭔가 돌고 있다"를 한눈에 — 기술 진단칩이 아니라 사람이 읽는 한 줄(메인 흐름, 항상 보임).
function renderWorkBanner(st = {}) {
  const panel = document.getElementById("room-work-panel");
  const box = document.getElementById("room-work-banner");
  if (!panel || !box) return;
  const tasks = st.tasks || {};
  const state = st.상태 || "";
  const running = Number(tasks.running_count || 0);
  let text = "";
  let kind = "";
  if (running > 0) {
    const worker = tasks.latest_worker || "에이전트";
    const note = tasks.latest_heartbeat_note || tasks.latest_heartbeat_phase || "";
    text = `🔧 ${worker} 백그라운드 작업 중${note ? " — " + String(note).slice(0, 80) : ""} (${running}건 진행)`;
    kind = "work";
  } else if (state === "agent_running") {
    text = `✍️ ${st.current || "에이전트"} 응답 작성 중…`;
    kind = "agent";
  } else if (state === "manager_running" || state === "manager_queued" || state === "manager_retrying") {
    text = "💬 공간관리가 다음 차례를 정하는 중…";
    kind = "manager";
  }
  if (text) {
    box.textContent = text;
    box.dataset.kind = kind;
    panel.dataset.kind = kind;
    panel.hidden = false;
    // [모바일/Safari 프리즈 근본수정] 작업 콘솔 iframe을 방 안에 임베드하면 iOS WebKit 메인스레드가 포화돼
    // 화면이 얼고 터치가 다 먹통이 된다(대표 신고 'Safari에서 방 열면 렌더 안 됨+터치 먹통' — WebKit로
    // 재현·확증: 작업중/agent_running/manager_running 상태에서 방 열면 HANG, renderWorkConsole 비활성화 시 정상).
    // 그래서 모바일에선 콘솔 iframe을 아예 렌더하지 않고 접힘 상태로 둔다(배너 텍스트만 표시).
    if (window.matchMedia("(max-width: 720px)").matches) {
      setWorkConsoleCollapsed(true, { persist: false });
    } else {
      renderWorkConsole(workConsoleUrlFromTasks(tasks));
      setWorkConsoleCollapsed(workConsoleCollapsed, { persist: false });
    }
  } else {
    resetWorkPanel();
  }
}

// ── 감시 소견 가시화 ── 감시 에이전트가 실제로 기록한 두 기준 평가를 칩/패널로 표시 ──
const WATCH_STATUS = {
  ok:   { icon: "🟢", label: "좋음",  cls: "ok" },
  warn: { icon: "🟡", label: "주의",  cls: "warn" },
  bad:  { icon: "🔴", label: "문제",  cls: "bad" },
};
function watchStatus(s) { return WATCH_STATUS[s] || { icon: "⚪", label: "미상", cls: "unknown" }; }

function relTime(iso) {
  const t = Date.parse(iso || "");
  if (!Number.isFinite(t)) return "";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}초 전`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}분 전`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}시간 전`;
  return `${Math.round(hr / 24)}일 전`;
}

// 감시 칩(현재 방 한정): 라이브 '감시 중' + 마지막 소견의 목표/집단지성/시각. snapshot 맨 앞에 끼운다.
function watchChips() {
  const chips = [];
  if (monitorSession && monitorSession.space === currentSpace) {
    chips.push(["watchlive", "🟢 감시 중"]);
  }
  const r = latestWatchReport;
  if (r) {
    const g = watchStatus(r.goal && r.goal.status);
    const w = watchStatus(r.growth && r.growth.status);
    chips.push([`watch ${g.cls}`, `목표 ${g.icon}`]);
    chips.push([`watch ${w.cls}`, `집단지성 ${w.icon}`]);
    const issues = (r.findings || []).filter((f) => f && (f.severity === "warn" || f.severity === "bad")).length;
    if (issues) chips.push(["watch bad", `이슈 ${issues}`]);
    const rel = relTime(r.updated_at);
    if (rel) chips.push(["watch", `감시 ${rel}`]);
  }
  return chips;
}

function renderWatchReport(report) {
  const box = document.getElementById("room-watch-report");
  if (!box) return;
  const live = monitorSession && monitorSession.space === currentSpace;
  if (!report && !live) { box.dataset.empty = "yes"; box.innerHTML = ""; return; }
  box.dataset.empty = "no";
  const parts = [];
  parts.push('<div class="watch-report-chips">');
  if (live) parts.push('<span class="snapshot-chip watchlive">🟢 감시 중</span>');
  if (report) {
    const g = watchStatus(report.goal && report.goal.status);
    const w = watchStatus(report.growth && report.growth.status);
    parts.push(`<span class="snapshot-chip watch ${g.cls}">목표 ${g.icon} ${esc(g.label)}</span>`);
    parts.push(`<span class="snapshot-chip watch ${w.cls}">집단지성 ${w.icon} ${esc(w.label)}</span>`);
    const rel = relTime(report.updated_at);
    if (rel) parts.push(`<span class="snapshot-chip watch">감시 ${esc(rel)}</span>`);
  }
  parts.push("</div>");
  if (report) {
    const g = watchStatus(report.goal && report.goal.status);
    const w = watchStatus(report.growth && report.growth.status);
    if (report.goal && report.goal.note) parts.push(`<div class="watch-line"><b>목표</b> ${esc(report.goal.note)}</div>`);
    if (report.growth && report.growth.note) parts.push(`<div class="watch-line"><b>집단지성</b> ${esc(report.growth.note)}</div>`);
    if (report.summary) parts.push(`<div class="watch-summary">${esc(report.summary)}</div>`);
    const findings = (report.findings || []).filter(Boolean);
    if (findings.length) {
      parts.push('<ul class="watch-findings">');
      findings.forEach((f) => {
        const s = watchStatus(f.severity);
        parts.push(`<li class="sev-${esc(s.cls)}">${s.icon} ${esc(f.text)}</li>`);
      });
      parts.push("</ul>");
    }
    parts.push(`<div class="watch-meta">${esc(report.by || "감시")} · ${esc(report.updated_at || "")}</div>`);
  } else if (live) {
    parts.push('<div class="watch-summary">관리자에이전트가 이 방을 감시 중입니다. 진단을 마치면 소견이 여기 칩으로 표시됩니다.</div>');
  }
  box.innerHTML = parts.join("");
}

function renderSnapshot(st = {}) {
  const box = document.getElementById("room-snapshot");
  if (!box) return;
  const delivery = st.delivery || {};
  const active = st.active_wakes || [];
  const staleWakes = st.stale_wakes || [];
  const failures = st.failures || [];
  const claim = st.manager_claim || {};
  const publishLedger = st.publish_ledger || {};
  const candidateQueue = st.candidate_queue || {};
  const contextPacks = st.context_packs || {};
  const memory = st.space_memory || {};
  const learning = st.learning || {};
  const tasks = st.tasks || {};
  const releaseQueue = st.release_queue || {};
  const obligations = st.response_obligations || {};
  const rapidInput = st.rapid_input || {};
  const seatProjectionBaselines = st.seat_projection_baselines || [];
  const statusSeq = Number(st.snapshot_status_seq);
  const roomGeneration = Number(st.current_room_generation || st.orchestration?.current_room_generation);
  const publishCounts = publishLedger.counts || {};
  const outboxState = outboxCounts(currentSpace);
  const chips = [
    ["event", delivery.last_event_seq ? `event #${delivery.last_event_seq}` : "event 없음"],
    ["generation", Number.isFinite(roomGeneration) && roomGeneration > 0 ? `gen #${roomGeneration}` : "gen ?"],
    ["statusseq", Number.isFinite(statusSeq) && statusSeq > 0 && !st.status_legacy ? `status #${statusSeq}` : "status legacy"],
    ["messages", `${delivery.message_count || 0} messages`],
    ["wake", active.length ? `${active.length} active` : "active 없음"],
    ["failure", failures.length ? `${failures.length} failure` : "failure 없음"],
  ];
  const runningIntent = active.find((w) => w.intent_id)?.intent_id || staleWakes.find((w) => w.intent_id)?.intent_id || claim.intent_id || "";
  if (runningIntent) {
    chips.push(["intent", runningIntent.slice(0, 18)]);
  }
  if (Number(publishCounts.claimed || 0) > 0 || Number(publishCounts.committed || 0) > 0) {
    const committed = Number(publishCounts.committed || 0);
    const pending = Number(publishCounts.claimed || 0);
    chips.push(["publish", pending ? `pub ${committed} done · ${pending} pending` : `pub ${committed} done`]);
  }
  if (Number(candidateQueue.candidate_count || 0) > 0) {
    const count = Number(candidateQueue.candidate_count || 0);
    const pending = Number(candidateQueue.pending_count || 0);
    const errors = Number(candidateQueue.error_count || 0);
    const suffix = errors ? ` · ${errors} error` : "";
    chips.push(["candidate", `candidate ${count} · ${pending} pending${suffix}`]);
  }
  if (Number(contextPacks.wake_manifest_count || 0) > 0) {
    const count = Number(contextPacks.wake_manifest_count || 0);
    const latest = contextPacks.latest_recipient ? ` · ${contextPacks.latest_recipient}` : "";
    chips.push(["pack", `pack ${count}${latest}`]);
  }
  if (memory.projection_available || contextPacks.latest_memory_projection_id) {
    const seq = Number(memory.applied_event_seq || contextPacks.latest_memory_applied_event_seq || 0);
    const version = Number(memory.projection_version || contextPacks.latest_memory_projection_version || 0);
    const lag = Number(memory.projection_lag || contextPacks.latest_memory_projection_lag || 0);
    const lagText = lag ? ` · lag ${lag}` : "";
    chips.push(["memory", `memory #${seq || "?"} · v${version || "?"}${lagText}`]);
  }
  if (Number(contextPacks.turn_handoff_count || 0) > 0 || contextPacks.latest_turn_handoff_id) {
    const handoff = contextPacks.latest_turn_handoff || {};
    const target = handoff.target_agent || handoff.recipient || contextPacks.latest_recipient || "agent";
    chips.push(["handoff", `handoff ${shortId(target, 16)}`]);
  }
  if (contextPacks.latest_lesson_pack_status) {
    const included = (contextPacks.latest_included_lessons || []).length;
    const must = (contextPacks.latest_must_apply_lessons || []).length;
    chips.push(["lessonpack", `lesson ${contextPacks.latest_lesson_pack_status} · ${included} incl · ${must} must`]);
  }
  if (Number(tasks.task_count || 0) > 0) {
    const count = Number(tasks.task_count || 0);
    const state = tasks.latest_state || "unknown";
    const holdCount = Number(tasks.hold_task_count || 0);
    const running = Number(tasks.running_count || 0);
    const cancelPending = Number(tasks.cancel_requested_count || 0);
    const stale = Number(tasks.stale_task_count || 0);
    const reportDue = Number(tasks.progress_report_due_count || 0);
    const reportRequested = Number(tasks.progress_report_requested_count || 0);
    const steeringRuntime = Number(tasks.steering_runtime_count || 0);
    const hold = holdCount ? ` · ${holdCount} hold` : "";
    const live = running || cancelPending ? ` · ${running} run · ${cancelPending} cancel` : "";
    const staleText = stale ? ` · ${stale} stale` : "";
    const reportText = reportDue ? ` · ${reportDue} due` : reportRequested ? ` · ${reportRequested} report` : "";
    const runtimeText = steeringRuntime ? ` · ${steeringRuntime} steering` : "";
    chips.push(["tasks", `tasks ${count} · ${state}${live}${hold}${staleText}${reportText}${runtimeText}`]);
  }
  if (Number(releaseQueue.release_count || 0) > 0) {
    const count = Number(releaseQueue.release_count || 0);
    const pending = Number(releaseQueue.pending_count || 0);
    chips.push(["release", `release ${count} · ${pending} approval`]);
  }
  if (Number(obligations.obligation_count || 0) > 0) {
    const count = Number(obligations.obligation_count || 0);
    const open = Number(obligations.open_count || 0);
    const overdue = Number(obligations.overdue_open_count || 0);
    const state = obligations.latest_state || "unknown";
    const overdueText = overdue ? ` · ${overdue} overdue` : "";
    chips.push(["obligation", `reply ${count} · ${open} open${overdueText} · ${state}`]);
  }
  if (Number(learning.lesson_count || 0) > 0 || Number(learning.post_interaction_evaluation_count || 0) > 0 || Number(learning.post_task_evaluation_count || 0) > 0) {
    const lessons = Number(learning.lesson_count || 0);
    const evals = Number(learning.post_interaction_evaluation_count || 0) + Number(learning.post_task_evaluation_count || 0);
    const pendingPromotion = Number(learning.promotion_pending_count || learning.promotion_candidate_pending_count || 0);
    const openGaps = Number(learning.growth_gap_open_count || 0);
    const promotionText = pendingPromotion ? ` · ${pendingPromotion} review` : "";
    const gapText = openGaps ? ` · ${openGaps} gaps` : "";
    chips.push(["learning", `learn ${lessons} lessons · ${evals} evals${promotionText}${gapText}`]);
  }
  if (Number(st.projection_lag || 0) > 0) {
    chips.push(["lag", `lag ${st.projection_lag}`]);
  }
  if (Number(st.seat_projection_baseline_count || 0) > 0 || seatProjectionBaselines.length) {
    const count = Number(st.seat_projection_baseline_count || seatProjectionBaselines.length || 0);
    chips.push(["baseline", `late join ${count}`]);
  }
  if (Number(st.manager_read_lag || 0) > 0) {
    chips.push(["lag", `read lag ${st.manager_read_lag}`]);
  }
  if (Number(rapidInput.pending_input_count || 0) > 0 || Number(rapidInput.unread_event_count || 0) > 0) {
    chips.push(["backlog", `미처리 입력 ${Number(rapidInput.pending_input_count || 0)} · unread ${Number(rapidInput.unread_event_count || 0)}`]);
  }
  if (outboxState.total) {
    const text = [
      outboxState.pending && `${outboxState.pending} 대기`,
      outboxState.sending && `${outboxState.sending} 전송`,
      outboxState.acked && `${outboxState.acked} ACK`,
      outboxState.error && `${outboxState.error} 실패`,
    ].filter(Boolean).join(" · ");
    chips.unshift(["outbox", text || "전송 대기"]);
  }
  if (st.status_stale) {
    chips.push(["stale", "status stale"]);
  }
  if (staleWakes.length) {
    chips.push(["stale", `${staleWakes.length} stale wake`]);
  }
  if (claim.active || claim.claim_token) {
    chips.push(["claim", claim.active ? `claim #${claim.claim_seq || "?"}` : "claim idle"]);
  }
  if (st.manager_redrive_required || claim.manager_redrive_required) {
    chips.push(["redrive", "redrive 예약"]);
  }
  if (lastAck?.message_id || lastAck?.client_message_id) {
    const seq = lastAck.event_seq ? ` #${lastAck.event_seq}` : "";
    chips.unshift(["ack", lastAck.duplicate ? `ACK duplicate${seq}` : `ACK${seq}`]);
  }
  const allChips = [...watchChips(), ...chips];   // 감시 칩을 맨 앞에(가장 눈에 띄게)
  box.innerHTML = allChips.map(([kind, text]) => `<span class="snapshot-chip ${kind}">${esc(text)}</span>`).join("");
  box.dataset.failures = failures.length ? "yes" : "no";
}

function shortId(value, size = 18) {
  const text = String(value || "");
  return text.length > size ? text.slice(0, size) : text;
}

function shortText(value, size = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > size ? `${text.slice(0, size)}...` : text;
}

function candidateStateLabel(state) {
  return ({
    pending_synthesis: "pending",
    selected_published: "selected",
    synthesized_published: "synthesized",
    discarded: "discarded",
    superseded: "superseded",
    error: "error",
  })[state] || state || "unknown";
}

function candidateStateKind(state) {
  if (state === "pending_synthesis") return "pending";
  if (state === "selected_published") return "selected";
  if (state === "synthesized_published") return "synthesized";
  if (state === "discarded") return "discarded";
  if (state === "superseded") return "superseded";
  if (state === "error") return "error";
  return "unknown";
}

function candidateSortKey(item) {
  const time = Date.parse(item.transitioned_at || item.created_at || "");
  if (Number.isFinite(time)) return time;
  const index = Number(item._row_index);
  return Number.isFinite(index) ? index : 0;
}

function renderCandidatePanel(st = {}) {
  const panel = document.getElementById("room-candidate-panel");
  if (!panel) return;
  const queue = st.candidate_queue || {};
  const latest = Array.isArray(queue.latest) ? queue.latest : [];
  const stateCounts = queue.state_counts || {};
  const hasCandidates = Number(queue.candidate_count || 0) > 0 || latest.length > 0;
  if (!hasCandidates) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const countChips = Object.entries(stateCounts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([state, count]) => `
      <span class="candidate-count ${candidateStateKind(state)}">${esc(candidateStateLabel(state))} ${Number(count || 0)}</span>
    `).join("");
  const ordered = [...latest].sort((a, b) => candidateSortKey(b) - candidateSortKey(a));
  const rows = ordered.slice(0, 8).map((item) => {
    const state = item.state || "unknown";
    const kind = candidateStateKind(state);
    const target = item.target_agent || item.selected_by || item.synthesized_by || item.discarded_by || "candidate";
    const preview = shortText(
      item.public_summary
      || item.reply_preview
      || item.structured_public_reply
      || item.error
      || item.transition_reason
      || item.reason
      || "",
      170,
    );
    const meta = [
      candidateStateLabel(state),
      item.candidate_id && shortId(item.candidate_id, 16),
      item.turn_id && shortId(item.turn_id, 16),
      item.room_generation && `gen ${item.room_generation}`,
    ].filter(Boolean).join(" · ");
    const outcome = [
      item.published_message_id && `msg ${shortId(item.published_message_id, 16)}`,
      item.publish_effect_id && `pub ${shortId(item.publish_effect_id, 16)}`,
      item.transition_reason,
    ].filter(Boolean).join(" · ");
    return `
      <div class="candidate-row ${kind}">
        <div class="candidate-top">
          <span class="candidate-state">${esc(candidateStateLabel(state))}</span>
          <strong>${esc(shortId(target, 20))}</strong>
        </div>
        <div class="candidate-meta">${esc(meta)}</div>
        ${preview ? `<div class="candidate-preview">${esc(preview)}</div>` : ""}
        ${outcome ? `<div class="candidate-outcome">${esc(outcome)}</div>` : ""}
      </div>
    `;
  }).join("");
  panel.innerHTML = `
    <div class="candidate-head">
      <span>후보 큐</span>
      <div class="candidate-counts">${countChips}</div>
    </div>
    <div class="candidate-list">${rows}</div>
  `;
}

function promotionReviewStateLabel(state) {
  return ({
    pending_review: "검토대기",
    approved: "승인",
    rejected: "반려",
  })[state] || state || "unknown";
}

function promotionReviewStateKind(state) {
  if (state === "pending_review") return "pending";
  if (state === "approved") return "approved";
  if (state === "rejected") return "rejected";
  return "unknown";
}

function promotionReviewSortKey(item) {
  const time = Date.parse(item.reviewed_at || item.created_at || "");
  if (Number.isFinite(time)) return time;
  const index = Number(item._row_index);
  return Number.isFinite(index) ? index : 0;
}

function promotionReviewPanelHasRows(st = {}) {
  const learning = st.learning || {};
  return Number(learning.lesson_count || 0) > 0
    || Number(learning.promotion_candidate_count || 0) > 0
    || Number(learning.promotion_pending_count || learning.promotion_candidate_pending_count || 0) > 0
    || Number(learning.promotion_apply_pending_count || 0) > 0
    || Number(learning.resource_apply_count || 0) > 0
    || (Array.isArray(learning.promotion_items) && learning.promotion_items.length > 0)
    || (Array.isArray(learning.promotion_candidate_items) && learning.promotion_candidate_items.length > 0)
    || (Array.isArray(learning.promotion_pending_items) && learning.promotion_pending_items.length > 0);
}

function promotionApplyStateLabel(state) {
  return ({
    not_started: "적용대기",
    applied: "적용완료",
    applied_existing: "이미적용",
    blocked_path_exists: "적용차단",
    blocked_path_read_error: "적용차단",
  })[state] || state || "";
}

function promotionApplyStateKind(state) {
  if (state === "applied" || state === "applied_existing") return "applied";
  if (state && state.startsWith("blocked")) return "blocked";
  if (state === "not_started") return "pending";
  return "";
}

function renderPromotionReviewPanel(st = {}) {
  const panel = document.getElementById("room-promotion-review");
  if (!panel) return;
  const learning = st.learning || {};
  if (!promotionReviewPanelHasRows(st)) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const stateCounts = learning.promotion_state_counts || learning.promotion_candidate_state_counts || {};
  const targetCounts = learning.promotion_target_counts || learning.promotion_candidate_target_counts || {};
  const items = Array.isArray(learning.promotion_items)
    ? learning.promotion_items
    : (Array.isArray(learning.promotion_candidate_items) ? learning.promotion_candidate_items : []);
  const ordered = [...items].sort((a, b) => promotionReviewSortKey(b) - promotionReviewSortKey(a));
  const stateChips = Object.entries(stateCounts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([state, count]) => `
      <span class="promotion-review-count ${promotionReviewStateKind(state)}">${esc(promotionReviewStateLabel(state))} ${Number(count || 0)}</span>
    `).join("");
  const targetChips = Object.entries(targetCounts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([target, count]) => `
      <span class="promotion-review-count target">${esc(target)} ${Number(count || 0)}</span>
    `).join("");
  const applyChips = [
    Number(learning.promotion_apply_pending_count || 0) > 0
      ? `<span class="promotion-review-count pending">적용대기 ${Number(learning.promotion_apply_pending_count || 0)}</span>`
      : "",
    Number(learning.resource_apply_applied_count || 0) > 0
      ? `<span class="promotion-review-count approved">적용완료 ${Number(learning.resource_apply_applied_count || 0)}</span>`
      : "",
    Number(learning.resource_apply_blocked_count || 0) > 0
      ? `<span class="promotion-review-count rejected">적용차단 ${Number(learning.resource_apply_blocked_count || 0)}</span>`
      : "",
  ].join("");
  const rows = ordered.slice(0, 8).map((item) => {
    const state = item.state || "unknown";
    const kind = promotionReviewStateKind(state);
    const applyState = item.apply_state || "";
    const applyKind = promotionApplyStateKind(applyState);
    const reviewActions = state === "pending_review" ? `
      <button class="promotion-review-action" type="button" data-promotion-action="approve" data-promotion-id="${esc(item.promotion_id)}">승인</button>
      <button class="promotion-review-action danger" type="button" data-promotion-action="reject" data-promotion-id="${esc(item.promotion_id)}">반려</button>
    ` : "";
    const applyAction = state === "approved" && (!applyState || applyState === "not_started") ? `
      <button class="promotion-review-action apply" type="button" data-promotion-action="apply" data-promotion-id="${esc(item.promotion_id)}">적용</button>
    ` : "";
    const actions = reviewActions || applyAction ? `
      <div class="promotion-review-actions">
        ${reviewActions}${applyAction}
      </div>
    ` : "";
    const meta = [
      promotionReviewStateLabel(state),
      promotionApplyStateLabel(applyState),
      item.target_kind,
      shortId(item.promotion_id, 16),
      item.lesson_id && `lesson ${shortId(item.lesson_id, 14)}`,
    ].filter(Boolean).join(" · ");
    const review = [item.reviewed_by, item.review_reason].filter(Boolean).join(" · ");
    const applyMeta = [
      item.applied_path,
      item.applied_by && `by ${item.applied_by}`,
      item.apply_detail,
    ].filter(Boolean).join(" · ");
    return `
      <div class="promotion-review-row ${kind} ${applyKind ? `apply-${applyKind}` : ""}">
        <div class="promotion-review-top">
          <span class="promotion-review-state">${esc(promotionReviewStateLabel(state))}</span>
          <strong>${esc(shortText(item.title || item.target_path_suggestion || item.lesson_id, 70))}</strong>
        </div>
        <div class="promotion-review-meta">${esc(meta)}</div>
        ${item.instruction_preview ? `<div class="promotion-review-preview">${esc(shortText(item.instruction_preview, 180))}</div>` : ""}
        ${item.target_path_suggestion ? `<div class="promotion-review-path">${esc(item.target_path_suggestion)}</div>` : ""}
        ${applyMeta ? `<div class="promotion-review-meta apply">${esc(shortText(applyMeta, 180))}</div>` : ""}
        ${review ? `<div class="promotion-review-meta">${esc(review)}</div>` : ""}
        ${actions}
      </div>
    `;
  }).join("");
  const empty = rows ? "" : `<div class="promotion-review-empty">승격 후보 없음 · 명시된 promotion_target 레슨만 후보가 됩니다</div>`;
  panel.innerHTML = `
    <div class="promotion-review-head">
      <span>성장 후보</span>
      <div class="promotion-review-counts">${stateChips}${targetChips}${applyChips}</div>
      <button class="promotion-review-action scan" type="button" data-promotion-action="scan">후보 생성</button>
    </div>
    <div class="promotion-review-list">${rows || empty}</div>
  `;
  panel.querySelectorAll("[data-promotion-action]").forEach((btn) => {
    btn.onclick = () => handlePromotionReviewAction(btn.dataset.promotionAction, btn.dataset.promotionId || "", btn);
  });
}

function flowStateLabel(state) {
  return ({
    done: "완료",
    current: "진행",
    pending: "대기",
    queued: "대기",
    failed: "실패",
    stopped: "멈춤",
    approval: "승인",
  })[state] || state || "대기";
}

function flowStateKind(state) {
  if (state === "current" || state === "queued") return "current";
  if (state === "done") return "done";
  if (state === "failed") return "failed";
  if (state === "stopped") return "stopped";
  if (state === "approval") return "approval";
  return "pending";
}

function renderChatFlowPanel(st = {}) {
  const panel = document.getElementById("room-chat-flow");
  if (!panel) return;
  const flow = st.chat_flow || {};
  const phases = Array.isArray(flow.phases) ? flow.phases : [];
  if (!phases.length) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const latest = flow.latest_message || {};
  const current = flow.current || {};
  const blockers = Array.isArray(flow.blockers) ? flow.blockers : [];
  const headMeta = [
    latest.event_seq && `event #${latest.event_seq}`,
    latest.intent_id && shortId(latest.intent_id, 18),
    current.status_stale && "status stale",
    current.staleness_ms != null && formatDuration(current.staleness_ms),
  ].filter(Boolean).join(" · ");
  const rows = phases.map((phase) => {
    const kind = flowStateKind(phase.state);
    const meta = [
      phase.action,
      phase.target && shortId(phase.target, 22),
      phase.event_seq && `event #${phase.event_seq}`,
    ].filter(Boolean).join(" · ");
    return `
      <div class="chat-flow-step ${kind}">
        <div class="chat-flow-top">
          <span class="chat-flow-state">${esc(flowStateLabel(phase.state))}</span>
          <strong>${esc(phase.label || phase.key || "")}</strong>
        </div>
        ${meta ? `<div class="chat-flow-meta">${esc(meta)}</div>` : ""}
        ${phase.detail ? `<div class="chat-flow-detail">${esc(shortText(phase.detail, 150))}</div>` : ""}
      </div>
    `;
  }).join("");
  const blockerHtml = blockers.length ? `
    <div class="chat-flow-blockers">
      ${blockers.slice(0, 3).map((item) => `<span>${esc(shortText(item, 120))}</span>`).join("")}
    </div>
  ` : "";
  panel.innerHTML = `
    <div class="chat-flow-head">
      <span>채팅 흐름</span>
      <strong>${esc(current.label || "상태 대기")}</strong>
      ${headMeta ? `<em>${esc(headMeta)}</em>` : ""}
    </div>
    <div class="chat-flow-list">${rows}</div>
    ${blockerHtml}
  `;
}

function obligationStateLabel(state) {
  return ({
    open: "대기",
    assigned: "인계",
    delegated: "작업",
    answered: "답변",
    manager_closed: "종료",
    superseded: "대체",
    cancelled: "취소",
    timed_out: "시간초과",
  })[state] || state || "unknown";
}

function obligationStateKind(state) {
  if (state === "open") return "open";
  if (state === "assigned" || state === "delegated") return "active";
  if (state === "answered") return "answered";
  if (state === "manager_closed" || state === "superseded" || state === "cancelled") return "closed";
  if (state === "timed_out") return "failed";
  return "unknown";
}

function obligationSortKey(item) {
  const time = Date.parse(item.closed_at || item.updated_at || "");
  if (Number.isFinite(time)) return time;
  const index = Number(item._row_index);
  return Number.isFinite(index) ? index : 0;
}

function renderObligationPanel(st = {}) {
  const panel = document.getElementById("room-obligation-panel");
  if (!panel) return;
  const obligations = st.response_obligations || {};
  const latest = Array.isArray(obligations.latest) ? obligations.latest : [];
  const openItems = Array.isArray(obligations.open_items) ? obligations.open_items : [];
  if (!Number(obligations.obligation_count || 0) && !latest.length && !openItems.length) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const stateCounts = obligations.state_counts || {};
  const countChips = Object.entries(stateCounts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([state, count]) => `
      <span class="obligation-count ${obligationStateKind(state)}">${esc(obligationStateLabel(state))} ${Number(count || 0)}</span>
    `).join("");
  const overdueChip = Number(obligations.overdue_open_count || 0) > 0
    ? `<span class="obligation-count failed">초과 ${Number(obligations.overdue_open_count || 0)}</span>`
    : "";
  const rowsSource = openItems.length ? openItems : latest;
  const rows = [...rowsSource].sort((a, b) => obligationSortKey(b) - obligationSortKey(a)).slice(0, 8).map((item) => {
    const state = item.state || "unknown";
    const kind = obligationStateKind(state);
    const overdue = Boolean(item.overdue);
    const target = item.assigned_to || item.responder || item.target_actor || "space";
    const meta = [
      obligationStateLabel(state),
      item.source_event_seq && `event #${item.source_event_seq}`,
      item.room_generation && `gen ${item.room_generation}`,
      item.published_message_id && `msg ${shortId(item.published_message_id, 14)}`,
      item.task_id && `task ${shortId(item.task_id, 14)}`,
      item.age_ms != null && `age ${formatDuration(item.age_ms)}`,
      item.remaining_ms != null && !overdue && `left ${formatDuration(item.remaining_ms)}`,
      overdue && "overdue",
    ].filter(Boolean).join(" · ");
    const policy = [
      item.auto_policy && `auto_policy ${item.auto_policy}`,
      item.policy_reason,
      Array.isArray(item.policy_blockers) && item.policy_blockers.length ? `protected ${item.policy_blockers.join(", ")}` : "",
    ].filter(Boolean).join(" · ");
    const preview = shortText(policy || item.transition_reason || item.source_text_preview || item.obligation_id, 180);
    return `
      <div class="obligation-row ${kind}${overdue ? " overdue" : ""}">
        <div class="obligation-top">
          <span class="obligation-state">${esc(obligationStateLabel(state))}</span>
          <strong>${esc(shortId(target, 22))}</strong>
        </div>
        <div class="obligation-meta">${esc(meta)}</div>
        ${preview ? `<div class="obligation-preview">${esc(preview)}</div>` : ""}
      </div>
    `;
  }).join("");
  panel.innerHTML = `
    <div class="obligation-head">
      <span>응답 의무</span>
      <div class="obligation-counts">${countChips}${overdueChip}</div>
    </div>
    <div class="obligation-list">${rows || `<div class="obligation-empty">열린 응답 의무 없음</div>`}</div>
  `;
}

function renderTurnHandoffPanel(st = {}) {
  const panel = document.getElementById("room-turn-handoff");
  if (!panel) return;
  const contextPacks = st.context_packs || {};
  const handoff = contextPacks.latest_turn_handoff || {};
  const handoffId = handoff.turn_handoff_id || contextPacks.latest_turn_handoff_id || "";
  if (!handoffId) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const responseTarget = handoff.response_target || {};
  const returnContract = handoff.return_contract || {};
  const allowedActions = Array.isArray(handoff.allowed_actions) ? handoff.allowed_actions : [];
  const disallowedActions = Array.isArray(handoff.disallowed_actions) ? handoff.disallowed_actions : [];
  const target = handoff.target_agent || handoff.recipient || contextPacks.latest_recipient || "agent";
  const contract = [
    returnContract.kind,
    returnContract.structured_request_schema,
    returnContract.request_work_route,
    returnContract.published_by,
  ].filter(Boolean).join(" · ");
  const targetMeta = [
    handoff.delivery_type || contextPacks.latest_delivery_type,
    handoff.source_event_seq && `event #${handoff.source_event_seq}`,
    responseTarget.reply_to_message_id && `reply ${shortId(responseTarget.reply_to_message_id, 14)}`,
    responseTarget.intent_id && shortId(responseTarget.intent_id, 14),
  ].filter(Boolean).join(" · ");
  const ids = [
    handoff.wake_id && `wake ${shortId(handoff.wake_id, 16)}`,
    handoffId && `turn ${shortId(handoffId, 16)}`,
    handoff.context_pack_id && `ctx ${shortId(handoff.context_pack_id, 16)}`,
    handoff.manifest_id && `manifest ${shortId(handoff.manifest_id, 16)}`,
  ].filter(Boolean).join(" · ");
  const actionText = [
    ...allowedActions.slice(0, 3),
    allowedActions.length > 3 ? `+${allowedActions.length - 3}` : "",
  ].filter(Boolean).join(" · ");
  const blockers = disallowedActions.slice(0, 3).join(" · ");
  const rows = [
    ["대상", targetMeta],
    ["이유", shortText(handoff.why_you || handoff.manager_message_preview || "", 220)],
    ["계약", contract],
    ["허용", actionText],
    blockers ? ["금지", blockers] : null,
  ].filter(Boolean).map(([label, detail]) => `
    <div class="turn-handoff-row">
      <span>${esc(label)}</span>
      <strong>${esc(detail || "-")}</strong>
    </div>
  `).join("");
  const brief = shortText(handoff.turn_handoff_brief_preview || handoff.manager_message_preview || "", 260);
  panel.innerHTML = `
    <div class="turn-handoff-head">
      <span>턴 인계</span>
      <strong>${esc(shortId(target, 22))}</strong>
      ${ids ? `<em>${esc(ids)}</em>` : ""}
    </div>
    <div class="turn-handoff-grid">${rows}</div>
    ${brief ? `<div class="turn-handoff-brief">${esc(brief)}</div>` : ""}
  `;
}

function taskStateLabel(item = {}) {
  if (item.cancel_requested || item.state === "cancel_requested") return "cancel";
  if (item.steering_runtime_label) return item.steering_runtime_label;
  if (item.pending_steering_ack) return "ack wait";
  if (item.progress_report_due) return "report due";
  if (item.progress_report_requested_since_heartbeat) return "report req";
  if (item.heartbeat_stale) return "stale";
  return item.state || "running";
}

function taskStateKind(item = {}) {
  const runtime = item.steering_runtime_state || "";
  if (item.cancel_requested || item.state === "cancel_requested") return "cancel";
  if (runtime === "revise_detected" || runtime === "revise_restarting") return "restarting";
  if (runtime === "revise_applied") return "applied";
  if (runtime === "progress_seen" || runtime === "progress_requested") return "reported";
  if (runtime === "ack_wait") return "steering";
  if (item.pending_steering_ack) return "steering";
  if (item.progress_report_due) return "due";
  if (item.progress_report_requested_since_heartbeat) return "reported";
  if (item.heartbeat_stale) return "stale";
  if (item.state === "running") return "running";
  if (item.state === "blocked" || item.state === "error") return "error";
  return "idle";
}

function taskAttentionScore(item = {}) {
  const runtime = item.steering_runtime_state || "";
  if (item.cancel_requested || item.state === "cancel_requested") return 110;
  if (item.pending_steering_ack) return 100;
  if (runtime === "revise_detected" || runtime === "revise_restarting") return 95;
  if (runtime === "revise_applied" || runtime === "progress_seen" || runtime === "progress_requested") return 80;
  if (item.progress_report_due) return 70;
  if (item.heartbeat_stale) return 60;
  if (item.progress_report_requested_since_heartbeat) return 40;
  return 0;
}

function formatDuration(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return "";
  const sec = Math.floor(value / 1000);
  if (sec < 90) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 90) return `${min}m`;
  const hour = Math.floor(min / 60);
  return `${hour}h`;
}

function activeTaskItems(tasks = {}) {
  const items = (Array.isArray(tasks.active_items) && tasks.active_items.length) ? tasks.active_items : [
    ...(Array.isArray(tasks.running_items) ? tasks.running_items : []),
    ...(Array.isArray(tasks.cancel_requested_items) ? tasks.cancel_requested_items : []),
  ];
  return [...items].sort((a, b) => {
    const score = taskAttentionScore(b) - taskAttentionScore(a);
    if (score) return score;
    return String(a.task_id || "").localeCompare(String(b.task_id || ""));
  });
}

function taskPanelHasRows(st = {}) {
  const tasks = st.tasks || {};
  return (
    activeTaskItems(tasks).length > 0
    || Number(tasks.stale_task_count || 0) > 0
    || Number(tasks.progress_report_due_count || 0) > 0
    || Number(tasks.progress_report_requested_count || 0) > 0
    || Number(tasks.hold_task_count || 0) > 0
    || Number(tasks.release_enqueue_failed_count || 0) > 0
  );
}

function renderTaskPanel(st = {}) {
  const panel = document.getElementById("room-task-panel");
  if (!panel) return;
  const tasks = st.tasks || {};
  const activeItems = activeTaskItems(tasks);
  const stateCounts = tasks.state_counts || {};
  const rows = activeItems.slice(0, 8);
  const hasRows = taskPanelHasRows(st);
  if (!hasRows) {
    panel.dataset.empty = "yes";
    panel.innerHTML = "";
    return;
  }
  panel.dataset.empty = "no";
  const countChips = [
    Number(tasks.running_count || 0) ? ["running", `run ${Number(tasks.running_count || 0)}`] : null,
    Number(tasks.cancel_requested_count || 0) ? ["cancel", `cancel ${Number(tasks.cancel_requested_count || 0)}`] : null,
    Number(tasks.steering_runtime_count || 0) ? ["runtime", `steering ${Number(tasks.steering_runtime_count || 0)}`] : null,
    Number(tasks.stale_task_count || 0) ? ["stale", `stale ${Number(tasks.stale_task_count || 0)}`] : null,
    Number(tasks.progress_report_due_count || 0) ? ["due", `due ${Number(tasks.progress_report_due_count || 0)}`] : null,
    Number(tasks.progress_report_requested_count || 0) ? ["reported", `report ${Number(tasks.progress_report_requested_count || 0)}`] : null,
    Number(tasks.hold_task_count || 0) ? ["hold", `hold ${Number(tasks.hold_task_count || 0)}`] : null,
    Number(tasks.release_enqueue_failed_count || 0) ? ["error", `release error ${Number(tasks.release_enqueue_failed_count || 0)}`] : null,
  ].filter(Boolean).map(([kind, text]) => `<span class="task-count ${kind}">${esc(text)}</span>`).join("");
  const stateSummary = Object.entries(stateCounts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([state, count]) => `${state} ${Number(count || 0)}`)
    .join(" · ");
  const activeRows = rows.map((item) => {
    const kind = taskStateKind(item);
    const heartbeatAge = formatDuration(item.heartbeat_age_ms);
    const threshold = formatDuration(item.heartbeat_stale_threshold_ms || tasks.heartbeat_stale_threshold_ms);
    const reportThreshold = formatDuration(item.progress_report_due_threshold_ms || tasks.progress_report_due_threshold_ms);
    const runnerTimeout = formatDuration(Number(item.runner_timeout_sec || 0) * 1000);
    const heartbeatInterval = formatDuration(Number(item.heartbeat_interval_sec || 0) * 1000);
    const heartbeat = [
      item.heartbeat_phase || "heartbeat",
      heartbeatAge && `age ${heartbeatAge}`,
      item.heartbeat_stale && threshold && `>${threshold}`,
      item.progress_report_due && reportThreshold && `report>${reportThreshold}`,
      item.last_heartbeat_at && item.last_heartbeat_at.replace("T", " "),
    ].filter(Boolean).join(" · ");
    const meta = [
      shortId(item.task_id, 16),
      item.room_generation && `gen ${item.room_generation}`,
      item.source_event_seq && `event #${item.source_event_seq}`,
      item.cancellation_reason,
      item.latest_steering_action && `steer ${item.latest_steering_action}`,
      item.latest_steering_reason_code,
      item.latest_steering_requested_at && `steer at ${item.latest_steering_requested_at.replace("T", " ")}`,
      item.latest_steering_control_request_source_event_seq && `control #${item.latest_steering_control_request_source_event_seq}`,
      item.pending_ack_steering_action && `pending ${item.pending_ack_steering_action}`,
      item.pending_ack_steering_seq && `ack #${item.pending_ack_steering_seq}`,
      item.steering_runtime_state,
      runnerTimeout && `timeout ${runnerTimeout}`,
      heartbeatInterval && `hb ${heartbeatInterval}`,
      item.work_settings_source && `cfg ${item.work_settings_source}`,
      item.pending_steering_ack && "ack 대기",
      item.progress_report_due && "보고 필요",
      item.progress_report_requested_since_heartbeat && "보고 요청됨",
    ].filter(Boolean).join(" · ");
    const steeringNote = [
      item.latest_steering_instruction && `최근 요청: ${item.latest_steering_instruction}`,
      item.pending_ack_steering_instruction && `ack 대기: ${item.pending_ack_steering_instruction}`,
      item.progress_report_due_reason && `보고 필요 사유: ${item.progress_report_due_reason}`,
    ].filter(Boolean).join(" · ");
    const steeringAckPending = Boolean(item.pending_steering_ack);
    const steeringActions = steeringAckPending ? "" : `
        <button class="task-action" type="button" data-task-action="progress" data-task-id="${esc(item.task_id)}">부분보고</button>
        <button class="task-action" type="button" data-task-action="revise" data-task-id="${esc(item.task_id)}">재지시</button>
    `;
    const settingsAction = `
        <button class="task-action" type="button" data-task-action="settings" data-task-id="${esc(item.task_id)}"
          data-runner-timeout-sec="${esc(item.runner_timeout_sec || 300)}"
          data-heartbeat-interval-sec="${esc(item.heartbeat_interval_sec || 10)}"
          data-heartbeat-stale-ms="${esc(item.heartbeat_stale_threshold_ms || 60000)}"
          data-progress-report-due-ms="${esc(item.progress_report_due_threshold_ms || 60000)}">설정</button>
    `;
    const actions = (!item.cancel_requested && item.state === "running") ? `
      <div class="task-actions">
        ${settingsAction}
        ${steeringActions}
        <button class="task-action danger" type="button" data-task-action="cancel" data-task-id="${esc(item.task_id)}">취소</button>
      </div>
    ` : "";
    return `
      <div class="task-row ${kind}">
        <div class="task-top">
          <span class="task-state">${esc(taskStateLabel(item))}</span>
          <strong>${esc(shortId(item.worker_agent || item.task_id, 22))}</strong>
          ${actions}
        </div>
        <div class="task-meta">${esc(meta)}</div>
        <div class="task-heartbeat">${esc(heartbeat)}</div>
        ${steeringNote ? `<div class="task-note steering-note">${esc(shortText(steeringNote, 220))}</div>` : ""}
        ${item.heartbeat_note ? `<div class="task-note">${esc(shortText(item.heartbeat_note, 150))}</div>` : ""}
      </div>
    `;
  }).join("");
  const holdRows = !activeRows && Number(tasks.hold_task_count || 0) ? `
    <div class="task-row hold">
      <div class="task-top"><span class="task-state">hold</span><strong>${esc(shortId(tasks.latest_hold_worker || tasks.latest_hold_task_id, 22))}</strong></div>
      <div class="task-meta">${esc([shortId(tasks.latest_hold_task_id), shortId(tasks.latest_hold_task_pack_id)].filter(Boolean).join(" · "))}</div>
      <div class="task-note">${esc(shortText(tasks.latest_hold_error || "레슨 적용 보고 확인 필요", 180))}</div>
    </div>
  ` : "";
  const releaseErrorRows = !activeRows && Number(tasks.release_enqueue_failed_count || 0) ? `
    <div class="task-row error">
      <div class="task-top"><span class="task-state">error</span><strong>${esc(shortId(tasks.latest_release_enqueue_failed_task_id || "release queue", 22))}</strong></div>
      <div class="task-note">${esc(shortText(tasks.latest_release_enqueue_failed_error || "공개 대기열 등록 실패", 180))}</div>
    </div>
  ` : "";
  panel.innerHTML = `
    <div class="task-head">
      <span>작업 제어</span>
      <div class="task-counts">${countChips}</div>
    </div>
    ${stateSummary ? `<div class="task-summary">${esc(stateSummary)}</div>` : ""}
    <div class="task-list">${activeRows || holdRows || releaseErrorRows}</div>
  `;
  panel.querySelectorAll("[data-task-action]").forEach((btn) => {
    btn.onclick = () => handleTaskAction(btn.dataset.taskAction, btn.dataset.taskId, btn);
  });
}

function renderStatusDetails(st = {}) {
  const box = document.getElementById("room-status-detail");
  if (!box) return;
  const rows = [];
  const active = st.active_wakes || [];
  const staleWakes = st.stale_wakes || [];
  const failures = st.failures || [];
  const recovery = st.recovery_actions || [];
  const tasks = st.tasks || {};
  const releaseQueue = st.release_queue || {};
  const candidateQueue = st.candidate_queue || {};
  const obligations = st.response_obligations || {};
  const memory = st.space_memory || {};
  const learning = st.learning || {};
  const rapidInput = st.rapid_input || {};
  const seatProjectionBaselines = st.seat_projection_baselines || [];
  const runningTasks = tasks.running_items || [];
  const cancelRequestedTasks = tasks.cancel_requested_items || [];
  const taskRuntimeActivity = Array.isArray(st.task_runtime_activity)
    ? st.task_runtime_activity
    : (Array.isArray(tasks.runtime_activity_items) ? tasks.runtime_activity_items : []);
  const hasTaskPanel = taskPanelHasRows(st);
  const pendingReleases = releaseQueue.pending_items || [];
  const approvedReleases = releaseQueue.approved_items || [];
  const pendingCandidates = candidateQueue.pending_items || [];
  const candidateErrors = candidateQueue.error_items || [];
  const candidatePanelHasRows = Number(candidateQueue.candidate_count || 0) > 0 || (candidateQueue.latest || []).length > 0;
  const hasPromotionReviewPanel = promotionReviewPanelHasRows(st);
  const lagByMember = st.projection_lag_by_member || [];
  if (memory.memory_source || memory.projection_available) {
    const directiveCount = Array.isArray(memory.user_directive_items) ? memory.user_directive_items.length : 0;
    const activeTopicCount = Array.isArray(memory.active_topic_threads) ? memory.active_topic_threads.length : 0;
    const dormantTopicCount = Array.isArray(memory.dormant_topic_threads) ? memory.dormant_topic_threads.length : 0;
    rows.push([
      memory.projection_lag ? "lag" : "memory",
      "맥락 projection",
      [
        memory.memory_source || "unknown",
        memory.applied_event_seq && `event #${memory.applied_event_seq}`,
        memory.projection_version && `v${memory.projection_version}`,
        directiveCount ? `directives ${directiveCount}` : "",
        activeTopicCount ? `active topics ${activeTopicCount}` : "",
        dormantTopicCount ? `dormant ${dormantTopicCount}` : "",
        memory.projection_lag ? `lag ${memory.projection_lag}` : "clean",
      ].filter(Boolean).join(" · "),
    ]);
  }
  if (Number(rapidInput.pending_input_count || 0) > 0 || Number(rapidInput.unread_event_count || 0) > 0) {
    rows.push([
      "backlog",
      "미처리 입력",
      [
        `${Number(rapidInput.pending_input_count || 0)}건`,
        rapidInput.latest_pending_event_seq && `event #${rapidInput.latest_pending_event_seq}`,
        rapidInput.latest_pending_intent_id && shortId(rapidInput.latest_pending_intent_id),
      ].filter(Boolean).join(" · "),
    ]);
  }
  active.slice(0, 4).forEach((w) => {
    rows.push(["active", w.actor || w.type || "active", [
      w.state,
      w.wake_id && `wake ${shortId(w.wake_id, 14)}`,
      w.turn_handoff_id && `turn ${shortId(w.turn_handoff_id, 14)}`,
      w.context_pack_id && `ctx ${shortId(w.context_pack_id, 14)}`,
      w.intent_id && shortId(w.intent_id),
      w.lease_expires_at_utc && `lease ${w.lease_expires_at_utc}`,
    ].filter(Boolean).join(" · ")]);
  });
  staleWakes.slice(0, 4).forEach((w) => {
    rows.push(["stale", w.actor || w.type || "stale", [w.state, w.reason].filter(Boolean).join(" · ")]);
  });
  lagByMember.slice(0, 4).forEach((m) => {
    const baseline = m.projection_baseline_event_seq ? ` · late join baseline #${m.projection_baseline_event_seq}` : "";
    const required = m.projection_required_event_count != null ? ` · required ${m.projection_required_event_count}` : "";
    rows.push(["lag", m.token || "member", `projection lag ${m.tail_lag || 0} · missing ${m.missing_count || 0}${baseline}${required}`]);
  });
  if (!lagByMember.length && seatProjectionBaselines.length) {
    seatProjectionBaselines.slice(0, 3).forEach((m) => {
      rows.push([
        "baseline",
        m.token || "member",
        [
          m.projection_baseline_event_seq && `baseline #${m.projection_baseline_event_seq}`,
          m.projection_required_event_count != null && `required ${m.projection_required_event_count}`,
          m.last_event_seq && `last #${m.last_event_seq}`,
        ].filter(Boolean).join(" · "),
      ]);
    });
  }
  if (Number(tasks.task_count || 0) > 0) {
    rows.push(["task", tasks.latest_worker || "task", [shortId(tasks.latest_task_id), tasks.latest_state, tasks.latest_release_queue_state].filter(Boolean).join(" · ")]);
  }
  taskRuntimeActivity.slice(0, 4).forEach((item) => {
    rows.push([
      "task-runtime",
      shortId(item.worker_agent || item.task_id || "task"),
      [
        item.label || item.state,
        shortId(item.task_id),
        item.steering_action && `steer ${item.steering_action}`,
        item.heartbeat_phase,
        item.at && item.at.replace("T", " "),
        shortText(item.detail, 120),
      ].filter(Boolean).join(" · "),
    ]);
  });
  if (!hasTaskPanel) {
    runningTasks.forEach((t) => {
      rows.push({
        kind: "task",
        label: shortId(t.worker_agent || t.task_id),
        detail: [shortId(t.task_id), t.state, t.heartbeat_phase, t.last_heartbeat_at && `hb ${t.last_heartbeat_at}`].filter(Boolean).join(" · "),
        actions: [
          { action: "cancel", label: "취소", taskId: t.task_id },
        ],
      });
    });
    cancelRequestedTasks.forEach((t) => {
      rows.push({
        kind: "task cancel",
        label: shortId(t.worker_agent || t.task_id),
        detail: [shortId(t.task_id), "취소 요청됨", t.cancellation_reason, t.heartbeat_phase].filter(Boolean).join(" · "),
        actions: [],
      });
    });
  }
  if (Number(releaseQueue.release_count || 0) > 0) {
    rows.push(["release", shortId(releaseQueue.latest_source_task_id || releaseQueue.latest_release_id), [releaseQueue.latest_state, releaseQueue.latest_approval_state, `${releaseQueue.pending_count || 0} approval`].filter(Boolean).join(" · ")]);
  }
  if (Number(candidateQueue.candidate_count || 0) > 0) {
    rows.push(["candidate", shortId(candidateQueue.latest_target_agent || candidateQueue.latest_candidate_id), [candidateQueue.latest_state, `${candidateQueue.pending_count || 0} pending`, candidateQueue.latest_reply_preview].filter(Boolean).join(" · ")]);
  }
  if (Number(obligations.obligation_count || 0) > 0) {
    rows.push([
      "obligation",
      "응답 의무",
      [
        `${Number(obligations.open_count || 0)} open`,
        obligations.latest_state,
        shortId(obligations.latest_obligation_id || "", 16),
      ].filter(Boolean).join(" · "),
    ]);
  }
  if (Number(learning.promotion_candidate_count || 0) > 0) {
    rows.push([
      "learning",
      "성장 후보",
      [
        `${Number(learning.promotion_candidate_count || 0)}건`,
        `${Number(learning.promotion_pending_count || learning.promotion_candidate_pending_count || 0)} 검토대기`,
        learning.latest_promotion_target_kind,
        shortId(learning.latest_promotion_id || learning.latest_promotion_candidate_id),
      ].filter(Boolean).join(" · "),
    ]);
  } else if (!hasPromotionReviewPanel && Number(learning.lesson_count || 0) > 0) {
    rows.push(["learning", "레슨", `${Number(learning.lesson_count || 0)} lessons · 후보 생성 가능`]);
  }
  if (Number(learning.growth_gap_open_count || 0) > 0) {
    rows.push([
      "learning",
      "성장 갭",
      [
        `${Number(learning.growth_gap_open_count || 0)} open`,
        learning.growth_gap_state_counts && Object.keys(learning.growth_gap_state_counts).slice(0, 2).join(", "),
      ].filter(Boolean).join(" · "),
    ]);
  }
  if (!candidatePanelHasRows) {
    pendingCandidates.slice(0, 4).forEach((c) => {
      rows.push(["candidate", shortId(c.target_agent || c.candidate_id), [shortId(c.turn_id), c.state, c.structured_action, c.reply_preview].filter(Boolean).join(" · ")]);
    });
    candidateErrors.slice(0, 3).forEach((c) => {
      rows.push(["candidate error", shortId(c.target_agent || c.candidate_id), [shortId(c.turn_id), c.error].filter(Boolean).join(" · ")]);
    });
  }
  pendingReleases.forEach((r) => {
    rows.push({
      kind: "release",
      label: shortId(r.source_task_id || r.release_id),
      detail: [r.state, r.approval_state, r.public_summary].filter(Boolean).join(" · "),
      actions: [
        { action: "approve", label: "승인", releaseId: r.release_id },
        { action: "reject", label: "거절", releaseId: r.release_id },
      ],
    });
  });
  approvedReleases.forEach((r) => {
    rows.push({
      kind: "release",
      label: shortId(r.source_task_id || r.release_id),
      detail: [r.state, "공개 대기", r.public_summary].filter(Boolean).join(" · "),
      actions: [
        { action: "publish", label: "공개", releaseId: r.release_id },
      ],
    });
  });
  failures.slice(0, 4).forEach((f) => {
    rows.push(["failure", f.actor || f.상태 || "failure", [f.label, f.target, f.detail, f.task_id && shortId(f.task_id)].filter(Boolean).join(" · ")]);
  });
  recovery.slice(0, 3).forEach((item) => {
    rows.push(["recovery", "recovery", item]);
  });
  if (!rows.length) {
    box.dataset.empty = "yes";
    box.innerHTML = "";
    return;
  }
  box.dataset.empty = "no";
  box.innerHTML = rows.map((row) => {
    const item = Array.isArray(row) ? { kind: row[0], label: row[1], detail: row[2], actions: [] } : row;
    const actions = (item.actions || []).map((a) => `
      <button class="status-action" type="button"
        ${a.taskId ? `data-task-action="${esc(a.action)}" data-task-id="${esc(a.taskId)}"` : `data-release-action="${esc(a.action)}" data-release-id="${esc(a.releaseId)}"`}>
        ${esc(a.label)}
      </button>
    `).join("");
    return `
    <div class="status-detail-row ${esc(item.kind)}">
      <span>${esc(item.label)}</span>
      <strong>${esc(item.detail)}${actions ? `<div class="status-actions">${actions}</div>` : ""}</strong>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-release-action]").forEach((btn) => {
    btn.onclick = () => handleReleaseAction(btn.dataset.releaseAction, btn.dataset.releaseId, btn);
  });
  box.querySelectorAll("[data-task-action]").forEach((btn) => {
    btn.onclick = () => handleTaskAction(btn.dataset.taskAction, btn.dataset.taskId, btn);
  });
}

function openTaskControlModal(options = {}) {
  const modal = document.getElementById("task-control-modal");
  const form = document.getElementById("task-control-form");
  const title = document.getElementById("task-control-title");
  const label = document.getElementById("task-control-label");
  const input = document.getElementById("task-control-input");
  const note = document.getElementById("task-control-note");
  const submit = document.getElementById("task-control-submit");
  const cancel = document.getElementById("task-control-cancel");
  if (!modal || !form || !title || !label || !input || !note || !submit || !cancel) {
    return Promise.resolve(null);
  }
  title.textContent = options.title || "작업 제어";
  label.textContent = options.label || "내용";
  input.value = options.defaultText || "";
  input.required = Boolean(options.required);
  note.textContent = options.note || "";
  submit.textContent = options.submitText || "요청";
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  setTimeout(() => input.focus(), 0);
  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      form.removeEventListener("submit", onSubmit);
      cancel.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKeydown);
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
    };
    const done = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const onSubmit = (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (options.required && !value) {
        input.focus();
        return;
      }
      done(value);
    };
    const onCancel = () => done(null);
    const onBackdrop = (event) => {
      if (event.target === modal) done(null);
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") done(null);
    };
    form.addEventListener("submit", onSubmit);
    cancel.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKeydown);
  });
}

async function handleReleaseAction(action, releaseId, button) {
  if (!currentSpace || !releaseId) return;
  const oldText = button?.textContent || "";
  try {
    if (button) {
      button.disabled = true;
      button.textContent = "처리중";
    }
    if (action === "approve") {
      await api.approveRelease(currentSpace, releaseId, "대시보드 승인");
    } else if (action === "reject") {
      const reasonInput = prompt("거절 사유", "대표가 공개를 거절함");
      if (reasonInput === null) return;
      const reason = reasonInput.trim() || "대표가 공개를 거절함";
      await api.rejectRelease(currentSpace, releaseId, reason);
    } else if (action === "publish") {
      await api.publishRelease(currentSpace, releaseId, null);
      await refreshRoomChat();
    }
    await refreshRoomStatus({ forceActivity: true });
  } catch (err) {
    alert("실패: " + err.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText;
    }
  }
}

async function handleTaskAction(action, taskId, button) {
  if (!currentSpace || !taskId) return;
  let request;
  if (action === "cancel") {
    const reasonInput = await openTaskControlModal({
      title: "작업 취소",
      label: "취소 사유",
      defaultText: "대표가 작업 취소를 요청함",
      submitText: "취소 요청",
      note: "작업자에게 취소 steering을 남기고, 완료 결과가 늦게 도착해도 공개 대기열 등록을 막습니다.",
    });
    if (reasonInput === null) return;
    const reason = reasonInput.trim() || "대표가 작업 취소를 요청함";
    request = () => api.cancelTask(currentSpace, taskId, reason);
  } else if (action === "progress") {
    const instructionInput = await openTaskControlModal({
      title: "부분 보고 요청",
      label: "작업자에게 보낼 요청",
      defaultText: "현재 진행 상황, 막힌 점, 다음 단계, 부분 결과를 작업 상태에 남겨줘",
      submitText: "보고 요청",
      note: "작업은 멈추지 않고 진행 보고 steering만 추가합니다.",
    });
    if (instructionInput === null) return;
    const instruction = instructionInput.trim() || "현재 진행 상황, 막힌 점, 다음 단계, 부분 결과를 작업 상태에 남겨줘";
    request = () => api.progressTask(currentSpace, taskId, instruction);
  } else if (action === "revise") {
    const instructionInput = await openTaskControlModal({
      title: "작업 재지시",
      label: "새 지시",
      defaultText: "",
      submitText: "재지시",
      required: true,
      note: "작업자가 이 재지시를 확인하기 전에는 작업 결과 공개가 보류됩니다.",
    });
    if (instructionInput === null) return;
    const instruction = instructionInput.trim();
    if (!instruction) return;
    request = () => api.reviseTask(currentSpace, taskId, instruction);
  } else if (action === "settings") {
    const settings = await openWorkSettingsModal("작업 실행설정", button?.dataset || {}, {
      note: "현재 실행 중인 작업에도 반영됩니다. timeout을 줄이면 실행이 중단될 수 있습니다.",
    });
    if (!settings) return;
    request = () => api.updateTaskWorkSettings(currentSpace, taskId, settings);
  } else {
    return;
  }
  const oldText = button?.textContent || "";
  try {
    if (button) {
      button.disabled = true;
      button.textContent = "처리중";
    }
    await request();
    await refreshRoomStatus({ forceActivity: true });
  } catch (err) {
    alert("실패: " + err.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText;
    }
  }
}

async function handlePromotionReviewAction(action, promotionId, button) {
  if (!currentSpace) return;
  let request;
  if (action === "scan") {
    request = () => api.scanLessonPromotions(currentSpace, 20);
  } else if (action === "approve") {
    if (!promotionId) return;
    request = () => api.approveLessonPromotion(currentSpace, promotionId, "대시보드 승인");
  } else if (action === "reject") {
    if (!promotionId) return;
    const reasonInput = await openTaskControlModal({
      title: "성장 후보 반려",
      label: "반려 사유",
      defaultText: "대표가 지식/스킬 승격을 보류함",
      submitText: "반려",
      note: "레슨은 유지하고, 전역 지식/스킬 파일에는 반영하지 않습니다.",
    });
    if (reasonInput === null) return;
    const reason = reasonInput.trim() || "대표가 지식/스킬 승격을 보류함";
    request = () => api.rejectLessonPromotion(currentSpace, promotionId, reason);
  } else if (action === "apply") {
    if (!promotionId) return;
    const reasonInput = await openTaskControlModal({
      title: "성장 후보 적용",
      label: "적용 사유",
      defaultText: "대표가 승인된 성장 후보를 지식/스킬 리소스로 적용함",
      submitText: "적용",
      note: "기존 리소스 파일은 덮어쓰지 않습니다. 충돌하면 적용차단 상태로 남깁니다.",
    });
    if (reasonInput === null) return;
    const reason = reasonInput.trim() || "대표가 승인된 성장 후보를 지식/스킬 리소스로 적용함";
    request = () => api.applyLessonPromotion(currentSpace, promotionId, reason);
  } else {
    return;
  }
  const oldText = button?.textContent || "";
  try {
    if (button) {
      button.disabled = true;
      button.textContent = "처리중";
    }
    await request();
    await refreshRoomStatus({ forceActivity: true });
  } catch (err) {
    alert("실패: " + err.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = oldText;
    }
  }
}

export async function refreshRoomChat() {
  if (!currentSpace) return;
  const space = currentSpace;
  let rows;
  try {
    rows = await api.spaceMessages(space, 160);
  } catch (err) {
    rows = await readTranscriptFallback(space, 160);
  }
  if (space !== currentSpace) return;
  try {
    const hb = await api.spaceHandback(space);
    handbackMessageId = hb && hb.needs_representative ? (hb.highlight_message_id || "") : "";
    handbackReason = hb && hb.needs_representative ? (hb.reason || "") : "";
  } catch (err) {
    handbackMessageId = "";
    handbackReason = "";
  }
  try {
    const ap = await api.spaceApprovals(space);
    const map = {};
    for (const p of (ap && ap.pending) || []) {
      if (p.highlight_message_id) map[p.highlight_message_id] = p;
    }
    approvalsByMsgId = map;
  } catch (err) {
    approvalsByMsgId = {};
  }
  if (space !== currentSpace) return;
  renderMessages(rows);
}

// 완료된 비동기 작업 결과(release_queue pending)를 reflow로 대화에 회수한다.
// reflow는 저위험만 자동 공개하고 고위험은 결재 대기로 둔다(백엔드 D0). 대시보드 폴링이 그 '주체'.
let reflowInFlight = false;
let lastReflowMs = 0;
const REFLOW_MIN_INTERVAL_MS = 4000;   // 고위험 보류분이 남아도 과도한 재시도 방지(throttle)
function maybeReflow(space, st) {
  const pending = Number((st.release_queue || {}).pending_count || 0);
  if (pending <= 0 || reflowInFlight) return;
  const now = Date.now();
  if (now - lastReflowMs < REFLOW_MIN_INTERVAL_MS) return;
  reflowInFlight = true;
  lastReflowMs = now;
  api.reflowSpace(space)
    .then(() => { if (space === currentSpace) refreshRoomChat().catch(() => {}); })
    .catch(() => {})
    .finally(() => { reflowInFlight = false; });
}

async function refreshRoomStatus(options = {}) {
  const box = document.getElementById("room-status");
  if (!currentSpace || !box) return;
  const space = currentSpace;
  try {
    const st = await api.spaceStatus(space);
    if (space !== currentSpace) return;
    latestRoomStatus = st;
    latestWatchReport = st.watch_report || null;   // 감시 소견(상태칩 가시화)
    maybeReflow(space, st);   // 완료된 비동기 작업 결과를 대화로 자동 회수(저위험은 자동 공개)
    const activityRows = await loadActivityRows(space, st.activity || [], options);
    if (space !== currentSpace) return;
    const state = st.상태 || "unknown";
    let text = "대기";
    if (state === "manager_queued") text = "공간관리 대기";
    else if (state === "manager_running") text = "공간관리 판단 중";
    else if (state === "manager_retrying") text = "JSON 재요청 중";
    else if (state === "agent_running") text = `${st.current || "에이전트"} 응답 중`;
    else if (state === "idle" && st.last_action === "wake_failed") text = "턴 전달 실패";
    else if (state === "idle" && st.last_action === "lesson_application_missing") text = "레슨 보고 누락";
    else if (state === "idle" && st.last_action === "manager_failed") text = "공간관리 실패";
    else if (state === "idle" && st.last_action === "stop") text = "턴 멈춤";
    else if (state === "idle") text = "대기";
    if (st.status_stale) text = state === "idle" ? "상태 지연" : `상태 지연 · ${text}`;
    box.textContent = text;
    box.dataset.state = state;
    box.dataset.stale = st.status_stale ? "yes" : "no";
    updateRoomHeadCompact();
    // [모바일/Safari 프리즈 근본수정] 상태 응답이 크다(레빗방 ~700KB: snapshot·space_memory·candidate_queue…).
    // 매 폴(1.5s)마다 무거운 패널 11개를 통째로 재렌더하면 Safari(iOS/WebKit)가 못 따라가 메인스레드가 포화돼
    // 화면이 얼고 터치가 다 먹통이 된다(대표 신고 'Safari에서 방 열면 렌더 안 됨+터치 먹통' — WebKit로 재현·확증:
    // full 상태=8s 폴링 후 HANG / slim 상태=정상). Chromium은 빨라서 버텼다. 대부분의 폴은 이 무거운 데이터가
    // 안 바뀌므로, 그 슬라이스의 sig가 직전과 같으면 패널 재렌더를 통째로 건너뛴다(포화 제거). 가벼운 상태텍스트·
    // transient 말풍선은 아래에서 항상 갱신하므로 '생각 중'·상태 표시는 계속 산다.
    // 무거운 패널은 라이브 데이터(heartbeat 등)라 매 폴 바뀌므로 sig 스킵이 안 통한다.
    // 대신 렌더 '빈도'를 제한한다: 최소 HEAVY_PANEL_MIN_INTERVAL_MS(4s)마다만 재렌더. 그 사이 폴에서는
    // 가벼운 상태텍스트·transient 말풍선만 갱신 → Safari 메인스레드가 무거운 재렌더에 포화되지 않는다.
    const nowMs = Date.now();
    const heavyDue = (nowMs - lastHeavyRenderMs) >= HEAVY_PANEL_MIN_INTERVAL_MS;
    if (heavyDue) {
      lastHeavyRenderMs = nowMs;
      renderActivity(activityRows);
      renderWorkBanner(st);
      renderWatchReport(latestWatchReport);
      renderSnapshot(st);
      renderStatusDetails(st);
      renderChatFlowPanel(st);
      renderObligationPanel(st);
      renderTurnHandoffPanel(st);
      renderTaskPanel(st);
      renderCandidatePanel(st);
      renderPromotionReviewPanel(st);
      applyObserverVisibility();
    }
    renderMessages(lastMessageRows);
  } catch {
    if (space !== currentSpace) return;
    latestRoomStatus = {};
    lastHeavyRenderMs = 0;   // 오류 후 다음 성공 폴에서 패널을 즉시 다시 렌더
    latestWatchReport = null;
    box.textContent = "상태 확인 불가";
    box.dataset.state = "unknown";
    box.dataset.stale = "unknown";
    updateRoomHeadCompact();
    renderActivity([]);
    renderWorkBanner({});
    renderWatchReport(null);
    renderSnapshot({});
    renderStatusDetails({});
    renderChatFlowPanel({});
    renderObligationPanel({});
    renderTurnHandoffPanel({});
    renderTaskPanel({});
    renderCandidatePanel({});
    renderPromotionReviewPanel({});
    applyObserverVisibility();
  }
}

async function readTranscriptFallback(space, limit) {
  const path = `공간/${space}/대화.jsonl`;
  const res = await fetch(`/api/files/raw?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`대화기록을 열 수 없음 (${res.status})`);
  const text = await res.text();
  return text.split(/\r?\n/)
    .filter((line) => line.trim())
    .slice(-limit)
    .map((line) => JSON.parse(line));
}

export async function openSpaceChat(space) {
  const seq = ++openSeq;
  // 다른 방으로 전환하면 감시 패널을 접고 비운다(같은 방 재진입이면 유지).
  if (monitorSession && monitorSession.space !== space) resetMonitorView();
  currentSpace = space;
  // 새로고침/재진입 후에도 8687에 살아있는 감시 세션이 있으면 패널을 자동 복원한다.
  restoreMonitorIfRunning(space).catch(() => {});
  lastAck = null;
  latestActivityRows = [];
  lastMessageRows = [];
  latestRoomMembers = [];
  latestRoomStatus = {};
  lastHeavyRenderMs = 0;   // 새 공간은 무거운 패널을 즉시 한 번 렌더
  lastActivityFetchMs = 0;
  lastActivitySpace = "";
  document.getElementById("room-title").textContent = space;
  document.getElementById("room-input").value = "";
  resetWorkPanel();
  renderRoomSpaceSelect();
  renderRoomParticipants();
  updateRoomHeadCompact();
  switchView("roomView");
  if (window.matchMedia("(max-width: 720px)").matches) {
    // [모바일 터치 먹통 방지] 공간대화 콤보박스로 공간을 바꾸면 그 <select>가 포커스로 남는다.
    // iOS Safari는 활성 요소(특히 폼 컨트롤) 포커스가 만든 문서 스크롤 오프셋만큼 position:fixed/sticky
    // 요소의 터치 히트테스트가 어긋나, 전환 직후 '채팅이 안 그려지고 터치가 다 먹통'으로 보인다(대표 신고).
    // 전송 후 처리(sendCurrentMessage 모바일 분기)와 같은 방식으로, 활성 요소 포커스를 풀어 오프셋을
    // 없앤 뒤 채팅 패널을 화면에 정렬한다.
    if (document.activeElement && typeof document.activeElement.blur === "function") {
      document.activeElement.blur();
    }
    document.getElementById("chat-panel").scrollIntoView({ block: "start" });
  }
  await refreshRoomChat();
  if (seq !== openSeq) return;
  scrollRoomToLatest({ smooth: false });
  requestAnimationFrame(() => scrollRoomToLatest({ smooth: false }));
  await refreshRoomStatus({ forceActivity: true });
  if (seq !== openSeq) return;
  await refreshRoomParticipants();
  if (seq !== openSeq) return;
  await refreshRoomMemberControl();
  if (seq !== openSeq) return;
  updateSendButton();
  if (refreshTimer) clearInterval(refreshTimer);
  if (statusTimer) clearInterval(statusTimer);
  refreshTimer = setInterval(() => { if (!pollPaused) refreshRoomChat().catch(() => {}); }, MESSAGE_REFRESH_MS);
  statusTimer = setInterval(() => { if (!pollPaused) refreshRoomStatus().catch(() => {}); }, STATUS_REFRESH_MS);
}

export function clearSpaceChatIfCurrent(space) {
  if (!space || space !== currentSpace) return;
  currentSpace = "";
  latestActivityRows = [];
  lastMessageRows = [];
  latestRoomMembers = [];
  latestRoomStatus = {};
  if (refreshTimer) clearInterval(refreshTimer);
  if (statusTimer) clearInterval(statusTimer);
  refreshTimer = null;
  statusTimer = null;
  document.getElementById("room-title").textContent = "공간을 선택하세요";
  renderRoomSpaceSelect();
  renderRoomParticipants();
  document.getElementById("room-status").textContent = "상태 없음";
  document.getElementById("room-status").dataset.state = "unknown";
  document.getElementById("room-status").dataset.stale = "unknown";
  resetWorkPanel();
  updateRoomHeadCompact();
  document.getElementById("room-messages").innerHTML = `<div class="room-empty">공간을 선택하세요</div>`;
  updateLatestButton();
  document.getElementById("room-input").value = "";
  renderActivity([]);
  renderSnapshot({});
  renderStatusDetails({});
  renderChatFlowPanel({});
  renderObligationPanel({});
  renderTurnHandoffPanel({});
  renderTaskPanel({});
  renderCandidatePanel({});
  renderPromotionReviewPanel({});
  refreshRoomMemberControl().catch(() => {});
}

async function refreshAfterPost(targetSpace) {
  if (currentSpace !== targetSpace) return;
  try {
    await refreshRoomChat();
    await refreshRoomStatus({ forceActivity: true });
  } catch (err) {
    console.warn("표시 갱신 실패", err);
  }
}

async function processOutboxQueue(refreshAll) {
  if (outboxProcessing) return;
  outboxProcessing = true;
  updateSendButton();
  try {
    while (true) {
      const item = outbox.find((entry) => entry.state === "pending");
      if (!item) break;
      item.state = "sending";
      item.updatedAt = Date.now();
      if (currentSpace === item.space) {
        renderMessages(lastMessageRows);
        renderSnapshot(latestRoomStatus);
        updateSendButton();
      }
      try {
        const result = await api.postSpace(item.space, item.text, "대표", true, item.clientMessageId);
        item.ack = result.ack || { client_message_id: item.clientMessageId };
        item.state = "acked";
        item.updatedAt = Date.now();
        if (currentSpace === item.space) {
          lastAck = item.ack;
        }
        if (currentSpace === item.space) {
          renderMessages(lastMessageRows);
          renderSnapshot(latestRoomStatus);
          updateSendButton();
        }
        await refreshAfterPost(item.space);
        try {
          await refreshAll();
        } catch (err) {
          console.warn("전체 목록 갱신 실패", err);
        }
      } catch (err) {
        item.state = "error";
        item.error = err.message || String(err);
        item.updatedAt = Date.now();
        if (currentSpace === item.space) {
          renderMessages(lastMessageRows);
          renderSnapshot(latestRoomStatus);
          updateSendButton();
        }
      }
    }
  } finally {
    outboxProcessing = false;
    updateSendButton();
  }
}

// ── 감시모드: 관리자에이전트를 '이 방' 컨텍스트로 8687 인터랙티브 세션으로 띄운다 ──
const MONITOR_PORT = 8687;
let monitorSession = null;   // { id, space }

function monitorBase() {
  return `${location.protocol}//${location.hostname}:${MONITOR_PORT}`;
}

function setMonitorCollapsed(collapsed) {
  const panel = document.getElementById("room-monitor");
  if (!panel) return;
  panel.classList.toggle("collapsed", collapsed);
  panel.setAttribute("aria-hidden", collapsed ? "true" : "false");
  const toggle = document.getElementById("room-monitor-toggle");
  if (toggle) toggle.textContent = collapsed ? "▾" : "▴";
}

// 방을 전환하면 감시 패널은 숨기고 비운다(세션 자체는 8687에 살아 있어 터미널 독에서 종료 가능).
function resetMonitorView() {
  const frame = document.getElementById("room-monitor-frame");
  if (frame) frame.src = "about:blank";
  const meta = document.getElementById("room-monitor-meta");
  if (meta) meta.textContent = "";
  const open = document.getElementById("room-monitor-open");
  if (open) { open.hidden = true; open.removeAttribute("href"); }
  setMonitorCollapsed(true);
  const panel = document.getElementById("room-monitor");
  if (panel) panel.hidden = true;   // 세션 없을 땐 패널(빈 바)을 아예 숨긴다
  monitorSession = null;
}

// 8687에 살아있는 '감시:{space}' 세션을 찾는다(가장 최근 것). 새로고침/재진입 후 재연결의 근거.
async function fetchAliveWatchSession(space) {
  let sessions = [];
  try {
    const data = await fetch(`${monitorBase()}/api/sessions`).then((r) => r.json());
    sessions = data.sessions || [];
  } catch (_) { return null; }   // 8687 미응답 — 조용히 패스
  const title = `감시:${space}`;
  const alive = sessions.filter((s) => s && s.alive && s.title === title)
    .sort((a, b) => (b.created || 0) - (a.created || 0));
  return alive[0] || null;
}

function metaFromShell(shell) {
  const m = /exec\s+(\w+)/.exec(shell || "");
  const eng = m ? (m[1] === "agy" ? "gemini" : m[1]) : "감시";
  return `${eng} · 복원됨`;
}

// 패널을 특정 세션에 붙인다(신규 실행·복원 공용).
function attachMonitorSession(space, session, metaText, { scroll = false } = {}) {
  monitorSession = { id: session.id, space };
  const url = `${monitorBase()}/static/attach.html?sid=${encodeURIComponent(session.id)}`;
  const panel = document.getElementById("room-monitor");
  if (panel) panel.hidden = false;
  const frame = document.getElementById("room-monitor-frame");
  if (frame) frame.src = url;
  const meta = document.getElementById("room-monitor-meta");
  if (meta) meta.textContent = metaText || "";
  const open = document.getElementById("room-monitor-open");
  if (open) { open.href = url; open.hidden = false; }
  // 모바일: 감시 터미널 iframe(≈240px)이 방을 덮어 채팅 메시지·입력창이 화면 밖으로 밀리고
  // iframe이 터치를 삼키던 문제(대표 신고: 방 진입 후 터치 안 먹음) → 폰에선 접힌 채(헤드 바만)
  // 붙인다. 채팅이 우선이고, 감시 터미널이 필요하면 헤드의 ▾ 토글로 펼친다. 데스크톱은 그대로 펼침.
  const monitorMobile = window.matchMedia("(max-width: 720px)").matches;
  setMonitorCollapsed(monitorMobile);
  if (scroll && panel && !monitorMobile) panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  renderWatchReport(latestWatchReport);
  renderSnapshot(latestRoomStatus);
}

// 방을 (재)열 때, 8687에 살아있는 감시 세션이 있으면 패널을 자동 복원한다(새로고침 후에도 유지).
async function restoreMonitorIfRunning(space) {
  const s = await fetchAliveWatchSession(space);
  if (!s || space !== currentSpace) return;
  attachMonitorSession(space, s, metaFromShell(s.shell));
}

async function launchMonitor() {
  if (!currentSpace) return;
  const space = currentSpace;
  // 이미 이 방을 감시 중인 세션이 있으면 중복 spawn 대신 그 세션에 다시 연결한다(claude 중복 실행 방지).
  const existing = await fetchAliveWatchSession(space);
  if (existing) {
    if (space !== currentSpace) return;
    attachMonitorSession(space, existing, metaFromShell(existing.shell), { scroll: true });
    return;
  }
  const data = await openRuntimeModal("감시 엔진/모델 (관리자에이전트)", "claude", "claude-opus-4-8");
  if (!data) return;
  let res;
  try {
    res = await api.watchSpace(space, data.engine, data.model);
  } catch (e) {
    alert("감시 실행 실패: " + e.message);
    return;
  }
  if (space !== currentSpace) return;   // 실행 중 방을 바꿨으면 띄우지 않는다
  attachMonitorSession(space, { id: res.session_id }, `${res.engine} · ${res.model}`, { scroll: true });
}

async function closeMonitor() {
  const session = monitorSession;
  resetMonitorView();
  renderWatchReport(latestWatchReport);   // 라이브 칩 즉시 제거(소견 칩은 유지)
  renderSnapshot(latestRoomStatus);
  if (session && session.id) {
    try {
      await fetch(`${monitorBase()}/api/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
    } catch (_) { /* 세션이 이미 없거나 8687 미응답 — 무시 */ }
  }
}

function wireMonitor() {
  const btn = document.getElementById("room-monitor-btn");
  if (btn) btn.onclick = () => launchMonitor().catch((e) => alert("감시 실행 실패: " + e.message));
  const toggle = document.getElementById("room-monitor-toggle");
  if (toggle) toggle.onclick = () => {
    const panel = document.getElementById("room-monitor");
    setMonitorCollapsed(!panel.classList.contains("collapsed"));
  };
  const close = document.getElementById("room-monitor-close");
  if (close) close.onclick = () => closeMonitor();
}

export function wireRoomChat(refreshAll) {
  document.querySelectorAll(".vtab").forEach((btn) => {
    btn.onclick = () => switchView(btn.dataset.view);
  });
  setupImageLightbox();
  setupAttachUpload();
  wireObserverControls();
  wireRoomHeadCollapse();
  wireMonitor();
  wireWorkConsole();
  wireLatestButton();
  const roomSpaceSelect = document.getElementById("room-space-select");
  if (roomSpaceSelect) {
    roomSpaceSelect.onchange = async () => {
      const space = roomSpaceSelect.value;
      if (!space || space === currentSpace) return;
      try {
        await openSpaceChat(space);
      } catch (err) {
        alert("실패: " + err.message);
        refreshRoomSpaceSelect().catch(() => {});
      }
    };
  }
  document.getElementById("room-refresh").onclick = () => Promise.all([
    refreshRoomChat(),
    refreshRoomStatus({ forceActivity: true }),
    refreshRoomParticipants(),
    refreshRoomMemberControl(),
  ]).catch((e) => alert("실패: " + e.message));
  document.getElementById("room-activity").onclick = (e) => {
    const btn = e.target.closest("[data-activity-filter]");
    if (!btn) return;
    activityFilter = btn.dataset.activityFilter || "all";
    renderActivity(latestActivityRows);
  };
  const sendCurrentMessage = async (e) => {
    e.preventDefault();
    if (!currentSpace) return;
    const targetSpace = currentSpace;
    const input = document.getElementById("room-input");
    const text = input.value.trim();
    if (!text) return;
    const clientMessageId = newClientMessageId(targetSpace);
    outbox.push({
      id: clientMessageId,
      space: targetSpace,
      text,
      clientMessageId,
      state: "pending",
      ack: null,
      error: "",
      createdAt: new Date().toISOString(),
      updatedAt: Date.now(),
    });
    input.value = "";
    renderMessages(lastMessageRows);
    renderSnapshot(latestRoomStatus);
    updateSendButton();
    processOutboxQueue(refreshAll).catch((err) => console.error("outbox 처리 실패", err));
    // 모바일: 전송 후 입력에 재포커스하면 키보드가 계속 떠 있어 화면을 가린다. 그래서 blur로 키보드를
    // 닫는다(닫힘만으로 터치 먹통이 방지된다 — 모바일 body는 position:static; overflow:auto라 유령
    // 오프셋이 없다). 예전엔 여기서 window.scrollTo(0,0)까지 했는데, 정적 스크롤 body에선 그게 페이지를
    // 최상단으로 순간이동시켜 '보내면 맨 위로 튄다'가 되므로 제거한다. 데스크톱은 종전대로 포커스 유지.
    if (window.matchMedia("(max-width: 720px)").matches) {
      input.blur();
    } else {
      input.focus();
    }
  };
  const roomForm = document.getElementById("room-form");
  const roomInput = document.getElementById("room-input");
  roomForm.onsubmit = sendCurrentMessage;
  roomInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    if (typeof roomForm.requestSubmit === "function") roomForm.requestSubmit();
    else roomForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  document.getElementById("room-member-form").onsubmit = async (e) => {
    e.preventDefault();
    if (!currentSpace) return;
    const select = document.getElementById("room-member-select");
    const person = select.value;
    if (!person) return;
    try {
      await api.join(person, currentSpace);
      await refreshAll();
      await refreshRoomStatus({ forceActivity: true });
      await refreshRoomParticipants();
      await refreshRoomMemberControl();
    } catch (err) {
      alert("실패: " + err.message);
    }
  };
  updateSendButton();
  refreshRoomSpaceSelect().catch(() => {});
  refreshRoomMemberControl().catch(() => {});
}
