"""사물 앵커 정합 — 초기 포즈를 모를 때 (월드맵 없이) 저장된 모델을 현재 클라우드에 붙인다.

전제: ARKit/ARCore 세션 월드는 중력 정렬(Y-up)이다. 따라서 같은 사물의 저장 모델(로컬 프레임)과
현재 세션 클라우드 사이의 미지수는 yaw(수직축 회전) + 평행이동 3자유도뿐이다.

방법: yaw 후보 × 위치 힌트 후보마다 거친 ICP를 돌리고, 절단 거리 점수로 순위를 매긴 뒤
상위 후보를 전체 점으로 정밀 ICP(p2p → p2plane)로 다듬는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from spatial_core.registration.icp import _nearest, estimate_normals, icp_align, icp_plane_align, icp_yaw_align, icp_yaw_plane_refine


@dataclass
class GlobalAlignResult:
    """transform: cloud(세션) → target(모델 로컬) 변환. 사물의 세션 포즈 = inverse(transform)."""

    transform: np.ndarray
    rmse: float
    inlier_ratio: float
    score: float  # 절단 평균 거리 비율(0=완벽, 1=전부 불일치)
    yaw_deg: float  # 사물의 세션 yaw (수직축 기준)
    candidates: int
    iterations: int


def _rot_about(axis: int, theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    if axis == 1:  # Y-up
        return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])
    if axis == 2:  # Z-up
        return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def _truncated_score(src: np.ndarray, dst: np.ndarray, T: np.ndarray, trunc: float) -> float:
    cur = src @ T[:3, :3].T + T[:3, 3]
    d, _ = _nearest(cur, dst)
    return float(np.mean(np.minimum(d, trunc) / trunc))


def _subsample(a: np.ndarray, n: int, seed: int) -> np.ndarray:
    if len(a) <= n:
        return a
    return a[np.random.default_rng(seed).choice(len(a), n, replace=False)]


def yaw_search_align(
    source: np.ndarray,
    target: np.ndarray,
    target_normals: np.ndarray,
    hint: np.ndarray | None = None,
    *,
    up_axis: int = 1,
    yaw_step_deg: float = 15.0,
    coarse_src: int = 500,
    coarse_dst: int = 2000,
    coarse_iters: int = 25,
    corr_m: float = 0.15,
    trunc_m: float = 0.05,
    refine_top: int = 3,
    seed: int = 0,
) -> GlobalAlignResult:
    """source: 현재 세션 클라우드(N,3). target/target_normals: 모델 로컬 표면 샘플(M,3).

    hint: 세션 좌표에서 사물 중심 추정(사용자가 조준한 점). 없으면 클라우드 중심만 사용.
    """
    src = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    nrm = np.asarray(target_normals, dtype=np.float64).reshape(-1, 3)
    if len(src) < 10 or len(dst) < 10:
        raise ValueError("yaw_search_align requires >=10 points in both clouds")

    src_c = _subsample(src, coarse_src, seed)
    dst_idx = np.random.default_rng(seed + 1).choice(len(dst), min(coarse_dst, len(dst)), replace=False)
    dst_c = dst[dst_idx]

    cloud_centroid = src.mean(axis=0)
    model_centroid = dst.mean(axis=0)
    hint_pos = None if hint is None else np.asarray(hint, dtype=np.float64).reshape(3)

    steps = max(1, int(round(360.0 / yaw_step_deg)))
    scored: list[tuple[float, np.ndarray]] = []
    for k in range(steps):
        R = _rot_about(up_axis, 2.0 * np.pi * k / steps)  # 사물의 세션 회전 (local→session)
        # 모델 로컬 원점이 놓일 세션 위치 후보: 사용자 힌트 / 무게중심 일치
        positions = [cloud_centroid - R @ model_centroid]
        if hint_pos is not None:
            positions.insert(0, hint_pos)
        for pos in positions:
            T0 = np.eye(4)
            T0[:3, :3] = R.T
            T0[:3, 3] = -R.T @ pos
            res = icp_align(
                src_c, dst_c, init_transform=T0, max_iterations=coarse_iters,
                max_correspondence_dist_m=corr_m, trim_ratio=0.3, tolerance=1e-6,
            )
            scored.append((_truncated_score(src_c, dst_c, res.transform, trunc_m), res.transform))

    scored.sort(key=lambda s: s[0])
    # 정밀화: 단계마다 점수를 비교해 가장 좋은 것을 채택한다.
    # (p2plane은 법선이 부정확하면 오히려 악화될 수 있다 — 희소 VIO 점의 PCA 법선이 그렇다)
    best: tuple[float, np.ndarray, object] | None = None
    iters = 0
    for _, T in scored[:refine_top]:
        coarse = icp_align(src, dst, init_transform=T, max_iterations=40,
                           max_correspondence_dist_m=0.10, trim_ratio=0.25, tolerance=1e-7)
        fine = icp_plane_align(src, dst, nrm, init_transform=coarse.transform, max_iterations=40,
                               max_correspondence_dist_m=0.06, trim_ratio=0.25, tolerance=1e-7)
        iters += coarse.iterations + fine.iterations
        for cand_T, cand_res in ((T, coarse), (coarse.transform, coarse), (fine.transform, fine)):
            sc = _truncated_score(src, dst, cand_T, trunc_m)
            if best is None or sc < best[0]:
                best = (sc, cand_T, cand_res)
    assert best is not None
    score, T, _ = best
    # 채택된 변환 기준으로 지표 재계산 (대응 상한 6cm)
    d, _idx = _nearest(src @ T[:3, :3].T + T[:3, 3], dst)
    inl = d <= 0.06
    rmse = float(np.sqrt(np.mean(d[inl] ** 2))) if inl.any() else float("inf")
    inlier_ratio = float(inl.mean())
    R_obj = np.linalg.inv(T)[:3, :3]
    if up_axis == 1:
        yaw = float(np.degrees(np.arctan2(R_obj[0, 2], R_obj[0, 0])))
    else:
        yaw = float(np.degrees(np.arctan2(R_obj[1, 0], R_obj[0, 0])))
    return GlobalAlignResult(
        transform=T, rmse=rmse, inlier_ratio=inlier_ratio, score=score,
        yaw_deg=yaw, candidates=len(scored), iterations=iters,
    )


@dataclass
class ObjectAlignResult:
    """transform: observed(세션) → model(로컬). 사물의 세션 포즈 = inverse(transform)."""

    transform: np.ndarray
    score: float  # 사물 크기 비례 절단 거리 점수 (0=완벽)
    rmse: float
    inlier_ratio: float  # observed 점 중 τ 이내 비율
    model_coverage: float  # 모델 표면 중 관측 점이 τ' 이내로 덮은 비율 (부분 관측이면 낮은 게 정상)
    yaw_deg: float
    tau_m: float
    candidates: int
    best_init: str  # 어떤 초기값에서 수렴했나: prior | hint | center | centroid | obb


def align_object(
    observed: np.ndarray,
    model: np.ndarray,
    *,
    hints: list[np.ndarray] | None = None,
    prior: np.ndarray | None = None,
    extra_yaws_deg: list[float] | None = None,
    up_axis: int = 1,
    yaw_step_deg: float = 10.0,
    plane_refine: bool = True,
    coarse_src: int = 400,
    coarse_dst: int = 1500,
    refine_top: int = 4,
    seed: int = 0,
) -> ObjectAlignResult:
    """분리된 관측 사물(observed)을 저장 모델(model, 로컬 프레임)에 맞춘다 — 사물 대 사물 정합.

    - 4자유도(yaw+이동): 중력 정렬을 가정하므로 기울기는 풀지 않는다.
    - 초기값 후보: yaw 격자(+OBB로 얻은 extra_yaws) × 위치 후보(사용자 힌트, 관측 AABB 중심, 무게중심 일치).
    - prior: 4x4 (model→session) — 월드맵 복원 시 설계 포즈. 있으면 그 주변 초기값을 추가한다.
    - τ(일치 거리)는 사물 크기에 비례: 21cm 사물에 6cm 기준은 무의미하다.
    """
    obs = np.asarray(observed, dtype=np.float64).reshape(-1, 3)
    mdl = np.asarray(model, dtype=np.float64).reshape(-1, 3)
    if len(obs) < 10 or len(mdl) < 10:
        raise ValueError("align_object requires >=10 points in both clouds")

    diag = float(np.linalg.norm(mdl.max(axis=0) - mdl.min(axis=0)))
    tau = float(np.clip(0.07 * diag, 0.008, 0.04))  # 일치 판정 거리
    corr = float(np.clip(0.5 * diag, 0.08, 0.35))  # ICP 대응 상한

    src_c = _subsample(obs, coarse_src, seed)
    dst_c = _subsample(mdl, coarse_dst, seed + 1)
    obs_c = obs.mean(axis=0)
    obs_box = (obs.min(axis=0) + obs.max(axis=0)) / 2.0
    mdl_c = mdl.mean(axis=0)

    yaws = [2.0 * np.pi * k / max(1, int(round(360.0 / yaw_step_deg))) for k in range(max(1, int(round(360.0 / yaw_step_deg))))]
    for y in (extra_yaws_deg or []):
        yaws.append(np.radians(y))

    def init_T(R: np.ndarray, pos: np.ndarray) -> np.ndarray:
        T0 = np.eye(4)
        T0[:3, :3] = R.T
        T0[:3, 3] = -R.T @ pos
        return T0

    inits: list[tuple[str, np.ndarray]] = []
    for yaw in yaws:
        R = _rot_about(up_axis, yaw)  # local→session
        positions: list[tuple[str, np.ndarray]] = [("center", obs_box), ("centroid", obs_c - R @ mdl_c)]
        for h in (hints or []):
            positions.append(("hint", np.asarray(h, dtype=np.float64).reshape(3)))
        for name, pos in positions:
            inits.append((name, init_T(R, pos)))
    if prior is not None:
        P0 = np.asarray(prior, dtype=np.float64)
        for dy in (-10.0, 0.0, 10.0):  # 설계 포즈 주변 ±10°
            R = _rot_about(up_axis, np.radians(dy)) @ P0[:3, :3]
            inits.append(("prior", init_T(R, P0[:3, 3])))

    scored: list[tuple[float, str, np.ndarray]] = []
    for name, T0 in inits:
        res = icp_yaw_align(src_c, dst_c, init_transform=T0, up_axis=up_axis, max_iterations=20,
                            max_correspondence_dist_m=corr, trim_ratio=0.3, tolerance=1e-6)
        scored.append((_truncated_score(src_c, dst_c, res.transform, tau), name, res.transform))
    scored.sort(key=lambda s: s[0])

    best = None
    for _, name, T in scored[:refine_top]:
        res = icp_yaw_align(obs, mdl, init_transform=T, up_axis=up_axis, max_iterations=40,
                            max_correspondence_dist_m=corr * 0.6, trim_ratio=0.2, tolerance=1e-8)
        sc = _truncated_score(obs, mdl, res.transform, tau)
        if best is None or sc < best[0]:
            best = (sc, name, res.transform)
    assert best is not None
    score, name, T = best
    if plane_refine and len(mdl) >= 100:
        # 점-면 정밀화: 점-점 ICP가 평평한 면에서 미끄러져 남기는 회전·옆 위치 오차를 줄인다 (모델 점 이웃으로 법선 추정)
        try:
            mdl_n = estimate_normals(mdl)
            r2 = icp_yaw_plane_refine(obs, mdl, mdl_n, T, up_axis=up_axis, max_correspondence_dist_m=corr * 0.5)
            sc2 = _truncated_score(obs, mdl, r2.transform, tau)
            if sc2 <= score * 1.03 + 1e-6:  # 일치도가 나빠지지 않을 때만 채택
                score, T = sc2, r2.transform
        except Exception:  # pragma: no cover — 정밀화 실패는 기존 결과 유지
            pass

    d_obs, _ = _nearest(obs @ T[:3, :3].T + T[:3, 3], mdl)
    inl = d_obs <= tau
    inlier_ratio = float(inl.mean())
    rmse = float(np.sqrt(np.mean(d_obs[inl] ** 2))) if inl.any() else float("inf")
    # 모델 표면 중 관측이 덮은 비율: 관측을 모델 프레임으로 옮겨 모델→관측 최근접 거리
    obs_in_model = obs @ T[:3, :3].T + T[:3, 3]
    d_mdl, _ = _nearest(mdl, obs_in_model)
    coverage = float((d_mdl <= tau * 1.5).mean())
    R_obj = np.linalg.inv(T)[:3, :3]
    pa, pb = [i for i in range(3) if i != up_axis]
    yaw = float(np.degrees(np.arctan2(R_obj[pb, pa], R_obj[pa, pa])))
    return ObjectAlignResult(transform=T, score=score, rmse=rmse, inlier_ratio=inlier_ratio,
                             model_coverage=coverage, yaw_deg=yaw, tau_m=tau, candidates=len(scored), best_init=name)
