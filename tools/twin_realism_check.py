"""디지털 트윈 사실감 점검(헤드리스 Chrome/CDP): 재질(Unlit·sRGB·이방성)·점진 LOD·카메라 걷기·투어·사진 모드·미관측 강조·성능 보호를 확인하고 스크린샷을 남긴다.
   .venv/bin/python tools/twin_realism_check.py http://127.0.0.1:8113 /tmp/shots-twin-rl [buildingId]   (GUARD=1 이면 성능 보호를 켠 채 실행)"""
import os
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
proc=subprocess.Popen([CHROME,"--headless=new","--remote-debugging-port=9378",f"--user-data-dir={tempfile.mkdtemp(prefix='chr-')}","--no-first-run","--remote-allow-origins=*","--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader","--ignore-gpu-blocklist","--window-size=1280,1000","about:blank"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try: urllib.request.urlopen("http://127.0.0.1:9378/json/version",timeout=1).read(); break
        except Exception: time.sleep(.3)
    page=next(t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9378/json").read()) if t["type"]=="page")
    c=CDP(page["webSocketDebuggerUrl"])
    for d in("Page","Runtime","Log"): c.call(d+".enable")
    def viewport(w,h,mobile):
        c.call("Emulation.setDeviceMetricsOverride",width=w,height=h,deviceScaleFactor=1,mobile=mobile)
        if mobile: c.call("Emulation.setTouchEmulationEnabled",enabled=True)
    GUARD=os.environ.get("GUARD")=="1"
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
        c.ev("localStorage.removeItem('twin.quality')",False)
        c.call("Page.navigate",url=BASE+"/admin/"+h); time.sleep(1.5); c.ev("location.reload()",False); time.sleep(2)
        for _ in range(60):
            time.sleep(.25)
            if c.ev("!!(window.__twin&&window.__twin.m&&window.__twin.container.isConnected)"): break
        if not GUARD: c.ev("window.__twin.guardOn=false",False)
        time.sleep(wait); c.drain()
    STATS="""(()=>{const e=window.__twin;const out={fps:e.fps,tris:e.tris,texMB:Math.round(e.texBytes/1048576),q:e.qualityLabel,pr:e.renderer.getPixelRatio(),guard:e.guardSteps,tiers:{},fades:e._fades.length,q_len:e._q.length,active:e._qActive};
      for(const [id,s] of e.spaces) out.tiers[id]=[s.loaded,s.texTier,s.tiers&&s.tiers.map(t=>t.t).join('/')];
      const ms=[];e.scene.traverse(o=>{if(o.isMesh&&o.material&&o.userData.info&&o.userData.info.layer==='textured'){const m=o.material;ms.push({t:m.type,cs:m.map&&m.map.colorSpace,an:m.map&&m.map.anisotropy,mip:m.map&&m.map.generateMipmaps,flipY:m.map&&m.map.flipY,sz:m.map&&[m.map.image.width,m.map.image.height],side:m.side,po:m.polygonOffsetFactor,tm:m.toneMapped})}});out.mats=ms.slice(0,3);out.nmat=ms.length;
      out.out={cs:e.renderer.outputColorSpace,tm:e.renderer.toneMapping,aa:e.renderer.getContext().getContextAttributes().antialias,cam:[e.camera.near,e.camera.far,e.camera.fov]};return out})()"""
    def stats(): return c.ev(STATS)
    go("#/twin/"+BID,6); c.shot("a1-exterior"); print("A exterior",json.dumps(stats(),ensure_ascii=False))
    # 걷기 + LOD 승격
    c.ev("document.querySelectorAll('.tw-mode')[1].click()",False); time.sleep(1.5)
    c.ev("document.querySelector('.tw-entr-row .btn.primary').click()",False)
    for i in range(6):
        time.sleep(1.5); s=stats(); print(f"B walk t={1.5*(i+1):.1f}s",json.dumps(s["tiers"]),"texMB",s["texMB"],"fades",s["fades"],"q",s["q_len"],s["active"])
    c.shot("b1-walk-lod"); print("B mats",json.dumps(stats()["mats"]),json.dumps(stats()["out"]))
    # 카메라 키 입력: 벽 미끄러짐/감속
    c.ev("window.__twin.keys.add('KeyW')",False); time.sleep(1.2)
    v1=c.ev("window.__twin.vel.length()"); c.ev("window.__twin.keys.delete('KeyW')",False); time.sleep(.12); v2=c.ev("window.__twin.vel.length()"); time.sleep(.8); v3=c.ev("window.__twin.vel.length()")
    print("C velocity run/decel(.12s)/stop(.9s):",round(v1,2),round(v2,2),round(v3,2))
    # 미관측 강조
    c.ev("window.__twin.yaw=Math.PI/2",False); time.sleep(1.2); c.shot("c1-tour-look")
    c.ev("window.__twin.setUnobserved(true)",False); time.sleep(.8); c.shot("c2-unobserved-on"); c.ev("window.__twin.setUnobserved(false)",False)
    # 광학/검수 목적 select
    c.ev("(()=>{const s=document.querySelector('select[aria-label=\"보기 목적\"]');s.value='review';s.dispatchEvent(new Event('change'))})()",False); time.sleep(.8); c.shot("c3-review-purpose"); print("review unobs:",c.ev("window.__twin.unobservedOn"),"legend:",c.ev("document.querySelector('.tw-legend').textContent"))
    c.ev("(()=>{const s=document.querySelector('select[aria-label=\"보기 목적\"]');s.value='tour';s.dispatchEvent(new Event('change'))})()",False)
    # FOV
    c.ev("window.__twin.setFov(85)",False); time.sleep(.6); c.shot("c4-fov85"); c.ev("window.__twin.setFov(65)",False)
    # 투어 자동 비행
    c.ev("document.querySelector('.tw-tourbtn').click()",False)
    for i in range(4): time.sleep(2.2); print("D tour",c.ev("(document.querySelector('.tw-tourpill')||{}).textContent"),c.ev("window.__twin.touring")); c.shot(f"d{i+1}-tour")
    c.ev("window.__twin.stopTour()",False)
    # 사진 모드
    c.ev("document.dispatchEvent(new KeyboardEvent('keydown',{key:'p'}))",False); time.sleep(1.2); c.shot("e1-photo")
    print("E photo visible:",c.ev("""(()=>{const v=(q)=>{const e=document.querySelector(q);return !!e&&getComputedStyle(e).display!=='none'&&e.offsetParent!==null||(!!e&&getComputedStyle(e).position==='fixed')};return {tools:v('.tw-tools'),fps:v('.tw-fps'),legend:v('.tw-legend'),side:v('.tw-side'),notice:v('.tw-notice'),noticeText:(document.querySelector('.tw-notice')||{}).textContent,inferred:window.__twin.inferredVisible}})()"""))
    c.ev("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))",False); time.sleep(.6)
    # 화질 낮음 -> 텍스처 다운스케일 / LOD 제한
    c.ev("(()=>{const s=document.querySelector('select[aria-label=\"화질\"]');s.value='low';s.dispatchEvent(new Event('change'))})()",False); time.sleep(4)
    s=stats(); print("F low",json.dumps({k:s[k] for k in("q","pr","texMB","tiers","fades","mats","out")},ensure_ascii=False)); c.shot("f1-low")
    c.ev("(()=>{const s=document.querySelector('select[aria-label=\"화질\"]');s.value='high';s.dispatchEvent(new Event('change'))})()",False); time.sleep(5)
    s=stats(); print("F high",json.dumps({k:s[k] for k in("q","pr","texMB","tiers")},ensure_ascii=False)); c.shot("f2-high")
    # FPS 비교: 모드별
    for q in("high","low"):
        c.ev(f"window.__twin.setQuality('{q}')",False); time.sleep(4); fs=[]
        for _ in range(4): time.sleep(.6); fs.append(c.ev("window.__twin.fps"))
        print("FPS",q,fs)
    c.ev("window.__twin.setQuality('high')",False)
    # 성능 보호(옵션)
    if GUARD:
        c.ev("window.__twin.guardOn=true;window.__twin._graceUntil=0",False)
        for i in range(8): time.sleep(2.5); s=stats(); print("G guard",s["fps"],s["guard"],s["pr"],s["out"]["cam"][2]); 
        print("toasts:",c.ev("[...document.querySelectorAll('.toast, .toasts > *')].map(e=>e.textContent)"))
    # 모바일
    viewport(390,844,True); go("#/twin/"+BID,10); c.shot("m1"); print("mobile",json.dumps(stats()["q"]),stats()["pr"],c.ev("document.documentElement.scrollWidth"))
    c.ev("document.querySelectorAll('.tw-mode')[1].click()",False); time.sleep(1); c.ev("document.querySelector('.tw-entr-row .btn.primary').click()",False); time.sleep(6); c.shot("m2-walk")
    c.ev("document.dispatchEvent(new KeyboardEvent('keydown',{key:'p'}))",False); time.sleep(1); c.shot("m3-photo")
    c.drain(1)
    print("ERRORS:",len(c.errs)); [print(" ",e) for e in c.errs[:15]]
finally:
    proc.terminate()
