#!/usr/bin/env python3
"""디지털 트윈 부하·동시성 시험 — 여러 세션이 동시에 프레임을 올리고, 그 사이 관리자·모바일 API를 계속 두드린다.

확인: 오류율(5xx·타임아웃), 지연(p50/p95), 업로드 처리량, 재구성 큐 직렬화(동시 1), SQLite 락·커넥션 풀 고갈 여부.
사용: .venv/bin/python tools/twin_stress.py http://127.0.0.1:8222 [--sessions 4] [--frames 30]   (임시 서버에서 실행)
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_twin as st  # noqa: E402
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--frames", type=int, default=30)
    a = ap.parse_args()
    c0 = httpx.Client(base_url=a.base, timeout=120)
    c0.post("/api/v1/projects", json={"projectId": "STRESS", "name": "부하"})
    spaces = [c0.post("/api/v1/projects/STRESS/spaces", json={"spaceId": f"ST-{i}"}).json()["id"] for i in range(a.sessions)]
    lat: list[float] = []
    errs: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()

    def hammer():  # 업로드 중에도 읽기 API가 막히지 않는지
        c = httpx.Client(base_url=a.base, timeout=60)
        paths = ["/healthz", "/api/v1/version", "/api/v1/sync/state", "/api/v1/buildings", "/api/v1/cad-catalog"]
        i = 0
        while not stop.is_set():
            p = paths[i % len(paths)]
            t0 = time.time()
            try:
                r = c.get(p)
                with lock:
                    lat.append(time.time() - t0)
                    if r.status_code >= 500:
                        errs.append(f"{p} {r.status_code}")
            except Exception as e:  # noqa: BLE001
                with lock:
                    errs.append(f"{p} {type(e).__name__}")
            i += 1
            time.sleep(0.02)

    def session(i: int):
        c = httpx.Client(base_url=a.base, timeout=120)
        room = SynthRoom(size=(4.0 + i * 0.3, 2.7, 3.5))
        sid = c.post(f"/api/v1/spaces/{spaces[i]}/scan-sessions", json={"mode": "SPACE", "relocalized": True}).json()["scanSessionId"]
        t0 = time.time()
        n = 0
        for f in orbit_frames(room, n=a.frames, seed=i):
            st.upload(c, sid, f)
            n += 1
        up = time.time() - t0
        c.post(f"/api/v1/scan-sessions/{sid}/finalize", json={}).raise_for_status()
        r = c.post(f"/api/v1/scan-sessions/{sid}/reconstruct", json={"voxelM": 0.05, "textureMaxPx": 1024})
        return sid, n, up, r.status_code

    th = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
    [t.start() for t in th]
    T0 = time.time()
    with ThreadPoolExecutor(a.sessions) as ex:
        res = list(ex.map(session, range(a.sessions)))
    up_total = time.time() - T0
    # 재구성이 동시 1로 직렬 처리되는지: 진행 중(RUNNING)인 세션은 항상 ≤1
    max_running = 0
    t1 = time.time()
    done = {}
    while time.time() - t1 < 600 and len(done) < len(res):
        running = 0
        for sid, *_ in res:
            s = c0.get(f"/api/v1/scan-sessions/{sid}/status").json()
            if s["stage"].startswith("RUNNING"):
                running += 1
            if s["status"] in ("READY", "NEED_MORE_CAPTURE", "FAILED"):
                done[sid] = s["status"]
        max_running = max(max_running, running)
        time.sleep(0.5)
    stop.set()
    time.sleep(0.3)
    frames = sum(r[1] for r in res)
    print(f"세션 {a.sessions} × 프레임 {a.frames} = {frames}장 업로드 {up_total:.1f}초 ({frames / up_total:.1f}장/초), 재구성 완료 {len(done)}/{len(res)}: {sorted(set(done.values()))}")
    print(f"동시 재구성 최대 {max_running}개 (기대 ≤1) | 읽기 API 요청 {len(lat)}건 p50 {statistics.median(lat) * 1000:.0f}ms p95 {sorted(lat)[int(len(lat) * .95)] * 1000:.0f}ms")
    print("오류:", errs[:10] if errs else "없음", f"({len(errs)}건)")
    ok = not errs and max_running <= 1 and len(done) == len(res) and all(v in ("READY", "NEED_MORE_CAPTURE") for v in done.values())
    print("결과:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
