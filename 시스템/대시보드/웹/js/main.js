// 부팅과 조립만 한다.
import { api } from "./api.js?v=20260629-29";
import { renderPeople, wirePeople } from "./people.js?v=20260629-29";
import { renderSpaces, wireSpaces } from "./spaces.js?v=20260629-29";
import { openDir, wireFiles, startFilesAutoRefresh } from "./files.js?v=20260629-29";
import { wireViewer } from "./viewer.js?v=20260629-29";
import { wireRoomChat } from "./room-chat.js?v=20260629-29";
import { wireTerminalDock } from "./terminal.js?v=20260629-29";
import { setupMobileViewport } from "./mobile.js?v=20260629-29";
import { setupMobileResize } from "./mobile-resize.js?v=20260629-29";
import { renderCases, wireCases } from "./cases.js?v=20260629-29";

async function refreshAll() {
  await Promise.all([renderPeople(), renderSpaces()]);
}

async function init() {
  try {
    await api.health();
    document.getElementById("status").classList.add("ok");
  } catch (_) {}
  wirePeople(refreshAll);
  wireSpaces(refreshAll);
  wireFiles();
  wireViewer();
  wireRoomChat(refreshAll);
  wireTerminalDock();
  setupMobileViewport();
  setupMobileResize();
  wireCases();
  await refreshAll();
  renderCases();
  await openDir("");          // 루트폴더 파일 목록
  startFilesAutoRefresh();    // 디스크 변경을 감지해 파일 탭을 자동 갱신
}

init();
