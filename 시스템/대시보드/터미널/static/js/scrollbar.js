function bindScrollbar(){
  vp = term.element ? term.element.querySelector('.xterm-viewport') : null;
  if(!vp) return;
  vp.addEventListener('scroll', updateScrollbar, {passive:true});
  try{ if(term.onScroll) term.onScroll(()=>updateScrollbar()); }catch(e){}
  const ov=document.getElementById('scrollOverlay'), thumb=document.getElementById('scrollThumb');
  let dragging=false, startY=0, startTop=0;
  const onMove=(e)=>{
    if(!dragging) return;
    const y=(e.touches?e.touches[0].clientY:e.clientY);
    const track=ov.clientHeight, th=thumb.offsetHeight;
    let top=Math.max(0, Math.min(track-th, startTop+(y-startY)));
    const denom=(track-th)||1;
    vp.scrollTop=(top/denom)*(vp.scrollHeight-vp.clientHeight);
    if(e.cancelable) e.preventDefault();
  };
  const onUp=()=>{ dragging=false; thumb.classList.remove('drag');
    document.removeEventListener('mousemove',onMove); document.removeEventListener('mouseup',onUp);
    document.removeEventListener('touchmove',onMove); document.removeEventListener('touchend',onUp); };
  const onDown=(e)=>{ dragging=true; thumb.classList.add('drag');
    startY=(e.touches?e.touches[0].clientY:e.clientY); startTop=thumb.offsetTop;
    document.addEventListener('mousemove',onMove); document.addEventListener('mouseup',onUp);
    document.addEventListener('touchmove',onMove,{passive:false}); document.addEventListener('touchend',onUp);
    if(e.cancelable) e.preventDefault(); };
  thumb.addEventListener('mousedown',onDown);
  thumb.addEventListener('touchstart',onDown,{passive:false});
  // [폰 UX] '맨 아래로' 버튼 클릭 → 즉시 바닥으로
  const tb=document.getElementById('toBottom');
  if(tb && !tb._bound){ tb._bound=true; tb.addEventListener('click',()=>{
    try{ term.scrollToBottom(); }catch(e){}
    requestAnimationFrame(()=>{ updateScrollbar(); updateToBottom(); });
  }); }
  updateScrollbar();
}

function updateScrollbar(){
  if(!vp) return;
  const ov=document.getElementById('scrollOverlay'), thumb=document.getElementById('scrollThumb');
  if(!ov||!thumb) return;
  const sh=vp.scrollHeight, ch=vp.clientHeight;
  const track=ov.clientHeight;
  // 무조건 표시: 스크롤 불가여도 트랙은 보이고, thumb는 비율대로(가득 차면 전체 높이)
  let th, top;
  if(sh<=ch+2){ th=track; top=0; }
  else { th=Math.max(28, track*ch/sh); top=(track-th)*(vp.scrollTop/((sh-ch)||1)); }
  thumb.style.height=th+'px';
  thumb.style.top=top+'px';
  ov.classList.add('show');
  updateToBottom();
}

// [폰 UX] '맨 아래로' 버튼 — 바닥에서 충분히 위로 올라왔을 때만 표시
function updateToBottom(){
  const b=document.getElementById('toBottom'); if(!b) return;
  if(!vp){ b.classList.remove('show'); return; }
  const gap = vp.scrollHeight - vp.scrollTop - vp.clientHeight;
  b.classList.toggle('show', gap > 40);
}

// [폰 UX] 선택/복사 결과 토스트
let _selToastTimer=null;
function showSelToast(msg, ms){
  const t=document.getElementById('selToast'); if(!t) return;
  t.textContent=msg; t.classList.add('show');
  clearTimeout(_selToastTimer);
  _selToastTimer=setTimeout(()=>{ t.classList.remove('show'); }, ms||1400);
}

// [폰 UX] 터미널 터치 제스처를 '캡처 단계'에서 선점한다.
//   ★진짜 문제였던 것: 빈 칸 첫 터치는 스크롤 됐지만, 글자 위를 첫 터치하면 xterm 이 그걸
//     selection(드래그 선택) 으로 가로채 스크롤이 안 됐다.
//   ★해법: termWrap 에서 capture + stopPropagation 으로 터치를 xterm 보다 먼저 잡아 안 넘기고,
//     끌면 vp.scrollTop 을 직접 움직여 '글자 위에서도' 스크롤되게 한다. 0.55초 꾹 누르면 선택 모드.
// ── [폰 UX] 터미널 터치: 투명 오버레이(#touchCatcher)가 모든 터치를 선점 ──
