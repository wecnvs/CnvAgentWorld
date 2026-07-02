<#
  recover_vm.ps1 — 도윤 Windows '호스트에서' 실행. Hyper-V 게스트 VM의 cu_helper를
  안전하게 (재)설치·복구한다. 2026-06-30 BB-Win11-2 복구에서 검증한 절차를 정본화.

  왜 이 순서인가(교훈, [[vm-recovery-playbook]] / 스킬 가상윈도우-VM사용 케이스):
    - 강제재부팅(Restart-VM -Force) 남발은 게스트 통합서비스(IC)/VMBus를 깨 Copy-VMFile(0x800710DF)·
      PowerShell Direct를 둘 다 먹통으로 만든다 → 절대 먼저 쓰지 마라.
    - IC가 죽어 보이면 '재부팅'이 아니라 'IC 토글'(Disable→Enable)로 재핸드셰이크 → 재부팅 0회 복구.
    - PSDirect가 멈추면 -Credential 누락(비대화형 자격 프롬프트 무한대기)을 먼저 의심.
    - 헬퍼는 설치 전 호스트에서 ParseFile로 구문 선검증(과거 JPEG판 구문오류가 VM을 깨뜨린 전력).

  사용(맥/관리자 쪽 래퍼가 scp로 이 스크립트+헬퍼+cred를 호스트 ASCII 임시경로에 올린 뒤):
    powershell -NoProfile -ExecutionPolicy Bypass -File recover_vm.ps1 `
       -VMName BB-Win11-2 -CredFile C:\Users\Public\bbc.json -HelperSrc C:\Users\Public\cu_helper.ps1 `
       -Port 8599 -HostTail REDACTED_HOST_TAIL -ProxyPort 8602
  CredFile = vm_credentials.json 형식({accounts:[{user|username|name, password|pass|pwd}]}).
  실행 후 호출자가 CredFile(대외비)을 반드시 삭제한다.
#>
param(
  [Parameter(Mandatory=$true)][string]$VMName,
  [Parameter(Mandatory=$true)][string]$CredFile,
  [Parameter(Mandatory=$true)][string]$HelperSrc,
  [string]$User='brainbase',
  [int]$Port=8599,
  [string]$HostTail='',
  [int]$ProxyPort=0
)
$ErrorActionPreference='Continue'

# 0) 헬퍼 구문 선검증(설치 전 — 깨진 헬퍼 배포 방지)
$perr=$null
[void][System.Management.Automation.Language.Parser]::ParseFile($HelperSrc,[ref]$null,[ref]$perr)
if($perr -and $perr.Count -gt 0){ Write-Output ("ABORT: HelperSrc 구문오류 "+$perr.Count+"건 — 설치 안 함. 첫 오류: "+$perr[0].Message); return }
Write-Output "helper ParseFile OK"

# 1) 자격 로드
$j = Get-Content $CredFile -Raw -Encoding UTF8 | ConvertFrom-Json
$acct = $j.accounts | Where-Object { ($_.username -eq $User) -or ($_.user -eq $User) -or ($_.name -eq $User) } | Select-Object -First 1
if(-not $acct){ Write-Output ("ABORT: 계정 못 찾음: "+$User); return }
$pw=$acct.password; if(-not $pw){$pw=$acct.pass}; if(-not $pw){$pw=$acct.pwd}
if(-not $pw){ Write-Output "ABORT: 비번 필드 없음"; return }
function Cred($u,$p){ New-Object System.Management.Automation.PSCredential($u,(ConvertTo-SecureString $p -AsPlainText -Force)) }
function PSDirect-OK($vm,$u,$p,$t){
  $job=Start-Job -ScriptBlock { param($vm,$u,$p)
    $c=New-Object System.Management.Automation.PSCredential($u,(ConvertTo-SecureString $p -AsPlainText -Force))
    Invoke-Command -VMName $vm -Credential $c -ScriptBlock { $env:COMPUTERNAME } -EA Stop } -ArgumentList $vm,$u,$p
  $ok=$false; if(Wait-Job $job -Timeout $t){ if(Receive-Job $job -EA SilentlyContinue){ $ok=$true } } else { Stop-Job $job -EA SilentlyContinue }
  Remove-Job $job -Force -EA SilentlyContinue; return $ok
}

