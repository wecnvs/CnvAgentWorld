let _selActive=false, _momentum=null, _momentumVel=0;
let _selR={sR:0,sC:0,eR:0,eC:0};   // 현재 선택의 절대버퍼 좌표(시작 행/열, 끝 행/열)

function _cellH(){
  try{ const d=term._core._renderService.dimensions; const h=d&&d.css&&d.css.cell&&d.css.cell.height; if(h>0) return h; }catch(e){}
  try{ const r=term.element.querySelector('.xterm-rows')||term.element.querySelector('.xterm-screen'); if(r&&term.rows) return r.offsetHeight/term.rows; }catch(e){}
  return 18;
}
function _cellOfAbs(x,y){       // 화면좌표 → 절대버퍼 {absRow,col}
  const r=term.element.getBoundingClientRect();
  const cw=r.width/term.cols, chh=r.height/term.rows;
  let col=Math.floor((x-r.left)/cw), row=Math.floor((y-r.top)/chh);
  col=Math.max(0,Math.min(term.cols-1,col)); row=Math.max(0,Math.min(term.rows-1,row));
  return {absRow:term.buffer.active.viewportY+row, col};
}
function _wordRange(absRow,col){ // 누른 지점의 '단어'(연속 비공백) 범위
  try{
    const line=term.buffer.active.getLine(absRow); if(!line) return {sC:col,eC:col};
    const text=line.translateToString(true);
    const isW=(ch)=>ch && /\S/.test(ch);
    if(!isW(text[col])) return {sC:col,eC:col};
    let s=col,e=col;
    while(s>0 && isW(text[s-1])) s--;
    while(e<text.length-1 && isW(text[e+1])) e++;
    return {sC:s,eC:e};
  }catch(e){ return {sC:col,eC:col}; }
}
function _normSel(){ let {sR,sC,eR,eC}=_selR; if(eR<sR||(eR===sR&&eC<sC)){ return {sR:eR,sC:eC,eR:sR,eC:sC}; } return {sR,sC,eR,eC}; }
function _applySel(){
  try{ const n=_normSel(); const len=(n.eR-n.sR)*term.cols+(n.eC-n.sC)+1; term.clearSelection(); term.select(n.sC,n.sR,Math.max(1,len)); }catch(e){}
  _updateHandles();
}
function _cellToWrapPx(absRow,col){
  const tr=term.element.getBoundingClientRect(), wr=document.getElementById('termWrap').getBoundingClientRect();
  const cw=tr.width/term.cols, chh=tr.height/term.rows, sr=absRow-term.buffer.active.viewportY;
  return {x:(tr.left-wr.left)+col*cw, y:(tr.top-wr.top)+sr*chh, chh, vis:sr>=0&&sr<term.rows};
}
function _updateHandles(){
  const hs=document.getElementById('selHandleStart'), he=document.getElementById('selHandleEnd');
  if(!hs||!he) return;
  if(!_selActive){ hs.classList.remove('show'); he.classList.remove('show'); return; }
  const n=_normSel(), p1=_cellToWrapPx(n.sR,n.sC), p2=_cellToWrapPx(n.eR,n.eC+1);
  if(p1.vis){ hs.style.left=p1.x+'px'; hs.style.top=(p1.y+p1.chh)+'px'; hs.classList.add('show'); } else hs.classList.remove('show');
  if(p2.vis){ he.style.left=p2.x+'px'; he.style.top=(p2.y+p2.chh)+'px'; he.classList.add('show'); } else he.classList.remove('show');
}
function _clearSel(){ _selActive=false; try{ term.clearSelection(); }catch(e){} _updateHandles(); }
function _copySel(){ let s=''; try{ s=term.getSelection?term.getSelection():''; }catch(e){} if(s) copyToClipboard(s).then(ok=>showSelToast(ok?('✓ 복사됨 ('+s.length+'자)'):'복사 실패 — 📄복사 버튼')); }

function _setupHandle(elId, which){    // 선택 양끝 핸들 드래그로 범위 조정
  const h=document.getElementById(elId); if(!h) return;
  const move=(e)=>{
    const t=e.touches?e.touches[0]:e; if(!t) return;
    const c=_cellOfAbs(t.clientX, t.clientY - _cellH()*1.1);   // 핸들은 글자 아래라 위로 보정
    if(which==='start'){ _selR.sR=c.absRow; _selR.sC=c.col; } else { _selR.eR=c.absRow; _selR.eC=c.col; }
    _applySel();
    if(e.cancelable) e.preventDefault();
  };
  const up=()=>{ document.removeEventListener('touchmove',move); document.removeEventListener('touchend',up); _copySel(); };
  h.addEventListener('touchstart',(e)=>{
    document.addEventListener('touchmove',move,{passive:false});
    document.addEventListener('touchend',up);
    e.stopPropagation(); if(e.cancelable) e.preventDefault();
  },{passive:false});
}

