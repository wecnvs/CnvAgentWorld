function bindChromeActions(){
  const actions = {
    'collapse-side': () => toggleSide(true),
    'toggle-side': () => toggleSide(),
    'new-session': () => newSession(),
    'font-down': () => bumpFont(-1),
    'font-up': () => bumpFont(1),
    'toggle-theme': () => toggleTheme(),
    'rename-current': () => renameCurrent(),
    'clear-current': () => clearCurrent(),
    'kill-current': () => killCurrent(),
  };
  document.querySelectorAll('[data-action]').forEach(el=>{
    const fn = actions[el.dataset.action];
    if(fn) el.addEventListener('click', fn);
  });
}

(async function(){
  bindChromeActions();
  if(isTouch) document.getElementById('sideToggle').classList.remove('hidden');
  await loadServerInfo();
  await loadShells();
  await loadSessions();
  setInterval(loadSessions, 5000);

  // [업로드] 클립보드 이미지 붙여넣기(데스크탑 Ctrl+V·모바일 공통) → 업로드 후 경로 입력
  document.addEventListener('paste', (e)=>{
    try{
      const dt=e.clipboardData; if(!dt||!dt.items) return;
      for(const it of dt.items){
        if(it.type && it.type.startsWith('image/')){
          const f=it.getAsFile();
          if(f){ e.preventDefault(); uploadAndInsert(f, 'paste.'+((it.type.split('/')[1])||'png')); return; }
        }
      }
    }catch(err){}
  });
  // [업로드] 📎 첨부 파일 선택 → 업로드
  const _fi=document.getElementById('fileInput');
  if(_fi) _fi.addEventListener('change', ()=>{
    const f=_fi.files && _fi.files[0];
    if(f) uploadAndInsert(f, f.name);
    _fi.value='';
  });
})();
