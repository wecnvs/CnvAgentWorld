function curDims(){
  if(fit && term){ try{ const d=fit.proposeDimensions(); if(d&&d.cols) return d; }catch(e){} }
  return {cols:120, rows:30};
}

function doFit(){
  if(!fit||!term) return;
  try{ fit.fit(); }catch(e){}
  if(ws && ws.readyState===1){
    ws.send(JSON.stringify({type:'resize', cols:term.cols, rows:term.rows}));
  }
  updateScrollbar();
}

// 소프트 키보드가 올라오면 layout viewport 는 그대로라 화면이 위로 밀려 올라간다.
// visualViewport 높이에 #app 을 맞춰 줄이고 xterm 을 재-fit(해상도 재설정)해서,
// 키보드 위 남은 영역에 터미널이 딱 들어오고 화면 위치가 유지되게 한다.
function setupViewport(){
  const vv=window.visualViewport;
  if(!vv) return;
  const app=document.getElementById('app');
  let raf=0;
  const apply=()=>{
    const kb=window.innerHeight - vv.height;   // 키보드 높이 추정
    if(kb>100){ app.style.height=vv.height+'px'; window.scrollTo(0,0); }
    else { app.style.height=''; }              // 키보드 닫힘 → CSS 100vh 복원
    cancelAnimationFrame(raf);
    raf=requestAnimationFrame(doFit);          // 새 영역에 터미널 재-fit
  };
  vv.addEventListener('resize', apply);
  vv.addEventListener('scroll', apply);
}

function attach(id){
  if(!libReady()){
    const ph=document.getElementById('placeholder');
    ph.classList.remove('hidden');
    ph.innerHTML='<div class="big">⚠️</div><div>터미널 라이브러리 로드 실패<br>새로고침(Ctrl+Shift+R) 후 다시 시도해주세요.</div>';
    document.getElementById('term').classList.add('hidden');
    return;
  }
  curId = id;
  document.getElementById('placeholder').classList.add('hidden');
  document.getElementById('term').classList.remove('hidden');
  if(isTouch) document.getElementById('mobilebar').classList.remove('hidden');
  ensureTerm();
  term.reset();
  renderList();
  const s = sessions.find(x=>x.id===id);
  document.getElementById('curTitle').textContent = s? s.title : '터미널';
  document.getElementById('curMeta').textContent = s? (shortShell(s.shell)) : '';
  if(ws){ try{ws.onclose=null; ws.close();}catch(e){} ws=null; }
  connectWS(id);
  // 모바일: 세션 한 번 탭 → 목록 접고 터미널 전체 표시
  if(isMobileWidth()) toggleSide(true);
}

// [3.2] 연결 상태 배지 — 'idle'|'warn'(연결/재연결중)|'ok'|'bad'
function setConn(state){
  const el=document.getElementById('connStat'); if(!el) return;
  el.classList.remove('ok','warn','bad');
  const txt=el.querySelector('.ctxt');
  if(state==='ok'){ el.classList.add('ok'); if(txt)txt.textContent='연결됨'; }
  else if(state==='warn'){ el.classList.add('warn'); if(txt)txt.textContent='연결중…'; }
  else if(state==='bad'){ el.classList.add('bad'); if(txt)txt.textContent='끊김'; }
  else { if(txt)txt.textContent='대기'; }
}

function connectWS(id){
  clearTimeout(reconnectTimer);
  setConn('warn');
  ws = new WebSocket(wsBase() + '/ws/' + id);
  ws.onopen = ()=>{ setConn('ok'); setTimeout(doFit, 60); if(!isTouch) term.focus(); };
  ws.onmessage = (ev)=>{
    let m; try{ m=JSON.parse(ev.data); }catch(e){ return; }
    if(m.type==='output'){ term.write(m.data); requestAnimationFrame(updateScrollbar); }
    else if(m.type==='exit'){ term.write('\r\n\x1b[33m[프로세스 종료됨]\x1b[0m\r\n'); loadSessions(); }
    else if(m.type==='error'){ term.write('\r\n\x1b[31m['+m.message+']\x1b[0m\r\n'); }
  };
  ws.onclose = ()=>{
    if(curId===id){
      setConn('warn');   // 자동 재연결 대기 중
      reconnectTimer = setTimeout(()=>{ if(curId===id) connectWS(id); }, 1500);
    } else { setConn('idle'); }
  };
  ws.onerror = ()=>{ setConn('bad'); };
}

async function killSession(id, ev){
  if(ev) ev.stopPropagation();
  if(!confirm('이 터미널 세션을 종료할까요?')) return;
  try{ await fetch(API + '/api/sessions/'+id, {method:'DELETE'}); }catch(e){}
  if(curId===id){ curId=null; if(ws){try{ws.onclose=null;ws.close();}catch(e){}ws=null;}
    document.getElementById('term').classList.add('hidden');
    document.getElementById('mobilebar').classList.add('hidden');
    document.getElementById('placeholder').classList.remove('hidden');
    document.getElementById('curTitle').textContent='터미널 미선택';
    document.getElementById('curMeta').textContent='';
    setConn('idle');
  }
  loadSessions();
}
function killCurrent(){ if(curId) killSession(curId); }

async function renameCurrent(){
  if(!curId) return;
  const s = sessions.find(x=>x.id===curId);
  const t = prompt('터미널 이름', s? s.title : '');
  if(t==null) return;
  try{ await fetch(API + '/api/sessions/'+curId+'/rename', {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:t})}); }catch(e){}
  document.getElementById('curTitle').textContent=t;
  loadSessions();
}
function clearCurrent(){ if(term) term.clear(); }