function setupTouchSelect(){
  const el=document.getElementById('touchCatcher'); if(!el) return;
  let lpTimer=null, mode=null, sx=0, sy=0, lastY=0, accumPx=0, lastT=0, vel=0;   // mode: null|'scroll'|'selected'
  const cancelMomentum=()=>{ if(_momentum){ cancelAnimationFrame(_momentum); _momentum=null; } };
  const startMomentum=(v)=>{
    cancelMomentum(); let vv=v;
    const step=()=>{
      vv*=0.95;                               // 감속(관성) — 조금 더 길고 부드럽게
      _momentumVel=vv;                        // 이어끌기 가속 누적에 사용
      if(Math.abs(vv)<0.35){ _momentum=null; _momentumVel=0; return; }
      accumPx+=vv; const rh=_cellH(), ln=(accumPx/rh)|0;
      if(ln!==0){ try{ term.scrollLines(ln); }catch(e){} accumPx-=ln*rh; updateScrollbar(); }
      _momentum=requestAnimationFrame(step);
    };
    _momentum=requestAnimationFrame(step);
  };
  el.addEventListener('touchstart',(e)=>{
    if(e.touches.length!==1) return;
    el._hadM=!!_momentum; const _carry=_momentum?(_momentumVel||0):0; cancelMomentum();   // ★관성 중 톡=멈춤 / 이어 끌면 속도 누적
    const t=e.touches[0]; sx=t.clientX; sy=t.clientY; lastY=t.clientY; lastT=performance.now();
    mode=null; accumPx=0; vel=_carry*0.5;
    if(e.cancelable) e.preventDefault();
    clearTimeout(lpTimer);
    lpTimer=setTimeout(()=>{                            // 0.5초 꾹 → 단어 선택 + 핸들
      const c=_cellOfAbs(sx,sy), w=_wordRange(c.absRow,c.col);
      _selR={sR:c.absRow,sC:w.sC,eR:c.absRow,eC:w.eC}; _selActive=true; mode='selected';
      try{ navigator.vibrate&&navigator.vibrate(15); }catch(e){}
      _applySel(); _copySel();
      showSelToast('🔵 단어 선택됨 — 양끝 ● 핸들을 끌어 범위 조정', 2600);
    }, 500);
  },{passive:false});
  el.addEventListener('touchmove',(e)=>{
    if(e.touches.length!==1) return;
    const t=e.touches[0];
    if(mode==='selected'){ if(e.cancelable) e.preventDefault(); return; }   // 선택 완료 — 조정은 핸들로
    if(mode===null && (Math.abs(t.clientX-sx)>8 || Math.abs(t.clientY-sy)>8)){ clearTimeout(lpTimer); mode='scroll'; if(_selActive) _clearSel(); }
    if(mode==='scroll'){
      const now=performance.now(); let dt=now-lastT; lastT=now;
      if(dt<8) dt=8; else if(dt>50) dt=50;     // dt 클램프(프레임 들쭉날쭉에 속도 튐 방지)
      const dpx=lastY-t.clientY; accumPx+=dpx;
      const rh=_cellH(), ln=(accumPx/rh)|0;
      if(ln!==0){ try{ term.scrollLines(ln); }catch(e){} accumPx-=ln*rh; updateScrollbar(); }
      vel = vel*0.6 + (dpx/dt*16)*0.4;         // EMA — 부드러운 속도(삐걱임 완화)
      if(e.cancelable) e.preventDefault();
    }
    lastY=t.clientY;
  },{passive:false});
  const onEnd=()=>{
    clearTimeout(lpTimer);
    if(mode==='scroll'){ if(Math.abs(vel)>2.5) startMomentum(vel); }       // ★관성 스크롤 시작
    else if(mode===null){
      if(_selActive) _clearSel();                                           // 선택 중 빈곳 탭 → 해제
      else if(!el._hadM){ try{ const ov=document.getElementById('ovin'); if(ov) ov.focus(); }catch(e){} }  // 탭 → 키보드(관성멈춤 톡은 제외)
    }
    mode=null;
  };
  el.addEventListener('touchend', onEnd);
  el.addEventListener('touchcancel', onEnd);
  _setupHandle('selHandleStart','start');
  _setupHandle('selHandleEnd','end');
}

