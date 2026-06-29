const SENT=' ';   // non-breaking space (오프스크린 textarea라 화면엔 안 보임)
function setupTouchInput(ta){
  try{
    ta.setAttribute('autocomplete','off'); ta.setAttribute('autocorrect','off');
    ta.setAttribute('autocapitalize','off'); ta.setAttribute('spellcheck','false');
    ta.setAttribute('inputmode','text');
  }catch(e){}

  // ── iOS Safari 한글 IME 전략 (screen 탭과 동일한 검증된 방식) ──
  // iOS Safari 는 한글 조합에서 compositionstart/end 를 신뢰성 있게 쏘지 않고,
  // 입력을 delete+insert 쌍으로 처리한다(facebook/lexical#5841, w3c/input-events#137).
  // 그래서 composing 플래그에 의존하면 자모가 그대로 새어나간다.
  // 해법: ta.value 를 진리로 삼아, 이미 PTY 로 보낸 문자열(_confirmed)과 diff 하여
  // 최소한의 Backspace(\x7f) + 새 문자만 전송한다. 끝 글자가 미완성 한글 자모면
  // 전송을 보류한다(다음 input 에 완성 음절이 와서 교체됨).
  let _confirmed='';

  const isJamo=(ch)=>{ const c=ch.charCodeAt(0);
    return (c>=0x1100&&c<=0x11FF)      // Hangul Jamo
        || (c>=0x3130&&c<=0x318F)      // Hangul Compatibility Jamo
        || (c>=0xA960&&c<=0xA97F)      // Hangul Jamo Extended-A
        || (c>=0xD7B0&&c<=0xD7FF); };  // Hangul Jamo Extended-B

  const sync=()=>{
    const cur=ta.value||'';
    const last=cur.length?cur[cur.length-1]:'';
    const target=(last&&isJamo(last))?cur.slice(0,-1):cur;  // 끝 자모는 보류
    let common=0; const m=Math.min(_confirmed.length,target.length);
    while(common<m && _confirmed[common]===target[common]) common++;
    let out='';
    for(let i=_confirmed.length-common;i>0;i--) out+='\x7f';   // 잘못 보낸 것 롤백
    for(let i=common;i<target.length;i++) out+=target[i];      // 새 문자 추가
    if(out) sendInput(out);
    _confirmed=target;
    if(cur.length>64){ try{ta.value='';}catch(e){} _confirmed=''; }  // 무한 증가 방지
  };

  // 보류 중인 글자(끝 자모 등)까지 flush 후 Enter. 이미 확정된 텍스트는 지우지 않는다.
  const commitEnter=()=>{
    const cur=ta.value||'';
    let out='';
    for(let i=_confirmed.length;i<cur.length;i++) out+=cur[i];
    out+='\r'; sendInput(out);
    _confirmed=''; try{ta.value='';}catch(e){}
  };

  // 특수키/제어문자 전송 시 diff 상태 리셋(셸이 라인을 바꾸므로 stale 방지)
  const resetDiff=()=>{ _confirmed=''; try{ta.value='';}catch(e){} };
  ta._resetDiff=resetDiff;
  ta._commitEnter=commitEnter;   // [3.2] 모바일 ⏎ Enter 버튼이 보류 글자까지 flush 후 Enter

  ta.addEventListener('compositionend', ()=>{ setTimeout(sync,0); });
  ta.addEventListener('input', (e)=>{
    const it=(e&&e.inputType)||'';
    if(it==='insertLineBreak' || it==='insertParagraph'){ commitEnter(); return; }
    if(ctrlArmed){                                  // Ctrl 무장: 다음 한 글자를 Ctrl 조합으로
      const cur=ta.value||''; const ch=cur.slice(_confirmed.length);
      if(ch){ const c=ch[ch.length-1].toLowerCase().charCodeAt(0);
        sendInput((c>=97&&c<=122)?String.fromCharCode(c-96):ch); }
      ctrlArmed=false; setCtrlUI(false); resetDiff(); return;
    }
    if(altArmed){                                   // Alt 무장: 다음 한 글자를 ESC 접두(Meta)로
      const cur=ta.value||''; const ch=cur.slice(_confirmed.length);
      if(ch){ sendInput('\x1b'+ch[ch.length-1]); }
      altArmed=false; setAltUI(false); resetDiff(); return;
    }
    setTimeout(sync,0);   // 다음 틱에 읽어 iOS IME 변형이 끝난 value 를 본다
  });

  ta.addEventListener('keydown', (e)=>{
    const k=e.key;
    // 빈 필드 백스페이스는 iOS 가 input 을 안 쏨 → keydown 폴백
    if((k==='Backspace'||e.code==='Backspace') && (ta.value||'').length===0){
      sendInput('\x7f'); _confirmed=''; e.preventDefault(); return;
    }
    if(k==='Enter'){ commitEnter(); e.preventDefault(); return; }
    if((e.ctrlKey||e.metaKey) && k && k.length===1){   // 하드웨어 Ctrl/Cmd 조합
      const kl=k.toLowerCase();
      // ① 붙여넣기: Ctrl/Cmd+V → 클립보드를 읽어 PTY로(제어문자 \x16 전송 금지)
      if(kl==='v'){ e.preventDefault(); pasteClipboard(ta); return; }
      // ② 복사: 선택영역이 있으면 클립보드로 복사, 없으면 Ctrl-C 인터럽트(\x03)
      if(kl==='c'){
        const sel = (term && term.getSelection) ? term.getSelection() : '';
        if(sel){ copyToClipboard(sel); e.preventDefault(); return; }
        sendInput('\x03'); resetDiff(); e.preventDefault(); return;
      }
      const c=kl.charCodeAt(0);
      if(c>=97&&c<=122){ sendInput(String.fromCharCode(c-96)); resetDiff(); e.preventDefault(); return; }
    }
    const map={Tab:'\t',Escape:'\x1b',ArrowUp:'\x1b[A',ArrowDown:'\x1b[B',ArrowLeft:'\x1b[D',ArrowRight:'\x1b[C'};
    if(map[k]){ sendInput(map[k]); resetDiff(); e.preventDefault(); return; }
  });

  // 네이티브 붙여넣기(우클릭 메뉴 → 붙여넣기, 일부 브라우저 Ctrl+V) — 클립보드 내용 직접 전송
  ta.addEventListener('paste', (e)=>{
    try{
      const t=(e.clipboardData||window.clipboardData);
      const txt = t ? t.getData('text') : '';
      if(txt){ e.preventDefault(); resetDiff(); sendInput(txt); }
    }catch(err){}
  });

  ta.addEventListener('focus', resetDiff);
}

