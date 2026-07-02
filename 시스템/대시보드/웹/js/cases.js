// 스킬 검토 패널 — 스킬 설명/SKILL.md를 보고, 그 아래 케이스를 검토한다.
// §9.1: 자동 격리된 모순(conflict)을 전 스킬 통합 배너로 노출하고, 조건 좁혀 분기(branch)로 해소한다.
import { api } from "./api.js?v=20260702-13";

let selectedSkill = null;
let reviewIndex = {};   // skill name -> { conflicts, review }
let skillIndex = {};     // skill name -> list summary

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export async function renderCases() {
  const listEl = document.getElementById("cases-skill-list");
  if (!listEl) return;
  await renderReviewBanner();   // 통합 검토큐 먼저(어떤 스킬에 모순이 있는지 인덱스 채움)
  await renderPromotionBacklog();  // 승격 대기 백로그(대표 원클릭 승격 — candidate 적체 해소)
  let skills = [];
  try {
    skills = await api.listSkills();
  } catch (e) {
    listEl.innerHTML = "";
    listEl.append(el("li", "empty", "스킬 목록 실패: " + e.message));
    return;
  }
  // 모순/검토가 있는 스킬을 맨 위로, 그다음 케이스 많은 순
  skills.sort((a, b) => {
    const ra = reviewIndex[a.name] || {}, rb = reviewIndex[b.name] || {};
    if ((rb.conflicts || 0) !== (ra.conflicts || 0)) return (rb.conflicts || 0) - (ra.conflicts || 0);
    if ((rb.review || 0) !== (ra.review || 0)) return (rb.review || 0) - (ra.review || 0);
    return (b.maturity?.cases || 0) - (a.maturity?.cases || 0);
  });
  listEl.innerHTML = "";
  skillIndex = {};
  if (!skills.length) {
    listEl.append(el("li", "empty", "스킬 없음"));
    return;
  }
  skills.forEach((s) => {
    skillIndex[s.name] = s;
    const li = el("li", "cases-skill-item");
    const m = s.maturity || {};
    const rv = reviewIndex[s.name] || {};
    const nameRow = el("div", "cases-skill-name", s.name);
    if (rv.conflicts) nameRow.append(el("span", "case-badge conflict", `모순 ${rv.conflicts}`));
    else if (rv.review) nameRow.append(el("span", "case-badge review", `검토 ${rv.review}`));
    li.append(nameRow);
    const meta = el("div", "cases-skill-meta");
    meta.textContent = m.is_new
      ? "신규 (케이스 0)"
      : `케이스 ${m.cases || 0}` + (m.warn_harmful ? ` · ⚠ harmful ${m.harmful}` : "");
    li.append(meta);
    if (s.name === selectedSkill) li.classList.add("active");
    li.onclick = () => {
      selectedSkill = s.name;
      renderCases();
    };
    listEl.append(li);
  });
  if (selectedSkill) renderSkillDetail(selectedSkill);
}

async function renderReviewBanner() {
  const banner = document.getElementById("cases-review-banner");
  reviewIndex = {};
  let data;
  try {
    data = await api.skillsReview();
  } catch (e) {
    if (banner) banner.hidden = true;
    return;
  }
  (data.skills || []).forEach((s) => { reviewIndex[s.skill] = { conflicts: s.conflicts, review: s.review }; });
  if (!banner) return;
  if (!data.total_review) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  banner.hidden = false;
  banner.innerHTML = "";
  const conf = data.total_conflicts || 0;
  const head = el("div", "review-banner-head",
    conf ? `⚠ 해소 대기 모순 ${conf}건 — 대표 판단 필요` : `검토 대기 ${data.total_review}건`);
  if (conf) head.classList.add("danger");
  banner.append(head);
  const chips = el("div", "review-banner-chips");
  (data.skills || []).forEach((s) => {
    const label = s.conflicts ? `${s.skill} · 모순 ${s.conflicts}` : `${s.skill} · 검토 ${s.review}`;
    const chip = el("button", "review-chip" + (s.conflicts ? " danger" : ""), label);
    chip.type = "button";
    chip.onclick = () => { selectedSkill = s.skill; renderCases(); };
    chips.append(chip);
  });
  banner.append(chips);
}

