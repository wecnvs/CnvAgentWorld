<#
  cu_helper.ps1 — VM 대화형 세션 안에서 도는 '컴퓨터유즈 헬퍼 데몬' (PowerShell/.NET 자립형, python 불필요)

  목적: 호스트의 에이전트가 이 VM의 '자체 화면'을 원격으로 캡처·클릭·타이핑하게 한다.
        VM마다 이 데몬을 그 VM의 대화형(콘솔) 세션에서 돌리면, 각 VM이 자기 화면을 쳐서
        여러 에이전트가 서로 다른 VM에서 '동시 병렬' CU를 충돌 없이 할 수 있다.

  ★ 반드시 대화형 데스크톱 세션(예: 자동로그온 console 세션)에서 실행해야 화면 캡처/입력이 동작한다.
    서비스/비대화형 세션에서 돌리면 검은 화면/입력 무시가 된다. (배포 시 schtasks '로그온 시' 트리거 사용)

  사용: powershell -ExecutionPolicy Bypass -File cu_helper.ps1 [-Port 8599]

  엔드포인트:
    GET  /status                      → {ok,hostname,session,screen:{w,h},cursor:{x,y}}
    GET  /screenshot[?cursor=0]       → image/png (기본: 빨강 커서마커 오버레이)
    GET  /cursor                      → {x,y}
    POST /move    {x,y}               → 커서 이동(클릭 없음)
    POST /click   {x,y,button,double} → 클릭 (button: left|right|middle)
    POST /scroll  {x,y,amount}        → 휠(+위/-아래, notch)
    POST /type    {text}              → 클립보드 경유 붙여넣기(한글/특수문자 안전)
    POST /key     {keys}              → SendKeys 문법 키조합(예 "^c","{ENTER}","%{F4}")
    POST /run     {cmd,cwd}           → 이 세션에서 프로그램 실행(GUI앱 가능) → {ok,pid}. 대시보드 앱 탭의 '실행'.
    POST /ps      {pid}               → 그 pid 생존확인 → {ok,alive}
    POST /proclist{name}              → 프로세스명(확장자 무관)으로 실행 중 인스턴스 조회 → {ok,name,procs:[{pid,title,start}]}.
                                        대시보드 실행중 자동감지(외부 기동 포함)·에이전트 PID 타깃팅용. 비밀 미포함.
    POST /stop    {pid}               → 그 pid(+자식) 종료(taskkill /T /F) → {ok}
#>
param([int]$Port = 8599)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

# ── Win32 입력/커서/디스플레이 P/Invoke ──
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CU {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  public const uint MOVE=0x0001, LDOWN=0x0002, LUP=0x0004, RDOWN=0x0008, RUP=0x0010,
                    MDOWN=0x0020, MUP=0x0040, WHEEL=0x0800, ABSOLUTE=0x8000;
  public static POINT GetPos(){ POINT p; GetCursorPos(out p); return p; }

  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)] public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
    public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
    public int dmFields, dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
    public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
    public short dmLogPixels;
    public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
    public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2,
               dmPanningWidth, dmPanningHeight;
  }
  [DllImport("user32.dll")] public static extern int EnumDisplaySettings(string dev, int mode, ref DEVMODE dm);
  [DllImport("user32.dll")] public static extern int ChangeDisplaySettings(ref DEVMODE dm, int flags);
  public const int ENUM_CURRENT=-1, CDS_UPDATEREGISTRY=0x01, CDS_TEST=0x02, DM_PELSWIDTH=0x80000, DM_PELSHEIGHT=0x100000;

  public static string SetRes(int w, int h){
    DEVMODE dm = new DEVMODE();
    dm.dmDeviceName = new string(new char[32]);
    dm.dmFormName = new string(new char[32]);
    dm.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
    if (EnumDisplaySettings(null, ENUM_CURRENT, ref dm) == 0) return "ENUM_FAIL";
    dm.dmPelsWidth = w; dm.dmPelsHeight = h;
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT;
    int r = ChangeDisplaySettings(ref dm, CDS_UPDATEREGISTRY);
    return "rc=" + r;  // 0=DISP_CHANGE_SUCCESSFUL, -2=BADMODE
  }
}
"@

function Get-CursorXY { $p = [CU]::GetPos(); return @{ x = $p.X; y = $p.Y } }

