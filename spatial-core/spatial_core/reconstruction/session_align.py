"""세션(또는 구간) 간 정합 — 중력축(Y)이 맞춰져 있으므로 yaw + 3축 이동(4자유도)만 푼다.

순서: 바닥 높이 맞춤 → 수평 법선 각도 히스토그램 순환 상관으로 yaw 후보 → 점유 격자 FFT 상관으로 이동 후보
→ 점-대-평면 ICP(yaw+이동) 정제 → 겹침 점 비율로 평가. 대칭 방(직사각형)은 yaw 후보가 여러 개이므로
상위 두 후보의 점수가 비슷하면 `ambiguous=True`로 돌려 자동 병합을 막는다(지시서 §5.4).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from ..registration.icp import estimate_normals, icp_yaw_plane_refine


@dataclass
class AlignResult:
    transform: np.ndarray  # src → dst 4x4
    fitness: float  # 겹친 점 비율 (작은 쪽 기준)
    rmse: float
    overlap_points: int
    ambiguous: bool
    second_fitness: float
    accepted: bool
    reason: str = ""


def _voxel_down(p: np.ndarray, cell: float, cap: int = 60000) -> np.ndarray:
    q = np.floor(p / cell).astype(np.int64)
    _, idx = np.unique(q, axis=0, return_index=True)
    out = p[idx]
    if len(out) > cap:
        out = out[np.random.default_rng(0).choice(len(out), cap, replace=False)]
    return out


def _floor_y(p: np.ndarray) -> float:
    h, e = np.histogram(p[:, 1], bins=np.arange(p[:, 1].min() - 0.02, p[:, 1].max() + 0.04, 0.02))
    thr = 0.25 * h.max()
    for i in range(len(h)):
        if h[i] >= thr and (i == 0 or h[i] >= h[i - 1]) and (i + 1 >= len(h) or h[i] >= h[i + 1]):
            return float(0.5 * (e[i] + e[i + 1]))
    return float(np.percentile(p[:, 1], 1))


def _ang_hist(n: np.ndarray, nb: int = 360) -> np.ndarray:
    horiz = np.abs(n[:, 1]) < 0.3
    th = np.arctan2(n[horiz, 2], n[horiz, 0]) % (2 * np.pi)
    h, _ = np.histogram(th, bins=nb, range=(0, 2 * np.pi))
    return np.convolve(np.r_[h[-2:], h, h[:2]].astype(float), np.ones(5), mode="valid")


def _rot_y(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _occ(p: np.ndarray, lo: np.ndarray, shape: tuple[int, int], cell: float) -> np.ndarray:
    g = np.zeros(shape, np.float32)
    ij = np.floor((p[:, [0, 2]] - lo) / cell).astype(int)
    ok = (ij[:, 0] >= 0) & (ij[:, 0] < shape[0]) & (ij[:, 1] >= 0) & (ij[:, 1] < shape[1])
    np.add.at(g, (ij[ok, 0], ij[ok, 1]), 1.0)
    return np.minimum(g, 3.0)


def align_clouds(src: np.ndarray, dst: np.ndarray, cell: float = 0.05, min_fitness: float = 0.35, min_overlap: int = 1500,
                 ambiguity_margin: float = 0.03) -> AlignResult:
    s = _voxel_down(src, 0.04)
    d = _voxel_down(dst, 0.04)
    if len(s) < 500 or len(d) < 500:
        return AlignResult(np.eye(4), 0.0, 1.0, 0, False, 0.0, False, "점이 너무 적음")
    fy_s, fy_d = _floor_y(s), _floor_y(d)
    ns = estimate_normals(s)
    nd = estimate_normals(d)
    hs, hd = _ang_hist(ns), _ang_hist(nd)
    # 순환 상관 → yaw 후보 (dst ≈ rot(yaw)·src ⇒ src 각도 + yaw ≈ dst 각도)
    corr = np.fft.irfft(np.fft.rfft(hd) * np.conj(np.fft.rfft(hs)), n=len(hd))
    cand_bins = []
    order = np.argsort(-corr)
    for b in order:
        if all(min(abs(b - c), len(corr) - abs(b - c)) > 6 for c in cand_bins):
            cand_bins.append(int(b))
        if len(cand_bins) >= 6:
            break
    tree_d = cKDTree(d)
    walls_d = d[(d[:, 1] > fy_d + 0.3) & (d[:, 1] < fy_d + 2.0)]
    best = None
    scored: list[tuple[float, np.ndarray, float, int]] = []
    for b in cand_bins:
        yaw = b * 2 * np.pi / len(corr)
        R = _rot_y(-yaw)  # 각도 정의(atan2(z,x))와 Y축 회전 방향 보정
        s_rot = (s - np.array([0, fy_s, 0])) @ R.T + np.array([0, fy_d, 0])
        walls_s = s_rot[(s_rot[:, 1] > fy_d + 0.3) & (s_rot[:, 1] < fy_d + 2.0)]
        if len(walls_s) < 100 or len(walls_d) < 100:
            continue
        lo = np.minimum(walls_s[:, [0, 2]].min(0), walls_d[:, [0, 2]].min(0)) - 1.0
        hi = np.maximum(walls_s[:, [0, 2]].max(0), walls_d[:, [0, 2]].max(0)) + 1.0
        shape = tuple(np.ceil((hi - lo) / cell).astype(int))
        if shape[0] * shape[1] > 6e6:
            continue
        gs, gd = _occ(walls_s, lo, shape, cell), _occ(walls_d, lo, shape, cell)
        pad = (shape[0] * 2, shape[1] * 2)
        fs = np.fft.rfft2(gs, pad)
        fd = np.fft.rfft2(gd, pad)
        cc = np.fft.irfft2(fd * np.conj(fs), pad)
        # 상위 3개 이동 피크
        flat = np.argsort(-cc, axis=None)[:200]
        picks = []
        for fi in flat:
            ix, iz = np.unravel_index(fi, cc.shape)
            sx = ix if ix < shape[0] else ix - pad[0]
            sz = iz if iz < shape[1] else iz - pad[1]
            if all(abs(sx - a) + abs(sz - c) > 6 for a, c in picks):
                picks.append((sx, sz))
            if len(picks) >= 3:
                break
        for sx, sz in picks:
            t = np.array([sx * cell, 0.0, sz * cell])
            T0 = np.eye(4)
            T0[:3, :3] = R
            T0[:3, 3] = -R @ np.array([0, fy_s, 0]) + np.array([0, fy_d, 0]) + t
            res = icp_yaw_plane_refine(s, d, nd, T0, up_axis=1, max_iterations=25, max_correspondence_dist_m=0.10, trim_ratio=0.3, point_weight=0.05)
            T = res.transform
            ps = s @ T[:3, :3].T + T[:3, 3]
            dist, _ = tree_d.query(ps, k=1)
            inl = dist < 0.03
            fit = float(inl.mean())
            tree_s = cKDTree(ps)
            dist2, _ = tree_s.query(d, k=1)
            fit_d = float((dist2 < 0.03).mean())
            f = max(fit, fit_d)  # 작은 쪽 기준 (한쪽이 부분 겹침이어도 높은 값)
            scored.append((f, T, float(np.sqrt(np.mean(dist[inl] ** 2))) if inl.any() else 1.0, int(inl.sum())))
    if not scored:
        return AlignResult(np.eye(4), 0.0, 1.0, 0, False, 0.0, False, "후보 없음")
    scored.sort(key=lambda x: -x[0])
    f1, T1, rm1, ov1 = scored[0]
    # 두 번째 후보가 첫 후보와 의미 있게 다른 변환일 때만 모호성 판단
    second = 0.0
    for f2, T2, *_ in scored[1:]:
        dT = np.linalg.inv(T1) @ T2
        ang = abs(np.degrees(np.arctan2(dT[0, 2], dT[0, 0])))
        if np.linalg.norm(dT[:3, 3]) > 0.25 or ang > 5:
            second = f2
            break
    amb = f1 > 0 and (f1 - second) < ambiguity_margin
    ok = f1 >= min_fitness and ov1 >= min_overlap and not amb
    reason = "" if ok else ("대칭/모호: 두 후보 점수가 비슷함" if amb else "겹침 부족")
    return AlignResult(T1, f1, rm1, ov1, amb, second, ok, reason)
