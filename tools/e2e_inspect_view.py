#!/usr/bin/env python3
"""백오피스 '검수 표시 설정' 끝단 점검: 브라우저에서 체크박스를 눌러 → 서버 저장 → 모바일(단말 API)이 읽는 값까지 확인하고 원복한다.
사용: .venv/bin/python tools/e2e_inspect_view.py http://127.0.0.1:8111 [asset_id]"""
import json, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
import websocket

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8111"
ASSET = sys.argv[2] if len(sys.argv) > 2 else None
KEY = (Path(__file__).resolve().parent.parent / ".api-key").read_text().strip()
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def device_view(aid):
    req = urllib.request.Request(f"{BASE}/api/v1/assets/{aid}", headers={"X-API-Key": KEY})
    return json.loads(urllib.request.urlopen(req).read())["inspectView"]


class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, max_size=None, timeout=30); self.i = 0
    def call(self, m, **p):
        self.i += 1; mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": m, "params": p}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg: raise RuntimeError(msg["error"])
                return msg.get("result", {})
    def ev(self, e, aw=True):
        r = self.call("Runtime.evaluate", expression=e, awaitPromise=aw, returnByValue=True)
        if "exceptionDetails" in r: raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "eval"))
        return r["result"].get("value")


proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9334", f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-e2e-')}",
                         "--no-first-run", "--remote-allow-origins=*", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                         "--window-size=1600,1000", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
ok = False
try:
    for _ in range(50):
        try: urllib.request.urlopen("http://127.0.0.1:9334/json/version", timeout=1).read(); break
        except Exception: time.sleep(0.3)
    page = next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9334/json").read()) if t["type"] == "page")
    c = CDP(page["webSocketDebuggerUrl"]); c.call("Page.enable"); c.call("Runtime.enable")
    c.call("Page.navigate", url=f"{BASE}/admin/"); time.sleep(2.5)
    c.ev("""(()=>{document.querySelector('#u').value='admin';document.querySelector('#p').value='admin123!@#';
            document.querySelector('form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));})()""", False)
    for _ in range(30):
        time.sleep(0.4)
        if c.ev("!!document.querySelector('#page')"): break
    aid = ASSET or c.ev("fetch('/admin/api/assets').then(r=>r.json()).then(j=>j.assets.find(a=>a.modelOk!==false).assetId)")
    before = device_view(aid)
    print("자산", aid, "| 처음 모바일이 읽는 값:", before)
    c.ev(f"location.hash='#/assets/{aid}'", False); time.sleep(3.5)
    # 외곽선 체크박스(세 번째)와 표면(첫 번째)을 반대로 눌러 본다
    labels = c.ev("[...document.querySelectorAll('label')].filter(l=>l.querySelector('input[type=checkbox]')&&l.textContent.includes('외곽선')).length")
    print("화면에서 찾은 '외곽선' 체크박스:", labels)
    c.ev("[...document.querySelectorAll('label')].find(l=>l.querySelector('input[type=checkbox]')&&l.textContent.includes('외곽선')).querySelector('input').click()", False)
    time.sleep(1.8)  # 자동 저장 대기
    status = c.ev("(document.body.innerText.match(/저장됨[^\\n]*|저장 실패/)||[''])[0]")
    after = device_view(aid)
    print("저장 상태 문구:", status)
    print("클릭 후 모바일이 읽는 값:", after)
    ok = after["outline"] != before["outline"]
    print("✓ 체크 → 자동 저장 → 모바일 반영 성공" if ok else "✗ 반영되지 않음")
    # 원복
    c.ev("[...document.querySelectorAll('label')].find(l=>l.querySelector('input[type=checkbox]')&&l.textContent.includes('외곽선')).querySelector('input').click()", False)
    time.sleep(1.8)
    print("원복 후:", device_view(aid))
finally:
    proc.terminate()
sys.exit(0 if ok else 1)
