#!/bin/sh
# 대시보드 수정 회귀 체크 — 대시보드 프론트(웹/js·css·html)나 서버를 고칠 때마다 '완료 보고 전' 실행한다.
# 대표 지시(2026-07-02): "체크리스트로 대시보드를 수정할 때마다 이런 내용들이 제대로 오류 없이 도는지 체크."
# 절대경로 하드코딩 없음 — 스크립트 위치 기준으로 루트폴더를 잡는다(워크스페이스 이전 가능).
set -u
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)   # 도구/기본/대시보드체크 → 루트폴더
cd "$ROOT" || exit 2
fail=0

echo "▶ [1/3] JS 문법 검사 (웹/js/*.js)"
if command -v node >/dev/null 2>&1; then
  for f in 시스템/대시보드/웹/js/*.js; do
    node --check "$f" || { echo "  ✗ 문법오류: $f"; fail=1; }
  done
  [ $fail -eq 0 ] && echo "  ✓ 모든 JS 문법 OK"
else
  echo "  ⚠ node 없음 — JS 문법 검사 생략(설치 권장)"
fi

echo "▶ [2/3] 정적 테스트 (캐시버전 단일 일관성·필수 구조·API 경로)"
python3 -m pytest 시스템/tests/test_dashboard_static.py -q || fail=1

echo "▶ [3/3] 모바일 실브라우저 테스트 (진입·콤보박스 공간전환·스플릿바 탭·터치·스크롤·전송)"
if python3 -c "import playwright" >/dev/null 2>&1; then
  CNV_DASHBOARD_PLAYWRIGHT=1 python3 -m pytest 시스템/tests/test_dashboard_mobile_playwright.py -q || fail=1
else
  echo "  ⚠ playwright 없음 — 모바일 테스트 생략. 실기기/시뮬레이터로 수동 확인 필수(터치·렌더)."
fi

echo ""
if [ $fail -eq 0 ]; then
  echo "✅ 대시보드 체크 통과 — 완료 보고 가능"
else
  echo "❌ 대시보드 체크 실패 — 위 오류를 해결한 뒤 다시 돌리고 통과해야 완료다"
fi
exit $fail
