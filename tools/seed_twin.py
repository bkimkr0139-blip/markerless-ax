#!/usr/bin/env python3
"""합성 방 2개 + 복도를 실제 API로 올려 디지털 트윈 데모 건물을 만든다 (실기기 없이 전체 파이프라인·웹 투어 점검용).

    .venv/bin/python tools/seed_twin.py http://127.0.0.1:8113 [--key API_KEY] [--small]

주의: 운영 서버에 쓰면 데모 건물이 섞인다 — 검증은 임시 DB 서버(`tools/twin_server.sh`)에서 하자.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spatial-core"))
from spatial_core.reconstruction.camera import to_mat16  # noqa: E402
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames  # noqa: E402


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def upload(c: httpx.Client, sid: str, f) -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(f.rgb).save(buf, "JPEG", quality=90)
    parts = {"rgb": buf.getvalue(), "depth": f.depth.astype("<f4").tobytes(), "conf": f.conf.astype(np.uint8).tobytes()}
    K = f.K_rgb
    meta = {"frameIndex": f.index, "segmentIndex": 0, "timestampNs": f.index * 33_000_000, "cameraTransform": to_mat16(f.T_world_cam),
            "intrinsics": [K[0, 0], 0, 0, 0, K[1, 1], 0, K[0, 2], K[1, 2], 1], "imageWidth": f.rgb_size[0], "imageHeight": f.rgb_size[1],
            "depthWidth": f.depth.shape[1], "depthHeight": f.depth.shape[0], "trackingState": "normal", "motionBlurScore": 0.0}
    decl = {k: {"bytes": len(v), "sha256": sha(v)} for k, v in parts.items()}
    r = c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": decl, "idempotencyKey": f"{sid}-{f.index}"})
    r.raise_for_status()
    for k, v in parts.items():
        for off in range(0, len(v), 1 << 20):
            c.put(f"/api/v1/scan-sessions/{sid}/frames/{f.index}/parts/{k}?offset={off}", content=v[off: off + (1 << 20)],
                  headers={"X-Sha256": decl[k]["sha256"]}).raise_for_status()
    c.post(f"/api/v1/scan-sessions/{sid}/frames/complete-upload", json={"frameIndex": f.index}).raise_for_status()


def scan(c: httpx.Client, space: str, room: SynthRoom, n: int, seed: int) -> dict:
    sid = c.post(f"/api/v1/spaces/{space}/scan-sessions", json={"mode": "SPACE", "deviceId": "seed", "relocalized": True}).json()["scanSessionId"]
    for f in orbit_frames(room, n=n, seed=seed):
        upload(c, sid, f)
    c.post(f"/api/v1/scan-sessions/{sid}/finalize", json={}).raise_for_status()
    c.post(f"/api/v1/scan-sessions/{sid}/reconstruct", json={"voxelM": 0.04, "textureMaxPx": 1536}).raise_for_status()
    while True:
        st = c.get(f"/api/v1/scan-sessions/{sid}/status").json()
        if st["status"] in ("READY", "NEED_MORE_CAPTURE", "FAILED"):
            return st
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--key", default="")
    ap.add_argument("--small", action="store_true")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, headers={"X-API-Key": a.key} if a.key else {}, timeout=120)
    n = 60 if a.small else 90
    c.post("/api/v1/projects", json={"projectId": "DEMO", "name": "데모"})
    b = c.post("/api/v1/buildings", json={"projectCode": "DEMO", "name": "데모 본관", "buildingType": "concrete"}).json()
    f1 = c.post("/api/v1/floors", json={"buildingId": b["id"], "level": "1F", "elevationM": 0.0, "heightM": 3.0}).json()
    rooms = [("DM-101", "기계실", SynthRoom(size=(5.0, 2.7, 4.0), door=("z0", 1.0, 2.0, 2.0)), 0.0),
             ("DM-102", "전기실", SynthRoom(size=(4.0, 2.7, 4.0), boxes=(((0.5, 0, 0.5), (0.6, 1.0, 0.5)),)), 5.2),
             ("DM-103", "복도", SynthRoom(size=(9.2, 2.7, 1.6), boxes=()), 0.0)]
    for i, (code, name, room, x) in enumerate(rooms):
        sp = c.post("/api/v1/projects/DEMO/spaces", json={"spaceId": code, "name": name, "level": "1F"}).json()["id"]
        T = np.eye(4)
        T[0, 3] = x
        T[2, 3] = 4.2 if code == "DM-103" else 0.0
        c.put(f"/api/v1/spaces/{sp}/placement", json={"buildingId": b["id"], "floorId": f1["id"], "tBuildingSpace": to_mat16(T),
                                                      "kind": "CORRIDOR" if code == "DM-103" else "ROOM"}).raise_for_status()
        st = scan(c, str(sp), room, n, seed=i)
        print(code, st["status"], st.get("quality"), st.get("coverage"))
    print("envelope", c.post(f"/api/v1/buildings/{b['id']}/generate-envelope", json={}).json())
    print("building", b["id"])


if __name__ == "__main__":
    main()
