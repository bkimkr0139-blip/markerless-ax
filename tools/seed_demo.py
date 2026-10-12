#!/usr/bin/env python3
"""백오피스 데모 데이터 시딩 — 시연·화면 검증용. 실제 서버가 아니라 별도 DB의 데모 서버에 넣는 것을 전제로 한다.

사용:  MARKERLESS_DATABASE_URL=sqlite:////tmp/demo/m.db MARKERLESS_STORAGE_DIR=/tmp/demo/s \
         .venv/bin/python -m uvicorn app.main:app --port 8222   (backend/ 에서)
       .venv/bin/python tools/seed_demo.py http://127.0.0.1:8222
"""
import base64
import json
import struct
import sys
import zlib
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import test_auto_match as T  # noqa: E402  (합성 장면 헬퍼 재사용)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8222"
H = {"X-Requested-With": "markerless-admin"}
rng = np.random.default_rng(42)


def png_rgba(arr: np.ndarray) -> bytes:
    h, w, _ = arr.shape
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def silhouette(w=160, h=120, seed=0):
    """흑백 실루엣 흉내: 사다리꼴 몸통 + 음영 + 알파 윤곽 (실제 앱은 사진에서 오려낸다)."""
    r = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    inside = (xx > 8) & (xx < w - 8) & (yy > 10) & (yy < h - 6) & (xx + yy * 0.12 < w - 6)
    g = (70 + 110 * (xx / w) + 18 * np.sin(yy / 5.0) + r.normal(0, 6, (h, w))).clip(0, 255).astype(np.uint8)
    a = np.where(inside, 255, 0).astype(np.uint8)
    return png_rgba(np.stack([g * (a > 0), g * (a > 0), g * (a > 0), a], axis=-1).astype(np.uint8))