# 2) PSDirect 확보 — 안 되면 IC 토글(재부팅 X), 그래도 안 되면 graceful 재부팅
$ok = PSDirect-OK $VMName $User $pw 30
Write-Output ("PSDirect初: "+$ok)
if(-not $ok){
  Write-Output "IC 토글(재부팅 없이)…"
  Get-VMIntegrationService -VMName $VMName | ForEach-Object { Disable-VMIntegrationService -VMName $VMName -Name $_.Name -EA SilentlyContinue }
  Start-Sleep 3
  Get-VMIntegrationService -VMName $VMName | ForEach-Object { Enable-VMIntegrationService -VMName $VMName -Name $_.Name -EA SilentlyContinue }
  Start-Sleep 8
  $ok = PSDirect-OK $VMName $User $pw 35
  Write-Output ("PSDirect after toggle: "+$ok)
}
if(-not $ok){
  Write-Output "graceful 재부팅 후 재시도(강제 -Force는 최후수단)…"
  try { Stop-VM -Name $VMName -Confirm:$false -EA Stop } catch { try { Stop-VM -Name $VMName -Force -Confirm:$false -EA Stop } catch {} }
  $i=0; while(((Get-VM $VMName).State -ne 'Off') -and $i -lt 30){ Start-Sleep 2; $i++ }
  Start-VM -Name $VMName -EA SilentlyContinue
  for($i=0;$i -lt 16;$i++){ Start-Sleep 10; if(PSDirect-OK $VMName $User $pw 25){ $ok=$true; break } }
  Write-Output ("PSDirect after reboot: "+$ok)
}
if(-not $ok){ Write-Output "RESULT: PSDirect 복구 실패 — 콘솔 점검 필요"; return }

# 3) PSDirect로 정상헬퍼 설치 + CUHelper 작업 재등록·기동
$content=[IO.File]::ReadAllText($HelperSrc)
$r = Invoke-Command -VMName $VMName -Credential (Cred $User $pw) -ArgumentList $Port,$User,$pw,$content -ScriptBlock {
  param($Port,$User,$Pw,$Content)
  New-Item -ItemType Directory -Force -Path 'C:\cu_helper' | Out-Null
  Set-Content -Path 'C:\cu_helper\cu_helper.ps1' -Value $Content -Encoding UTF8
  Remove-NetFirewallRule -DisplayName "CU Helper $Port" -EA SilentlyContinue
  New-NetFirewallRule -DisplayName "CU Helper $Port" -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow -Profile Any | Out-Null
  $rk="HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
  Set-ItemProperty $rk -Name AutoAdminLogon -Value "1" -Type String
  Set-ItemProperty $rk -Name DefaultUserName -Value $User -Type String
  Set-ItemProperty $rk -Name DefaultPassword -Value $Pw -Type String
  Stop-ScheduledTask -TaskName CUHelper -EA SilentlyContinue
  Unregister-ScheduledTask -TaskName CUHelper -Confirm:$false -EA SilentlyContinue
  $a=New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File C:\cu_helper\cu_helper.ps1 -Port $Port"
  $p=New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
  $t=New-ScheduledTaskTrigger -AtLogOn -User $User
  Register-ScheduledTask -TaskName CUHelper -Action $a -Principal $p -Trigger $t -Force | Out-Null
  Get-CimInstance Win32_Process -EA SilentlyContinue | Where-Object { $_.CommandLine -match 'cu_helper.ps1' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
  Start-Sleep 2
  Start-ScheduledTask -TaskName CUHelper
  $listen=$false; for($k=0;$k -lt 10;$k++){ Start-Sleep 3; $listen=(Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue).TcpTestSucceeded; if($listen){break} }
  $ip=(((Get-NetIPAddress -AddressFamily IPv4 -EA SilentlyContinue) | Where-Object {$_.IPAddress -notmatch '^127\.'}).IPAddress -join ',')
  return @{ listen=$listen; ip=$ip; host=$env:COMPUTERNAME }
}
Write-Output ("install: listen="+$r.listen+" ip="+$r.ip+" host="+$r.host)

# 4) (선택) 호스트 포트프록시 갱신 — VM NAT IP가 바뀌었을 때
if($HostTail -and $ProxyPort -gt 0){
  $vmip=($r.ip -split ',' | Where-Object { $_ -match '^172\.' } | Select-Object -First 1)
  if($vmip){
    cmd /c "netsh interface portproxy delete v4tov4 listenport=$ProxyPort listenaddress=$HostTail" 2>$null | Out-Null
    cmd /c "netsh interface portproxy add v4tov4 listenport=$ProxyPort listenaddress=$HostTail connectport=$Port connectaddress=$vmip" | Out-Null
    Write-Output ("portproxy "+$ProxyPort+" -> "+$vmip+":"+$Port+" 갱신")
  }
}
if($r.listen){ Write-Output "RESULT: VM HELPER OK" } else { Write-Output "RESULT: 설치됐으나 미LISTEN(콘솔세션 확인)" }
