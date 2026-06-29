// 모바일(터치) 전용: 한글 IME 자모분리 해결 + 소프트키보드 viewport 보정.
// 데스크톱에선 아무것도 하지 않는다.
(function () {
  const api = window.__termApi;
  if (!api || !api.isTouch) return;
  const { term, sendData, sendResize } = api;

  document.body.classList.add("touch");

  // xterm 기본 textarea 는 한글 합성을 깨뜨린다 → 비활성화하고 전용 오버레이만 입력받는다.
  try {
    if (term.textarea) { term.textarea.setAttribute("readonly", "readonly"); term.textarea.tabIndex = -1; }
  } catch (_) {}

  const ov = document.getElementById("ovin");
  const wrap = document.querySelector(".termwrap");

  // 오버레이가 터미널을 덮고 있으므로 스크롤을 직접 처리한다.
  //  - 탭(거의 안 움직임)  → 오버레이 포커스 = 키보드 열림
  //  - 드래그              → 터미널 출력 스크롤(스크롤백)
  let startY = 0, accum = 0, moved = false;
  ov.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    startY = e.touches[0].clientY; accum = 0; moved = false;
    if (document.activeElement !== ov) e.preventDefault();   // 기본 포커스 보류(탭/드래그를 우리가 구분)
  }, { passive: false });
  ov.addEventListener("touchmove", (e) => {
    if (e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    accum += (startY - y); startY = y;                       // 손가락 내리면 위(과거)로 스크롤
    const lh = Math.max(8, wrap.clientHeight / Math.max(1, term.rows));
    const lines = Math.trunc(accum / lh);
    if (lines) { term.scrollLines(lines); accum -= lines * lh; moved = true; }
  }, { passive: true });
  ov.addEventListener("touchend", () => {
    if (!moved && document.activeElement !== ov) ov.focus();  // 탭이면 키보드 열기
  }, { passive: true });

  // ── 한글 IME diff-sync (BBC 검증 방식) ──
  // value 를 진리로 삼아, 이미 보낸 _confirmed 와 비교해 최소 Backspace(\x7f)+새 문자만 전송.
  // 끝 글자가 미완성 자모면 전송 보류(다음 input 에 완성 음절이 와서 교체).
  let _confirmed = "";
  const isJamo = (ch) => {
    const c = ch.charCodeAt(0);
    return (c >= 0x1100 && c <= 0x11FF) || (c >= 0x3130 && c <= 0x318F)
        || (c >= 0xA960 && c <= 0xA97F) || (c >= 0xD7B0 && c <= 0xD7FF);
  };
  function resetDiff() { _confirmed = ""; try { ov.value = ""; } catch (_) {} }
  function diff(target) {
    let common = 0;
    const m = Math.min(_confirmed.length, target.length);
    while (common < m && _confirmed[common] === target[common]) common++;
    let out = "";
    for (let i = _confirmed.length - common; i > 0; i--) out += "\x7f";
    for (let i = common; i < target.length; i++) out += target[i];
    return out;
  }
  function targetOf() {
    const cur = ov.value || "";
    const last = cur.length ? cur[cur.length - 1] : "";
    return (last && isJamo(last)) ? cur.slice(0, -1) : cur;
  }
  function sync() {
    const target = targetOf();
    const out = diff(target);
    if (out) sendData(out);
    _confirmed = target;
    if ((ov.value || "").length > 64) resetDiff();   // 무한 증가 방지
  }
  function commitEnter() {
    const target = targetOf();
    const out = diff(target);
    if (out) sendData(out);
    sendData("\r");
    resetDiff();
  }

  ov.addEventListener("compositionend", () => setTimeout(sync, 0));
  ov.addEventListener("input", (e) => {
    const it = (e && e.inputType) || "";
    if (it === "insertLineBreak" || it === "insertParagraph") { commitEnter(); return; }
    setTimeout(sync, 0);   // 다음 틱에 IME 변형이 끝난 value 를 읽는다
  });
  ov.addEventListener("blur", resetDiff);

  // ── 특수키 줄 ──
  const MAP = { esc: "\x1b", tab: "\t", "c-c": "\x03", up: "\x1b[A", down: "\x1b[B", left: "\x1b[D", right: "\x1b[C" };
  document.getElementById("mkeys").addEventListener("click", (e) => {
    const k = e.target.getAttribute("data-k");
    if (MAP[k]) { sendData(MAP[k]); ov.focus(); }
  });

  // ── 소프트키보드 viewport 보정 (화면 밀림 방지) ──
  const vv = window.visualViewport;
  if (vv) {
    let raf = 0;
    const apply = () => {
      document.body.style.height = Math.round(vv.height) + "px";
      window.scrollTo(0, 0);
      sendResize();
    };
    const onCh = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(apply); };
    vv.addEventListener("resize", onCh);
    vv.addEventListener("scroll", onCh);
    apply();
  }
})();
