#!/usr/bin/env python3
"""자동 매칭 벤치마크 — 합성 자산 라이브러리에 다양한 관측(전체/부분/잡음/방해물/미등록 사물)을 넣고
오매칭·미매칭률을 잰다. 매칭 로직을 바꿀 때마다 돌려 개선·퇴보를 숫자로 확인한다.

    .venv/bin/python tools/match_bench.py [--seeds 2] [--quick] [--json out.json] [--verbose]

판정 분류(시나리오마다):
  correct_accept  정답 자산을 1등으로 '자동 확정'
  suggest_ok      자동 확정은 아니지만 후보 상위(≤3)에 정답 포함 → 사용자 선택으로 해결(미매칭이지만 구제)
  miss            정답을 못 찾음(후보에도 없음)
  false_accept    다른 자산을 '자동 확정'  ← 가장 위험 (오매칭)
  correct_reject  미등록 사물을 확정하지 않음
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "spatial-core"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from spatial_core.registration import write_glb  # noqa: E402


# ------------------------------------------------------------------ 합성 형상 (점 + 바깥 법선)

def _rot_y(deg: float) -> np.ndarray:
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def box(rng, size, n, center=(0, 0, 0)):
    """size=(x,y,z) m. 면적에 비례해 균일 샘플. 반환 (pts, normals)"""
    sx, sy, sz = size
    areas = np.array([sy * sz, sy * sz, sx * sz, sx * sz, sx * sy, sx * sy])
    face = rng.choice(6, n, p=areas / areas.sum())
    u = rng.random((n, 2)) - 0.5
    p = np.zeros((n, 3))
    nr = np.zeros((n, 3))
    for f in range(6):
        m = face == f
        ax = f // 2
        sgn = 1 if f % 2 == 0 else -1
        o = [i for i in range(3) if i != ax]
        p[m, ax] = sgn * size[ax] / 2
        p[m, o[0]] = u[m, 0] * size[o[0]]
        p[m, o[1]] = u[m, 1] * size[o[1]]
        nr[m, ax] = sgn
    return p + np.array(center), nr


def cylinder(rng, r, h, n, center=(0, 0, 0)):
    a_side, a_cap = 2 * np.pi * r * h, np.pi * r * r
    p_side = a_side / (a_side + 2 * a_cap)
    kind = rng.choice(3, n, p=[p_side, (1 - p_side) / 2, (1 - p_side) / 2])
    th = rng.random(n) * 2 * np.pi
    rr = np.where(kind == 0, r, r * np.sqrt(rng.random(n)))
    y = np.where(kind == 0, (rng.random(n) - 0.5) * h, np.where(kind == 1, h / 2, -h / 2))
    p = np.stack([rr * np.cos(th), y, rr * np.sin(th)], 1)
    nr = np.zeros((n, 3))
    s = kind == 0
    nr[s] = np.stack([np.cos(th[s]), np.zeros(s.sum()), np.sin(th[s])], 1)
    nr[kind == 1] = [0, 1, 0]
    nr[kind == 2] = [0, -1, 0]
    return p + np.array(center), nr


def sphere(rng, r, n, center=(0, 0, 0)):
    d = rng.normal(0, 1, (n, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    return d * r + np.array(center), d


def union(*parts):
    return np.vstack([p for p, _ in parts]), np.vstack([n for _, n in parts])


def l_object(rng, n=3000):
    a = box(rng, (0.30, 0.10, 0.12), n // 2, (0, 0.05, 0))
    b = box(rng, (0.12, 0.16, 0.12), n // 2, (-0.09, 0.13, 0))
    return union(a, b)


# 라이브러리: 일부러 닮은 쌍(트윈)을 섞는다 — 같은 가로세로에 높이만 3cm 차이 등
def library(rng, n=3000):
    return {
        "BOX-A": box(rng, (0.40, 0.25, 0.30), n),
        "BOX-B": box(rng, (0.40, 0.28, 0.30), n),   # A와 높이 3cm 차이 (트윈)
        "BOX-C": box(rng, (0.42, 0.25, 0.27), n),   # A와 가로·세로 2~3cm 차이 (트윈)
        "L-OBJ": l_object(rng, n),
        "SLAB": box(rng, (0.42, 0.05, 0.26), n),
        "CYL-A": cylinder(rng, 0.12, 0.30, n),
        "CYL-B": cylinder(rng, 0.12, 0.34, n),      # 트윈
        "TALL": box(rng, (0.20, 0.60, 0.20), n),
        "SMALL": box(rng, (0.15, 0.10, 0.10), n),
        "CAB": box(rng, (0.60, 0.80, 0.40), n),
    }


def unknowns(rng, n=3000):
    """등록되지 않은 사물 — 자동 확정되면 오매칭이다."""
    return {
        "?BIG-BOX": box(rng, (0.55, 0.40, 0.35), n),
        "?BALL": sphere(rng, 0.09, n, (0, 0.09, 0)),
        "?FLAT-CYL": cylinder(rng, 0.20, 0.10, n),
        "?NEAR-A": box(rng, (0.40, 0.25, 0.345), n),  # A보다 높이 4.5cm 큼 (등록 아님)
        "?THIN": box(rng, (0.50, 0.03, 0.05), n),
    }


# ------------------------------------------------------------------ 관측 시나리오

def visible(pts, nrm, cam):
    """카메라(cam 위치)를 향한 면만 남긴다 (자기 가림 단순 모델: 법선·뷰 방향)."""
    v = cam - pts
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return (nrm * v).sum(1) > 0.05


def view(rng, pts, nrm, az_deg, el_deg=30.0, dist=1.0, extra_views=()):
    ctr = (pts.min(0) + pts.max(0)) / 2
    keep = np.zeros(len(pts), bool)
    for az, el in [(az_deg, el_deg), *extra_views]:
        a, e = np.radians(az), np.radians(el)
        cam = ctr + dist * np.array([np.sin(a) * np.cos(e), np.sin(e), np.cos(a) * np.cos(e)])
        keep |= visible(pts, nrm, cam)
    return pts[keep]


SCENARIOS = {
    # 이름: (설명, 관측 점 생성 함수(rng, pts, nrm) -> 점, 노이즈 m, 밀도)
    "full": ("전체 둘러본 스캔", lambda r, p, n: p, 0.002, 0.5),
    "full_noisy": ("전체, 잡음 5mm", lambda r, p, n: p, 0.005, 0.4),
    "front": ("한 방향 + 위", lambda r, p, n: view(r, p, n, r.uniform(0, 360)), 0.003, 0.5),
    "two": ("두 방향", lambda r, p, n: view(r, p, n, a := r.uniform(0, 360), 30, 1.0, [(a + 90, 30)]), 0.003, 0.5),
    "sparse": ("전체, 성긴 점(15%)", lambda r, p, n: p, 0.003, 0.15),
    "low": ("낮은 각도 한 면", lambda r, p, n: view(r, p, n, r.uniform(0, 360), 5), 0.003, 0.5),
}
QUICK = ["full", "front", "two"]


def make_scene(rng, obj_pts, noise, density, clutter=True):
    placed_c = np.array([rng.uniform(-0.4, 0.4), 0.0, rng.uniform(-0.4, 0.4)])
    R = _rot_y(rng.uniform(0, 360))
    ctr = (obj_pts.min(0) + obj_pts.max(0)) / 2
    low = obj_pts[:, 1].min()
    p = (obj_pts - ctr) @ R.T
    p[:, 1] += ctr[1] - low  # 바닥에 놓음
    p = p + placed_c
    p = p[rng.random(len(p)) < density]
    p = p + rng.normal(0, noise, p.shape)
    table = np.stack([rng.uniform(-0.9, 0.9, 1800), np.zeros(1800), rng.uniform(-0.9, 0.9, 1800)], 1)
    wall = np.stack([rng.uniform(-1, 1, 900), rng.uniform(0, 1.0, 900), np.full(900, -1.0)], 1)
    parts = [table + rng.normal(0, 0.003, table.shape), wall + rng.normal(0, 0.003, wall.shape), p]
    if clutter:
        cp, _ = box(rng, (0.12, 0.12, 0.12), 300, (0.6 * rng.choice([-1, 1]), 0.06, 0.6 * rng.choice([-1, 1])))
        parts.append(cp)
    scene = np.vstack(parts)
    hint = p[np.argmin(np.linalg.norm(p - p.mean(0), axis=1))] + rng.normal(0, 0.02, 3)
    return scene, hint


def cloud(P):
    return {"count": len(P), "base64": base64.b64encode(P.astype(np.float32).tobytes()).decode()}


# ------------------------------------------------------------------ 실행

def register(c, aid, pts):
    ctr = (pts.min(0) + pts.max(0)) / 2
    kept = pts - ctr
    n = len(kept)
    verts = np.vstack([kept + [0.012, 0, 0], kept + [-0.006, 0.0104, 0], kept + [-0.006, -0.0104, 0]]).astype(np.float32)
    tris = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], 1).astype(np.uint32)
    meta = {"assetId": aid, "projectId": "P1", "spaceId": "S1",
            "designPose": {"positionM": dict(zip("xyz", ctr)), "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}}
    r = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)},
               files={"model": ("a.glb", write_glb(verts, tris), "model/gltf-binary")})
    assert r.status_code == 201, r.text


def classify(truth, resp):
    """truth: 자산 id 또는 None(미등록). resp: /match 응답 dict 또는 오류."""
    cands = resp.get("candidates", []) if resp else []
    best = resp.get("best") if resp else None
    top3 = [x["assetId"] for x in cands[:3]]
    if truth is None:
        return "false_accept" if best else "correct_reject"
    if best == truth:
        return "correct_accept"
    if best:
        return "false_accept"
    return "suggest_ok" if truth in top3 else "miss"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only", help="쉼표로 구분한 자산 id만")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="matchbench_")
    app = create_app(database_url=f"sqlite:///{tmp}/t.db", storage_dir=f"{tmp}/s")
    rng0 = np.random.default_rng(1234)
    lib = library(rng0)
    unk = unknowns(rng0)
    scen = {k: v for k, v in SCENARIOS.items() if not args.quick or k in QUICK}
    only = set(args.only.split(",")) if args.only else None

    rows = []
    t0 = time.time()
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sid = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()["id"]
        for aid, (p, _n) in lib.items():
            register(c, aid, p)

        cases = [(aid, name, lib[aid]) for aid, name in ((a, a) for a in lib) if not only or aid in only]
        if not only:
            cases += [(None, k, v) for k, v in unk.items()]

        for truth, name, (p, nrm) in cases:
            for sname, (_desc, gen, noise, dens) in scen.items():
                for seed in range(args.seeds):
                    rng = np.random.default_rng(zlib.crc32(f'{name}|{sname}|{seed}'.encode()))
                    try:
                        obs = gen(rng, p, nrm)
                    except Exception:
                        continue
                    if len(obs) < 60:
                        continue
                    scene, hint = make_scene(rng, obs, noise, dens)
                    body = {"spaceId": sid, "hintCenter": dict(zip("xyz", hint)), "cloud": cloud(scene),
                            "trackingNormal": True, "worldMapRestored": False}
                    tt = time.time()
                    r = c.post("/api/v1/match", json=body)
                    dt = time.time() - tt
                    resp = r.json() if r.status_code == 200 else {"error": r.text[:120]}
                    if r.status_code != 200 and r.status_code != 422:
                        outcome = "error"
                    else:
                        outcome = classify(truth, resp)
                    top = resp.get("candidates", [{}])[0] if resp.get("candidates") else {}
                    rows.append({"truth": truth or name, "unknown": truth is None, "scenario": sname, "seed": seed,
                                 "outcome": outcome, "best": resp.get("best"), "top": top.get("assetId"),
                                 "sim": top.get("similarity"), "sec": round(dt, 2)})
                    if args.verbose or outcome in ("false_accept", "error"):
                        print(f"  [{outcome}] truth={truth or name} sc={sname}#{seed} -> best={resp.get('best')} "
                              f"top={top.get('assetId')} sim={top.get('similarity')}", flush=True)

    report(rows, time.time() - t0)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"\nsaved {args.json}")


def report(rows, secs):
    outs = ["correct_accept", "suggest_ok", "miss", "false_accept", "correct_reject", "error"]
    known = [r for r in rows if not r["unknown"]]
    unk = [r for r in rows if r["unknown"]]

    def pct(n, d):
        return f"{100 * n / d:5.1f}%" if d else "   -  "

    def count(rs, o):
        return sum(1 for r in rs if r["outcome"] == o)

    print(f"\n=== 자동 매칭 벤치마크 — 케이스 {len(rows)}건, {secs:.0f}초 (평균 {np.mean([r['sec'] for r in rows]):.2f}s/건) ===")
    print(f"\n[등록 자산 관측 {len(known)}건]")
    print(f"  자동 확정(정답)   {pct(count(known, 'correct_accept'), len(known))}   ← 효율")
    print(f"  후보 구제         {pct(count(known, 'suggest_ok'), len(known))}   (확정은 안 됐지만 상위3에 정답)")
    print(f"  완전 미매칭       {pct(count(known, 'miss'), len(known))}")
    print(f"  오매칭(오확정)    {pct(count(known, 'false_accept'), len(known))}   ← 안전 (0%여야 함)")
    print(f"\n[미등록 사물 관측 {len(unk)}건]")
    print(f"  올바르게 거부     {pct(count(unk, 'correct_reject'), len(unk))}")
    print(f"  오매칭(오확정)    {pct(count(unk, 'false_accept'), len(unk))}   ← 안전 (0%여야 함)")

    print("\n[시나리오별 — 등록 자산]  확정/구제/미매칭/오매칭")
    by = defaultdict(list)
    for r in known:
        by[r["scenario"]].append(r)
    for k, rs in by.items():
        print(f"  {k:11s} n={len(rs):3d}  " + "  ".join(f"{pct(count(rs, o), len(rs))}" for o in outs[:4]))
    print("\n[자산별 — 등록 자산]  확정/구제/미매칭/오매칭")
    by = defaultdict(list)
    for r in known:
        by[r["truth"]].append(r)
    for k, rs in by.items():
        print(f"  {k:8s} n={len(rs):3d}  " + "  ".join(f"{pct(count(rs, o), len(rs))}" for o in outs[:4]))
    if unk:
        print("\n[미등록 사물별]  거부/오매칭")
        by = defaultdict(list)
        for r in unk:
            by[r["truth"]].append(r)
        for k, rs in by.items():
            print(f"  {k:10s} n={len(rs):3d}  거부 {pct(count(rs, 'correct_reject'), len(rs))}  오매칭 {pct(count(rs, 'false_accept'), len(rs))}")
    wrong = [r for r in rows if r["outcome"] == "false_accept"]
    conf = defaultdict(int)
    for r in wrong:
        conf[(r["truth"], r["best"])] += 1
    if conf:
        print("\n[오매칭 혼동쌍]  정답 → 확정된 자산")
        for (a, b), n in sorted(conf.items(), key=lambda x: -x[1]):
            print(f"  {a} → {b}: {n}건")


if __name__ == "__main__":
    main()
