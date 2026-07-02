# 대시보드 서버(8686) 수동 제어 (Windows PowerShell) — POSIX ctl.sh의 대응.
#  용법:  powershell -ExecutionPolicy Bypass -File ctl.ps1 [start|stop|restart|status]
#  ※ 이 맥 개발환경에선 미검증 — 실제 Windows에서 1회 확인 필요.
param([string]$Action = "status")
$ErrorActionPreference = "SilentlyContinue"
$Here = $PSScriptRoot
$BindPort = if ($env:PORT) { [int]$env:PORT } else { 8686 }
$RunDir = Join-Path (Split-Path $Here -Parent) ".run"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$Log = Join-Path $RunDir "server.log"
$ErrLog = Join-Path $RunDir "server.err.log"

function Get-PortPids {
  try { return @((Get-NetTCPConnection -LocalPort $BindPort -State Listen).OwningProcess | Sort-Object -Unique) }
  catch { return @() }
}
function Test-Up { (Get-PortPids).Count -gt 0 }

switch ($Action) {
  "start" {
    if (Test-Up) { "[8686] 이미 실행 중 (pid $((Get-PortPids) -join ' '))"; break }
    Start-Process -FilePath "powershell" -WindowStyle Hidden `
      -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $Here "run.ps1")) `
      -RedirectStandardOutput $Log -RedirectStandardError $ErrLog | Out-Null
    for ($i = 0; $i -lt 8 -and -not (Test-Up); $i++) { Start-Sleep 1 }
    if (Test-Up) { "[8686] 시작됨 (pid $((Get-PortPids) -join ' '), :$BindPort)" } else { "[8686] 시작 실패 — $Log 확인" }
  }
  "stop" {
    if (-not (Test-Up)) { "[8686] 실행 중 아님"; break }
    foreach ($p in Get-PortPids) { Stop-Process -Id $p -Force }
    "[8686] 중지됨"
  }
  "restart" { & $PSCommandPath stop; Start-Sleep 1; & $PSCommandPath start }
  default {
    if (Test-Up) { "[8686] up   pid $((Get-PortPids) -join ' ')  :$BindPort" } else { "[8686] down  :$BindPort" }
  }
}
