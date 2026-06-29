#!/opt/homebrew/bin/python3.13
"""웹앱 요소 정확 좌표 찾기 — Chrome 원격디버깅(CDP) 기반 (macOS / 크로스플랫폼)
사용법: web_locate.py "<버튼/요소 텍스트 일부>"

브라우저 버튼 좌표를 픽셀로 추측하지 말고, DOM 에서 getBoundingClientRect 로
**정확한 화면 중심 좌표**를 계산해 출력한다. 그 좌표로 move.py→캡처확인→click.py.

전제: Chrome 을 원격디버깅 포트로 띄워야 한다.
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
     --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=/tmp/cdp <url>
(macOS 는 UI Automation(win) 대신 CDP/AX 사용. 지식: computer_use_web_app_reliable_click)

출력: "x y | '실제텍스트'"  (없으면 NOTFOUND)
※ 본 도구는 win 의 UIA 방식과 목적이 동일한 mac 병렬본이며, 실기기 검증을 권장한다.
"""
import sys, json, urllib.request

try:
    import websocket  # websocket-client
except Exception:
    print("NOTFOUND (websocket-client 필요: pip install websocket-client)")
    sys.exit(3)

target = sys.argv[1]
PORT = sys.argv[2] if len(sys.argv) > 2 else "9222"

# 1) 페이지 타깃의 WebSocket 디버거 URL 얻기
try:
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=4))
except Exception as e:
    print(f"NOTFOUND (CDP /json 접속 실패 — Chrome 을 --remote-debugging-port={PORT} 로 기동했나? {e})")
    sys.exit(1)
pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
if not pages:
    print("NOTFOUND (page 타깃 없음)")
    sys.exit(1)
ws_url = pages[0]["webSocketDebuggerUrl"]

# 2) Runtime.evaluate 로 요소 화면좌표 계산
js = (
    "(()=>{const t=%s;"
    "const els=[...document.querySelectorAll('button,a,[role=button],[role=tab],input,li')];"
    "const el=els.find(e=>(e.innerText||e.value||'').includes(t));"
    "if(!el)return JSON.stringify({f:false});"
    "const r=el.getBoundingClientRect();"
    "return JSON.stringify({f:true,"
    "x:Math.round(window.screenX+r.left+r.width/2),"
    "y:Math.round(window.screenY+(window.outerHeight-window.innerHeight)+r.top+r.height/2),"
    "text:(el.innerText||el.value||'').trim().slice(0,40)});})()"
) % json.dumps(target)

ws = websocket.create_connection(ws_url, timeout=6)
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                    "params": {"expression": js, "returnByValue": True}}))
res = None
for _ in range(10):
    msg = json.loads(ws.recv())
    if msg.get("id") == 1:
        res = msg
        break
ws.close()

try:
    val = json.loads(res["result"]["result"]["value"])
except Exception:
    print("NOTFOUND (evaluate 실패)")
    sys.exit(2)
if not val.get("f"):
    print(f"NOTFOUND ('{target}' DOM 에서 못 찾음)")
    sys.exit(2)
print(f"{val['x']} {val['y']} | {val['text']!r}")
