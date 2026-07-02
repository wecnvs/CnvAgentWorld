// API 호출만 책임진다.
async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// 비예외 호출: 409(락 경합/거부) 같은 '예상된 실패'도 던지지 않고 {ok,status,data}로 돌려준다.
async function jSoft(url, opts) {
  let r;
  try { r = await fetch(url, opts); }
  catch (e) { return { ok: false, status: 0, data: { message: e.message } }; }
  let data = {};
  try { data = (await r.json()) || {}; } catch (_) {}
  return { ok: r.ok, status: r.status, data };
}

export const api = {
  health: () => j("/api/health"),
  engineModels: () => j("/api/engine-models"),
  people: () => j("/api/people"),
  createPerson: (person) => j("/api/people", json("POST", person)),
  deletePerson: (person) => j(`/api/people/${encodeURIComponent(person)}`, { method: "DELETE" }),
  updatePersonRuntime: (person, runtime) => j(`/api/people/${encodeURIComponent(person)}/runtime`, json("PATCH", runtime)),
  updatePersonWorkSettings: (person, settings) => j(`/api/people/${encodeURIComponent(person)}/work-settings`, json("PATCH", settings)),
  personRole: (person) => j(`/api/people/${encodeURIComponent(person)}/role`),
  savePersonRole: (person, text) => j(`/api/people/${encodeURIComponent(person)}/role`, json("PUT", { text })),
  spaces: () => j("/api/spaces"),
  createSpace: (space) => j("/api/spaces", json("POST", space)),
  deleteSpace: (space) => j(`/api/spaces/${encodeURIComponent(space)}`, { method: "DELETE" }),
  join: (person, space) => j("/api/spaces/join", json("POST", { person, space })),
  updateSpaceRuntime: (space, runtime) => j(`/api/spaces/${encodeURIComponent(space)}/manager-runtime`, json("PATCH", runtime)),
  updateSpaceWorkSettings: (space, settings) => j(`/api/spaces/${encodeURIComponent(space)}/work-settings`, json("PATCH", settings)),
  updateSeatWorkSettings: (space, person, settings) =>
    j(`/api/spaces/${encodeURIComponent(space)}/members/${encodeURIComponent(person)}/work-settings`, json("PATCH", settings)),
  spaceGuide: (space) => j(`/api/spaces/${encodeURIComponent(space)}/guide`),
  saveSpaceGuide: (space, text) => j(`/api/spaces/${encodeURIComponent(space)}/guide`, json("PUT", { text })),
  spaceMessages: (space, limit = 120) =>
    j(`/api/spaces/${encodeURIComponent(space)}/messages?limit=${encodeURIComponent(limit)}`),
  spaceStatus: (space) => j(`/api/spaces/${encodeURIComponent(space)}/status`),
  spaceHandback: (space) => j(`/api/spaces/${encodeURIComponent(space)}/handback`),
  spaceApprovals: (space) => j(`/api/spaces/${encodeURIComponent(space)}/approvals`),
  approvePlan: (space, planId, actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/plans/${encodeURIComponent(planId)}/approve`, json("POST", { actor })),
  rejectPlan: (space, planId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/plans/${encodeURIComponent(planId)}/reject`, json("POST", { actor, reason })),
  spaceActivity: (space, limit = 80) =>
    j(`/api/spaces/${encodeURIComponent(space)}/activity?limit=${encodeURIComponent(limit)}`),
  postSpace: (space, text, requester = "대표", run_manager = true, client_message_id = "") =>
    j(`/api/spaces/${encodeURIComponent(space)}/post`, json("POST", { text, requester, run_manager, client_message_id })),
  tickSpace: (space) => j(`/api/spaces/${encodeURIComponent(space)}/tick`, json("POST", {})),
  watchSpace: (space, engine, model) =>
    j(`/api/spaces/${encodeURIComponent(space)}/watch`, json("POST", { engine, model })),
  reflowSpace: (space) => j(`/api/spaces/${encodeURIComponent(space)}/reflow`, json("POST", {})),
  approveRelease: (space, releaseId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/releases/${encodeURIComponent(releaseId)}/approve`, json("POST", { actor, reason })),
  rejectRelease: (space, releaseId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/releases/${encodeURIComponent(releaseId)}/reject`, json("POST", { actor, reason })),
  publishRelease: (space, releaseId, text = null, actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/releases/${encodeURIComponent(releaseId)}/publish`, json("POST", { actor, text })),
  cancelTask: (space, taskId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/tasks/${encodeURIComponent(taskId)}/cancel`, json("POST", { actor, reason })),
  steerTask: (space, taskId, action, instruction = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/tasks/${encodeURIComponent(taskId)}/steer`, json("POST", { actor, action, instruction })),
  progressTask: (space, taskId, instruction = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/tasks/${encodeURIComponent(taskId)}/progress`, json("POST", { actor, instruction })),
  reviseTask: (space, taskId, instruction = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/tasks/${encodeURIComponent(taskId)}/revise`, json("POST", { actor, instruction })),
  updateTaskWorkSettings: (space, taskId, settings, actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/tasks/${encodeURIComponent(taskId)}/work-settings`, json("PATCH", { actor, ...settings })),
  scanLessonPromotions: (space, limit = 20, actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/learning/promotions/scan`, json("POST", { actor, limit })),
  approveLessonPromotion: (space, promotionId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/learning/promotions/${encodeURIComponent(promotionId)}/approve`, json("POST", { actor, reason })),
  rejectLessonPromotion: (space, promotionId, reason = "", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/learning/promotions/${encodeURIComponent(promotionId)}/reject`, json("POST", { actor, reason })),
  applyLessonPromotion: (space, promotionId, reason = "승인된 성장 후보를 리소스로 적용", actor = "대표") =>
    j(`/api/spaces/${encodeURIComponent(space)}/learning/promotions/${encodeURIComponent(promotionId)}/apply`, json("POST", { actor, reason })),
  listFiles: (path = "") => j(`/api/files?path=${encodeURIComponent(path)}`),
  uploadFile: (dir, file) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("dir", dir);
    return j("/api/files/upload", { method: "POST", body: fd });   // Content-Type은 브라우저가 boundary와 함께 설정
  },
  // 스킬 케이스(경우의 수) — 대표가 candidate를 검토·승인/강등/폐기
  listSkills: () => j("/api/skills"),
  skillDetail: (skill) => j(`/api/skills/${encodeURIComponent(skill)}`),
  skillsReview: () => j("/api/skills/review"),
  skillCases: (skill) => j(`/api/skills/${encodeURIComponent(skill)}/cases`),
  branchCase: (skill, caseId, applies_when, { does_not_apply_when = null, restore_to = null, rationale = "", by = "대표" } = {}) =>
    j(`/api/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}/branch`,
      json("POST", { applies_when, does_not_apply_when, restore_to, rationale, by })),
  promoteCase: (skill, caseId, rationale = "", by = "대표") =>
    j(`/api/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}/promote`, json("POST", { by, rationale })),
  demoteCase: (skill, caseId, rationale = "", by = "대표") =>
    j(`/api/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}/demote`, json("POST", { by, rationale })),
  retireCase: (skill, caseId, rationale = "", by = "대표") =>
    j(`/api/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}/retire`, json("POST", { by, rationale })),
  caseEvent: (skill, caseId, event, rationale = "", by = "대표") =>
    j(`/api/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}/event`, json("POST", { event, by, rationale })),
  // 앱 레지스트리 — 앱/ 폴더의 애플리케이션을 종류별로 보고(실행/열기/중지/다운로드)
  listApps: () => j("/api/apps"),
  runApp: (dir) => j("/api/apps/run", json("POST", { dir })),
  stopApp: (dir) => j("/api/apps/stop", json("POST", { dir })),
  stopAppInstance: (dir, pid) => j("/api/apps/stop-instance", json("POST", { dir, pid })),
  // 컴퓨터유즈/원격제어 — 세션(타깃)별 락 + 라이브 화면/입력.
  //  연결정보(host/port)는 서버 내부에서만. 브라우저는 타깃 별칭만 보낸다.
  cuStatus: (target = "") => j(`/api/cu/status${target ? `?target=${encodeURIComponent(target)}` : ""}`),
  cuAcquire: (agent_id, target, agent_name = "", note = "") =>
    jSoft("/api/cu/acquire", json("POST", { agent_id, target, agent_name, note })),       // 409=busy도 값으로
  cuHeartbeat: (agent_id, target) => jSoft("/api/cu/heartbeat", json("POST", { agent_id, target })),
  cuRelease: (agent_id, target) => jSoft("/api/cu/release", json("POST", { agent_id, target })),
  cuViewStatus: (target) => j(`/api/cu/view/status?target=${encodeURIComponent(target)}`),
  cuInput: (payload) => jSoft("/api/cu/view/input", json("POST", payload)),               // 409=lock_not_held도 값으로
  cuScreenshotUrl: (target) => `/api/cu/view/screenshot?target=${encodeURIComponent(target)}`,
};