// 클립보드로 복사 (선택영역). 보안 컨텍스트면 navigator.clipboard, 아니면 execCommand 폴백.
async function copyToClipboard(text){
  if(!text) return false;
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      await navigator.clipboard.writeText(text); return true;
    }
  }catch(e){}
  try{
    const ta=document.createElement('textarea');
    ta.value=text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok=document.execCommand('copy'); document.body.removeChild(ta); return ok;
  }catch(e){ return false; }
}

// [폰/PC] 파일·이미지 업로드 우회 — 서버(8687)에 저장하고 그 '경로'를 터미널에 입력한다.
//   PTY는 텍스트만 흐르므로 이미지를 직접 못 보냄 → claude code/codex 등이 경로로 이미지를 읽게 한다.
//   ※ /api/upload 는 서버 재시작 후 활성화(없으면 404 → 안내만, 기존 기능 무손상).
async function uploadAndInsert(blob, filename){
  if(!blob) return;
  let ext='.bin';
  if(filename && filename.includes('.')) ext='.'+filename.split('.').pop();
  else if(blob.type && blob.type.includes('/')) ext='.'+blob.type.split('/')[1];
  showSelToast('⬆️ 업로드 중… '+(filename||''), 6000);
  try{
    const r=await fetch(API+'/api/upload?ext='+encodeURIComponent(ext), {method:'POST', body:blob});
    if(!r.ok){
      if(r.status===404) showSelToast('⚠️ 이미지 업로드는 8687 서버 재시작 후 활성화됩니다', 4000);
      else showSelToast('업로드 실패 ('+r.status+')', 3000);
      return;
    }
    const j=await r.json();
    if(j && j.ok && j.path){
      sendInput(j.path);   // 저장된 절대경로를 그대로 터미널에 입력
      showSelToast('✓ 업로드됨 · 경로 입력: '+j.path.split(/[\\/]/).pop(), 3200);
    } else showSelToast('업로드 실패: '+((j&&j.error)||'알수없음'), 3000);
  }catch(e){ showSelToast('업로드 오류 (8687 재시작 필요?)', 3500); }
}

