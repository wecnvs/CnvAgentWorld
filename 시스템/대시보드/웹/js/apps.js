// 앱 탭: 앱/ 폴더의 애플리케이션을 종류(kind)에 맞춰 카드로 그린다.
// - web-app   : 실행 → 서버가 포트를 열고, 내가 접속한 호스트(location.hostname)로 브라우저 새 탭(Tailscale 대응).
// - standalone/external : 실행 → 서버 호스트에서 실행. 실행 중 상태 + 중지 버튼.
// - revit-addin/install-only : 설치파일 다운로드만.
// 다운로드는 기존 파일 API(/api/files/raw?download=1)를 그대로 쓴다.
import { api } from "./api.js?v=20260702-08";

// 원격제어는 '별도 창'으로 연다(대표가 채팅하면서 동시에 원격제어 가능하게). remote.html이 그리드를 그린다.
// 같은 창 이름을 쓰므로 같은 세션 버튼을 또 눌러도 새 창이 아니라 기존 창이 떠오른다.
function openRemoteWindow(names) {
  names = (names || []).filter(Boolean);
  if (!names.length) return;
  const q = names.map(encodeURIComponent).join(",");
  const winName = names.length === 1 ? "cnv-remote-" + names[0] : "cnv-remote-grid";
  window.open(`/static/remote.html?targets=${q}`, winName, "noopener=0,width=1280,height=860");
}

const KIND_LABEL = {
  "web-app": "웹앱",
  "standalone": "독립실행",
  "external": "외부프로그램",
  "revit-addin": "레빗 애드인",
  "install-only": "설치형",
  "기타": "앱",
};
const KIND_ICON = {
  "web-app": "🌐", "standalone": "🖥️", "external": "🧩",
  "revit-addin": "🏗️", "install-only": "📦", "기타": "📦",
};
const STATUS_LABEL = { ready: "사용가능", wip: "작업중", deprecated: "지원종료" };

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const dlUrl = (path) => `/api/files/raw?path=${encodeURIComponent(path)}&download=1`;
// 내가 접속한 호스트 기준(Tailscale 주소 등)으로 web-app 포트 주소를 만든다.
const webUrl = (port) => `http://${location.hostname}:${port}`;

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

// 종류·실행상태에 맞는 액션 버튼(HTML). 실행/중지/열기는 data-action, 다운로드는 <a>.
function actionsFor(app) {
  const out = [];
  const dir = esc(app.dir);
  if (app.web) {
    if (app.running) {
      const port = app.running_port || app.port;
      out.push(`<button class="app-act open" type="button" data-action="open" data-port="${esc(port)}">↗ 열기</button>`);
      out.push(`<button class="app-act stop" type="button" data-action="stop" data-dir="${dir}">■ 중지</button>`);
    } else {
      out.push(`<button class="app-act run" type="button" data-action="run" data-dir="${dir}" data-web="1" data-port="${esc(app.port || "")}" title="${esc(app.run)}">▶ 실행</button>`);
    }
  } else if (app.runnable) {
    const insts = Array.isArray(app.instances) ? app.instances : [];
    if (insts.length) {
      // 자동감지된 실행 중 인스턴스를 PID별로 종료(대시보드 밖에서 켠 것 포함) — 대표가 특정 인스턴스
      // (예: 중복 Revit)를 골라 끌 수 있게. cu-helper→/stop, local→taskkill.
      for (const it of insts) {
        const t = it.title ? " — " + it.title : "";
        out.push(`<button class="app-act stop stop-inst" type="button" data-action="stop-instance" data-dir="${dir}" data-pid="${esc(it.pid)}" data-title="${esc(it.title || "")}" title="이 인스턴스 종료${esc(t)}">■ PID ${esc(it.pid)} 종료</button>`);
      }
    } else if (app.running) {
      out.push(`<button class="app-act stop" type="button" data-action="stop" data-dir="${dir}">■ 중지</button>`);
    } else {
      out.push(`<button class="app-act run" type="button" data-action="run" data-dir="${dir}" data-web="0" data-label="${esc(app.name)}" title="${esc(app.run)}">▶ 실행</button>`);
    }
  }
  // 원격 세션(cu-helper)이면: 그 세션 화면을 브라우저에서 보고 클릭/타이핑하는 패널을 연다.
  if (app.target_channel === "cu-helper" && !app.download_only) {
    out.push(`<button class="app-act remote" type="button" data-action="remote" data-target="${esc(app.target)}" data-label="${esc(app.name)}" title="이 세션 화면을 브라우저에서 라이브로 보고 클릭/타이핑">🖥 원격제어</button>`);
  }
  if (app.install_path) {
    out.push(`<a class="app-act install" href="${dlUrl(app.install_path)}" title="${esc(app.install_path.split("/").pop())}">⤓ 설치파일</a>`);
  }
  if (app.download_path && app.download_path !== app.install_path) {
    const name = app.download_path.split("/").pop();
    out.push(`<a class="app-act download" href="${dlUrl(app.download_path)}" title="${esc(name)}">⤓ ${esc(name)}</a>`);
  }
  return out.join("");
}

