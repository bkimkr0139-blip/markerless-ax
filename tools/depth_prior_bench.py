#!/usr/bin/env python3
"""깊이 품질 벤치마크 — 실제 RGB-D(NYU Depth V2 val)에서 iPhone LiDAR급 깊이를 모사해 각 기법의 오차를 정답과 비교한다.

모사: 정답 깊이를 192×256으로 줄이고 잡음(0.4%+3mm)·무작위 누락 4%·4.5m 초과 무효·경계 비행 픽셀 제거·유리 같은 직사각 구멍 1개를 넣는다.
비교: ① 쌍선형 업샘플 ② RGB 가이드 업샘플(관측만) ③ 단안 AI(Depth Anything V2)를 LiDAR에 정렬해 빈 곳만 채움 — 관측 픽셀은 LiDAR 유지.
사용: .venv/bin/python tools/depth_prior_bench.py [--n 60] [--data /tmp/nyu/val/official]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import h5py
import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spatial-core"))
from spatial_core.reconstruction.depth_prior import DepthPrior, densify, fill_holes_nearest  # noqa: E402


def simulate_lidar(gt: np.ndarray, rng: np.random.Generator):
    H, W = gt.shape
    h, w = 192, 256
    d = gt[(np.arange(h) * H // h)[:, None], (np.arange(w) * W // w)[None, :]].astype(np.float64)
    d = d * (1 + rng.normal(0, 0.004, d.shape)) + rng.normal(0, 0.003, d.shape)
    valid = np.ones_like(d, bool)
    valid &= rng.random(d.shape) > 0.04
    valid &= d < 4.5
    gy, gx = np.gradient(d)
    edge = (np.hypot(gx, gy) > 0.15)
    valid &= ~(ndimage.binary_dilation(edge, iterations=1) & (rng.random(d.shape) < 0.7))
    rh, rw = int(h * rng.uniform(0.15, 0.3)), int(w * rng.uniform(0.15, 0.3))
    y0, x0 = rng.integers(0, h - rh), rng.integers(0, w - rw)
    valid[y0:y0 + rh, x0:x0 + rw] = False  # 유리/검은 면 같은 구멍
    d = np.where(valid, d, 0.0)
    return d.astype(np.float32), valid


def metrics(pred, gt, mask):
    m = mask & (pred > 0) & (gt > 0.2)
    if m.sum() < 50:
        return None
    p, g = pred[m], gt[m]
    return {"n": int(m.sum()), "absrel": float(np.mean(np.abs(p - g) / g)), "rmse_m": float(np.sqrt(np.mean((p - g) ** 2))),
            "d1": float(np.mean(np.maximum(p / g, g / p) < 1.25))}


LOCAL = True


def main() -> None:
    global LOCAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--global-affine", action="store_true")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--data", default="/tmp/nyu/val/official")
    ap.add_argument("--json", default="logs/depth_prior_bench.json")
    a = ap.parse_args()
    LOCAL = not a.global_affine
    files = sorted(Path(a.data).glob("*.h5"))[: a.n]
    prior = DepthPrior()
    print("국소 아핀:", LOCAL, "| AI 모델:", "사용" if prior.available else "없음(고전 기법만)", f"| 프레임 {len(files)}")
    rng = np.random.default_rng(0)
    acc: dict[str, list] = {k: [] for k in ("bilinear_obs", "guided_obs", "bilinear_edge", "guided_edge", "ai_hole", "nearest_hole", "ai_hole_far", "ai_hole_inrange", "nearest_hole_inrange")}
    cover = {"lidar": [], "lidar+ai": []}
    fits = []
    t_ai = []
    for fp in files:
        with h5py.File(fp) as f:
            rgb = np.array(f["rgb"]).transpose(1, 2, 0)
            gt = np.array(f["depth"]).astype(np.float32)
        gt_ok = gt > 0.2
        lr, vlr = simulate_lidar(gt, rng)
        H, W = gt.shape
        # 기준선 ①: 쌍선형(유효 가중)
        num = ndimage.zoom(np.where(vlr, lr, 0), (H / 192, W / 256), order=1)
        den = ndimage.zoom(vlr.astype(float), (H / 192, W / 256), order=1)
        bil = np.where(den > 0.5, num / np.maximum(den, 1e-6), 0)
        t0 = time.time()
        dz = densify(lr, np.where(vlr, 2, 0).astype(np.uint8), rgb, prior, local_affine=LOCAL, guided=True)
        dg = densify(lr, np.where(vlr, 2, 0).astype(np.uint8), rgb, None, guided=False)
        t_ai.append(time.time() - t0)
        sup = dz.obs_valid
        gy, gx = np.gradient(gt)
        edge = ndimage.binary_dilation(np.hypot(gx, gy) > 0.15, iterations=2)
        for key, pred, mask in (("bilinear_obs", dg.depth_obs, sup), ("guided_obs", dz.depth_obs, sup), ("bilinear_edge", dg.depth_obs, sup & edge), ("guided_edge", dz.depth_obs, sup & edge)):
            r = metrics(pred, gt, mask)
            if r:
                acc[key].append(r)
        hole = gt_ok & ~sup
        if dz.fit is not None:
            fits.append({"ok": dz.fit.ok, "r2": dz.fit.r2, "inlier": dz.fit.inlier})
        near = fill_holes_nearest(dz.depth_obs, sup)
        r = metrics(near, gt, hole)
        if r:
            acc["nearest_hole"].append(r)
        if dz.inferred_valid.any():
            r = metrics(dz.depth_inferred, gt, hole & dz.inferred_valid)
            if r:
                acc["ai_hole"].append(r)
            inr = hole & dz.inferred_valid & (gt < 4.0)
            r = metrics(dz.depth_inferred, gt, inr)
            if r:
                acc["ai_hole_inrange"].append(r)
            r = metrics(near, gt, inr)
            if r:
                acc["nearest_hole_inrange"].append(r)
            far = hole & dz.inferred_valid & (gt > 4.0)
            r = metrics(dz.depth_inferred, gt, far)
            if r:
                acc["ai_hole_far"].append(r)
        cover["lidar"].append(float((sup & gt_ok).sum() / gt_ok.sum()))
        cover["lidar+ai"].append(float(((sup | dz.inferred_valid) & gt_ok).sum() / gt_ok.sum()))

    def agg(lst):
        if not lst:
            return None
        w = np.array([x["n"] for x in lst], float)
        return {k: round(float(np.average([x[k] for x in lst], weights=w)), 4) for k in ("absrel", "rmse_m", "d1")} | {"frames": len(lst)}

    out = {k: agg(v) for k, v in acc.items()}
    out["coverage"] = {k: round(float(np.mean(v)), 3) for k, v in cover.items()}
    out["alignFit"] = {"accepted": sum(f["ok"] for f in fits), "of": len(fits), "meanR2": round(float(np.mean([f["r2"] for f in fits])), 3) if fits else None}
    out["secPerFrame"] = round(float(np.mean(t_ai)), 2)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    Path(a.json).parent.mkdir(exist_ok=True)
    Path(a.json).write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
