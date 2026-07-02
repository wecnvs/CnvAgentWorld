// 부팅과 조립만 한다.
import { api } from "./api.js?v=20260702-08";
import { renderPeople, wirePeople } from "./people.js?v=20260702-08";
import { renderSpaces, wireSpaces } from "./spaces.js?v=20260702-08";
import { openDir, wireFiles, startFilesAutoRefresh } from "./files.js?v=20260702-08";
import { wireViewer } from "./viewer.js?v=20260702-08";
import { wireRoomChat, refreshRoomSpaceSelect } from "./room-chat.js?v=20260702-08";
import { wireTerminalDock } from "./terminal.js?v=20260702-08";
import { setupMobileViewport } from "./mobile.js?v=20260702-08";
import { setupMobileResize } from "./mobile-resize.js?v=20260702-08";
import { renderCases, wireCases } from "./cases.js?v=20260702-08";
import { renderApps, wireApps } from "./apps.js?v=20260702-08";

async function refreshAll() {
  await Promise.all([renderPeople(), renderSpaces()]);
  await refreshRoomSpaceSelect();
}

// 테마(라이트/다크) 토글 — 초기값은 <head> 인라인 스크립트가 페인트 전에 확정한다.
function wireTheme() {
  const btn = document.getElementById("theme-toggle");
  const root = document.documentElement;
  const sync = () => {
    const light = root.getAttribute("data-theme") === "light";
    if (btn) {
      btn.textContent = light ? "☀️" : "🌙";
      btn.title = light ? "다크 모드로 전환" : "라이트 모드로 전환";
      btn.setAttribute("aria-pressed", String(light));
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", light ? "#f4f6fa" : "#0e1117");
  };
  if (btn) {
    btn.onclick = () => {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("cnv.theme", next); } catch (_) {}
      sync();
    };
  }
  sync();
}

// 모든 모달에 공통 백드롭 탭·Escape 닫기(안전망). 모달은 position:fixed; inset:0(전체 덮음)라,
// 취소 버튼이 화면 밖(모바일에서 패널이 뷰포트보다 큰 경우 등)이면 닫을 수가 없어 전체 터치가
// 막히는 '갇힘' 상태가 생긴다. 백드롭(패널 바깥)을 탭하거나 Escape를 누르면 그 모달의 취소 버튼을
// 클릭해(각 모달의 정리 로직까지 실행) 닫는다. 취소 버튼이 없으면 클래스만 제거해 강제로 연다.
function dismissModal(modal) {
  if (!modal) return;
  const cancel = modal.querySelector('[id$="-cancel"]');
  if (cancel) cancel.click();   // 각 모달의 정리 로직(리스너 제거·폴링 복귀 등)을 실행
  // 보증: 취소 핸들러가 없거나 무효라도 반드시 닫힌다(갇힘·전체 터치 차단 방지). 이중 close는 멱등.
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
}
function wireModalDismiss() {
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (t && t.classList && t.classList.contains("modal") && t.classList.contains("open")) dismissModal(t);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = document.querySelector(".modal.open");
    if (open) dismissModal(open);
  });
}

async function init() {
  try {
    await api.health();
    document.getElementById("status").classList.add("ok");
  } catch (_) {}
  wireTheme();
  wireModalDismiss();
  wirePeople(refreshAll);
  wireSpaces(refreshAll);
  wireFiles();
  wireViewer();
  wireRoomChat(refreshAll);
  wireTerminalDock();
  setupMobileViewport();
  setupMobileResize();
  wireCases();
  wireApps();
  await refreshAll();
  renderCases();
  renderApps();
  await openDir("");          // 루트폴더 파일 목록
  startFilesAutoRefresh();    // 디스크 변경을 감지해 파일 탭을 자동 갱신
}

init();