function fileList(app) {
  const primary = new Set([app.install_path, app.download_path].filter(Boolean));
  const rest = (app.files || []).filter((f) => !primary.has(f.경로));
  if (!rest.length) return "";
  const rows = rest.map((f) => `
    <a class="app-file ${f.설치파일 ? "is-install" : ""}" href="${dlUrl(f.경로)}" title="다운로드">
      <span class="afn">${esc(f.이름)}</span><span class="afs">${fmtSize(f.크기)}</span>
    </a>`).join("");
  return `<details class="app-files"><summary>폴더 파일 ${rest.length}개</summary><div class="app-files-body">${rows}</div></details>`;
}

function appCard(app) {
  const kind = app.kind || "기타";
  const insts = Array.isArray(app.instances) ? app.instances : [];
  // 실행 중 인스턴스(외부 기동 포함) PID를 노출 → 에이전트에게 "이 PID 대상으로" 지시 가능.
  const instInfo = insts.length
    ? ` <span class="app-instances" title="${esc(insts.map((i) => "PID " + i.pid + (i.title ? " — " + i.title : "")).join(" | "))}">${insts.length > 1 ? insts.length + "개 · " : ""}PID ${esc(insts.map((i) => i.pid).join(", "))}</span>`
    : "";
  const runningBadge = app.running
    ? `<span class="app-running">● 실행 중${app.web && (app.running_port || app.port) ? ` :${esc(app.running_port || app.port)}` : ""}${instInfo}</span>`
    : "";
  const targetBadge = app.download_only
    ? ""
    : `<span class="app-target ch-${esc(app.target_channel)}${app.target_unconfigured ? " unconfigured" : ""}" title="실행 위치: ${esc(app.target_text)}${app.target_unconfigured ? " — 레지스트리 미구성" : ""}${app.target_limited ? " (원격 — 상태추적 제한)" : ""}">${esc(app.target_icon)} ${esc(app.target_text)}${app.target_unconfigured ? " ⚠" : ""}</span>`;
  const badges = [
    `<span class="app-kind k-${esc(kind)}">${KIND_ICON[kind] || "📦"} ${esc(KIND_LABEL[kind] || kind)}</span>`,
    targetBadge,
    app.grade ? `<span class="app-grade g-${esc(app.grade)}">${esc(app.grade)}</span>` : "",
    app.platform ? `<span class="app-plat">${esc(app.platform)}</span>` : "",
    app.version ? `<span class="app-ver">v${esc(app.version)}</span>` : "",
    app.status ? `<span class="app-status s-${esc(app.status)}">${esc(STATUS_LABEL[app.status] || app.status)}</span>` : "",
    runningBadge,
  ].filter(Boolean).join("");
  const actions = actionsFor(app);
  const webHint = app.web && !app.running
    ? `<div class="app-webhint">실행하면 ${app.target_local ? `내가 접속한 주소(${esc(location.hostname)})` : `${esc(app.target_text)}`}로 브라우저가 열립니다</div>` : "";
  return `
    <article class="app-card ${app.running ? "is-running" : ""}" data-kind="${esc(kind)}">
      <div class="app-card-head">
        <span class="app-name">${esc(app.name)}</span>
        <span class="app-badges">${badges}</span>
      </div>
      ${app.description ? `<div class="app-desc">${esc(app.description)}</div>` : ""}
      ${actions ? `<div class="app-actions">${actions}</div>` : `<div class="app-actions app-noact">실행/다운로드 항목 없음 — 매니페스트에 url·run·install 지정</div>`}
      ${webHint}
      ${fileList(app)}
      <div class="app-path">📁 ${esc(app.dir)}</div>
    </article>`;
}