// 모바일 특수키 줄 (Esc·Ctrl·Tab·방향키·Ctrl-C)
function setupKeys(ta){
  document.querySelectorAll('.mk').forEach(b=>{
    b.addEventListener('click', ()=>{
      const k=b.dataset.k;
      if(k==='enter'){ if(ta&&ta._commitEnter) ta._commitEnter(); else sendInput('\r'); if(ta) ta.focus(); return; }
      if(k==='paste'){ pasteClipboard(ta); return; }
      if(k==='attach'){ const fi=document.getElementById('fileInput'); if(fi) fi.click(); if(ta) ta.focus(); return; }
      if(k==='copy'){
        let sel=(term&&term.getSelection)?term.getSelection():'';
        if(!sel && term && term.selectAll && term.getSelection){ term.selectAll(); sel=term.getSelection(); }
        copyToClipboard(sel).then(ok=>{ const o=b.textContent; b.textContent=ok?'✓ 복사됨':'복사 실패'; setTimeout(()=>{b.textContent=o;},1200); });
        if(ta) ta.focus(); return;
      }
      if(k==='ctrl'){ ctrlArmed=!ctrlArmed; if(ctrlArmed){altArmed=false;setAltUI(false);} setCtrlUI(ctrlArmed); if(ta) ta.focus(); return; }
      if(k==='alt'){ altArmed=!altArmed; if(altArmed){ctrlArmed=false;setCtrlUI(false);} setAltUI(altArmed); if(ta) ta.focus(); return; }
      const map={esc:'\x1b',tab:'\t',up:'\x1b[A',down:'\x1b[B',left:'\x1b[D',right:'\x1b[C',
        home:'\x1b[H',end:'\x1b[F',pgup:'\x1b[5~',pgdn:'\x1b[6~',del:'\x1b[3~',
        ctrlc:'\x03',ctrld:'\x04',ctrlz:'\x1a',
        pipe:'|',slash:'/',tilde:'~',dash:'-'};
      if(map[k]) sendInput(map[k]);
      if(ta){ if(ta._resetDiff) ta._resetDiff(); ta.focus(); }   // 특수키 후 diff 상태 리셋(라인 변경 대비)
    });
  });
}

// 📋 붙여넣기 — 아이폰/모바일에서 Cmd+V 대용.
//  ① 보안 컨텍스트(HTTPS·localhost)면 navigator.clipboard.readText() 로 자동 붙여넣기.
//  ② 실패(iOS Safari + http LAN 접속 등 비보안 컨텍스트)하면 native prompt 로 폴백.
//     prompt 입력칸은 iOS 기본 "붙여넣기" 메뉴가 떠서, 거기에 붙여넣고 확인하면
//     그대로 터미널로 전송된다(어떤 환경에서도 동작하는 안전한 경로).
async function pasteClipboard(ta){
  let txt='';
  try{
    if(navigator.clipboard && navigator.clipboard.readText){
      txt = await navigator.clipboard.readText();
    }
  }catch(e){ txt=''; }
  if(!txt){
    // 폴백: 사용자가 직접 붙여넣을 수 있는 native 입력창
    const p = prompt('여기에 붙여넣기(길게 눌러 "붙여넣기") 후 확인:', '');
    if(p==null) return;       // 취소
    txt = p;
  }
  if(txt){
    if(ta && ta._resetDiff) ta._resetDiff();  // diff 상태 리셋(붙인 내용이 stale 안 되게)
    sendInput(txt);
    if(ta){ try{ ta.focus(); }catch(e){} }
  }
}
