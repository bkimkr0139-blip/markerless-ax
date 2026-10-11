#!/usr/bin/env python3
"""백오피스 모바일 점검 — 폰 폭(390px)에서 모든 화면의 가로 넘침·작은 터치 대상을 찾고 스크린샷을 저장한다.

(원본 설명) 백오피스 UI 점검 — Chrome 헤드리스(CDP)로 로그인 후 모든 화면을 열어 콘솔 오류를 모으고 스크린샷을 저장한다.

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
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots-m")
USER = sys.argv[3] if len(sys.argv) > 3 else "admin"
PASS = sys.argv[4] if len(sys.argv) > 4 else "admin123!@#"
W, Hh = 390, 844
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
        h = self.ev("Math.max(document.documentElement.scrollHeight, innerHeight)")
        w = self.ev("document.documentElement.clientWidth")
        data = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                         clip={"x": 0, "y": 0, "width": w, "height": min(h, 6000), "scale": 1})["data"]
        import base64
        (OUT / f"{name}.png").write_bytes(base64.b64decode(data))


def main():
    proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9334", f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-prof-mk-')}", "--no-first-run", "--remote-allow-origins=*",
                             "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
                             f"--window-size={W},{Hh}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:9334/json/version", timeout=1).read(); break
            except Exception:
                time.sleep(0.3)
        tabs = json.loads(urllib.request.urlopen("http://127.0.0.1:9334/json").read())
        page = next(t for t in tabs if t["type"] == "page")
        c = CDP(page["webSocketDebuggerUrl"])
        for d in ("Page", "Runtime"):
            c.call(f"{d}.enable")
        c.call("Emulation.setDeviceMetricsOverride", width=W, height=Hh, deviceScaleFactor=2, mobile=True)
        c.call("Page.navigate", url=f"{BASE}/admin/")
        time.sleep(2.5)
        c.ev(f"""(()=>{{document.querySelector('#u').value={json.dumps(USER)};document.querySelector('#p').value={json.dumps(PASS)};
                 document.querySelector('form').dispatchEvent(new Event('submit',{{cancelable:true,bubbles:true}}));}})()""", False)
        for _ in range(30):
            time.sleep(0.4)
            if c.ev("!!document.querySelector('#page')"):
                break
        else:
            print("로그인 실패"); return 1
        time.sleep(1.5)
        AUDIT = """(()=>{
          const vw = document.documentElement.clientWidth, out = {over: [], small: 0, smallEx: [], docW: document.documentElement.scrollWidth, vw};
          for (const el of document.querySelectorAll('#page *')) {
            const r = el.getBoundingClientRect();
            if (r.width && r.right > vw + 1 && !el.closest('.tbl-wrap') && !el.closest('.tabs') && !el.closest('.chips') && !el.closest('.viewer-tools')) {
              out.over.push((el.tagName + '.' + (el.className||'')).slice(0,50) + ' right=' + Math.round(r.right));
            }
          }
          out.over = [...new Set(out.over)].slice(0, 8);
          out.narrow = [];  // 글자가 좁은 칸에서 한 글자씩 줄바꿈되는 곳
          for (const el of document.querySelectorAll('#page *')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').trim(); if (t.length < 5) continue;
            const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
            const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.4;
            if (r.width && r.width < 56 && r.height > lh * 3) out.narrow.push(t.slice(0, 14) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
            else if (r.width && r.height > lh * 2.5 && t.length / (r.height / lh) < 3.2 && !/\s/.test(t)) out.narrow.push(t.slice(0, 14) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
          }
          out.narrow = [...new Set(out.narrow)].slice(0, 6);
          for (const el of document.querySelectorAll('#page button, #page a, #page input, #page select, #page .chip')) {
            const r = el.getBoundingClientRect();
            if (r.width && (r.height < 32 || r.width < 32) && getComputedStyle(el).visibility !== 'hidden' && el.type !== 'checkbox' && el.type !== 'radio') {
              out.small++; if (out.smallEx.length < 4) out.smallEx.push((el.textContent||el.title||el.tagName).trim().slice(0,12) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
            }
          }
          return out; })()"""
        pages = [("dashboard", "#/dashboard", None), ("assets", "#/assets", ".tbl"), ("inspections", "#/inspections", ".tbl"),
                 ("issues", "#/issues", ".board"), ("spaces", "#/spaces", ".tbl"), ("devices", "#/devices", None),
                 ("users", "#/users", None), ("audit", "#/audit", None), ("system", "#/system", None),
                 ("twin", "#/twin?mock=1", ".tw-viewer canvas")]
        aid = c.ev("fetch('/admin/api/assets').then(r=>r.json()).then(j=>j.assets[0].assetId)")
        oid = c.ev("fetch('/admin/api/inspections').then(r=>r.json()).then(j=>(j.inspections[0]||{}).id||null)")
        iid = c.ev("fetch('/admin/api/issues').then(r=>r.json()).then(j=>(j.issues[0]||{}).id||null)")
        if iid:
            pages.append(("issue-drawer", f"#/issues/{iid}", ".drawer"))
        pages.append(("asset-detail", f"#/assets/{aid}", ".viewer canvas"))
        if oid:
            pages.append(("inspection-detail", f"#/inspections/{oid}", ".viewer canvas"))
        only = sys.argv[5].split(",") if len(sys.argv) > 5 else None
        for name, h, wf in pages:
            if only and name not in only:
                continue
            c.ev(f"location.hash = '{h}'", False)
            t0 = time.time()
            while time.time() - t0 < 10:
                time.sleep(0.4)
                if wf and c.ev(f"!!document.querySelector({json.dumps(wf)})"): break
                if not wf and time.time() - t0 > 1.8: break
            time.sleep(1.2)
            r = c.ev(AUDIT)
            c.shot(name)
            flag = "OK " if not r["over"] and not r["narrow"] and r["docW"] <= r["vw"] + 1 else ("깨짐" if r["narrow"] else "넘침")
            print(f"[{flag}] {name:18s} docW={r['docW']}/{r['vw']} 작은대상={r['small']} {r['smallEx']} {r['over'][:3]} 좁은글자={r['narrow']}")
        return 0
    finally:
        proc.terminate()


sys.exit(main())