// 원격 세션(원격 컴퓨터·VM) — 앱과 별개로 cu-helper 타깃을 직접 화면제어하는 카드.
//  서로 독립된 세션이라 동시에 띄워 각각 조작할 수 있다(도윤 호스트 / VM-A / VM-B …).
function sessionCard(t) {
  const label = esc(t.label || t.name);
  const isLocal = t.channel === "local";
  const badge = isLocal
    ? `<span class="app-target ch-local">🖥️ 서버 컴퓨터</span>`
    : `<span class="app-target ch-cu-helper">🪟 원격 세션</span>`;
  return `<article class="app-card sess-card">
      <div class="app-card-head">
        <span class="app-name">${label}</span>
        <span class="app-badges">${badge}</span>
      </div>
      <div class="app-actions">
        <button class="app-act remote" type="button" data-action="remote" data-target="${esc(t.name)}" data-label="${label}" title="이 ${isLocal ? "서버 컴퓨터" : "세션"} 화면을 브라우저에서 보고 클릭/타이핑">🖥 원격제어</button>
      </div>
    </article>`;
}
function sessionSection(targets) {
  // 서버 컴퓨터(local) + 원격 윈도우/VM(cu-helper)을 원격제어 카드로. 모두 서로 독립·동시 제어 가능.
  // desktop-frvh9d8은 도윤컴-호스트와 동일 호스트라 중복 제외(레빗 앱 카드가 별도로 그걸 가리킴).
  const sess = (targets || []).filter(
    (t) => (t.channel === "cu-helper" || t.channel === "local") && t.name !== "desktop-frvh9d8");
  if (!sess.length) return "";
  // 서버 컴퓨터(local)를 맨 앞에 — 중앙 컨트롤센터의 '이 서버'가 먼저 보이게.
  sess.sort((a, b) => (a.channel === "local" ? -1 : 0) - (b.channel === "local" ? -1 : 0));
  return `<div class="apps-subhead"><span>원격 세션 — 서버 컴퓨터·원격·VM (서로 독립·동시 제어)</span>
      <button class="rp-btn sess-openall" type="button" data-action="remote-all" title="모든 세션을 한 화면 그리드로">▦ 전체 그리드로 열기</button></div>
    <div class="sess-grid">${sess.map(sessionCard).join("")}</div>`;
}

export async function renderApps() {
  const box = document.getElementById("apps-list");
  if (!box) return;
  let data;
  try {
    data = await api.listApps();
  } catch (e) {
    box.innerHTML = `<div class="empty">앱 목록 실패: ${esc(e.message)}</div>`;
    return;
  }
  const apps = data.apps || [];
  const appsHtml = apps.length
    ? apps.map(appCard).join("")
    : `<div class="empty apps-empty">등록된 앱이 없습니다.<br>
      <code>앱/&lt;등급&gt;/&lt;이름&gt;/앱.md</code> 로 추가하세요 — 스킬 <code>앱등록</code> 참고.</div>`;
  box.innerHTML = appsHtml + sessionSection(data.targets);
}

