"""numpy 전용 ICP — point-to-point(coarse) + point-to-plane(fine) 2단 정합.

개발지시서 9.3/9.4절의 정밀화 단계. PoC v1 설계:
- 대응점 탐색은 청크 단위 브루트포스(부분집합 N≤3000 × M≤24000 수준에서 충분히 빠름)
- 아웃라이어 처리: correspondence 거리 상한 + 잔차 하위 (1-trim_ratio) 트리밍
- 초깃값: 클라우드가 공간 프레임이므로 항등 (COORDINATE.md 3절)
- 2단 구성: p2p로 수 cm 포획 → p2plane이 대칭/평면 사물의 접선 미끄럼 제거
  (p2p 단독은 법선방향 잔차만 안정적으로 잡아 접선방향으로 미끄러진다 — 실측 확인)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class ICPResult:
    """source를 target에 정합시키는 강체변환 결과.

    transform: 4x4 동차행렬. p_target = transform[:3,:3] @ p_source + transform[:3,3]
    rmse: 최종 인라이어 대응점 잔차 RMS (미터)
    inlier_ratio: 최종 라운드에서 대응거리 상한을 통과한 source 점 비율
    """

    transform: np.ndarray
    rmse: float
    inlier_ratio: float
    iterations: int
    converged: bool


try:  # scipy가 있으면 KD-tree (수십 배 빠름), 없으면 브루트포스로 폴백
    from scipy.spatial import cKDTree as _KDTree
except ImportError:  # pragma: no cover
    _KDTree = None

_TREE_CACHE: dict[int, tuple[np.ndarray, "_KDTree"]] = {}  # id(dst) -> (dst 참조, tree)
_TREE_CACHE_MAX = 8


def _tree_for(dst: np.ndarray):
    """dst별 KD-tree 캐시. dst 참조를 같이 들고 있어 id 재사용으로 인한 오염을 막는다."""
    hit = _TREE_CACHE.get(id(dst))
    if hit is not None and hit[0] is dst:
        return hit[1]
    tree = _KDTree(dst)
    if len(_TREE_CACHE) >= _TREE_CACHE_MAX:
        _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
    _TREE_CACHE[id(dst)] = (dst, tree)
    return tree


def _nearest(src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """src(N,3) 각 점의 dst(M,3) 최근점 거리와 인덱스."""
    if _KDTree is not None:
        d, j = _tree_for(dst).query(src, workers=-1 if len(src) >= 20_000 else 1)  # 작은 질의는 스레드 기동 비용이 더 크다
        return d.astype(np.float64, copy=False), j.astype(np.int64, copy=False)
    n = src.shape[0]
    dists = np.empty(n, dtype=np.float64)
    idxs = np.empty(n, dtype=np.int64)
    chunk = 1024  # 거리 행렬 청크 — 메모리 상한 관리 (1024×12000×8B ≈ 98MB)
    dst_sq = np.sum(dst * dst, axis=1)
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        d2 = (
            np.sum(src[s:e] * src[s:e], axis=1)[:, None]
            - 2.0 * (src[s:e] @ dst.T)
            + dst_sq[None, :]
        )
        j = np.argmin(d2, axis=1)
        idxs[s:e] = j
        dists[s:e] = np.sqrt(np.maximum(d2[np.arange(e - s), j], 0.0))
    return dists, idxs


def _rigid_step(p: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """대응점쌍 (p→q)에 대한 최소제곱 강체변환 (Kabsch/SVD)."""
    pc = p.mean(axis=0)
    qc = q.mean(axis=0)
    H = (p - pc).T @ (q - qc)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1.0, 1.0, d])
    R = Vt.T @ D @ U.T
    t = qc - R @ pc
    return R, t


def icp_align(
    source: np.ndarray,
    target: np.ndarray,
    init_transform: np.ndarray | None = None,
    max_iterations: int = 30,
    max_correspondence_dist_m: float | None = None,
    trim_ratio: float = 0.2,
    tolerance: float = 1e-7,
) -> ICPResult:
    """point-to-point ICP.

    source : (N,3) 이동시킬 점집합 (측정 포인트 클라우드, 미터)
    target : (M,3) 고정 점집합 (자산 메시 샘플, 미터)
    init_transform : 초깃값 4x4 (예: designPose). None이면 항등.
    max_correspondence_dist_m : 대응점 최대 거리 상한. None이면 trim만 사용.
    trim_ratio : 라운드마다 버릴 최악 대응점 비율 (0~0.9)
    tolerance : R/t 스텝 변화가 이 값 이하이면 수렴
    """
    src = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    if len(src) < 3 or len(dst) < 3:
        raise ValueError("icp_align requires >=3 points in both clouds")

    T = np.eye(4) if init_transform is None else np.asarray(init_transform, dtype=np.float64).copy()
    R = T[:3, :3]
    t = T[:3, 3]

    keep_n = max(3, int(round(len(src) * (1.0 - min(max(trim_ratio, 0.0), 0.9)))))
    converged = False
    used = 0

    for it in range(1, max_iterations + 1):
        used = it
        cur = src @ R.T + t
        dists, idxs = _nearest(cur, dst)

        order = np.argsort(dists)
        keep = order[:keep_n]
        if max_correspondence_dist_m is not None:
            keep = keep[dists[keep] <= max_correspondence_dist_m]
        if len(keep) < 3:
            break

        R_step, t_step = _rigid_step(cur[keep], dst[idxs[keep]])
        R = R_step @ R
        t = R_step @ t + t_step

        step = max(
            float(np.abs(R_step - np.eye(3)).max()),
            float(np.abs(t_step).max()),
        )
        if step < tolerance:
            converged = True
            break

    final = src @ R.T + t
    dists, idxs = _nearest(final, dst)
    if max_correspondence_dist_m is not None:
        mask = dists <= max_correspondence_dist_m
    else:
        mask = np.ones(len(dists), dtype=bool)
    n_in = int(mask.sum())
    # 마스크가 비면 rmse=inf (NaN 방지) — 신뢰도 강하로 이어진다
    rmse = float(np.sqrt(np.mean(dists[mask] ** 2))) if n_in > 0 else float("inf")
    inlier_ratio = n_in / len(dists)

    T_out = np.eye(4)
    T_out[:3, :3] = R
    T_out[:3, 3] = t
    return ICPResult(
        transform=T_out,
        rmse=rmse,
        inlier_ratio=inlier_ratio,
        iterations=used,
        converged=converged,
    )


def icp_plane_align(
    source: np.ndarray,
    target: np.ndarray,
    target_normals: np.ndarray,
    init_transform: np.ndarray | None = None,
    max_iterations: int = 30,
    max_correspondence_dist_m: float | None = None,
    trim_ratio: float = 0.2,
    tolerance: float = 1e-7,
) -> ICPResult:
    """point-to-plane ICP (개발지시서 9.3 권장).

    대칭·평면 위주 사물에서 point-to-point의 접선 미끄럼을 억제한다.
    target_normals: target과 1:1 대응되는 단위 법선 (M,3).
    최종 rmse는 잔차(점-평면 거리) RMS.
    """
    src = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    nrm = np.asarray(target_normals, dtype=np.float64).reshape(-1, 3)
    if len(src) < 3 or len(dst) != len(nrm) or len(dst) < 3:
        raise ValueError("icp_plane_align requires matching target/normals, >=3 points")

    R = np.eye(3) if init_transform is None else np.asarray(init_transform, dtype=np.float64)[:3, :3].copy()
    t = np.zeros(3) if init_transform is None else np.asarray(init_transform, dtype=np.float64)[:3, 3].copy()

    keep_n = max(3, int(round(len(src) * (1.0 - min(max(trim_ratio, 0.0), 0.9)))))
    converged = False
    used = 0

    for it in range(1, max_iterations + 1):
        used = it
        cur = src @ R.T + t
        dists, idxs = _nearest(cur, dst)

        order = np.argsort(dists)
        keep = order[:keep_n]
        if max_correspondence_dist_m is not None:
            keep = keep[dists[keep] <= max_correspondence_dist_m]
        if len(keep) < 6:  # 6-DOF 최소 제약
            break

        p = cur[keep]
        q = dst[idxs[keep]]
        n = nrm[idxs[keep]]

        # 선형화: n·(p + ω×p + Δt − q) = 0 → A[ω,Δt] = b
        # A_i = [p_i × n_i, n_i], b_i = n_i·(q_i − p_i)
        A = np.hstack([np.cross(p, n), n])
        b = np.einsum("ij,ij->i", q - p, n)
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        w, dt = sol[:3], sol[3:]

        theta = float(np.linalg.norm(w))
        if theta < 1e-12:
            R_step, t_step = np.eye(3), dt
        else:
            k = w / theta
            K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            R_step = np.eye(3) + np.sin(theta) * K + (1 - np.cos(theta)) * (K @ K)
            t_step = dt
        R = R_step @ R
        t = R_step @ t + t_step

        step = max(float(np.abs(R_step - np.eye(3)).max()), float(np.abs(t_step).max()))
        if step < tolerance:
            converged = True
            break

    final = src @ R.T + t
    dists, idxs = _nearest(final, dst)
    if max_correspondence_dist_m is not None:
        mask = dists <= max_correspondence_dist_m
    else:
        mask = np.ones(len(dists), dtype=bool)
    n_in = int(mask.sum())
    if n_in > 0:
        # 최종 잔차는 점-평면 거리로 계산 (회전/법선 방향 품질 반영)
        plane_res = np.einsum(
            "ij,ij->i", nrm[idxs[mask]], final[mask] - dst[idxs[mask]]
        )
        rmse = float(np.sqrt(np.mean(plane_res**2)))
    else:
        rmse = float("inf")

    T_out = np.eye(4)
    T_out[:3, :3] = R
    T_out[:3, 3] = t
    return ICPResult(
        transform=T_out,
        rmse=rmse,
        inlier_ratio=n_in / len(dists),
        iterations=used,
        converged=converged,
    )


def icp_yaw_align(
    source: np.ndarray,
    target: np.ndarray,
    init_transform: np.ndarray | None = None,
    up_axis: int = 1,
    max_iterations: int = 30,
    max_correspondence_dist_m: float | None = None,
    trim_ratio: float = 0.2,
    tolerance: float = 1e-7,
) -> ICPResult:
    """4자유도 ICP: 수직축(up) 회전(yaw) + 3축 이동만 푼다.

    AR 세션 월드는 중력 정렬이라 사물의 기울기(roll/pitch)는 없다. 6자유도 ICP는 희소·부분 관측에서
    기울어져 수렴하기 쉽고 오버레이가 비뚤어 보인다 — yaw를 닫힌 형식(2D Procrustes)으로 풀어 막는다.
    """
    src = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    if len(src) < 3 or len(dst) < 3:
        raise ValueError("icp_yaw_align requires >=3 points in both clouds")
    pa, pb = [i for i in range(3) if i != up_axis]

    T = np.eye(4) if init_transform is None else np.asarray(init_transform, dtype=np.float64).copy()
    R, t = T[:3, :3].copy(), T[:3, 3].copy()
    keep_n = max(3, int(round(len(src) * (1.0 - min(max(trim_ratio, 0.0), 0.9)))))
    converged, used = False, 0
    for it in range(1, max_iterations + 1):
        used = it
        cur = src @ R.T + t
        dists, idxs = _nearest(cur, dst)
        keep = np.argsort(dists)[:keep_n]
        if max_correspondence_dist_m is not None:
            keep = keep[dists[keep] <= max_correspondence_dist_m]
        if len(keep) < 3:
            break
        p_, q_ = cur[keep], dst[idxs[keep]]
        pc, qc = p_.mean(axis=0), q_.mean(axis=0)
        pp, qq = p_[:, [pa, pb]] - pc[[pa, pb]], q_[:, [pa, pb]] - qc[[pa, pb]]
        cross = float(np.sum(pp[:, 0] * qq[:, 1] - pp[:, 1] * qq[:, 0]))
        dot = float(np.sum(pp[:, 0] * qq[:, 0] + pp[:, 1] * qq[:, 1]))
        alpha = np.arctan2(cross, dot)  # (pa,pb) 평면 2D 회전각
        c, s_ = np.cos(alpha), np.sin(alpha)
        R_step = np.eye(3)
        R_step[pa, pa], R_step[pa, pb], R_step[pb, pa], R_step[pb, pb] = c, -s_, s_, c
        t_step = qc - R_step @ pc
        R = R_step @ R
        t = R_step @ t + t_step
        if max(abs(alpha), float(np.abs(t_step).max())) < tolerance:
            converged = True
            break

    final = src @ R.T + t
    dists, _ = _nearest(final, dst)
    mask = dists <= max_correspondence_dist_m if max_correspondence_dist_m is not None else np.ones(len(dists), bool)
    n_in = int(mask.sum())
    rmse = float(np.sqrt(np.mean(dists[mask] ** 2))) if n_in > 0 else float("inf")
    T_out = np.eye(4)
    T_out[:3, :3], T_out[:3, 3] = R, t
    return ICPResult(transform=T_out, rmse=rmse, inlier_ratio=n_in / len(dists), iterations=used, converged=converged)


def estimate_normals(points: np.ndarray, k: int = 16) -> np.ndarray:
    """점군 표면 법선(부호 없음): 이웃 k개 공분산의 최소 고유벡터.

    앱이 올리는 모델 GLB는 점마다 작은 삼각형이라 삼각형 법선이 의미 없다 — 점 이웃으로 새로 구한다."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) < k + 1 or _KDTree is None:
        return np.tile([0.0, 1.0, 0.0], (len(pts), 1))
    _, idx = _tree_for(pts).query(pts, k=k, workers=-1 if len(pts) >= 20_000 else 1)
    nb = pts[idx] - pts[idx].mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb)
    w, v = np.linalg.eigh(cov)
    return v[:, :, 0]


