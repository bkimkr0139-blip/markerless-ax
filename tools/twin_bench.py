#!/usr/bin/env python3
"""공간 재구성 벤치마크(합성 정답 기준) — 실측 Ground Truth가 아니다. 결과는 logs/twin_bench.json + 표준출력.

측정: ① 기하 정확도(벽·바닥·천장 위치, 방 치수) ② 텍스처 색 정확도·커버리지 ③ 재방문 5회 병합 안정성(면적·이중 표면)
      ④ 잡음/포즈 오차 강건성 ⑤ 성능(프레임/초, 최대 메모리). 사용: .venv/bin/python tools/twin_bench.py [--quick]
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spatial-core"))
from spatial_core.reconstruction.camera import Frame  # noqa: E402
from spatial_core.reconstruction.mesh import remove_small_components  # noqa: E402
from spatial_core.reconstruction.structure import extract_structure  # noqa: E402
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames  # noqa: E402
from spatial_core.reconstruction.texture import build_texture  # noqa: E402
from spatial_core.reconstruction.tsdf import SparseTSDF  # noqa: E402


def rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def perturb(frames: list[Frame], pos_m: float, rot_deg: float, seed: int) -> list[Frame]:
    """프레임 포즈에 임의 오차(추적 드리프트 모사)를 준다."""
    rng = np.random.default_rng(seed)
    out = []
    for f in frames:
        T = f.T_world_cam.copy()
        a = np.radians(rng.normal(0, rot_deg, 3))
        Rx = np.array([[1, 0, 0], [0, np.cos(a[0]), -np.sin(a[0])], [0, np.sin(a[0]), np.cos(a[0])]])
        Ry = np.array([[np.cos(a[1]), 0, np.sin(a[1])], [0, 1, 0], [-np.sin(a[1]), 0, np.cos(a[1])]])
        Rz = np.array([[np.cos(a[2]), -np.sin(a[2]), 0], [np.sin(a[2]), np.cos(a[2]), 0], [0, 0, 1]])
        T[:3, :3] = T[:3, :3] @ (Rx @ Ry @ Rz)
        T[:3, 3] += rng.normal(0, pos_m, 3)
        out.append(Frame(f.index, T, f.K_rgb, f.rgb_size, f.depth, f.conf, f.rgb))
    return out


def fuse(frames, voxel=0.04, stride=2, track=False):
    from spatial_core.reconstruction.pose_track import PoseTracker

    t = SparseTSDF(voxel=voxel)
    tr = PoseTracker(t) if track else None
    t0 = time.time()
    for i, f in enumerate(frames):
        if tr is not None:
            f, _ = tr.correct(f, i)
        t.integrate(f, stride=stride)
    return t, time.time() - t0


def geom_error(mesh, size):
    """바닥/천장/벽 4면 근처 면의 평면 오차(mm)."""
    c, n, a = mesh.face_centers(), mesh.face_normals(), mesh.face_areas()
    X, Y, Z = size
    planes = [(0, 0.0, 1), (0, X, -1), (1, 0.0, 1), (1, Y, -1), (2, 0.0, 1), (2, Z, -1)]
    errs = []
    for ax, val, sgn in planes:
        sel = (n[:, ax] * sgn > 0.9) & (np.abs(c[:, ax] - val) < 0.12)
        if sel.sum() < 50:
            continue
        w = a[sel]
        errs.append(float(np.average(np.abs(c[sel, ax] - val), weights=w)) * 1000)
    return errs


def color_error(room, mesh, tm):
    """텍스처 색을 정답 albedo와 비교 (관측된 텍셀만, 면 중심 샘플)."""
    c, n = mesh.face_centers(), mesh.face_normals()
    errs = []
    X, Y, Z = room.size
    idx = np.arange(0, len(c), max(1, len(c) // 4000))
    # 정답 라벨: 가장 가까운 평면(법선 축 기준)
    for fi in idx:
        ax = int(np.argmax(np.abs(n[fi])))
        sgn = n[fi, ax] > 0
        label = {(0, True): 0, (0, False): 1, (2, True): 2, (2, False): 3, (1, True): 4, (1, False): 5}[(ax, bool(sgn))]
        lab = np.array([label])
        gt = room._albedo(c[fi][None] + 0 * n[fi][None], lab)[0]
        uv = tm.uv[tm.f[fi]].mean(0)
        x, y = int(uv[0] * tm.atlas.shape[1]), int(uv[1] * tm.atlas.shape[0])
        if 0 <= y < tm.atlas.shape[0] and 0 <= x < tm.atlas.shape[1] and tm.observed_mask[y, x]:
            errs.append(np.abs(tm.atlas[y, x].astype(float) - gt).mean())
    return float(np.mean(errs)) if errs else None, len(errs)


def quality_techniques(quick: bool) -> dict:
    """정밀·품질 기법별 효과(합성 정답). 효과가 입증된 기법만 파이프라인 기본값으로 켠다 — 결과는 docs/REPORT.md에 그대로 기록."""
    from spatial_core.reconstruction.pose_graph import apply_to_frames, optimize, so3_exp
    from spatial_core.reconstruction.refine_mesh import regularize

    out: dict = {}
    room = SynthRoom(size=(5.0, 2.7, 4.0))
    base = orbit_frames(room, n=120, rgb_size=(160, 120), depth_size=(80, 60))

    def spread(mesh):
        c, n = mesh.face_centers(), mesh.face_normals()
        w = (np.abs(c[:, 0]) < 0.3) & (np.abs(n[:, 0]) > 0.9)
        return round(float(np.percentile(c[w, 0], 95) - np.percentile(c[w, 0], 5)) * 1000, 1)

    def drift(frames, sig_t, sig_r, seed):
        rng = np.random.default_rng(seed)
        G = np.eye(4)
        res = []
        for f in frames:
            d = np.eye(4)
            d[:3, :3] = so3_exp(np.radians(rng.normal(0, sig_r, 3)))
            d[:3, 3] = rng.normal(0, sig_t, 3)
            G = d @ G
            res.append(Frame(f.index, G @ f.T_world_cam, f.K_rgb, f.rgb_size, f.depth, f.conf, f.rgb))
        return res

    pg = {}
    for name, (st_, sr) in {"drift 2mm/프레임·0.05°": (0.002, 0.05), "drift 4mm/프레임·0.1°": (0.004, 0.1)}.items():
        dr = drift(base, st_, sr, 1)
        _t, m0 = fuse(dr)[0], remove_small_components(fuse(dr)[0].extract_mesh(), 0.1)
        res = optimize(dr, key_step=4)
        _t2, _ = fuse(apply_to_frames(dr, res, 4))
        m1 = remove_small_components(_t2.extract_mesh(), 0.1)
        pg[name] = {"wallLayerSpreadMm": {"보정 전": spread(m0), "포즈 그래프": spread(m1)}, "accepted": res.accepted, "loops": res.loops}
    clean = optimize(base, key_step=4)
    pg["drift 없음(회귀)"] = {"accepted": clean.accepted}
    out["poseGraph"] = pg

    # 메시 정규화
    box = ((1.2, 0.0, 1.0), (0.5, 0.8, 0.5))
    rm = SynthRoom(size=(5, 2.7, 4), boxes=(box,))
    reg = {}
    for noise in (0.003, 0.008):
        rng = np.random.default_rng(1)
        fr = [Frame(f.index, f.T_world_cam, f.K_rgb, f.rgb_size, (f.depth + rng.normal(0, noise, f.depth.shape) * (f.depth > 0)).astype(np.float32), f.conf, f.rgb)
              for f in orbit_frames(rm, n=90)]
        t, _ = fuse(fr, voxel=0.03)
        m = remove_small_components(t.extract_mesh(), 0.1)
        r, stats = regularize(m, extract_structure(m, t.surface_points()))

        def flat(mm):
            X, Y, Z = rm.size
            e = [np.abs(mm.v[np.abs(mm.v[:, ax] - val) < 0.04, ax] - val).mean() * 1000 for ax, val in ((0, 0.0), (0, X), (1, 0.0), (1, Y), (2, 0.0), (2, Z))]
            return round(float(np.mean(e)), 2)

        reg[f"깊이 잡음 {noise * 1000:.0f}mm"] = {"구조면 평균 평면편차(mm)": {"전": flat(m), "후": flat(r)}, "평면오차(mm)": {"전": round(float(np.mean(geom_error(m, rm.size))), 2),
                                                                                          "후": round(float(np.mean(geom_error(r, rm.size))), 2)}, "corners": stats.corners}
    out["meshRegularize"] = reg
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    out: dict = {}
    room = SynthRoom(size=(5.0, 2.7, 4.0), door=("z0", 1.0, 2.0, 2.0))
    n = 60 if a.quick else 120
    frames = orbit_frames(room, n=n, rgb_size=(320, 240), depth_size=(80, 60))
    t, dt = fuse(frames)
    mesh = remove_small_components(t.extract_mesh(), 0.1)
    errs = geom_error(mesh, room.size)
    st = extract_structure(mesh, t.surface_points())
    P = np.array(st.polygon)
    ext = np.sort(P.max(0) - P.min(0))
    out["geometry"] = {"planeErrMm": [round(e, 1) for e in errs], "planeErrMeanMm": round(float(np.mean(errs)), 1),
                       "roomDimErrMm": [round(abs(ext[0] - 4.0) * 1000, 1), round(abs(ext[1] - 5.0) * 1000, 1)],
                       "coverage": round(st.overall_coverage, 3), "walls": len(st.walls), "doorFound": any(o.kind == "DOOR" for o in st.openings)}
    tm = build_texture(mesh, frames, atlas_px=1024)
    ce, nce = color_error(room, mesh, tm)
    out["texture"] = {"meanAbsColorErr(0-255)": None if ce is None else round(ce, 1), "samples": nce, **{k: tm.stats[k] for k in ("textureCoverage", "unobservedFaces", "texelCm")}}

    # 재방문 5회: 면적 드리프트·이중 표면
    area0 = mesh.area()
    thick = []
    for k in range(5):
        for f in orbit_frames(room, n=30, seed=100 + k):
            t.integrate(f, stride=2)
        m = remove_small_components(t.extract_mesh(), 0.1)
        c, nn = m.face_centers(), m.face_normals()
        w = (np.abs(c[:, 0]) < 0.2) & (np.abs(nn[:, 0]) > 0.9)
        thick.append(round(float(np.percentile(c[w, 0], 95) - np.percentile(c[w, 0], 5)) * 1000, 1))
    out["revisit5"] = {"areaDriftPct": round(100 * (m.area() - area0) / area0, 2), "wallThicknessMm": thick}

    # 잡음/포즈 오차
    rob = {}
    for name, (pos, rot) in {"pose±0cm": (0, 0), "pose±1cm/0.3°": (0.01, 0.3), "pose±2cm/0.6°": (0.02, 0.6), "pose±4cm/1.2°": (0.04, 1.2)}.items():
        row = {}
        for tag, track in (("noTrack", False), ("track", True)):
            tt, _ = fuse(perturb(frames, pos, rot, 1), track=track)
            mm = remove_small_components(tt.extract_mesh(), 0.1)
            c, nn = mm.face_centers(), mm.face_normals()
            w = (np.abs(c[:, 0]) < 0.25) & (np.abs(nn[:, 0]) > 0.9)
            row[tag] = {"planeErrMeanMm": round(float(np.mean(geom_error(mm, room.size))), 1),
                        "wallLayerSpreadMm": round(float(np.percentile(c[w, 0], 95) - np.percentile(c[w, 0], 5)) * 1000, 1) if w.sum() > 50 else None}
        rob[name] = row
    out["poseRobustness"] = rob

    # 성능: 실기기 규모(깊이 256×192, RGB 960×720, 300프레임)
    if not a.quick:
        big = orbit_frames(room, n=300, rgb_size=(960, 720), depth_size=(256, 192))
        tb, dtb = fuse(big, voxel=0.03, stride=2)
        t1 = time.time()
        mb = remove_small_components(tb.extract_mesh(), 0.1)
        t_mesh = time.time() - t1
        t2 = time.time()
        tmb = build_texture(mb, big[::2], atlas_px=2048)
        t_tex = time.time() - t2
        out["performance"] = {"frames": 300, "fuseSec": round(dtb, 1), "fuseFps": round(300 / dtb, 1), "meshSec": round(t_mesh, 1), "textureSec": round(t_tex, 1),
                              "faces": int(len(mb.f)), "blocks": tb.block_count, "peakRssMB": round(rss_mb()), "textureCoverage": tmb.stats["textureCoverage"]}
    out["quality"] = quality_techniques(a.quick)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    Path("logs").mkdir(exist_ok=True)
    Path("logs/twin_bench.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