async function doRun(btn) {
  const dir = btn.dataset.dir;
  const isWeb = btn.dataset.web === "1";
  // 팝업 차단 회피: 클릭 제스처 안에서 빈 탭을 먼저 연 뒤, 실행 성공하면 그 탭을 주소로 이동.
  let tab = null;
  if (isWeb) { try { tab = window.open("about:blank", "_blank"); } catch (_) {} }
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "실행 중…";
  try {
    const r = await api.runApp(dir);
    // 원격 세션 앱이면 실행과 동시에 원격제어 창을 띄운다(대표 의도: 실행=즉시 원격제어).
    if (!isWeb && r.channel === "cu-helper" && r.target) {
      try { openRemoteWindow([r.target]); } catch (_) {}
    }
    if (isWeb && r.port) {
      if (r.ready === false) {
        if (tab) tab.close();
        alert(`포트 ${r.port}가 열리지 않았습니다 — 매니페스트 run 명령과 0.0.0.0 바인드를 확인하세요.`);
      } else {
        // 로컬이면 내가 접속한 호스트(location.hostname=Tailscale 등), 원격이면 그 타깃 host로 연다.
        const host = r.open_host || location.hostname;
        const url = `http://${host}:${r.port}`;
        if (tab) tab.location.href = url; else window.open(url, "_blank", "noopener");
      }
    }
    await renderApps();
  } catch (e) {
    if (tab) tab.close();
    btn.textContent = prev;
    btn.disabled = false;
    alert("실행 실패: " + e.message);
  }
}

async function doStop(btn) {
  const dir = btn.dataset.dir;
  btn.disabled = true;
  btn.textContent = "중지 중…";
  try {
    await api.stopApp(dir);
    await renderApps();
  } catch (e) {
    btn.disabled = false;
    alert("중지 실패: " + e.message);
  }
}

async function doStopInstance(btn) {
  const dir = btn.dataset.dir;
  const pid = parseInt(btn.dataset.pid, 10);
  const title = btn.dataset.title || "";
  // 실제 프로세스 종료(되돌리기 불가)라 확인. 문서 열린 세션이면 저장 안 된 작업 손실 경고.
  const warn = title ? `\n\n"${title}"\n(문서가 열려 있으면 저장 안 된 작업이 손실될 수 있어요)` : "";
  if (!confirm(`PID ${pid} 인스턴스를 종료할까요?${warn}`)) return;
  btn.disabled = true;
  btn.textContent = "종료 중…";
  try {
    const r = await api.stopAppInstance(dir, pid);
    if (r && r.stopped === false && r.reason) {
      // 이미 종료됨 등 — 목록만 갱신
    }
    await renderApps();
  } catch (e) {
    btn.disabled = false;
    alert("인스턴스 종료 실패(PID " + pid + "): " + e.message);
  }
}

export function wireApps() {
  const refresh = document.getElementById("apps-refresh");
  if (refresh) refresh.onclick = () => renderApps();
  const list = document.getElementById("apps-list");
  if (list) {
    list.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-action]");
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === "run") { e.preventDefault(); doRun(btn); }
      else if (action === "stop") { e.preventDefault(); doStop(btn); }
      else if (action === "stop-instance") { e.preventDefault(); doStopInstance(btn); }
      else if (action === "open") { e.preventDefault(); window.open(webUrl(btn.dataset.port), "_blank", "noopener"); }
      else if (action === "remote") { e.preventDefault(); openRemoteWindow([btn.dataset.target]); }
      else if (action === "remote-all") { e.preventDefault(); openRemoteWindow([...document.querySelectorAll('#apps-list .sess-card [data-action="remote"]')].map((b) => b.dataset.target)); }
    });
  }
  document.querySelectorAll('.vtab[data-view="appsView"]').forEach((tab) => {
    tab.addEventListener("click", () => renderApps());
  });
}
