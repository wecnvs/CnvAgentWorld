#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""컴퓨터유즈 전역 락 헬퍼 — 한 머신(물리 화면 하나)에서 동시에 1개 에이전트만 GUI 자동화하도록 직렬화.
여러 에이전트가 같은 화면/마우스/키보드를 동시에 조작하면 충돌하므로, CU 시작 전 반드시 락을 잡고,
긴 작업 중엔 heartbeat로 연장하고, 끝나면 반드시 release 한다.

사용법:
  python cu_lock.py acquire <agent_id> [--note "작업설명"] [--ttl 600]
      → 획득 성공: 종료코드 0. 다른 에이전트 사용중: 종료코드 2 (보유자/만료시간 출력) → 기다렸다 재시도.
  python cu_lock.py heartbeat <agent_id> [--ttl 600]   # 긴 작업 중 주기적으로(임대시간 절반쯤마다)
  python cu_lock.py release <agent_id>                 # 작업 끝나면 반드시
  python cu_lock.py status                             # 누가 CU 중인지 확인

서버: http://127.0.0.1:8585 (대시보드). 표준 라이브러리만 사용 → win/mac 공통.
"""
import sys, json, argparse, urllib.request, urllib.error

BASE = "http://127.0.0.1:8585"

def _call(path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["acquire", "heartbeat", "release", "status"])
    ap.add_argument("agent_id", nargs="?", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--ttl", type=int, default=600)
    ap.add_argument("--target", default="host", help="화면 단위(기본 host). VM이면 vms.json의 이름 예: bb-win11")
    a = ap.parse_args()

    if a.cmd == "status":
        code, d = _call("/api/cu/status")
        print(json.dumps(d, ensure_ascii=False))
        return 0

    if not a.agent_id:
        print("ERROR: agent_id 필요 (너의 에이전트 id)"); return 1

    if a.cmd == "acquire":
        code, d = _call("/api/cu/acquire", {"agent_id": a.agent_id, "note": a.note, "ttl": a.ttl, "target": a.target})
        if code == 200 and d.get("acquired"):
            print("✅ CU 락 획득 — 이제 컴퓨터유즈 시작. 끝나면 반드시 release.")
            print(json.dumps(d, ensure_ascii=False))
            return 0
        print("⛔ CU 락 획득 실패 — 다른 에이전트가 사용 중이거나 오류. 기다렸다 재시도하세요.")
        print(json.dumps(d, ensure_ascii=False))
        return 2
    if a.cmd == "heartbeat":
        code, d = _call("/api/cu/heartbeat", {"agent_id": a.agent_id, "ttl": a.ttl, "target": a.target})
        print(json.dumps(d, ensure_ascii=False)); return 0 if d.get("ok") else 2
    if a.cmd == "release":
        code, d = _call("/api/cu/release", {"agent_id": a.agent_id, "target": a.target})
        print("✅ CU 락 해제" if d.get("ok") else json.dumps(d, ensure_ascii=False)); return 0

if __name__ == "__main__":
    sys.exit(main())
