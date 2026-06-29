// 8687 독립 터미널 앱: 세션 목록·생성·전환·종료를 스스로 관리한다.
// 대시보드(8686)는 이 페이지를 iframe 하나로 끼우기만 한다 → 둘은 완전히 독립.
const term = new Terminal({
  fontSize: 13,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  cursorBlink: true,
  theme: { background: "#0e1117", foreground: "#e6edf3" },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("term"));
fit.fit();

let ws = null, curId = null, sessions = [];

function sendData(str) { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data: str })); }
function sendResize() {
  fit.fit();
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
}

// 터치 기기에선 xterm 기본 입력을 끄고(아래 mobile.js 가 IME 처리), 데스크톱만 그대로 쓴다.
const isTouch = window.matchMedia("(pointer: coarse)").matches || ("ontouchstart" in window);
if (!isTouch) term.onData((d) => sendData(d));
window.addEventListener("resize", sendResize);

// mobile.js 가 쓸 수 있게 노출
window.__termApi = { term, fit, sendData, sendResize, isTouch };

async function loadSessions() {
  const j = await (await fetch("/api/sessions")).json();
  sessions = j.sessions || [];
  renderTabs();
  return sessions;
}

function renderTabs() {
  document.getElementById("sess-tabs").innerHTML = sessions.map((s) =>
    `<div class="stab ${s.id === curId ? "active" : ""}" data-id="${s.id}">
       <span>${s.title}</span><span class="x" data-kill="${s.id}" title="종료">×</span>
     </div>`).join("");
}

function attach(sid) {
  if (ws) { try { ws.close(); } catch (_) {} ws = null; }
  curId = sid;
  term.reset();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/${sid}`);
  ws.onmessage = (e) => term.write(e.data);
  ws.onopen = () => sendResize();
  ws.onclose = () => {};
  localStorage.setItem("term_last", sid);
  renderTabs();
  if (!isTouch) term.focus();
}

async function newSession() {
  const s = await (await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  })).json();
  await loadSessions();
  attach(s.id);
}

async function killSession(sid) {
  await fetch("/api/sessions/" + sid, { method: "DELETE" });
  await loadSessions();
  if (curId === sid) {
    if (sessions.length) attach(sessions[sessions.length - 1].id);
    else { curId = null; term.reset(); }
  }
}

document.getElementById("new-sess").onclick = newSession;
document.getElementById("sess-tabs").onclick = (e) => {
  const kill = e.target.closest("[data-kill]");
  if (kill) { killSession(kill.dataset.kill); return; }
  const tab = e.target.closest(".stab");
  if (tab) attach(tab.dataset.id);
};

(async function init() {
  await loadSessions();
  if (sessions.length) {
    const last = localStorage.getItem("term_last");
    attach(sessions.some((s) => s.id === last) ? last : sessions[0].id);
  } else {
    await newSession();
  }
})();
