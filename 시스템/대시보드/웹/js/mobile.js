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
    // [모바일 하단→최상단 튐 근본수정] 예전엔 키보드가 닫히는 순간 window.scrollTo(0,0)으로 문서
    // 스크롤을 0으로 되돌렸다. 이는 body가 position:fixed(문서 스크롤 봉인)이던 데스크톱 기준 코드다.
    // 지금 모바일(≤720px)은 body가 position:static; overflow:auto로 '페이지 전체가 네이티브 스크롤'이라,
    // 이 리셋이 실제 페이지를 최상단으로 순간이동시킨다. 특히 iOS는 맨 아래 도달 시 주소창/툴바가 다시
    // 펼쳐지며 visualViewport 높이가 줄어 '키보드 닫힘'으로 오인 → scrollTo(0,0) 발동 → '맨 아래로
    // 내렸는데 최상단으로 튄다'(대표 신고). 정적 스크롤 body에는 되돌릴 유령 오프셋이 없으므로 여기서
    // 스크롤을 건드리지 않는다 — 키보드는 입력 blur로 닫는다(sendCurrentMessage/공간전환 분기).
  };

  const onCh = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(apply); };
  vv.addEventListener("resize", onCh);
  vv.addEventListener("scroll", onCh);
  apply();
}
