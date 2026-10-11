#!/usr/bin/env python3
"""백오피스 UI 점검 — Chrome 헤드리스(CDP)로 로그인 후 모든 화면을 열어 콘솔 오류를 모으고 스크린샷을 저장한다.

사용: .venv/bin/python tools/ui_check.py http://127.0.0.1:8222 /tmp/shots [admin 'admin123!@#']
"""
import json
import subprocess
import tempfile
import sys
import time
import urllib.request
from pathlib import Path

import websocket

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8222"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots")
USER = sys.argv[3] if len(sys.argv) > 3 else "admin"
PASS = sys.argv[4] if len(sys.argv) > 4 else "admin123!@#"
W, Hh = 1600, 1000
OUT.mkdir(parents=True, exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, max_size=None, timeout=30)
        self.i = 0
        self.events = []

    def call(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    def drain(self, secs=0.2):
        self.ws.settimeout(secs)
        try:
            while True:
                self.events.append(json.loads(self.ws.recv()))
        except Exception:
            pass
        self.ws.settimeout(30)

    def ev(self, expr, await_=True):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=await_, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "eval error"))
        return r["result"].get("value")

    def shot(self, name):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        import base64
        (OUT / f"{name}.png").write_bytes(base64.b64decode(data))


def main():
    proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9333", f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-prof-mk-')}", "--no-first-run", "--remote-allow-origins=*",
                             "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
                             f"--window-size={W},{Hh}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=1).read(); break
            except Exception:
                time.sleep(0.3)
        tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json").read())
        page = next(t for t in tabs if t["type"] == "page")
        c = CDP(page["webSocketDebuggerUrl"])
        for d in ("Page", "Runtime", "Log"):
            c.call(f"{d}.enable")
        c.call("Emulation.setDeviceMetricsOverride", width=W, height=Hh, deviceScaleFactor=1, mobile=False)
        errors = []

        def collect():
            c.drain(0.3)
            for e in c.events:
                if e.get("method") == "Runtime.exceptionThrown":
                    errors.append("EXC " + e["params"]["exceptionDetails"].get("exception", {}).get("description", "?")[:300])
                elif e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] in ("error", "warning"):
                    errors.append(f"console.{e['params']['type']}: " + " ".join(str(a.get('value', a.get('description', ''))) for a in e["params"]["args"])[:300])
                elif e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] == "error":
                    errors.append("LOG " + e["params"]["entry"]["text"][:200] + " " + e["params"]["entry"].get("url", ""))
            c.events.clear()

        def goto(hash_, wait=2.2, name=None, wait_for=None):
            c.ev(f"location.hash = '{hash_}'", False)
            t0 = time.time()
            while time.time() - t0 < 12:
                time.sleep(0.4)
                if wait_for and c.ev(f"!!document.querySelector({json.dumps(wait_for)})"):
                    break
                if not wait_for and time.time() - t0 > wait:
                    break
            time.sleep(wait if wait_for else 0.2)
            collect()
            if name:
                c.shot(name)

        c.call("Page.navigate", url=f"{BASE}/admin/")
        time.sleep(2.5)
        collect()
        if errors:
            print("초기 로드 오류:"); [print(" -", e) for e in sorted(set(errors))]
        c.shot("00-login")
        c.ev(f"""(()=>{{document.querySelector('#u').value={json.dumps(USER)};document.querySelector('#p').value={json.dumps(PASS)};
                 document.querySelector('form').dispatchEvent(new Event('submit',{{cancelable:true,bubbles:true}}));}})()""", False)
        for _ in range(30):
            time.sleep(0.4)
            if c.ev("!!document.querySelector('#page')"):
                break
        else:
            print("로그인 실패"); c.shot("00-login-fail"); collect(); print(errors); return 1
        time.sleep(2.0); collect(); c.shot("01-dashboard")

        goto("#/assets", name="02-assets", wait_for=".tbl")
        asset_id = c.ev("fetch('/admin/api/assets').then(r=>r.json()).then(j=>(j.assets[0]||{}).assetId)")
        if asset_id:  # 자산이 없는 임시 DB에서는 상세 화면을 건너뛴다
            goto(f"#/assets/{asset_id}", wait=3.5, name="03-asset-3d", wait_for=".viewer canvas")
            time.sleep(1.5); c.shot("03-asset-3d")
        goto("#/inspections", name="04-inspections", wait_for=".tbl")
        oid = c.ev("fetch('/admin/api/inspections').then(r=>r.json()).then(j=>j.inspections[0].id)")
        goto(f"#/inspections/{oid}", wait=3.5, name="05-inspection-review", wait_for=".viewer canvas")
        time.sleep(1.5); c.shot("05-inspection-review")
        c.ev("document.querySelectorAll('.step')[1].click()", False); time.sleep(0.8); c.shot("05b-review-step2")
        goto("#/issues", name="06-issues-board", wait_for=".board")
        iid = c.ev("fetch('/admin/api/issues').then(r=>r.json()).then(j=>(j.issues.find(i=>i.status==='ASSIGNED')||j.issues[0]||{}).id||null)")
        if iid:
            goto(f"#/issues/{iid}", wait=1.5, name="07-issue-drawer", wait_for=".drawer")
            c.ev("document.querySelector('.drawer .btn.icon').click()", False)
        c.ev("[...document.querySelectorAll('.chips .chip')].find(x=>x.textContent==='목록').click()", False); time.sleep(0.8); c.shot("06b-issues-list")
        for n, hsh in [("08-spaces", "#/spaces"), ("09-devices", "#/devices"), ("10-users", "#/users"), ("11-audit", "#/audit"), ("12-system", "#/system")]:
            goto(hsh, name=n)
        # 모바일 폭
        c.call("Emulation.setDeviceMetricsOverride", width=420, height=900, deviceScaleFactor=2, mobile=True)
        goto("#/dashboard", name="13-mobile-dashboard"); goto("#/issues", name="14-mobile-issues", wait_for=".board")
        print("스크린샷:", sorted(p.name for p in OUT.glob("*.png")))
        uniq = sorted(set(errors))
        print(f"콘솔 오류/경고 {len(uniq)}건")
        for e in uniq[:25]:
            print(" -", e)
        return 0 if not uniq else 2
    finally:
        proc.terminate()


sys.exit(main())