function Get-ScreenSize {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  return @{ w = $b.Width; h = $b.Height; x = $b.X; y = $b.Y }
}

function Capture-Bitmap([bool]$drawCursor = $true) {
  $s = Get-ScreenSize
  $bmp = New-Object System.Drawing.Bitmap($s.w, $s.h)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($s.x, $s.y, 0, 0, (New-Object System.Drawing.Size($s.w, $s.h)))
  if ($drawCursor) {
    $c = Get-CursorXY
    $cx = $c.x - $s.x; $cy = $c.y - $s.y
    $penR = New-Object System.Drawing.Pen([System.Drawing.Color]::Red, 2)
    $g.DrawEllipse($penR, $cx - 12, $cy - 12, 24, 24)
    $g.DrawLine($penR, $cx - 18, $cy, $cx + 18, $cy)
    $g.DrawLine($penR, $cx, $cy - 18, $cx, $cy + 18)
    $br = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::Yellow)
    $g.FillRectangle((New-Object System.Drawing.SolidBrush([System.Drawing.Color]::Black)), $cx + 14, $cy + 14, 96, 18)
    $g.DrawString("$($c.x),$($c.y)", (New-Object System.Drawing.Font("Consolas", 10)), $br, $cx + 16, $cy + 15)
    $penR.Dispose(); $br.Dispose()
  }
  $g.Dispose()
  return $bmp
}

# 비트맵 → 바이트(PNG 또는 JPEG, 선택적 가로축소). 사진배경 화면은 PNG가 5MB+라 JPEG로 30배+ 줄여 실시간에 가깝게.
function Encode-Bitmap($bmp, [string]$fmt, [int]$maxW, [int]$q) {
  $src = $bmp
  if ($maxW -gt 0 -and $bmp.Width -gt $maxW) {
    $nh = [int]($bmp.Height * $maxW / $bmp.Width)
    $resized = New-Object System.Drawing.Bitmap($maxW, $nh)
    $rg = [System.Drawing.Graphics]::FromImage($resized)
    $rg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::Bilinear
    $rg.DrawImage($bmp, 0, 0, $maxW, $nh); $rg.Dispose()
    $src = $resized
  }
  $ms = New-Object System.IO.MemoryStream
  if ($fmt -eq "jpg") {
    $jenc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.FormatID -eq [System.Drawing.Imaging.ImageFormat]::Jpeg.Guid } | Select-Object -First 1
    $eps = New-Object System.Drawing.Imaging.EncoderParameters(1)
    $eps.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [int64]$q)
    $src.Save($ms, $jenc, $eps)
  } else {
    $src.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  }
  if (-not [object]::ReferenceEquals($src, $bmp)) { $src.Dispose() }
  return $ms.ToArray()
}

function Do-Move([int]$x, [int]$y) { [CU]::SetCursorPos($x, $y) | Out-Null; Start-Sleep -Milliseconds 40 }

function Do-Click([int]$x, [int]$y, [string]$button = "left", [bool]$double = $false) {
  Do-Move $x $y
  switch ($button) {
    "right"  { $dn = [CU]::RDOWN; $up = [CU]::RUP }
    "middle" { $dn = [CU]::MDOWN; $up = [CU]::MUP }
    default  { $dn = [CU]::LDOWN; $up = [CU]::LUP }
  }
  [CU]::mouse_event($dn, 0, 0, 0, [IntPtr]::Zero); Start-Sleep -Milliseconds 30
  [CU]::mouse_event($up, 0, 0, 0, [IntPtr]::Zero)
  if ($double) {
    Start-Sleep -Milliseconds 80
    [CU]::mouse_event($dn, 0, 0, 0, [IntPtr]::Zero); Start-Sleep -Milliseconds 30
    [CU]::mouse_event($up, 0, 0, 0, [IntPtr]::Zero)
  }
}

function Do-Scroll([int]$x, [int]$y, [int]$amount) {
  if ($x -ge 0 -and $y -ge 0) { Do-Move $x $y }
  [CU]::mouse_event([CU]::WHEEL, 0, 0, [uint32]($amount * 120), [IntPtr]::Zero)
}

