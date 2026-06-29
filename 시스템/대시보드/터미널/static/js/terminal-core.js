function libReady(){ return (typeof Terminal!=='undefined') && (typeof FitAddon!=='undefined' && FitAddon.FitAddon); }
function ensureTerm(){
  if(term) return;
  if(!libReady()){
    throw new Error('터미널 라이브러리(xterm) 로드 실패 — 새로고침(Ctrl+Shift+R) 해주세요.');
  }
  term = new Terminal({
    cursorBlink:true, fontSize:fontSize, fontFamily:'Consolas,Menlo,monospace',
    theme: terminalTheme(),
    scrollback:5000, convertEol:false
  });
  fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  term.open(document.getElementById('term'));
  term.onData(d=>{
    // 터치 기기: xterm 자체 textarea 입력은 전부 무시한다(전용 오버레이 #ovin 이 전담).
    if(isTouch) return;
    sendInput(d);
  });
  // 터치 기기 — xterm textarea 를 쓰지 않고, 내가 통제하는 전용 오버레이(#ovin)에 입력을 건다.
  if(isTouch){
    const ov=document.getElementById('ovin');
    setupTouchInput(ov);
    setupKeys(ov);
    setupTouchSelect();   // [폰 UX] 꾹 눌러 텍스트 선택 → 끌어서 범위 → 떼면 복사
    const _tc=document.getElementById('touchCatcher'); if(_tc) _tc.style.display='block';   // 터치 가로채기 오버레이 ON
    // 터미널 영역 탭 → 오버레이 포커스(OS 키보드 오픈). 단 방금 long-press 선택 직후면 억제.
    const _t=document.getElementById('term');
    _t.addEventListener('click', ()=>{ if(_t._suppressClick) return; try{ ov.focus(); }catch(e){} });
    document.getElementById('termWrap').addEventListener('click', ()=>{ if(_t._suppressClick) return; try{ ov.focus(); }catch(e){} });
    try{ if(term.textarea){ term.textarea.setAttribute('readonly','readonly'); term.textarea.tabIndex=-1; } }catch(e){}
  }
  bindScrollbar();
  window.addEventListener('resize', doFit);
  setupViewport();
}

function sendInput(d){ if(ws && ws.readyState===1) ws.send(JSON.stringify({type:'input',data:d})); }

function setCtrlUI(on){ const b=document.querySelector('.mk[data-k="ctrl"]'); if(b) b.classList.toggle('on',on); }
function setAltUI(on){ const b=document.querySelector('.mk[data-k="alt"]'); if(b) b.classList.toggle('on',on); }
