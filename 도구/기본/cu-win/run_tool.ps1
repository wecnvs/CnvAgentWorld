# computer-use-win 도구 실행 래퍼 (PowerShell)
# 사용법: .\run_tool.ps1 screenshot.py
#         .\run_tool.ps1 click.py 960 540
#         .\run_tool.ps1 type_text.py "안녕하세요"
param(
    [Parameter(Mandatory=$true, Position=0)][string]$Script,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$ScriptArgs
)

$ScriptPath = Join-Path $PSScriptRoot $Script
& python $ScriptPath @ScriptArgs