function Do-Type([string]$text) {
  # 클립보드 경유 붙여넣기 — 한글/특수문자 안전(SendKeys 이스케이프 회피)
  [System.Windows.Forms.Clipboard]::SetText($text)
  Start-Sleep -Milliseconds 60
  [System.Windows.Forms.SendKeys]::SendWait("^v")
}

function Do-Key([string]$keys) { [System.Windows.Forms.SendKeys]::SendWait($keys) }

# ── HTTP 서버 ──
$listener = New-Object System.Net.HttpListener
$prefix = "http://+:$Port/"
try { $listener.Prefixes.Add($prefix); $listener.Start() }
catch {
  # '+' 바인딩이 막히면(URL ACL) 모든 IPv4로 폴백
  $listener = New-Object System.Net.HttpListener
  $listener.Prefixes.Add("http://*:$Port/"); $listener.Start()
}
Write-Host "[cu_helper] listening on port $Port (session: $((query session 2>$null) -join ' '))"

function Send-Json($ctx, $obj, [int]$code = 200) {
  $json = ($obj | ConvertTo-Json -Compress -Depth 6)
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  $ctx.Response.StatusCode = $code
  $ctx.Response.ContentType = "application/json; charset=utf-8"
  $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
  $ctx.Response.OutputStream.Close()
}

function Read-Body($ctx) {
  if (-not $ctx.Request.HasEntityBody) { return @{} }
  $sr = New-Object System.IO.StreamReader($ctx.Request.InputStream, [System.Text.Encoding]::UTF8)
  $raw = $sr.ReadToEnd(); $sr.Close()
  if (-not $raw) { return @{} }
  try { return ($raw | ConvertFrom-Json) } catch { return @{} }
}

