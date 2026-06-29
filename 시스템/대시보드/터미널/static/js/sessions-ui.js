function toggleSide(forceCollapse){
  const side=document.getElementById('side'), bd=document.getElementById('backdrop');
  const willCollapse = forceCollapse===true ? true : !side.classList.contains('collapsed');
  side.classList.toggle('collapsed', willCollapse);
  if(isMobileWidth()) bd.classList.toggle('hidden', willCollapse);
  else bd.classList.add('hidden');
  setTimeout(doFit, 230);
}

async function loadShells(){
  try{
    const r = await fetch(API + '/api/shells'); const j = await r.json();
    const sel = document.getElementById('shellSel'); sel.innerHTML='';
    (j.shells||[]).forEach(s=>{
      const o=document.createElement('option'); o.value=s.shell; o.textContent=s.name; sel.appendChild(o);
    });
  }catch(e){}
}

async function loadSessions(){
  try{
    const r = await fetch(API + '/api/sessions'); const j = await r.json();
    sessions = j.sessions||[];
    document.getElementById('wsRoot').textContent = 'cwd: ' + (j.workspace||'');
    renderList();
  }catch(e){
    document.getElementById('sessList').innerHTML =
      '<div class="empty warn">터미널 서버(8687) 응답 없음.<br>start_server 로 데몬을 켜주세요.</div>';
  }
}

function renderList(){
  const box = document.getElementById('sessList');
  if(!sessions.length){ box.innerHTML='<div class="empty">아직 터미널이 없습니다.<br>＋ 새 터미널로 시작하세요.</div>'; return; }
  box.innerHTML='';
  sessions.forEach(s=>{
    const el=document.createElement('div');
    el.className='sitem'+(s.id===curId?' active':'')+(s.alive?'':' dead');
    const dot=document.createElement('span');
    dot.className='dot';
    const name=document.createElement('span');
    name.className='nm';
    name.textContent=s.title || '';
    const meta=document.createElement('small');
    meta.textContent=`${shortShell(s.shell)} · ${s.alive?'실행중':'종료됨'}`;
    name.appendChild(meta);
    const kill=document.createElement('span');
    kill.className='x';
    kill.title='종료';
    kill.textContent='⨯';
    kill.addEventListener('click', ev=>killSession(s.id, ev));
    el.addEventListener('click', ev=>{ if(ev.target.classList.contains('x'))return; attach(s.id); });
    el.append(dot, name, kill);
    box.appendChild(el);
  });
}

function esc(t){return (t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function shortShell(p){ if(!p)return''; const b=p.split(/[\\/]/).pop(); return b; }

async function newSession(){
  const shell = document.getElementById('shellSel').value || '';
  const dims = curDims();
  try{
    const r = await fetch(API + '/api/sessions', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({shell, cols:dims.cols, rows:dims.rows})});
    const s = await r.json();
    if(s && s.id){ await loadSessions(); attach(s.id); }
  }catch(e){ alert('세션 생성 실패: '+e); }
}
