// 하단 터미널 독: 접기/펴기 + 높이조절.
// 터미널은 8687의 독립 앱이다. 여기선 그 앱을 iframe 하나로 끼우기만 한다.
// → 8686 재시작/새로고침은 iframe만 다시 로드할 뿐, 8687 세션은 그대로 유지된다(완전 독립).
const TERMINAL_PORT = 8687;

export function wireTerminalDock() {
  const dock = document.getElementById("terminal-dock");
  const toggle = document.getElementById("term-toggle");
  const framesBox = document.getElementById("term-frames");
  const frame = document.getElementById("term-frame");
  const splitter = document.getElementById("dock-splitter");
  const openLink = document.getElementById("term-open");
  const base = `${location.protocol}//${location.hostname}:${TERMINAL_PORT}/`;
  if (openLink) openLink.href = base;
  let loaded = false;

  // ── 높이 복원 + 스플릿바 드래그 ──
  const savedH = parseInt(localStorage.getItem("dock_height") || "", 10);
  if (savedH) framesBox.style.height = savedH + "px";
  splitter.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = framesBox.getBoundingClientRect().height;
    document.body.classList.add("dock-dragging");
    function onMove(ev) {
      let h = startH - (ev.clientY - startY);
      h = Math.max(120, Math.min(window.innerHeight - 200, h));
      framesBox.style.height = h + "px";
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.classList.remove("dock-dragging");
      localStorage.setItem("dock_height", String(parseInt(framesBox.style.height, 10)));
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // ── 접기/펴기 (펼칠 때 iframe 처음 로드) ──
  function setCollapsed(c) {
    dock.classList.toggle("collapsed", c);
    toggle.textContent = c ? "▴" : "▾";
    localStorage.setItem("dock_collapsed", c ? "1" : "0");
    if (!c && !loaded) { frame.src = base; loaded = true; }
  }
  toggle.onclick = () => setCollapsed(!dock.classList.contains("collapsed"));
  setCollapsed(localStorage.getItem("dock_collapsed") !== "0");  // 기본 접힘
}
