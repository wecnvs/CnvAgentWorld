# -*- coding: utf-8 -*-
"""호스트측 JPEG 변환 프록시 — VM 화면을 '실시간'으로 만든다 (호스트에서 실행, Python+Pillow).

왜: VM cu_helper(PowerShell)에 JPEG 인코딩을 넣으면 VM Windows Defender AMSI가
'ScriptContainedMaliciousContent'로 차단한다(화면캡처→JPEG 패턴). 그래서 VM엔 **작동하는 PNG
헬퍼를 그대로 두고**, 호스트가 VM의 PNG를 **빠른 로컬 링크로 받아 여기서 축소+JPEG 변환**한 뒤
작은 JPEG만 느린 Tailscale로 보낸다(2.5MB PNG → ~150KB JPEG). Python 프로세스라 PowerShell
AMSI와도 무관. 기존 netsh portproxy(생 TCP 포워드)를 이 프록시로 대체한다.

사용(호스트):
  python transcode_proxy.py --map 8601=172.28.221.203 8602=172.28.210.72
  (listen 포트 = Mac 대시보드가 붙는 포트. 우측 = 그 VM의 NAT IP. VM 헬퍼는 :8599 고정.)
  /screenshot 만 PNG→JPEG(쿼리 w,q 반영) 변환, 그 외(/status·/run·/click·/move…)는 그대로 포워드.
"""
import argparse
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

VM_PORT = 8599


def _make_handler(vm_ip: str):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _handle(self, method: str):
            path = self.path
            base = path.split("?", 1)[0]
            is_shot = base.endswith("/screenshot")
            body = None
            cl = int(self.headers.get("Content-Length", 0) or 0)
            if cl:
                body = self.rfile.read(cl)
            # screenshot은 변환 인자 빼고 원본 PNG(커서 마커 포함)를 받아 호스트에서 변환
            if is_shot:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
                want_w = int((qs.get("w", ["1280"])[0]) or 1280)
                want_q = int((qs.get("q", ["70"])[0]) or 70)
                fetch = f"http://{vm_ip}:{VM_PORT}/screenshot"
            else:
                fetch = f"http://{vm_ip}:{VM_PORT}{path}"
            try:
                req = urllib.request.Request(fetch, data=body, method=method)
                ct = self.headers.get("Content-Type")
                if ct:
                    req.add_header("Content-Type", ct)
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type", "application/octet-stream")
            except Exception as e:
                self.send_error(502, str(e)[:200])
                return
            if is_shot and data[:8].startswith(b"\x89PNG"):
                try:
                    from PIL import Image
                    im = Image.open(BytesIO(data)).convert("RGB")
                    iw, ih = im.size
                    if want_w and iw > want_w:
                        im = im.resize((want_w, max(1, round(ih * want_w / iw))), Image.BILINEAR)
                    buf = BytesIO()
                    im.save(buf, "JPEG", quality=max(30, min(95, want_q)))
                    data, ctype = buf.getvalue(), "image/jpeg"
                except Exception:
                    pass  # 변환 실패 시 원본 PNG 그대로(대시보드가 폴백 변환)
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return H


def _serve(port: int, vm_ip: str):
    srv = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(vm_ip))
    print(f"[proxy] :{port} -> {vm_ip}:{VM_PORT}  (screenshot→JPEG, rest=passthrough)", flush=True)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", nargs="+", required=True,
                    help="listenPort=vmIp ... 예: 8601=172.28.221.203 8602=172.28.210.72")
    args = ap.parse_args()
    threads = []
    for m in args.map:
        port_s, ip = m.split("=", 1)
        t = threading.Thread(target=_serve, args=(int(port_s), ip), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
