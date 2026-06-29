# 터미널 서버(:8687) 자동시작

macOS LaunchAgent로 터미널 서버를 **부팅/로그인 시 자동 기동**하고 **죽으면 자동 재시작**한다.

## 설치
```sh
sh 시스템/대시보드/터미널/자동시작/install_mac.sh
```
- `~/Library/LaunchAgents/com.cnvagentworld.terminal.plist` 생성·load
- `RunAtLoad=true`(로그인/부팅 기동) + `KeepAlive=true`(죽으면 재시작)
- python3 절대경로·작업폴더는 스크립트 위치 기준 자동 계산

## 제거
```sh
sh 시스템/대시보드/터미널/자동시작/install_mac.sh --uninstall
```

## 주의
- 설치 후에는 **수동 `run.sh`로 또 띄우지 말 것**(포트 충돌). 관리·재시작은 launchctl로:
  - 멈춤:  `launchctl unload ~/Library/LaunchAgents/com.cnvagentworld.terminal.plist`
  - 시작:  `launchctl load   ~/Library/LaunchAgents/com.cnvagentworld.terminal.plist`
- 코드 수정 후 반영: `launchctl kickstart -k gui/$(id -u)/com.cnvagentworld.terminal` (또는 프로세스 kill → KeepAlive가 새 코드로 재기동)
- 로그: 이 폴더의 `autostart.log` / `autostart.err`