// ── 승격 대기 백로그 — candidate 적체(감사: 75/100)를 대표가 훑고 원클릭 승격/폐기 ──
async function renderPromotionBacklog() {
  const box = document.getElementById("cases-promotion-backlog");
  if (!box) return;
  let data;
  try {
    data = await api.skillsPromotions();
  } catch (e) {
    box.hidden = true;
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = "";
  const head = el("div", "promotion-backlog-head",
    `승격 대기 ${data.total}건${data.ready_count ? ` (준비완료 ${data.ready_count}건 ⭐)` : ""} — 승격하면 확정 규칙으로 모든 방에 적용됩니다`);
  box.append(head);
  const list = el("div", "promotion-backlog-list");
  // 준비완료·worked 순으로 이미 정렬돼 옴. 과부하 방지로 상위 30건만, 나머지는 개수 고지.
  items.slice(0, 30).forEach((it) => {
    const row = el("div", "promotion-backlog-row" + (it.ready_to_promote ? " ready" : ""));
    const mark = it.ready_to_promote ? "⭐ " : "";
    const pol = it.polarity === "failed" ? "⛔" : "✅";
    row.append(el("div", "promotion-backlog-body",
      `${mark}[${it.skill}] ${pol} ${it.condition} → ${it.instruction}` +
      (it.worked ? ` (worked ${it.worked})` : "")));
    const actions = el("div", "promotion-backlog-actions");
    const promoteBtn = el("button", "ghost small", "승격");
    promoteBtn.type = "button";
    promoteBtn.onclick = async () => {
      promoteBtn.disabled = true;
      try {
        await api.promoteCase(it.skill, it.case_id, "대표 승격(승격 대기 백로그)");
      } catch (e) { alert(`승격 실패: ${e.message || e}`); }
      renderCases();
    };
    const retireBtn = el("button", "danger small", "폐기");
    retireBtn.type = "button";
    retireBtn.onclick = async () => {
      retireBtn.disabled = true;
      try {
        await api.retireCase(it.skill, it.case_id, "대표 폐기(승격 대기 백로그)");
      } catch (e) { alert(`폐기 실패: ${e.message || e}`); }
      renderCases();
    };
    const openBtn = el("button", "ghost small", "열기");
    openBtn.type = "button";
    openBtn.onclick = () => { selectedSkill = it.skill; renderCases(); };
    actions.append(promoteBtn, retireBtn, openBtn);
    row.append(actions);
    list.append(row);
  });
  if (items.length > 30) {
    list.append(el("div", "promotion-backlog-more", `… 외 ${items.length - 30}건 (승격/폐기하면 이어서 표시)`));
  }
  box.append(list);
}

async function renderSkillDetail(skill) {
  const detail = document.getElementById("cases-detail");
  if (!detail) return;
  detail.innerHTML = "";
  detail.append(el("div", "cases-detail-title", `${skill} — 스킬`));
  const cached = skillIndex[skill] || {};
  let data, info, detailError = "";
  try {
    [info, data] = await Promise.all([api.skillDetail(skill), api.skillCases(skill)]);
  } catch (e) {
    try {
      data = await api.skillCases(skill);
    } catch (caseError) {
      detail.append(el("div", "empty", "스킬 로드 실패: " + caseError.message));
      return;
    }
    detailError = e.message;
  }
  if (selectedSkill !== skill) return;
  const meta = { ...cached, ...(info || {}) };
  detail.append(renderSkillSummary(meta, data, detailError));
  detail.append(el("div", "cases-help",
    "케이스는 이 스킬 아래에 쌓이는 경우의 수입니다. 만들어지면 다음부터 자동으로 사용되고, "
    + "신뢰가 쌓이면 자동 승격됩니다. 대표님은 잘못된 케이스만 삭제하시면 됩니다."));
  detail.append(el("div", "cases-section-title", "케이스"));
  if (detailError) {
    detail.append(el("div", "cases-load-note", "SKILL.md 로드 실패: " + detailError));
  }
  if (!data) {
    detail.append(el("div", "empty", "케이스 로드 실패"));
    return;
  }
  const cases = data.cases || [];
  const conv = {};
  (data.convergence || []).forEach((c) => (conv[c.case_id] = c));
  const byId = {};
  cases.forEach((c) => (byId[c.case_id] = c));
  if (!cases.length) {
    detail.append(el("div", "empty", "이 스킬엔 아직 케이스가 없습니다."));
    return;
  }

  const act = (label, cls, fn) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.onclick = async () => {
      b.disabled = true;
      try {
        await fn();
        await renderCases();
      } catch (e) {
        alert(e.message);
        b.disabled = false;
      }
    };
    return b;
  };

  const condOf = (cid) => (byId[cid] ? (byId[cid].condition || cid) : cid);

  cases.forEach((c) => {
    const cv = conv[c.case_id] || {};
    const card = el("div", "case-card");
    card.dataset.status = c.status || "";

    const head = el("div", "case-card-head");
    head.append(el("span", "case-status", c.status || ""));
    head.append(el("span", "case-polarity " + (c.polarity || ""), c.polarity || ""));
    if (c.status === "conflict") head.append(el("span", "case-badge conflict", "모순 격리"));
    if (cv.ready_to_promote) head.append(el("span", "case-badge ready", "승격 준비"));
    if (cv.needs_review && c.status !== "conflict") head.append(el("span", "case-badge review", "검토 필요"));
    if (c.sensitivity === "confidential") head.append(el("span", "case-badge conf", "대외비"));
    card.append(head);

    card.append(el("div", "case-line", "상황: " + (c.condition || "")));
    card.append(el("div", "case-line strong", "지시: " + (c.instruction || "")));
    // 모순이면 무엇과 충돌하는지 보여준다(대표가 어느 쪽이 맞는지 판단할 근거)
    if (c.status === "conflict" && (c.conflicts_with || []).length) {
      const cw = el("div", "case-line conflict-with",
        "충돌 상대: " + c.conflicts_with.map(condOf).join(" / "));
      card.append(cw);
    }
    const confTxt = (cv.confidence == null) ? "-" : cv.confidence;
    // 사용 상태: 죽은 상태가 아니면 '다음에도 자동 사용됨'. 👍/👎는 에이전트 사용결과로 자동 집계.
    const inUse = !["superseded", "retired", "expired", "graduated"].includes(c.status || "");
    card.append(el("div", "case-metrics",
      `${inUse ? "✅ 자동 사용 중" : "⛔ 미사용"} · 👍 ${cv.worked || 0} · 👎 ${cv.harmful || 0} · 신뢰 ${confTxt}`
      + ` · ${c.status === "active" ? "승격됨" : "신뢰 쌓이면 자동 승격"} · 발의 ${c.proposed_by || "-"}`));

    const actions = el("div", "case-actions");
    if (c.status === "conflict") {
      // 격리된 모순: 조건 좁혀 분기(권장) / 이 쪽 폐기 / 그래도 이 쪽 채택
      actions.append(act("✂ 분기(조건 좁히기)", "primary small", () => doBranch(skill, c.case_id)));
      actions.append(act("🗑 삭제", "danger small", () => api.retireCase(skill, c.case_id, "대표 모순해소: 이 쪽 폐기")));
      actions.append(act("그래도 채택", "ghost small", () => api.promoteCase(skill, c.case_id, "대표 모순해소: 이 쪽 채택")));
    } else {
      // 주 액션: 잘못된 케이스 삭제(그만 쓰기). 나머지는 선택(보통 자동이라 손 댈 필요 없음).
      actions.append(act("🗑 삭제(그만 쓰기)", "danger small", () => api.retireCase(skill, c.case_id, "대표 삭제: 잘못된 케이스")));
      if (c.status !== "active") actions.append(act("지금 승격(선택)", "ghost small", () => api.promoteCase(skill, c.case_id, "대표 수동 승격")));
      if (c.status === "active" || c.status === "provisional_must") actions.append(act("강등(선택)", "ghost small", () => api.demoteCase(skill, c.case_id, "대표 강등")));
      actions.append(act("👍", "ghost small", () => api.caseEvent(skill, c.case_id, "worked")));
      actions.append(act("👎", "ghost small", () => api.caseEvent(skill, c.case_id, "harmful")));
    }
    card.append(actions);

    detail.append(card);
  });
}

