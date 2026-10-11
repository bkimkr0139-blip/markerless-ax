"""비밀번호 강제 변경 화면 점검(헤드리스 Chrome): 약한 기본 비밀번호로 로그인하면 변경 화면만 뜨고, 변경 후 앱이 열리는지 확인.
서버는 MARKERLESS_ENFORCE_PW_CHANGE=1 로 띄운다. 사용: .venv/bin/python tools/pwchange_check.py http://127.0.0.1:8114 /tmp/shots-pw"""
import json, subprocess, tempfile, sys, time, urllib.request, base64
from pathlib import Path
import websocket
BASE=sys.argv[1]; OUT=Path(sys.argv[2]); OUT.mkdir(parents=True, exist_ok=True)
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
    viewport(1280,900,False)
    c.call("Page.navigate",url=BASE+"/admin/"); time.sleep(2.5)
    c.ev("""(()=>{document.querySelector('#u').value='admin';document.querySelector('#p').value='admin123!@#';document.querySelector('form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));})()""",False)
    time.sleep(2.5); c.shot("pw1-forced")
    print("forced screen:", c.ev("document.querySelector('.login-card h1')&&document.querySelector('.login-card h1').textContent"), "| sidebar present:", c.ev("!!document.querySelector('.side, aside, nav')&&!!document.querySelector('#page')"))
    inputs="""(()=>{const i=[...document.querySelectorAll('.login-card input')];i[0].value=%s;i[1].value=%s;i[2].value=%s;document.querySelector('.login-card form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));})()"""
    c.ev(inputs%("'admin123!@#'","'short1'","'short1'"),False); time.sleep(1.5); c.shot("pw2-weak")
    print("weak rejected:", c.ev("(document.querySelector('.login-card .notice')||{}).textContent"))
    c.ev(inputs%("'admin123!@#'","'T7#vRq-91zLm'","'T7#vRq-91zLm'"),False); time.sleep(3); c.shot("pw3-after")
    print("app open:", c.ev("!!document.querySelector('#page')"), "| title:", c.ev("(document.querySelector('.crumb, .page-h h1')||{}).textContent"))
    c.drain(1); print("ERRORS:",len(c.errs)); [print(" ",e) for e in c.errs[:10]]
finally:
    proc.terminate()
