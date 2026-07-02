---
skill_id: skill_apprun_launch01
name: 앱실행
description: "대시보드 앱 탭에 등록된 '우리 앱'(웹앱·외부프로그램·원격 윈도우/VM 앱, 예: 원격 desktop의 Revit)을 실행·중지하는 방법. 대표가 「우리 앱 중 revit 실행해줘 / (앱탭의) 그 앱 띄워줘 / 메모장 실행 / 블렌더 켜줘 / 앱 실행해」라고 하면, 컴퓨터유즈로 화면을 바로 더듬지 말고 **등록된 실행경로(run_app)** 로 띄운다 — 그래야 그 앱의 target(서버 호스트/원격 cu-helper/SSH/VM)에서 매니페스트의 run이 실행되고, pidfile이 기록돼 **대시보드 앱탭에 '● 실행 중'으로 뜨고 중지(stop)도 추적**된다. 띄운 뒤 그 앱의 GUI를 조작해야 하면 그때 computer-use(win/mac/charter)로 조작한다. 핵심 용어: 우리 앱 실행, 앱 실행, 앱 띄우기, 앱탭 실행 버튼, run_app, 실행 상태, 원격 앱 실행, Revit 실행, cu-helper run, 앱 중지."
version: 1
last_updated: 2026-06-30T15:40:00
---

# 앱실행 — 등록된 '우리 앱'을 제대로 띄운다(추적·대시보드 반영)

## 언제 쓰나
대표가 **"우리 앱 중 X 실행해줘 / 앱탭의 그 앱 띄워 / revit 실행 / 메모장 켜줘 / 앱 실행"** 처럼, **대시보드 앱 탭에 등록된 앱**을 실행/중지하라고 할 때.
("우리 앱" = 대시보드 앱 탭의 앱들 = 루트 `앱/<등급>/<이름>/앱.md`. 이건 전역 지식이다.)

## 핵심 원칙 (이걸 어기면 대시보드가 현실을 모른다)
- **컴퓨터유즈로 화면을 바로 더듬어 '실행'하지 마라.** 그러면 대시보드는 그 앱이 떠 있는 줄 모른다(앱탭 '실행 중' 안 뜸, 중지도 못 함).
- **반드시 등록된 실행경로(run_app)로 띄운다.** run_app은 앱.md의 `run`을 그 앱의 **target**(서버 호스트=로컬 / 원격 윈도우·VM=cu-helper / SSH·Parallels)에서 실행하고, 받은 pid를 **pidfile에 기록** → 앱탭에 **● 실행 중**으로 뜨고, **중지(stop)도 같은 경로로 추적**된다.
- 띄운 **뒤에**, 그 앱의 GUI를 조작(클릭·타이핑·새 프로젝트 만들기 등)해야 하면 그때 **computer-use**(원격 윈도우=computer-use-win + cu-helper, 맥=computer-use-mac, 공통 헌장=computer-use-charter)로 조작한다. 발견기로 찾아 함께 적용.

## 1단계 — 어떤 앱인지 찾기
- 대표가 말한 이름으로 앱 레지스트리에서 찾는다. 목록: `GET /api/apps` (또는 루트 `앱/` 아래 `앱.md`들에서 name 매칭).
- 그 앱의 **폴더 경로**(루트 기준, 예: `앱/대외비/원격레빗`)를 확보한다. run_app은 이 경로(dir)로 호출한다.
- 매니페스트(`앱.md`)의 `kind`/`target`/`run`을 확인한다. `revit-addin`·`install-only`는 실행 대상이 아니라 다운로드만.

## 2단계 — 등록된 경로로 실행
대시보드와 **같은 경로**로 띄운다(둘 중 하나):
```
# (권장) 대시보드 서버 API — 서버가 띄우고 pidfile 기록·앱탭 반영
curl -s -X POST http://127.0.0.1:8686/api/apps/run -H "Content-Type: application/json" \
  -d '{"dir":"앱/대외비/원격레빗"}'
# (대안) 직접 호출
python3 -c "import sys; sys.path.insert(0,'시스템'); from core import apps; print(apps.run_app('앱/대외비/원격레빗'))"
```
- 응답에 `running:true`+`pid`가 오면 떴다. `already:true`면 **이미 실행 중**이니 중복 실행하지 말고 그대로 조작 단계로 간다.
- 원격(cu-helper) 앱이면 이 호출이 그 윈도우 콘솔에서 프로그램을 띄운다(자격·주소는 대외비에서 시스템이 읽음 — 평문 노출 금지).
- 실패하면(target 미등록·run 없음·헬퍼 미응답 등) 추측 말고 사유를 보고한다.

## 3단계 — (필요 시) GUI 조작
- 앱을 띄운 뒤 그 안에서 뭔가 해야 하면(예: Revit에서 새 프로젝트 생성) computer-use로 조작한다. 원격 윈도우면 cu-helper로 스크린샷→판단→클릭/타이핑(computer-use-win·charter, CU 락 필수).
- 조작은 '실행'과 별개 단계다. 실행은 run_app, 조작은 computer-use.

## 중지
```
curl -s -X POST http://127.0.0.1:8686/api/apps/stop -H "Content-Type: application/json" -d '{"dir":"앱/대외비/원격레빗"}'
```

## 검증 (이 스킬이 제대로 됐다고 보려면)
- `GET /api/apps`에서 그 앱의 `running:true`(앱탭에 ● 실행 중). 즉 **대시보드가 실행 상태를 안다.**
- (조작까지 했으면) 화면 캡처로 결과 증거.

## 흔한 실수 (이번 실증에서 실제로 난 것)
- "revit 실행"인데 run_app 안 거치고 vm_cu.py로 **바로 CU**해서 조작 → 작업은 됐지만 **대시보드엔 실행 안 된 걸로** 보였다(pidfile 없음). → 반드시 run_app로 띄운 뒤 조작.
- 이미 떠 있는데 또 run_app → run_app이 `already:true`로 막아주지만, 호출 전 상태(`GET /api/apps`)를 확인하면 더 깔끔.