def login(c, u, p):
    r = c.post("/admin/api/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text


def main():
    api = httpx.Client(base_url=BASE, timeout=60)
    admin = httpx.Client(base_url=BASE, timeout=60, headers=H)
    login(admin, "admin", "admin123!@#")

    api.post("/api/v1/projects", json={"projectId": "PJT001", "name": "한빛 복합단지 기계설비"})
    sp1 = api.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "B1-MECH", "name": "지하1층 기계실", "level": "B1"}).json()
    sp2 = api.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "RF-ELEC", "name": "옥상 전기실", "level": "RF"}).json()
    api.space_id = sp1["id"]

    # 사용자
    for u, dn, role in [("kim.inspector", "김현장", "inspector"), ("park.tech", "박조치", "inspector"), ("lee.manager", "이소장", "manager"), ("view.client", "발주처 열람", "viewer")]:
        admin.post("/admin/api/users", json={"username": u, "displayName": dn, "role": role, "password": "Demo-Pass-2026x"})

    # 자산 등록 (단말이 보낸 것처럼 deviceId 포함)
    specs = [("PUMP-301", "1호기 급수펌프", "설비", T._l_object(rng), (0.10, 0, 0.05), sp1["id"], "iPhone Air · 김현장"),
             ("PANEL-12", "분전반 DB-12", "전기", T._slab(rng), (-0.30, 0, 0.20), sp1["id"], "iPhone Air · 김현장"),
             ("VALVE-210", "냉수 밸브 박스", "배관", T._box(rng, [0, 0.09, 0], [0.22, 0.18, 0.16], 800), (0.2, 0, -0.1), sp1["id"], "iPhone 13 Pro · 박조치"),
             ("TANK-02", "팽창탱크 2호", "설비", T._box(rng, [0, 0.18, 0], [0.30, 0.36, 0.30], 1000), (-0.1, 0, 0.3), sp2["id"], "iPhone 13 Pro · 박조치")]
    for aid, name, fam, obj, place, spid, dev in specs:
        scene = np.vstack([T._background(rng), obj + np.array(place)])
        poi = T._nearest_point(obj + np.array(place), (obj + np.array(place)).mean(axis=0))
        seg = api.post("/api/v1/scan/segment", json={"cloud": T._cloud(scene), "poi": dict(zip("xyz", poi)), "maxRadius": 0.6}).json()
        mask = np.frombuffer(base64.b64decode(seg["mask"]), dtype=np.uint8).astype(bool)
        center = np.array([seg["center"][k] for k in "xyz"])
        kept = scene[mask] - center
        n = len(kept)
        from spatial_core.registration import write_glb
        verts = np.vstack([kept + [0.012, 0, 0], kept + [-0.006, 0.0104, 0], kept + [-0.006, -0.0104, 0]]).astype(np.float32)
        tris = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], 1).astype(np.uint32)
        space_code = "B1-MECH" if spid == sp1["id"] else "RF-ELEC"
        meta = {"assetId": aid, "projectId": "PJT001", "spaceId": space_code, "family": fam, "deviceId": dev, "name": name,
                "designPose": {"positionM": dict(zip("xyz", center)), "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}, "notes": f"{dev} 스캔 등록"}
        r = api.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)}, files={"model": ("a.glb", write_glb(verts, tris), "model/gltf-binary")})
        assert r.status_code == 201, r.text
        # 4방향 사진 실루엣 카드 (자산 로컬 프레임: OBB 면 앞에 세운다)
        ol = api.get(f"/api/v1/assets/{aid}/outline").json()
        c = np.array(ol["obb"]["center"]); cor = np.array(ol["obb"]["corners"])
        u = (cor[4] - cor[0]); L = np.linalg.norm(u); u /= L
        v = (cor[1] - cor[0]); W = np.linalg.norm(v); v /= W
        H_ = np.linalg.norm(cor[2] - cor[0])
        for side, (dirv, wid) in {"front": (-v, L), "right": (u, W), "back": (v, L), "left": (-u, W)}.items():
            right = np.cross(-dirv, [0, 1, 0]) * -1
            right = right / np.linalg.norm(right)
            m = {"center": (c + dirv * (np.linalg.norm(dirv * 0 + 1) * (W if side in ("front", "back") else L) / 2 + 0.002)).tolist(),
                 "right": right.tolist(), "up": [0, 1, 0], "forward": (-dirv).tolist(), "widthM": float(wid), "heightM": float(H_)}
            api.put(f"/api/v1/assets/{aid}/photos/{side}", data={"meta": json.dumps(m)}, files={"image": (f"{side}.png", silhouette(seed=hash(aid + side) % 99), "image/png")})
        print("자산 등록:", aid, name)

    # 검수: 자동 매칭 PASS/REVIEW + 크기 불일치 FAIL (이슈 자동 생성)
    def inspect_auto(yaw, shift, frac, device):
        obj = T._l_object(rng)
        placed = (obj - (obj.min(0) + obj.max(0)) / 2) @ T._ry(yaw).T + [shift[0], 0.105, shift[1]]
        scene = np.vstack([T._background(rng), placed[rng.random(len(placed)) < frac]])
        body = {"alignment": "object", "autoMatch": True, "spaceId": sp1["id"], "deviceId": device, "worldMapRestored": False,
                "hintCenter": {"x": shift[0], "y": 0.1, "z": shift[1]}, "cloud": T._cloud(scene), "trackingNormal": True}
        return api.post("/api/v1/measurements", json=body)

    for yaw, shift, frac, dev in [(35, (-0.3, 0.25), 0.7, "iPhone Air · 김현장"), (-70, (-0.25, 0.3), 0.65, "iPhone Air · 김현장"), (120, (-0.3, 0.2), 0.55, "iPhone 13 Pro · 박조치")]:
        r = inspect_auto(yaw, shift, frac, dev)
        print("검수:", r.status_code, r.json().get("result", {}).get("decision"))
    ball = rng.normal(0, 1, (700, 3)); ball = 0.08 * ball / np.linalg.norm(ball, axis=1, keepdims=True) + [0, 0.08, 0]
    for aid in ("PUMP-301", "PANEL-12"):
        body = {"alignment": "object", "assetId": aid, "deviceId": "iPhone 13 Pro · 박조치", "worldMapRestored": False,
                "hintCenter": {"x": 0, "y": 0.16, "z": 0}, "cloud": T._cloud(np.vstack([T._background(rng), ball])), "trackingNormal": True}
        r = api.post("/api/v1/measurements", json=body)
        print("검수(고정):", aid, r.json().get("result", {}).get("decision"))

    # 이슈 워크플로우 진행 상태를 다양하게
    ins = httpx.Client(base_url=BASE, timeout=60, headers=H); login(ins, "kim.inspector", "Demo-Pass-2026x")
    mgr = httpx.Client(base_url=BASE, timeout=60, headers=H); login(mgr, "lee.manager", "Demo-Pass-2026x")
    tech = httpx.Client(base_url=BASE, timeout=60, headers=H); login(tech, "park.tech", "Demo-Pass-2026x")

    def tr(c, iid, to, **kw):
        r = c.post(f"/admin/api/issues/{iid}/transition", json={"to": to, **kw}); assert r.status_code == 200, r.text

    issues = admin.get("/admin/api/issues").json()["issues"]
    if issues:
        i0 = issues[-1]["id"]
        tr(ins, i0, "CONFIRMED", comment="현장 확인: 기초 볼트 위치가 설계보다 40mm 이탈")
        tr(mgr, i0, "ASSIGNED", assignee="park.tech", dueDate="2030-02-15", comment="2주 내 재시공")
    mk = lambda **b: ins.post("/admin/api/issues", json=b).json()
    a = mk(assetId="VALVE-210", title="냉수 밸브 박스 지지대 처짐", description="지지대가 하중으로 처짐 — 육안 확인 필요", severity="MEDIUM")
    tr(ins, a["id"], "CONFIRMED", comment="확인됨")
    tr(mgr, a["id"], "ASSIGNED", assignee="park.tech", dueDate="2020-01-10", comment="긴급")  # 기한 초과 시연
    tr(tech, a["id"], "IN_PROGRESS", comment="자재 반입 후 착수")
    b = mk(assetId="TANK-02", title="팽창탱크 연결 배관 이격 부족", description="연결 배관과 벽체 간격 부족", severity="HIGH")
    tr(ins, b["id"], "CONFIRMED"); tr(mgr, b["id"], "ASSIGNED", assignee="park.tech", dueDate="2030-03-01")
    tr(tech, b["id"], "IN_PROGRESS"); tr(tech, b["id"], "RESOLVED", resolution="배관 경로 변경 및 지지대 추가 설치")
    c = mk(assetId="PANEL-12", title="분전반 도어 간섭", description="도어 개방 시 인접 설비와 간섭", severity="LOW")
    tr(ins, c["id"], "CONFIRMED"); tr(mgr, c["id"], "ASSIGNED", assignee="park.tech")
    tr(tech, c["id"], "IN_PROGRESS"); tr(tech, c["id"], "RESOLVED", resolution="경첩 위치 조정")
    tr(ins, c["id"], "REINSPECT", comment="재검수 요청")
    d = mk(title="2층 복도 센서 오탐 의심", description="자동 검수 결과가 현장과 달라 보임", severity="LOW")
    tr(ins, d["id"], "DISMISSED", comment="센서 보정 전 데이터 — 오탐으로 확인")
    print("완료:", admin.get("/admin/api/dashboard").json()["kpi"])


main()
