#!/usr/bin/env python3
"""자세(위치·방향) 정확도 벤치마크 — 자산을 지정한 검수에서 서버가 돌려준 포즈가 실제 놓인 포즈와 얼마나 다른지 잰다.

화면에서 '외곽선이 사물 옆으로 빗나간다'는 느낌은 모델 표면 점들이 실제 위치에서 얼마나 떨어졌는가(표면 변위)로 잰다.
대칭인 사물(상자 180°, 원통 임의 yaw 등)은 대칭으로 겹치는 자세는 같은 것으로 본다.

    .venv/bin/python tools/pose_bench.py [--seeds 3] [--json out.json] [--only ASSET,..] [--views front,two,full]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
import time
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match_bench as mb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def ellipsoid(rng, a, b, c, n, center=(0, 0, 0)):
    d = rng.normal(0, 1, (n, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    p = d * np.array([a, b, c])
    nr = d / np.array([a, b, c])  # 암시적 함수 기울기 방향
    nr /= np.linalg.norm(nr, axis=1, keepdims=True)
    return p + np.array(center), nr


def lib(rng, n=3000):
    return {
        "BOX-A": (mb.box(rng, (0.40, 0.25, 0.30), n), [0, 180]),
        "L-OBJ": (mb.l_object(rng, n), [0]),
        "CYL-A": (mb.cylinder(rng, 0.12, 0.30, n), list(range(0, 360, 15))),
        "TALL": (mb.box(rng, (0.20, 0.60, 0.20), n), [0, 90, 180, 270]),
        "CAB": (mb.box(rng, (0.60, 0.80, 0.40), n), [0, 180]),
        "PURI": (ellipsoid(rng, 0.19, 0.33, 0.17, n, (0, 0.33, 0)), [0, 180]),  # 공기청정기처럼 길쭉한 타원체
    }


def ry(deg):
    return mb._rot_y(deg)


def quat_R(q):
    x, y, z, w = q["x"], q["y"], q["z"], q["w"]
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def build_scene(rng, full, obs, cams, noise, density, ctr_ref=None, real=False):
    """full: 등록 모델 원본 점, obs: 관측 점(부분), cams: 관측 카메라 위치(원본 프레임). 반환 (scene, hint, R, t, cams_scene).
    모델 프레임 q(= full − AABB 중심)이 장면에서 q@R.T + t 로 놓인다."""
    ctr_full = (full.min(0) + full.max(0)) / 2 if ctr_ref is None else ctr_ref  # 모델 프레임 원점(등록 때 AABB 중심)
    low = full[:, 1].min()
    R = ry(rng.uniform(0, 360))
    placed = np.array([rng.uniform(-0.4, 0.4), 0.0, rng.uniform(-0.4, 0.4)])
    shift = np.array([0.0, ctr_full[1] - low, 0.0])
    t = shift + placed  # q=0 (AABB 중심) → 바닥에서 반 높이 위
    to_scene = lambda P: (P - ctr_full) @ R.T + t
    p = to_scene(obs)
    p = p[rng.random(len(p)) < density]
    if real:  # 실기기 같은 오차: 축별 크기 ±1.5%, 낮은 주파수 휨 ±8mm, 센서 잡음 2배, 바닥 근처 분리 누수
        c0 = p.mean(0)
        p = c0 + (p - c0) * (1 + rng.uniform(-0.015, 0.015, 3))
        ph = rng.uniform(0, 6.28, 3)
        p = p + 0.008 * np.stack([np.sin(4 * p[:, 2] + ph[0]), np.sin(4 * p[:, 0] + ph[1]), np.sin(4 * p[:, 1] + ph[2])], 1)
        noise = noise * 2
        leak = np.stack([p[:, 0].mean() + rng.normal(0, 0.12, 40), np.full(40, 0.004), p[:, 2].mean() + rng.normal(0, 0.12, 40)], 1)
        p = np.vstack([p, leak])
    p = p + rng.normal(0, noise, p.shape)
    table = np.stack([rng.uniform(-0.9, 0.9, 1800), np.zeros(1800), rng.uniform(-0.9, 0.9, 1800)], 1)
    wall = np.stack([rng.uniform(-1, 1, 900), rng.uniform(0, 1.0, 900), np.full(900, -1.0)], 1)
    cp, _ = mb.box(rng, (0.12, 0.12, 0.12), 300, (0.6 * rng.choice([-1, 1]), 0.06, 0.6 * rng.choice([-1, 1])))
    scene = np.vstack([table + rng.normal(0, 0.003, table.shape), wall + rng.normal(0, 0.003, wall.shape), p, cp])
    hint = p[np.argmin(np.linalg.norm(p - p.mean(0), axis=1))] + rng.normal(0, 0.02, 3)
    cams_s = [to_scene(np.array(c)[None])[0] for c in cams]
    return scene, hint, R, t, cams_s


def view_pts(rng, pts, nrm, views):
    """views: [(az, el)] 카메라 위치들. 반환 (보이는 점, 카메라 위치 리스트(원본 프레임))"""
    ctr = (pts.min(0) + pts.max(0)) / 2
    keep = np.zeros(len(pts), bool)
    cams = []
    for az, el in views:
        a, e = np.radians(az), np.radians(el)
        cam = ctr + 1.0 * np.array([np.sin(a) * np.cos(e), np.sin(e), np.cos(a) * np.cos(e)])
        cams.append(cam)
        keep |= mb.visible(pts, nrm, cam)
    return pts[keep], cams


def scenarios(rng):
    a = rng.uniform(0, 360)
    return {
        "full": ([(a + 90 * k, 30) for k in range(4)], 0.002),
        "three": ([(a, 30), (a + 70, 30), (a - 70, 30)], 0.003),
        "two": ([(a, 30), (a + 90, 30)], 0.003),
        "front": ([(a, 30)], 0.003),
        "low": ([(a, 8)], 0.003),
    }


def surface_displacement(Q, Re, te, Rg, tg, syms):
    est = Q @ Re.T + te
    best = 1e9
    for s in syms:
        gt = Q @ (Rg @ ry(s)).T + tg
        best = min(best, float(np.linalg.norm(est - gt, axis=1).mean()))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--only")
    ap.add_argument("--views", default="front,two,three,full,low")
    ap.add_argument("--json")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--real", action="store_true", help="실기기 같은 오차(불완전·잡음 있는 등록 모델, 휨·크기 오차, 분리 누수)를 넣는다")
    ap.add_argument("--camera", action="store_true", help="카메라 위치를 서버에 함께 보낸다 (cameraPositions)")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="posebench_")
    app = mb.create_app(database_url=f"sqlite:///{tmp}/t.db", storage_dir=f"{tmp}/s")
    rng0 = np.random.default_rng(99)
    L = lib(rng0)
    only = set(args.only.split(",")) if args.only else None
    want = set(args.views.split(","))
    rows = []
    t0 = time.time()
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sid = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()["id"]
        reg_ctr = {}
        for aid, ((p, nrm), _s) in L.items():
            rp = p
            if args.real:  # 등록 스캔도 불완전: 세 방향에서 본 점 + 잡음 4mm
                rr = np.random.default_rng(zlib.crc32(aid.encode()))
                a0 = rr.uniform(0, 360)
                rp, _ = view_pts(rr, p, nrm, [(a0, 30), (a0 + 110, 30), (a0 + 230, 30)])
                rp = rp + rr.normal(0, 0.004, rp.shape)
            reg_ctr[aid] = (rp.min(0) + rp.max(0)) / 2
            mb.register(c, aid, rp)
        for aid, ((p, nrm), syms) in L.items():
            if only and aid not in only:
                continue
            ctr = reg_ctr[aid]
            Q = (p - ctr)[::6]
            for seed in range(args.seeds):
                rng = np.random.default_rng(zlib.crc32(f"{aid}|{seed}".encode()))
                for sname, (views, noise) in scenarios(rng).items():
                    if sname not in want:
                        continue
                    obs, cams = view_pts(rng, p, nrm, views)
                    if len(obs) < 80:
                        continue
                    scene, hint, Rg, tg, cams_s = build_scene(rng, p, obs, cams, noise, 0.5, ctr_ref=reg_ctr[aid], real=args.real)
                    body = {"alignment": "object", "assetId": aid, "spaceId": sid, "hintCenter": dict(zip("xyz", hint)),
                            "cloud": mb.cloud(scene), "trackingNormal": True, "worldMapRestored": False}
                    if args.camera:
                        body["cameraPositions"] = [dict(zip("xyz", map(float, cp_))) for cp_ in cams_s]
                    r = c.post("/api/v1/measurements", json=body)
                    if r.status_code != 201:
                        rows.append({"asset": aid, "view": sname, "seed": seed, "err": r.text[:80], "disp": None})
                        continue
                    out = r.json()["observation"]["actualPose"]
                    Re = quat_R(out["quat"])
                    te = np.array([out["positionM"][k] for k in "xyz"])
                    disp = surface_displacement(Q, Re, te, Rg, tg, syms)
                    cen = float(np.linalg.norm(te - tg))
                    rows.append({"asset": aid, "view": sname, "seed": seed, "disp": disp, "center": cen})
                    if args.verbose:
                        print(f"  {aid:6s} {sname:6s}#{seed} 표면변위 {disp * 100:5.1f}cm 중심 {cen * 100:5.1f}cm", flush=True)
    report(rows, time.time() - t0)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))


def report(rows, secs):
    ok = [r for r in rows if r.get("disp") is not None]
    bad = [r for r in rows if r.get("disp") is None]
    print(f"\n=== 자세 정확도 — {len(ok)}건 (실패 {len(bad)}건), {secs:.0f}초 ===")
    d = np.array([r["disp"] for r in ok]) * 100

    def line(name, arr):
        if len(arr) == 0:
            return
        print(f"  {name:8s} n={len(arr):3d}  표면변위 중앙 {np.median(arr):4.1f}cm  p90 {np.percentile(arr, 90):4.1f}cm  "
              f"≤2cm {np.mean(arr <= 2) * 100:3.0f}%  ≤4cm {np.mean(arr <= 4) * 100:3.0f}%  >8cm {np.mean(arr > 8) * 100:3.0f}%")

    line("전체", d)
    print("\n[관측 방식별]")
    for v in ["full", "three", "two", "front", "low"]:
        line(v, np.array([r["disp"] * 100 for r in ok if r["view"] == v]))
    print("\n[자산별]")
    by = defaultdict(list)
    for r in ok:
        by[r["asset"]].append(r["disp"] * 100)
    for k, v in by.items():
        line(k, np.array(v))
    for r in bad[:5]:
        print("  실패:", r)


if __name__ == "__main__":
    main()
