#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vm_cu.py — 호스트 에이전트가 '특정 VM의 자체 화면'을 원격 CU하는 클라이언트.

각 VM 안에서 도는 cu_helper(도구/기본/vm-cu-helper/cu_helper.ps1)에 HTTP로 명령을 보낸다.
서로 다른 VM(target)을 두 에이전트가 동시에 조작해도 충돌하지 않는다(각 VM 자체 화면).

★ CU 시작 전 반드시 CU 락(target=VM명)을 잡아라:  run_tool.bat cu_lock.py acquire <agent_id> --target <vm> --note "..."
   → 락은 per-VM이라 다른 VM은 병렬, 같은 VM만 직렬.

사용법:
  python vm_cu.py <target> status
  python vm_cu.py <target> screenshot [out.png]      # 기본: TEMP/vm_<target>_screen.png (커서마커 포함). 경로를 출력 → Read로 본다.
  python vm_cu.py <target> cursor
  python vm_cu.py <target> move  <x> <y>
  python vm_cu.py <target> click <x> <y> [--right|--middle] [--double]
  python vm_cu.py <target> scroll <x> <y> <amount>   # +위 / -아래
  python vm_cu.py <target> type  "<텍스트>"
  python vm_cu.py <target> key   "<SendKeys>"         # 예: "^c" "{ENTER}" "%{F4}"

  <target> = vms.json 의 name(예: bb-win11) 또는 직접 host:port.

★ 안전클릭 프로토콜(절대원칙 1 동일): click 전에 반드시 move → screenshot 으로 커서 위치를 눈으로 확인 후 click.
"""
import sys, os, json, argparse, urllib.request, urllib.error
from pathlib import Path

def _registry_path():
    # 이 파일: 루트/도구/기본/cu-win/vm_cu.py. 위로 올라가며 '도구' 폴더를 가진 워크스페이스 루트를 찾고,
    # 그 아래 vm-cu-helper 레지스트리를 우선순위대로 탐색한다(절대경로 하드코딩 없음).
    #   1) 도구/대외비/vm-cu-helper/vms.json  ← 실제 host/port·자격증명(대외비, 깃 미추적)
    #   2) 도구/기본/vm-cu-helper/vms.json     ← 운영자가 채워둔 실제 레지스트리(있으면)
    #   3) 도구/기본/vm-cu-helper/vms.template.json ← 공개 템플릿(placeholder, 실제 연결 X)
    # 실제 비밀값은 1)에 두고 발견기로 참조한다(law.md §7 보안 불변식). 2)·3)은 공개 경로.
    here = Path(__file__).resolve()
    rels = [
        ("도구", "대외비", "vm-cu-helper", "vms.json"),
        ("도구", "기본", "vm-cu-helper", "vms.json"),
        ("도구", "기본", "vm-cu-helper", "vms.template.json"),
    ]
    for root in here.parents:
        if (root / "도구").is_dir():
            for rel in rels:
                cand = root.joinpath(*rel)
                if cand.exists():
                    return cand
    return None

def resolve_base(target):
    rp = _registry_path()
    if rp:
        try:
            reg = json.load(open(rp, encoding="utf-8"))
            for v in reg.get("vms", []):
                if str(v.get("name", "")).lower() == target.lower():
                    return "http://%s:%s" % (v["host"], v["port"])
        except Exception:
            pass
    if target.startswith("http"):
        return target.rstrip("/")
    if ":" in target:
        return "http://" + target
    return None

def call(base, path, body=None, raw=False, timeout=12):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if raw else json.loads(r.read().decode("utf-8"))

def main():
    if len(sys.argv) < 3:
        print(__doc__); return 1
    target = sys.argv[1]; cmd = sys.argv[2]; rest = sys.argv[3:]
    base = resolve_base(target)
    if not base:
        print("ERROR: target '%s' 을 vms.json에서 못 찾음(또는 host:port 형식 아님)" % target); return 1
    try:
        if cmd == "status":
            print(json.dumps(call(base, "/status"), ensure_ascii=False))
        elif cmd == "cursor":
            print(json.dumps(call(base, "/cursor"), ensure_ascii=False))
        elif cmd == "screenshot":
            out = rest[0] if rest else os.path.join(os.environ.get("TEMP", os.environ.get("TMP", ".")),
                                                    "vm_%s_screen.png" % target.replace(":", "_"))
            png = call(base, "/screenshot", raw=True)
            with open(out, "wb") as f:
                f.write(png)
            print("SCREENSHOT_SAVED: %s (%d bytes) — Read 도구로 이 이미지를 봐라" % (out, len(png)))
        elif cmd == "move":
            x, y = int(rest[0]), int(rest[1])
            print(json.dumps(call(base, "/move", {"x": x, "y": y}), ensure_ascii=False))
        elif cmd == "click":
            x, y = int(rest[0]), int(rest[1])
            btn = "right" if "--right" in rest else ("middle" if "--middle" in rest else "left")
            dbl = "--double" in rest
            print(json.dumps(call(base, "/click", {"x": x, "y": y, "button": btn, "double": dbl}), ensure_ascii=False))
        elif cmd == "scroll":
            x, y, amt = int(rest[0]), int(rest[1]), int(rest[2])
            print(json.dumps(call(base, "/scroll", {"x": x, "y": y, "amount": amt}), ensure_ascii=False))
        elif cmd == "type":
            print(json.dumps(call(base, "/type", {"text": rest[0]}), ensure_ascii=False))
        elif cmd == "key":
            print(json.dumps(call(base, "/key", {"keys": rest[0]}), ensure_ascii=False))
        elif cmd == "resolution":
            w, h = int(rest[0]), int(rest[1])
            print(json.dumps(call(base, "/resolution", {"w": w, "h": h}), ensure_ascii=False))
        else:
            print("unknown cmd: %s" % cmd); return 1
    except urllib.error.URLError as e:
        print("ERROR: VM 헬퍼 접속 실패(%s) — 헬퍼가 그 VM에서 도는지 확인: %s" % (base, e)); return 2
    return 0

if __name__ == "__main__":
    sys.exit(main())
