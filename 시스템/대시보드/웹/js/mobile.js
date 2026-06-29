// 모바일: 키보드 열림 상태만 표시한다.
// 페이지 전체 스크롤은 브라우저 기본 동작에 맡겨야 하단 스크롤이 상단으로 튀지 않는다.
export function setupMobileViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  let raf = 0;

  const apply = () => {
    const h = Math.round(vv.height);
    // 키보드 없는 상태의 기준 높이 기억
    if (Math.round(vv.offsetTop) === 0 && (!window._vvBase || h > window._vvBase)) window._vvBase = h;
    const base = window._vvBase || h;
    const keyboardOpen = Math.round(vv.offsetTop) > 0 || base - h > 120;
    document.body.classList.toggle("kb-open", keyboardOpen);
  };

  const onCh = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(apply); };
  vv.addEventListener("resize", onCh);
  vv.addEventListener("scroll", onCh);
  apply();
}