function renderSkillSummary(info, data, detailError = "") {
  const box = el("div", "skill-summary");
  const head = el("div", "skill-summary-head");
  head.append(el("span", "skill-summary-name", info.name || data.skill || selectedSkill || ""));
  if (info.grade) head.append(el("span", "case-badge", info.grade));
  const maturity = data?.maturity || info.maturity || {};
  head.append(el("span", "case-badge", `케이스 ${maturity.cases || 0}`));
  if (maturity.version) head.append(el("span", "case-badge", `v${maturity.version}`));
  if (maturity.warn_harmful) head.append(el("span", "case-badge review", `harmful ${maturity.harmful || 0}`));
  box.append(head);

  const desc = el("div", "skill-description", info.description || "frontmatter description이 비어 있습니다.");
  box.append(desc);
  if (info.path) box.append(el("div", "skill-path", info.path));

  const source = el("details", "skill-source");
  const summary = el("summary", "skill-source-summary", "SKILL.md 보기");
  source.append(summary);
  const pre = el("pre", "skill-source-code");
  const code = el("code", "", detailError ? "SKILL.md를 읽을 수 없습니다." : (info.content || ""));
  pre.append(code);
  source.append(pre);
  box.append(source);
  return box;
}

// 모순을 '병합·삭제' 말고 조건 좁혀 분기(more-specific-wins). 대표가 좁힐 키워드를 직접 준다.
async function doBranch(skill, caseId) {
  const raw = window.prompt(
    "이 케이스를 어떤 상황에만 적용할지 좁혀 적으세요 (쉼표로 키워드, 예: VIP,긴급).\n" +
    "상대 케이스와 겹치지 않게 조건을 좁히면 둘 다 각자 상황에서 살아납니다.");
  if (raw == null) return;                       // 취소
  const keywords = raw.split(",").map((s) => s.trim()).filter(Boolean);
  if (!keywords.length) { alert("키워드를 최소 하나 입력하세요."); return; }
  await api.branchCase(skill, caseId, { keywords, note: raw.trim() },
    { rationale: "대표 분기: " + raw.trim() });
  await renderCases();
}

export function wireCases() {
  const refresh = document.getElementById("cases-refresh");
  if (refresh) refresh.onclick = () => renderCases();
  // 케이스 탭을 처음 누를 때 로드되도록
  document.querySelectorAll('.vtab[data-view="casesView"]').forEach((btn) => {
    btn.addEventListener("click", () => renderCases());
  });
}