while ($listener.IsListening) {
  try {
    $ctx = $listener.GetContext()
    $path = $ctx.Request.Url.AbsolutePath.ToLower()
    $method = $ctx.Request.HttpMethod
    try {
      if ($path -eq "/status") {
        $sess = ""
        try { $sess = ((query session 2>$null | Out-String)).Trim() } catch {}
        Send-Json $ctx @{ ok = $true; hostname = $env:COMPUTERNAME; user = $env:USERNAME;
                          screen = (Get-ScreenSize); cursor = (Get-CursorXY); session = $sess }
      }
      elseif ($path -eq "/screenshot") {
        # ?cursor=0 커서마커 끔, ?fmt=jpg JPEG(빠름), ?w=1280 가로축소, ?q=70 품질
        $dc = $true
        if ($ctx.Request.QueryString["cursor"] -eq "0") { $dc = $false }
        $fmt = "" + $ctx.Request.QueryString["fmt"]; if (-not $fmt) { $fmt = "png" }
        $mw = 0; [int]::TryParse(("" + $ctx.Request.QueryString["w"]), [ref]$mw) | Out-Null
        $q = 70; if ($ctx.Request.QueryString["q"]) { [int]::TryParse(("" + $ctx.Request.QueryString["q"]), [ref]$q) | Out-Null }
        $bmp = Capture-Bitmap $dc
        $bytes = Encode-Bitmap $bmp $fmt $mw $q
        $bmp.Dispose()
        $ctx.Response.StatusCode = 200
        $ctx.Response.ContentType = if ($fmt -eq "jpg") { "image/jpeg" } else { "image/png" }
        $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $ctx.Response.OutputStream.Close()
      }
      elseif ($path -eq "/cursor") { Send-Json $ctx (Get-CursorXY) }
      elseif ($method -eq "POST" -and $path -eq "/move") {
        $b = Read-Body $ctx; Do-Move ([int]$b.x) ([int]$b.y); Send-Json $ctx @{ ok = $true; cursor = (Get-CursorXY) }
      }
      elseif ($method -eq "POST" -and $path -eq "/click") {
        $b = Read-Body $ctx
        $btn = if ($b.button) { [string]$b.button } else { "left" }
        $dbl = [bool]$b.double
        Do-Click ([int]$b.x) ([int]$b.y) $btn $dbl
        Send-Json $ctx @{ ok = $true; clicked = @{ x = [int]$b.x; y = [int]$b.y; button = $btn; double = $dbl } }
      }
      elseif ($method -eq "POST" -and $path -eq "/scroll") {
        $b = Read-Body $ctx
        $ax = if ($b.x -ne $null) { [int]$b.x } else { -1 }
        $ay = if ($b.y -ne $null) { [int]$b.y } else { -1 }
        Do-Scroll $ax $ay ([int]$b.amount); Send-Json $ctx @{ ok = $true }
      }
      elseif ($method -eq "POST" -and $path -eq "/type") {
        $b = Read-Body $ctx; Do-Type ([string]$b.text); Send-Json $ctx @{ ok = $true }
      }
      elseif ($method -eq "POST" -and $path -eq "/key") {
        $b = Read-Body $ctx; Do-Key ([string]$b.keys); Send-Json $ctx @{ ok = $true }
      }
      elseif ($method -eq "POST" -and $path -eq "/resolution") {
        $b = Read-Body $ctx
        $w = [int]$b.w; $h = [int]$b.h
        $res = [CU]::SetRes($w, $h)
        Start-Sleep -Milliseconds 400
        $now = Get-ScreenSize
        $ok = ($now.w -eq $w -and $now.h -eq $h)
        Send-Json $ctx @{ ok = $ok; requested = "$($w)x$($h)"; result = $res; screen = $now }
      }
      elseif ($method -eq "POST" -and $path -eq "/run") {
        # 이 대화형 세션에서 프로그램 실행 → 실제 pid 반환(대시보드 앱 탭 '실행'이 호출).
        # Win32_Process.Create는 전체 명령줄을 OS가 파싱하므로 따옴표 경로·인자를 그대로 처리한다.
        $b = Read-Body $ctx
        $rcmd = [string]$b.cmd
        $rcwd = [string]$b.cwd
        if (-not $rcmd) { Send-Json $ctx @{ ok = $false; error = "cmd required" } 400 }
        else {
          try {
            $cimArgs = @{ CommandLine = $rcmd }
            if ($rcwd) { $cimArgs["CurrentDirectory"] = $rcwd }
            $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $cimArgs
            if ($r.ReturnValue -eq 0 -and $r.ProcessId) {
              Send-Json $ctx @{ ok = $true; pid = [int]$r.ProcessId }
            } else {
              Send-Json $ctx @{ ok = $false; error = "Win32_Process.Create ReturnValue=$($r.ReturnValue)" }
            }
          } catch {
            Send-Json $ctx @{ ok = $false; error = "$($_.Exception.Message)" } 500
          }
        }
      }
      elseif ($method -eq "POST" -and $path -eq "/ps") {
        $b = Read-Body $ctx; $rpid = [int]$b.pid; $alive = $false
        try { if (Get-Process -Id $rpid -ErrorAction SilentlyContinue) { $alive = $true } } catch {}
        Send-Json $ctx @{ ok = $true; alive = $alive }
      }
      elseif ($method -eq "POST" -and $path -eq "/proclist") {
        # 프로세스명으로 실행 중 인스턴스 조회 → 대시보드 자동감지(외부 기동 포함)·에이전트 PID 타깃팅용.
        # name은 "Revit" 또는 "Revit.exe"(확장자 있으면 벗겨 Get-Process -Name에 사용). 비밀 미포함.
        $b = Read-Body $ctx
        $pname = ([string]$b.name) -replace '(?i)\.exe$',''
        $procs = @()
        if ($pname) {
          try {
            foreach ($p in @(Get-Process -Name $pname -ErrorAction SilentlyContinue)) {
              $st = ""
              try { $st = $p.StartTime.ToString("s") } catch {}
              $procs += @{ pid = [int]$p.Id; title = "$($p.MainWindowTitle)"; start = $st }
            }
          } catch {}
        }
        Send-Json $ctx @{ ok = $true; name = $pname; procs = @($procs) }
      }
      elseif ($method -eq "POST" -and $path -eq "/stop") {
        $b = Read-Body $ctx; $rpid = [int]$b.pid
        try { & taskkill /PID $rpid /T /F 2>$null | Out-Null } catch {}
        Send-Json $ctx @{ ok = $true }
      }
      else { Send-Json $ctx @{ ok = $false; error = "unknown endpoint $method $path" } 404 }
    } catch {
      Send-Json $ctx @{ ok = $false; error = "$($_.Exception.Message)" } 500
    }
  } catch {
    Start-Sleep -Milliseconds 100
  }
}
