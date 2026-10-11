"""디지털 트윈 웹 점검(헤드리스 Chrome): 실제 백엔드의 건물을 열어 오류·로딩·투어 이동을 확인하고 스크린샷을 남긴다.
   .venv/bin/python tools/twin_check.py http://127.0.0.1:8113 /tmp/shots-twin-real [buildingId]"""
import json, subprocess, tempfile, sys, time, urllib.request, base64
from pathlib import Path
import websocket
BASE=sys.argv[1]; OUT=Path(sys.argv[2]); BID=sys.argv[3] if len(sys.argv)>3 else "1"; OUT.mkdir(parents=True, exist_ok=True)
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
class CDP:
    def __init__(s,u): s.ws=websocket.create_connection(u,max_size=None,timeout=30); s.i=0; s.ev_=[]; s.errs=[]
    def _rec(s,m):
        if m.get("method")=="Runtime.exceptionThrown": s.errs.append("EXC "+json.dumps(m["params"]["exceptionDetails"].get("exception",{}).get("description",m["params"]["exceptionDetails"].get("text")))[:300])
        if m.get("method")=="Runtime.consoleAPICalled" and m["params"]["type"] in("error","warning"): s.errs.append(m["params"]["type"]+" "+" ".join(str(a.get("value",a.get("description",""))) for a in m["params"]["args"])[:300])
        if m.get("method")=="Log.entryAdded" and m["params"]["entry"]["level"]=="error": s.errs.append("LOG "+m["params"]["entry"]["text"][:200]+" "+m["params"]["entry"].get("url",""))
    def call(s,method,**p):
        s.i+=1; mid=s.i; s.ws.send(json.dumps({"id":mid,"method":method,"params":p}))
        while True:
            m=json.loads(s.ws.recv())
            if m.get("id")==mid:
                if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result",{})
            s._rec(m)
    def drain(s,t=0.3):
        s.ws.settimeout(t)
        try:
            while True: s._rec(json.loads(s.ws.recv()))
        except Exception: pass
        s.ws.settimeout(30)
    def ev(s,e,aw=True):
        r=s.call("Runtime.evaluate",expression=e,awaitPromise=aw,returnByValue=True)
        if "exceptionDetails" in r: raise RuntimeError(r["exceptionDetails"].get("exception",{}).get("description","eval"))
        return r["result"].get("value")
    def shot(s,name):
        d=s.call("Page.captureScreenshot",format="png")["data"]; (OUT/f"{name}.png").write_bytes(base64.b64decode(d))
proc=subprocess.Popen([CHROME,"--headless=new","--remote-debugging-port=9377",f"--user-data-dir={tempfile.mkdtemp(prefix='chr-')}","--no-first-run","--remote-allow-origins=*","--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader","--ignore-gpu-blocklist","--window-size=1280,1000","about:blank"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try: urllib.request.urlopen("http://127.0.0.1:9377/json/version",timeout=1).read(); break
        except Exception: time.sleep(.3)
    page=next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9377/json").read()) if t["type"]=="page")
    c=CDP(page["webSocketDebuggerUrl"])
    for d in("Page","Runtime","Log"): c.call(d+".enable")
    def viewport(w,h,mobile):
        c.call("Emulation.setDeviceMetricsOverride",width=w,height=h,deviceScaleFactor=1,mobile=mobile)
        if mobile: c.call("Emulation.setTouchEmulationEnabled",enabled=True)
    viewport(1280,1000,False)
    c.call("Page.navigate",url=BASE+"/admin/"); time.sleep(2.5)
    c.ev("""(()=>{document.querySelector('#u').value='admin';document.querySelector('#p').value='admin123!@#';document.querySelector('form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));})()""",False)
    for _ in range(30):
        time.sleep(.4)
        if c.ev("!!document.querySelector('#page')"): break
    def go(h,wait=8):
        c.ev(f"location.hash='{h}'",False); time.sleep(wait); c.drain()
    def state(): return c.ev("""(()=>{const e=window.__twin;const vw=document.documentElement.clientWidth;return {docW:document.documentElement.scrollWidth,vw,mode:e&&e.mode,loaded:e&&e.loadedCount&&e.loadedCount(),fps:e&&e.fps,tris:e&&e.tris,mock:!!(e&&e.isMock),notice:(document.querySelector('.tw-notice')||{}).textContent}})()""")
    go("#/twin/"+BID,10); c.shot("r1-exterior"); print("exterior",state())
    c.ev("document.querySelectorAll('.tw-mode')[1].click()",False); time.sleep(2); c.shot("r2-entrance")
    try:
        c.ev("document.querySelector('.tw-entr-row .btn.primary').click()",False); time.sleep(5); c.shot("r3-walk"); print("walk",state(), c.ev("window.__twin.currentSpaceId()"))
    except Exception as ex: print("no entrance row:",ex)
    c.ev("window.__twin.yaw=Math.PI/2",False); time.sleep(1.5); c.shot("r4-turn")
    viewport(390,844,True); go("#/twin/"+BID,10); c.shot("m1"); print("mobile",state())
    c.drain(1)
    print("ERRORS:",len(c.errs)); [print(" ",e) for e in c.errs[:15]]
finally:
    proc.terminate()
