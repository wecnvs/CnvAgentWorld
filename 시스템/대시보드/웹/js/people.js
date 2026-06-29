// 에이전트 패널 렌더링과 동작.
import { api } from "./api.js?v=20260629-29";

let engineCatalog = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}

async function loadEngineCatalog() {
  if (!engineCatalog) engineCatalog = await api.engineModels();
  return engineCatalog;
}

function modelOptions(engine, selected = "") {
  const models = engineCatalog?.models?.[engine] || [];
  const opts = models.map((m) => `<option value="${m}">${m}</option>`);
  if (selected && !models.includes(selected)) {
    opts.push(`<option value="${selected}">${selected} (저장됨)</option>`);
  }
  return opts.join("");
}

function setModelSelect(engine, selected = "") {
  const sel = document.getElementById("person-model");
  sel.innerHTML = modelOptions(engine, selected);
  sel.value = selected || engineCatalog?.default?.[engine] || "";
}

function workSettingsSummary(settings = {}) {
  const timeout = Number(settings.runner_timeout_sec || 300);
  const interval = Number(settings.heartbeat_interval_sec || 10);
  const stale = Number(settings.heartbeat_stale_ms || 60000);
  const due = Number(settings.progress_report_due_ms || stale);
  return `작업: timeout ${timeout}s · hb ${interval}s · stale ${stale}ms · due ${due}ms`;
}

async function openPersonModal() {
  await loadEngineCatalog();
  const dlg = document.getElementById("person-modal");
  const form = document.getElementById("person-form");
  const engineSel = document.getElementById("person-engine");
  const nameInput = document.getElementById("person-name");
  engineSel.innerHTML = engineCatalog.engines.map((e) => `<option value="${e}">${e}</option>`).join("");
  engineSel.value = engineCatalog.engines[0] || "claude";
  setModelSelect(engineSel.value);
  nameInput.value = "";
  engineSel.onchange = () => setModelSelect(engineSel.value);
  dlg.classList.add("open");
  nameInput.focus();
  return new Promise((resolve) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      resolve({
        name: nameInput.value.trim(),
        engine: engineSel.value,
        model: document.getElementById("person-model").value,
      });
      dlg.classList.remove("open");
    };
    document.getElementById("person-cancel").onclick = () => {
      resolve(null);
      dlg.classList.remove("open");
    };
  });
}

export async function renderPeople() {
  const ul = document.getElementById("people-list");
  const people = await api.people();
  if (!people.length) {
    ul.innerHTML = `<li class="empty">아직 에이전트이 없습니다</li>`;
    return;
  }
  ul.innerHTML = people.map((p) => `
    <li>
      <div class="row"><span class="name">${esc(p.이름)}</span><span class="code">${esc(p.코드)}</span></div>
      <div class="sub runtime">${esc(p.engine)}${p.model ? " · " + esc(p.model) : ""}</div>
      <div class="sub work-settings">${esc(workSettingsSummary(p.작업설정 || {}))}</div>
      <div class="sub">공간: ${p.공간.length ? p.공간.map(esc).join(", ") : "없음"}</div>
      <div class="edit-actions" aria-label="에이전트 수정">
        <button class="edit-btn person-runtime-btn" data-person="${esc(p.토큰)}" data-engine="${esc(p.engine)}" data-model="${esc(p.model)}">엔진/모델 수정</button>
        <button class="edit-btn person-work-settings-btn" data-person="${esc(p.토큰)}"
          data-runner-timeout-sec="${esc(p.작업설정?.runner_timeout_sec || 300)}"
          data-heartbeat-interval-sec="${esc(p.작업설정?.heartbeat_interval_sec || 10)}"
          data-heartbeat-stale-ms="${esc(p.작업설정?.heartbeat_stale_ms || 60000)}"
          data-progress-report-due-ms="${esc(p.작업설정?.progress_report_due_ms || 60000)}">작업 실행설정 수정</button>
        <button class="edit-btn person-role-btn" data-person="${esc(p.토큰)}">role 수정</button>
        <button class="edit-btn danger person-delete-btn" data-person="${esc(p.토큰)}" data-name="${esc(p.이름)}">삭제</button>
      </div>
    </li>`).join("");
}

