const API = '';            // 동일 출처(8687)
let term=null, fit=null, ws=null, curId=null, sessions=[];
let reconnectTimer=null;
let vp=null;               // xterm viewport (스크롤바용)
let serverOS='Unknown';
// 터치 기기 감지 (모바일 입력바 / IME 처리 분기)
const isTouch = (window.matchMedia && window.matchMedia('(pointer:coarse)').matches) || ('ontouchstart' in window);
// 모바일 입력 커밋 dedup
let mLastCommit='', mLastAt=0, ctrlArmed=false, altArmed=false;
// [3.2 개선] 글자 크기·테마는 localStorage 에 영속(기기별 기억)
let fontSize = parseInt(localStorage.getItem('cnv_term_font')||'13',10);
if(!(fontSize>=9 && fontSize<=26)) fontSize=13;
let themeOverride = localStorage.getItem('cnv_term_theme')||'';   // ''=서버OS자동 / 'light' / 'dark'

// 글자 크기 증감(+localStorage 저장 후 재-fit·resize 전파)
function bumpFont(d){
  fontSize=Math.max(9,Math.min(26,fontSize+d));
  localStorage.setItem('cnv_term_font',String(fontSize));
  if(term){ try{ term.options.fontSize=fontSize; }catch(e){} requestAnimationFrame(doFit); }
  showSelToast('글자 크기 '+fontSize+'px', 900);
}
// 라이트/다크 수동 전환(서버OS 자동값을 덮어씀)
function toggleTheme(){
  const cur=preferredThemeName();
  themeOverride = (cur==='light')?'dark':'light';
  localStorage.setItem('cnv_term_theme',themeOverride);
  applyThemeNow();
  showSelToast(themeOverride==='dark'?'🌙 다크 테마':'☀️ 라이트 테마', 900);
}

function wsBase(){
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return proto + '//' + location.host;
}

function isMobileWidth(){ return window.innerWidth <= 760; }

const TERM_THEMES = {
  light: {
    background:'#ffffff', foreground:'#1a1a1e', cursor:'#7c6aef',
    cursorAccent:'#ffffff', selectionBackground:'rgba(124,106,239,.30)',
    // ★흰 배경 대비 강화: 연한 회색(brightBlack)·노랑 계열을 진하게 → 안 보이던 글자가 보이게
    black:'#1a1a1e', red:'#b91c1c', green:'#0a7f5e', yellow:'#8a6500',
    blue:'#1d4ed8', magenta:'#8b21d4', cyan:'#0e6d85', white:'#3f3f46',
    brightBlack:'#52525b', brightRed:'#c62828', brightGreen:'#0a8f6a', brightYellow:'#a85a00',
    brightBlue:'#2563eb', brightMagenta:'#9333ea', brightCyan:'#0e7490', brightWhite:'#27272b'
  },
  dark: {
    background:'#0e0e10', foreground:'#ececf1', cursor:'#9585f5',
    cursorAccent:'#0e0e10', selectionBackground:'rgba(124,106,239,.40)',
    // ★어두운 배경 대비: black 이 배경(#0e0e10)과 똑같아 검정 글자가 안 보이던 것 → 분리
    black:'#45454f', red:'#ef4444', green:'#10a37f', yellow:'#e5a200',
    blue:'#3b82f6', magenta:'#a855f7', cyan:'#06b6d4', white:'#d4d4d8',
    brightBlack:'#9a9aae', brightRed:'#f87171', brightGreen:'#34d399', brightYellow:'#fbbf24',
    brightBlue:'#60a5fa', brightMagenta:'#c084fc', brightCyan:'#22d3ee', brightWhite:'#ffffff'
  }
};

function preferredThemeName(){
  if(themeOverride==='light' || themeOverride==='dark') return themeOverride;   // 수동 선택 우선
  return String(serverOS).toLowerCase().startsWith('win') ? 'dark' : 'light';
}

function terminalTheme(){
  return TERM_THEMES[preferredThemeName()];
}

// 현재 유효 테마(서버OS 자동 또는 수동 override)를 본문 chrome+터미널에 적용
function applyThemeNow(){
  const name = preferredThemeName();
  document.body.classList.toggle('os-windows', name==='dark');
  document.body.classList.toggle('os-mac', name==='light');
  if(term){
    try { term.options.theme = terminalTheme(); } catch(e) {}
    try { term.options.minimumContrastRatio = 7; } catch(e) {}   // 테마 전환 후에도 대비 보정 유지
    try { term.refresh(0, term.rows - 1); } catch(e) {}
  }
  const tb=document.getElementById('themeBtn');
  if(tb) tb.textContent = (name==='light') ? '🌙' : '☀️';        // 누르면 갈 방향을 아이콘으로 암시
}

function applyOsTheme(osName){
  serverOS = osName || serverOS || 'Unknown';
  applyThemeNow();
}

async function loadServerInfo(){
  try{
    const r = await fetch(API + '/health', {cache:'no-store'});
    const j = await r.json();
    applyOsTheme(j.os || '');
  }catch(e){
    applyOsTheme('');
  }
}
