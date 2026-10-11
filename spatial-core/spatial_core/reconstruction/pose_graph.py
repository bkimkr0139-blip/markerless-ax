"""포즈 그래프 최적화(루프 클로저) — VIO 드리프트(시간에 따라 누적되는 cm 단위 오차)를 되돌린다.

키프레임 사이의 연속 간선(주행 거리계, 국소적으로 정확)과, 시야가 겹치지만 시간상 먼 쌍의 ICP 루프 간선을 묶어 전체 포즈를 푼다.
루프 간선의 정보 행렬은 점-대-평면 ICP 헤시안에서 나온다 → 평면 위로 미끄러지는 방향은 정보가 작아 자동으로 약하게 반영된다
(프레임-모델 보정이 실패했던 이유가 이 퇴화였다). 병합은 '보정 후 잔차가 실제로 줄었을 때만' 채택한다.
회전은 월드 원점 기준 트위스트(ω,v): 갱신 p' = exp(ξ)·p, 잔차 r = log(T_j · T_pred⁻¹) (월드 프레임).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.spatial import cKDTree

from ..registration.icp import estimate_normals
from .camera import Frame, unproject


def _skew(w):
    return np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]])


def so3_exp(w):
    th = np.linalg.norm(w)
    if th < 1e-12:
        return np.eye(3) + _skew(w)
    K = _skew(w / th)
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


def so3_log(R):
    c = np.clip((np.trace(R) - 1) / 2, -1, 1)
    th = np.arccos(c)
    if th < 1e-9:
        return np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / 2
    return np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) * th / (2 * np.sin(th))


def se3_from(w, t):
    T = np.eye(4)
    T[:3, :3] = so3_exp(w)
    T[:3, 3] = t
    return T


def pose_log(T):
    """T를 (ω, t)로 — 회전벡터와 이동(단순 분리 표현; 작은 잔차에서 충분)."""
    return np.hstack([so3_log(T[:3, :3]), T[:3, 3]])


@dataclass
class PGOResult:
    poses: list[np.ndarray]  # 키프레임 보정 포즈 (카메라→월드)
    loops: int
    accepted: bool
    before_rms_mm: float
    after_rms_mm: float
    info: dict


def _pair_icp(src: np.ndarray, dst_pts: np.ndarray, dst_n: np.ndarray, tree: cKDTree, iters: int = 10, gate: float = 0.08):
    """src(월드, 현재 포즈)를 dst에 맞추는 월드 프레임 보정 G와 그 정보 행렬(6×6, [ω,t]), 잔차 RMS, 인라이어 수."""
    R = np.eye(3)
    t = np.zeros(3)
    H = np.zeros((6, 6))
    used = 0
    rms = 9.0
    for _ in range(iters):
        cur = src @ R.T + t
        d, idx = tree.query(cur, k=1)
        ok = d < gate
        if ok.sum() < 80:
            return None
        p, q, n = cur[ok], dst_pts[idx[ok]], dst_n[idx[ok]]
        r = np.einsum("ij,ij->i", p - q, n)
        keep = np.abs(r) <= np.quantile(np.abs(r), 0.75)
        p, n, r = p[keep], n[keep], r[keep]
        J = np.hstack([np.cross(p, n), n])
        H = J.T @ J
        dx = np.linalg.solve(H + 1e-9 * np.eye(6) + np.diag([1e-3] * 3 + [1e-2] * 3), -J.T @ r)
        dR = so3_exp(dx[:3])
        R, t = dR @ R, dR @ t + dx[3:]
        used = len(r)
        rms = float(np.sqrt(np.mean(r ** 2)))
        if np.linalg.norm(dx[3:]) < 1e-5 and np.linalg.norm(dx[:3]) < 1e-6:
            break
    G = np.eye(4)
    G[:3, :3], G[:3, 3] = R, t
    return G, H, rms, used


def optimize(frames: list[Frame], key_step: int = 4, min_gap: int = 8, max_loops: int = 400, sigma_odo_t: float = 0.004, sigma_odo_r_deg: float = 0.15,
             sigma_loop: float = 0.006) -> PGOResult:
    """frames(카메라→월드 포즈 포함)에서 키프레임을 뽑아 그래프를 풀고, 키프레임 보정 포즈를 돌려준다."""
    keys = list(range(0, len(frames), key_step))
    N = len(keys)
    T0 = [frames[k].T_world_cam.copy() for k in keys]
    pts, nrm, trees, cams = [], [], [], []
    for k in keys:
        fr = frames[k]
        dh, dw = fr.depth.shape
        stride = max(1, int(round(np.sqrt(dh * dw / 1500.0))))
        p, _ = unproject(fr, stride=stride, min_conf=1)
        if len(p) < 100:
            pts.append(p), nrm.append(np.zeros((0, 3))), trees.append(None), cams.append(fr.center)
            continue
        pts.append(p)
        nrm.append(estimate_normals(p, k=10))
        trees.append(cKDTree(p))
        cams.append(fr.center)
    # 루프 후보: 시간상 멀고(min_gap 키프레임 이상) 시야가 가까운 쌍
    loops = []
    for i in range(N):
        for j in range(i + min_gap, N):
            if trees[i] is None or trees[j] is None:
                continue
            # 점군 중심이 서로의 시야 범위에 겹치는지(거리 기준)
            ci, cj = pts[i].mean(0), pts[j].mean(0)
            if np.linalg.norm(ci - cj) < 2.5 and np.linalg.norm(cams[i] - cams[j]) < 5.0:
                loops.append((i, j))
    if len(loops) > max_loops:
        sel = np.random.default_rng(0).choice(len(loops), max_loops, replace=False)
        loops = [loops[s] for s in sorted(sel)]
    edges = []  # (i, j, Z(world-frame predicted offset as 4x4 'T_pred relation'), sqrt-info 6x6, kind)
    meas_rms = []
    for i in range(N - 1):  # 주행 거리 간선: T_pred_j = T_i^-1 ... 월드 프레임 잔차용으로 Z_ij = T_i^-1 T_j
        Z = np.linalg.inv(T0[i]) @ T0[i + 1]
        L = np.diag([1 / np.radians(sigma_odo_r_deg)] * 3 + [1 / sigma_odo_t] * 3)
        edges.append((i, i + 1, Z, L, "odo"))
    accepted_loops = 0
    for i, j in loops:
        res = _pair_icp(pts[j], pts[i], nrm[i], trees[i])
        if res is None:
            continue
        G, H, rms, used = res
        ang = np.degrees(np.linalg.norm(so3_log(G[:3, :3])))
        tr = np.linalg.norm(G[:3, 3])
        if used < 150 or rms > 0.02 or tr > 0.12 or ang > 6:
            continue
        # j를 G로 옮긴 포즈 → i 기준 상대 Z. 정보 행렬 = H/σ² (월드 프레임 [ω,t])
        Tj_new = G @ T0[j]
        Z = np.linalg.inv(T0[i]) @ Tj_new
        info = H / sigma_loop ** 2
        w, v = np.linalg.eigh(info + 1e-6 * np.eye(6))
        L = (v * np.sqrt(np.maximum(w, 0))) @ v.T
        edges.append((i, j, Z, L, "loop"))
        meas_rms.append(rms)
        accepted_loops += 1
    if accepted_loops < 3:
        return PGOResult(T0, accepted_loops, False, 0.0, 0.0, {"reason": f"루프 간선 {accepted_loops}개 — 최적화 안 함"})

    def pack(Ts):
        return np.hstack([np.hstack([so3_log(T[:3, :3]), T[:3, 3]]) for T in Ts])

    def unpack(x):
        return [se3_from(x[6 * k: 6 * k + 3], x[6 * k + 3: 6 * k + 6]) for k in range(N)]

    x0 = pack(T0)

    def residual(x):
        Ts = unpack(x)
        out = [x[:6] - x0[:6]]  # 게이지: 첫 키프레임 고정
        out[0] = out[0] * 1e4
        for i, j, Z, L, kind in edges:
            Tpred = Ts[i] @ Z
            D = Ts[j] @ np.linalg.inv(Tpred)  # 월드 프레임 잔차 변환
            r = np.hstack([so3_log(D[:3, :3]), D[:3, 3]])
            out.append(L @ r)
        return np.concatenate(out)

    S = lil_matrix((6 + 6 * len(edges), 6 * N), dtype=int)
    for k in range(6):
        S[k, k] = 1
    for e, (i, j, *_r) in enumerate(edges):
        S[6 + 6 * e: 12 + 6 * e, 6 * i: 6 * i + 6] = 1
        S[6 + 6 * e: 12 + 6 * e, 6 * j: 6 * j + 6] = 1
    r0 = residual(x0)
    sol = least_squares(residual, x0, jac_sparsity=S, method="trf", x_scale=1.0, max_nfev=30)
    Ts = unpack(sol.x)
    c0 = float(np.sqrt(np.mean(r0 ** 2)))
    c1 = float(np.sqrt(np.mean(residual(sol.x) ** 2)))
    ok = c1 < 0.8 * c0
    return PGOResult(Ts if ok else T0, accepted_loops, bool(ok), c0, c1, {"edges": len(edges), "loops": accepted_loops, "cost0": c0, "cost1": c1,
                                                                           "meanLoopRmsMm": round(1000 * float(np.mean(meas_rms)), 2)})


def apply_to_frames(frames: list[Frame], res: PGOResult, key_step: int) -> list[Frame]:
    """키프레임 보정(월드 프레임 변환 G_k = T_k' T_k⁻¹)을 사이 프레임에 보간 없이 가장 가까운 키프레임 값으로 적용."""
    if not res.accepted:
        return frames
    keys = list(range(0, len(frames), key_step))
    G = [res.poses[k] @ np.linalg.inv(frames[keys[k]].T_world_cam) for k in range(len(keys))]
    out = []
    for idx, f in enumerate(frames):
        k = min(len(keys) - 1, int(round(idx / key_step)))
        out.append(Frame(f.index, G[k] @ f.T_world_cam, f.K_rgb, f.rgb_size, f.depth, f.conf, f.rgb, f.exposure, f.blur, f.mask))
    return out
