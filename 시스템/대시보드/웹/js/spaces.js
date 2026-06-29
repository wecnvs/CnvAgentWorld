// 공간 패널 렌더링과 동작(생성·입장).
import { api } from "./api.js?v=20260629-30";
import { clearSpaceChatIfCurrent, openSpaceChat } from "./room-chat.js?v=20260629-30";
import { openPersonPickerModal, openRuntimeModal, openTextModal, openWorkSettingsModal } from "./people.js?v=20260629-30";

let engineCatalog = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}

function modelOptions(engine) {
  const models = engineCatalog?.models?.[engine] || [];
  return models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
}

async function ensureEngineCatalog() {
  if (!engineCatalog) engineCatalog = await api.engineModels();
  return engineCatalog;
}

function syncSpaceModels() {
  const engineSel = document.getElementById("space-engine");
  const modelSel = document.getElementById("space-model");
  modelSel.innerHTML = modelOptions(engineSel.value);
}

function newClientMessageId(space) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `ui-card:${space}:${random}`;
}

function workSettingsSummary(settings = {}) {
  const timeout = Number(settings.runner_timeout_sec || 300);
  const interval = Number(settings.heartbeat_interval_sec || 10);
  const stale = Number(settings.heartbeat_stale_ms || 60000);
  const due = Number(settings.progress_report_due_ms || stale);
  return `작업: timeout ${timeout}s · hb ${interval}s · stale ${stale}ms · due ${due}ms`;
}

function seatWorkSettingsPanel(space, members = []) {
  if (!members.length) return "";
  const rows = members
    .filter((m) => m?.토큰)
    .map((m) => {
      const settings = m.작업설정 || {};
      const configuredKeys = Array.isArray(m.좌석작업설정?.configured_keys) ? m.좌석작업설정.configured_keys : [];
      const label = `${m.이름 || m.토큰} 좌석 작업설정`;
      return {
        configured: configuredKeys.length > 0,
        html: `
        <button class="seat-work-settings-btn" type="button"
          title="${esc(workSettingsSummary(settings))}"
          data-space="${esc(space)}"
          data-person="${esc(m.토큰)}"
          data-configured-keys="${esc(configuredKeys.join(","))}"
          data-runner-timeout-sec="${esc(settings.runner_timeout_sec || 300)}"
          data-heartbeat-interval-sec="${esc(settings.heartbeat_interval_sec || 10)}"
          data-heartbeat-stale-ms="${esc(settings.heartbeat_stale_ms || 60000)}"
          data-progress-report-due-ms="${esc(settings.progress_report_due_ms || 60000)}">${esc(label)}</button>
      `,
      };
    });
  if (!rows.length) return "";
  const configuredCount = rows.filter((row) => row.configured).length;
  const countLabel = `${rows.length}명${configuredCount ? ` · 직접 ${configuredCount}` : ""}`;
  return `
    <details class="seat-settings-panel">
      <summary>
        <span>좌석 작업설정</span>
        <span class="seat-settings-count">${esc(countLabel)}</span>
      </summary>
      <div class="seat-actions" aria-label="좌석별 작업 실행설정">${rows.map((row) => row.html).join("")}</div>
    </details>
  `;
}

export async function renderSpaces() {
  const ul = document.getElementById("spaces-list");
  const spaces = await api.spaces();
  if (!spaces.length) {
    ul.innerHTML = `<li class="empty">아직 공간이 없습니다</li>`;
    return;
  }
  ul.innerHTML = spaces.map((s) => `
    <li>
      <div class="row">
        <span class="name">${esc(s.이름)}</span>
        <span class="code">${esc(s.코드)}</span>
      </div>
      <div class="sub runtime">관리자: ${esc(s.관리자?.engine || "")}${s.관리자?.model ? " · " + esc(s.관리자.model) : ""}</div>
      <div class="sub work-settings">${esc(workSettingsSummary(s.작업설정 || {}))}</div>
      <div class="sub">멤버: ${s.멤버.length ? s.멤버.map((m) => esc(m.이름)).join(", ") : "없음"}</div>
      ${seatWorkSettingsPanel(s.토큰, s.멤버 || [])}
      <div class="edit-actions" aria-label="공간 수정">
        <button class="edit-btn space-runtime-btn" data-space="${esc(s.토큰)}" data-engine="${esc(s.관리자?.engine || "")}" data-model="${esc(s.관리자?.model || "")}">관리자 엔진/모델 수정</button>
        <button class="edit-btn space-work-settings-btn" data-space="${esc(s.토큰)}"
          data-runner-timeout-sec="${esc(s.작업설정?.runner_timeout_sec || 300)}"
          data-heartbeat-interval-sec="${esc(s.작업설정?.heartbeat_interval_sec || 10)}"
          data-heartbeat-stale-ms="${esc(s.작업설정?.heartbeat_stale_ms || 60000)}"
          data-progress-report-due-ms="${esc(s.작업설정?.progress_report_due_ms || 60000)}">작업 실행설정 수정</button>
        <button class="edit-btn space-guide-btn" data-space="${esc(s.토큰)}">공간지침 수정</button>
        <button class="edit-btn danger space-delete-btn" data-space="${esc(s.토큰)}" data-name="${esc(s.이름)}">삭제</button>
      </div>
      <div class="row actions">
        <button class="ghost open-room-btn" data-space="${esc(s.토큰)}">열기</button>
        <button class="ghost post-btn" data-space="${esc(s.토큰)}">말하기</button>
        <button class="ghost tick-btn" data-space="${esc(s.토큰)}">진행</button>
        <button class="ghost join-btn" data-space="${s.토큰}">에이전트 입장</button>
      </div>
    </li>`).join("");
}