def icp_yaw_plane_refine(
    source: np.ndarray,
    target: np.ndarray,
    target_normals: np.ndarray,
    init_transform: np.ndarray,
    up_axis: int = 1,
    max_iterations: int = 20,
    max_correspondence_dist_m: float = 0.05,
    trim_ratio: float = 0.2,
    point_weight: float = 0.08,
    tolerance: float = 1e-6,
) -> ICPResult:
    """4자유도(수직축 yaw + 이동) 점-면 정밀화: 평평한 면에서 점이 면을 따라 미끄러지는(옆으로 빗나가는) 문제를 줄인다.

    점-점 ICP는 법선 방향만 안정적으로 맞추고 면 방향·회전은 미끄러진다 — 상자 한 면만 보여도 yaw가 ±10°+ 흔들린다.
    여기서는 대응마다 법선 방향 오차(주)에 점-점 오차(약, point_weight)를 섞어 경계(가장자리)도 잡는다.
    init_transform: source(세션) → target(모델 로컬) 변환. 법선은 target 프레임이라 회전하지 않는다.
    """
    src = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    nrm = np.asarray(target_normals, dtype=np.float64).reshape(-1, 3)
    T = np.asarray(init_transform, dtype=np.float64)
    R, t = T[:3, :3].copy(), T[:3, 3].copy()
    yaw_axis = np.zeros(3)
    yaw_axis[up_axis] = 1.0
    keep_n = max(6, int(round(len(src) * (1.0 - min(max(trim_ratio, 0.0), 0.9)))))
    converged, used = False, 0
    for it in range(1, max_iterations + 1):
        used = it
        cur = src @ R.T + t
        d, j = _nearest(cur, dst)
        keep = np.argsort(d)[:keep_n]
        keep = keep[d[keep] <= max_correspondence_dist_m]
        if len(keep) < 12:
            break
        p, q, n = cur[keep], dst[j[keep]], nrm[j[keep]]
        c = p.mean(axis=0)
        a = p - c
        rot = np.cross(yaw_axis, a)  # d(Ry(θ)a)/dθ
        r = q - p
        # 미지수 u = (θ, tx, ty, tz) — 법선 항(가중 1) + 점-점 항(가중 point_weight)
        g_n = np.concatenate([np.sum(n * rot, axis=1, keepdims=True), n], axis=1)
        rn = np.sum(n * r, axis=1)
        AtA = g_n.T @ g_n
        Atb = g_n.T @ rn
        for ax in range(3):
            g = np.zeros((len(p), 4))
            g[:, 0] = rot[:, ax]
            g[:, 1 + ax] = 1.0
            AtA += point_weight * (g.T @ g)
            Atb += point_weight * (g.T @ r[:, ax])
        AtA += 1e-6 * np.eye(4)
        try:
            u = np.linalg.solve(AtA, Atb)
        except np.linalg.LinAlgError:
            break
        theta = float(np.clip(u[0], -0.35, 0.35))
        c_, s_ = np.cos(theta), np.sin(theta)
        K = np.array([[0, -yaw_axis[2], yaw_axis[1]], [yaw_axis[2], 0, -yaw_axis[0]], [-yaw_axis[1], yaw_axis[0], 0]])
        R_step = np.eye(3) + s_ * K + (1 - c_) * (K @ K)  # Rodrigues
        t_step = c - R_step @ c + u[1:]
        R = R_step @ R
        t = R_step @ t + t_step
        if max(abs(theta), float(np.abs(u[1:]).max())) < tolerance:
            converged = True
            break
    final = src @ R.T + t
    d, _ = _nearest(final, dst)
    mask = d <= max_correspondence_dist_m
    n_in = int(mask.sum())
    rmse = float(np.sqrt(np.mean(d[mask] ** 2))) if n_in else float("inf")
    T_out = np.eye(4)
    T_out[:3, :3], T_out[:3, 3] = R, t
    return ICPResult(transform=T_out, rmse=rmse, inlier_ratio=n_in / len(d), iterations=used, converged=converged)
