# CnvAgentWorld 대시보드 서버 실행 (Windows PowerShell) — POSIX run.sh의 대응.
#  기본 127.0.0.1:8686 (외부 노출은 tailscale serve 권장). 환경변수 HOST/PORT로 변경.
#  용법:  powershell -ExecutionPolicy Bypass -File run.ps1
#  ※ 이 맥 개발환경에선 미검증 — 실제 Windows에서 1회 확인 필요(파이썬 런처 py -3 / python).
$ErrorActionPreference = "Stop"
$BindHost = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$BindPort = if ($env:PORT) { $env:PORT } else { "8686" }
Set-Location -Path $PSScriptRoot      # 시스템/대시보드/서버 (app.py가 있는 곳)
if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3 -m uvicorn app:app --host $BindHost --port $BindPort @args
} else {
  & python -m uvicorn app:app --host $BindHost --port $BindPort @args
}