export function wireSpaces(refreshAll) {
  const modal = document.getElementById("space-modal");
  const form = document.getElementById("space-form");
  const nameInput = document.getElementById("space-name");
  const engineSel = document.getElementById("space-engine");
  const cancel = document.getElementById("space-cancel");

  document.getElementById("add-space").onclick = async () => {
    await ensureEngineCatalog();
    engineSel.innerHTML = engineCatalog.engines.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
    engineSel.value = engineCatalog.engines[0];
    syncSpaceModels();
    nameInput.value = "";
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    nameInput.focus();
  };
  engineSel.onchange = syncSpaceModels;
  cancel.onclick = () => {
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api.createSpace({
        name: nameInput.value,
        engine: engineSel.value,
        model: document.getElementById("space-model").value,
      });
      cancel.onclick();
      await refreshAll();
    } catch (err) { alert("실패: " + err.message); }
  };

  document.getElementById("spaces-list").onclick = async (e) => {
    const joinBtn = e.target.closest(".join-btn");
    const openBtn = e.target.closest(".open-room-btn");
    const postBtn = e.target.closest(".post-btn");
    const tickBtn = e.target.closest(".tick-btn");
    const runtimeBtn = e.target.closest(".space-runtime-btn");
    const workSettingsBtn = e.target.closest(".space-work-settings-btn");
    const seatWorkSettingsBtn = e.target.closest(".seat-work-settings-btn");
    const guideBtn = e.target.closest(".space-guide-btn");
    const deleteBtn = e.target.closest(".space-delete-btn");
    const btn = joinBtn || openBtn || postBtn || tickBtn || runtimeBtn || workSettingsBtn || seatWorkSettingsBtn || guideBtn || deleteBtn;
    if (!btn) return;
    const space = btn.dataset.space;
    if (runtimeBtn) {
      const data = await openRuntimeModal("공간관리 엔진", runtimeBtn.dataset.engine, runtimeBtn.dataset.model);
      if (!data) return;
      try { await api.updateSpaceRuntime(space, data); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (workSettingsBtn) {
      const data = await openWorkSettingsModal("공간 작업 실행설정", workSettingsBtn.dataset);
      if (!data) return;
      try { await api.updateSpaceWorkSettings(space, data); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (seatWorkSettingsBtn) {
      const data = await openWorkSettingsModal("좌석 작업 실행설정", seatWorkSettingsBtn.dataset, { partial: true });
      if (!data) return;
      try { await api.updateSeatWorkSettings(space, seatWorkSettingsBtn.dataset.person, data); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (guideBtn) {
      try {
        const guide = await api.spaceGuide(space);
        const text = await openTextModal(`${space} 공간지침.md`, guide.text);
        if (text === null) return;
        await api.saveSpaceGuide(space, text);
        await refreshAll();
      } catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (deleteBtn) {
      const label = `${deleteBtn.dataset.name || "공간"} (${space})`;
      if (!confirm(`${label}을 삭제할까요?\n공간 대화와 공유파일, 각 에이전트의 이 공간 좌석도 제거됩니다.`)) return;
      try { await api.deleteSpace(space); clearSpaceChatIfCurrent(space); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (openBtn) {
      try { await openSpaceChat(space); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (postBtn) {
      const text = prompt(`'${space}'에 남길 말:`);
      if (!text) return;
      try {
        await openSpaceChat(space);
        await api.postSpace(space, text, "대표", true, newClientMessageId(space));
        await openSpaceChat(space);
        await refreshAll();
      }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (tickBtn) {
      try {
        await openSpaceChat(space);
        await api.tickSpace(space);
        await openSpaceChat(space);
        await refreshAll();
      }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    const people = await api.people();
    if (!people.length) { alert("먼저 에이전트를 만드세요."); return; }
    const person = await openPersonPickerModal(`${space} 에이전트 입장`, people, {
      excludeSpace: space,
      emptyText: "이 공간에 추가할 수 있는 에이전트가 없습니다.",
    });
    if (!person) return;
    try { await api.join(person, space); await refreshAll(); }
    catch (e) { alert("실패: " + e.message); }
  };
}