export function wirePeople(refreshAll) {
  document.getElementById("add-person").onclick = async () => {
    const data = await openPersonModal();
    if (!data || !data.name) return;
    try { await api.createPerson(data); await refreshAll(); }
    catch (e) { alert("실패: " + e.message); }
  };

  document.getElementById("people-list").onclick = async (e) => {
    const runtimeBtn = e.target.closest(".person-runtime-btn");
    const workSettingsBtn = e.target.closest(".person-work-settings-btn");
    const roleBtn = e.target.closest(".person-role-btn");
    const deleteBtn = e.target.closest(".person-delete-btn");
    if (runtimeBtn) {
      const data = await openRuntimeModal("에이전트 엔진", runtimeBtn.dataset.engine, runtimeBtn.dataset.model);
      if (!data) return;
      try { await api.updatePersonRuntime(runtimeBtn.dataset.person, data); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (workSettingsBtn) {
      const data = await openWorkSettingsModal("에이전트 작업 실행설정", workSettingsBtn.dataset);
      if (!data) return;
      try { await api.updatePersonWorkSettings(workSettingsBtn.dataset.person, data); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (roleBtn) {
      try {
        const role = await api.personRole(roleBtn.dataset.person);
        const text = await openTextModal(`${roleBtn.dataset.person} role.md`, role.text);
        if (text === null) return;
        await api.savePersonRole(roleBtn.dataset.person, text);
        await refreshAll();
      } catch (err) { alert("실패: " + err.message); }
      return;
    }
    if (deleteBtn) {
      const label = `${deleteBtn.dataset.name || "에이전트"} (${deleteBtn.dataset.person})`;
      if (!confirm(`${label}을 삭제할까요?\n참여 중인 공간의 멤버 목록에서도 제거됩니다.`)) return;
      try { await api.deletePerson(deleteBtn.dataset.person); await refreshAll(); }
      catch (err) { alert("실패: " + err.message); }
    }
  };
}

export function openPersonPickerModal(title, people = [], options = {}) {
  const modal = document.getElementById("person-picker-modal");
  const form = document.getElementById("person-picker-form");
  const heading = document.getElementById("person-picker-title");
  const select = document.getElementById("person-picker-select");
  const note = document.getElementById("person-picker-note");
  const submit = document.getElementById("person-picker-submit");
  const cancel = document.getElementById("person-picker-cancel");
  if (!modal || !form || !heading || !select || !note || !submit || !cancel) {
    return Promise.resolve(null);
  }
  const excludeSpace = options.excludeSpace || "";
  const available = (people || []).filter((p) => !excludeSpace || !(p.공간 || []).includes(excludeSpace));
  heading.textContent = title || "에이전트 선택";
  select.innerHTML = available.map((p) => {
    const runtime = [p.engine, p.model].filter(Boolean).join(" · ");
    return `<option value="${esc(p.토큰)}">${esc(p.이름)} (${esc(p.코드)})${runtime ? ` · ${esc(runtime)}` : ""}</option>`;
  }).join("");
  select.disabled = !available.length;
  submit.disabled = !available.length;
  note.textContent = available.length ? (options.note || "") : (options.emptyText || "입장 가능한 에이전트가 없습니다.");
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  setTimeout(() => select.focus(), 0);
  return new Promise((resolve) => {
    const cleanup = () => {
      form.onsubmit = null;
      cancel.onclick = null;
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
    };
    form.onsubmit = (e) => {
      e.preventDefault();
      const value = select.value || "";
      cleanup();
      resolve(value || null);
    };
    cancel.onclick = () => {
      cleanup();
      resolve(null);
    };
  });
}

export async function openRuntimeModal(title, engine, model) {
  await loadEngineCatalog();
  document.getElementById("edit-title").textContent = title;
  document.getElementById("edit-text-wrap").hidden = true;
  document.getElementById("edit-work-settings-wrap").hidden = true;
  document.getElementById("edit-runtime-wrap").hidden = false;
  const engineSel = document.getElementById("edit-engine");
  const modelSel = document.getElementById("edit-model");
  engineSel.innerHTML = engineCatalog.engines.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
  engineSel.value = engine || engineCatalog.engines[0] || "claude";
  const sync = () => {
    const models = engineCatalog.models[engineSel.value] || [];
    modelSel.innerHTML = models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (model && models.includes(model)) modelSel.value = model;
  };
  engineSel.onchange = () => { model = ""; sync(); };
  sync();
  return openEditModal(() => ({ engine: engineSel.value, model: modelSel.value }));
}

export async function openWorkSettingsModal(title, settings = {}, options = {}) {
  document.getElementById("edit-title").textContent = title;
  document.getElementById("edit-runtime-wrap").hidden = true;
  document.getElementById("edit-text-wrap").hidden = true;
  document.getElementById("edit-work-settings-wrap").hidden = false;
  const partial = Boolean(options.partial);
  const note = document.getElementById("edit-work-settings-note");
  const noteText = options.note || (partial ? "체크한 항목만 이 좌석의 직접 설정으로 저장됩니다." : "");
  note.textContent = noteText;
  note.hidden = !noteText;
  const configuredKeys = new Set(
    String(settings.configuredKeys || settings.configured_keys || "")
      .split(",")
      .map((key) => key.trim())
      .filter(Boolean),
  );
  const fields = [
    { key: "runner_timeout_sec", input: "edit-runner-timeout", checkbox: "edit-runner-timeout-override", datasetKey: "runnerTimeoutSec", fallback: 300 },
    { key: "heartbeat_interval_sec", input: "edit-heartbeat-interval", checkbox: "edit-heartbeat-interval-override", datasetKey: "heartbeatIntervalSec", fallback: 10 },
    { key: "heartbeat_stale_ms", input: "edit-heartbeat-stale", checkbox: "edit-heartbeat-stale-override", datasetKey: "heartbeatStaleMs", fallback: 60000 },
    { key: "progress_report_due_ms", input: "edit-progress-due", checkbox: "edit-progress-due-override", datasetKey: "progressReportDueMs", fallback: 60000 },
  ];
  fields.forEach((field) => {
    const input = document.getElementById(field.input);
    const checkbox = document.getElementById(field.checkbox);
    input.value = settings[field.datasetKey] || settings[field.key] || field.fallback;
    checkbox.checked = partial ? configuredKeys.has(field.key) : true;
    checkbox.closest(".work-override-row").hidden = !partial;
  });
  return openEditModal(() => {
    const values = {};
    const selected = [];
    fields.forEach((field) => {
      const checked = document.getElementById(field.checkbox).checked;
      if (!partial || checked) {
        values[field.key] = Number(document.getElementById(field.input).value || field.fallback);
        selected.push(field.key);
      }
    });
    if (partial) values.configured_keys = selected;
    return values;
  });
}

export async function openTextModal(title, text) {
  document.getElementById("edit-title").textContent = title;
  document.getElementById("edit-runtime-wrap").hidden = true;
  document.getElementById("edit-work-settings-wrap").hidden = true;
  document.getElementById("edit-work-settings-note").hidden = true;
  document.getElementById("edit-text-wrap").hidden = false;
  document.getElementById("edit-text").value = text || "";
  return openEditModal(() => document.getElementById("edit-text").value);
}

function openEditModal(valueFn) {
  const modal = document.getElementById("edit-modal");
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  return new Promise((resolve) => {
    document.getElementById("edit-cancel").onclick = () => {
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      resolve(null);
    };
    document.getElementById("edit-form").onsubmit = (e) => {
      e.preventDefault();
      const value = valueFn();
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      resolve(value);
    };
  });
}
