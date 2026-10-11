"""POI 시드 사물 분리.

입력: 스캔 점(세션 좌표, 중력 정렬 Y-up) + POI(사용자가 십자선으로 지정한 사물 중심).
출력: 사물에 속하는 점의 마스크 + 사물 중심/크기 + 품질 평가.

파이프라인 (센서 무관 — VIO 특징점도, LiDAR 메시 정점도 같은 입력):
 1. 법선 추정 (이웃 PCA)
 2. 지지면 제거: POI 아래에서 가장 높은 큰 수평면(탁자/바닥)을 ±2cm 대역으로 제거
 3. 큰 수직면 제거: 벽 등 (POI를 지나지 않는 큰 평면, RANSAC)
 4. 연결 성분: 남은 점을 반경 r로 연결 → POI 근처 시드가 속한 성분 = 사물
    r은 점 간격(최근접 거리 중앙값)에서 자동 결정. 사물이 배경으로 새면(과대) r을 줄여 재시도
 5. 이상점 제거 + 최대 반경 절단
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from spatial_core.segmentation.outline import denoise_tri_view

SUPPORT_BAND_M = 0.02  # 지지면 제거 대역 (±)
SUPPORT_BELOW_POI_M = 0.03  # POI보다 이만큼 아래인 평면만 지지면 후보 (POI가 놓인 면은 사물 윗면일 수 있음)
SEED_RADIUS_M = 0.06  # POI 주변 시드 반경
SUPPORT_MIN_SPREAD_M = 0.25  # 지지면(탁자·바닥)은 POI에서 수평으로 이만큼 퍼져 있어야 한다
MIN_GOOD_POINTS = 150
MIN_FAIR_POINTS = 60


@dataclass
class SegmentResult:
    mask: np.ndarray  # (N,) bool — 사물에 속하는 점
    center: np.ndarray  # (3,) 사물 AABB 중심 (입력과 같은 좌표계)
    aabb_min: np.ndarray
    aabb_max: np.ndarray
    n_in: int
    n_removed: int
    support_h: float | None  # 지지면 높이(up축 좌표), 없으면 None
    planes: list[dict] = field(default_factory=list)
    conn_radius: float = 0.0
    seeds: int = 0
    quality: str = "poor"  # good | fair | poor
    warnings: list[str] = field(default_factory=list)


def _estimate_normals(P: np.ndarray, tree: cKDTree, k: int) -> np.ndarray:
    k = min(k, len(P))
    _, idx = tree.query(P, k=k, workers=-1)
    nb = P[idx]  # (N,k,3)
    nb = nb - nb.mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb) / k
    _, vecs = np.linalg.eigh(cov)
    return vecs[:, :, 0]  # 최소 고유값 방향 = 법선


def _find_support_plane(
    h: np.ndarray, n_up: np.ndarray, poi_h: float, hdist: np.ndarray, local_r: float
) -> float | None:
    """수평면(법선이 위/아래) 점들의 높이 히스토그램에서 지지면(탁자·바닥)을 찾는다.

    같은 사물인데 문맥 반경에 따라 지지면이 달라지던 문제(선반·사물의 계단면을 탁자로 오인) 방지:
     - POI 주변 local_r 안의 점만 본다 (스캔 범위가 달라져도 같은 결과)
     - POI보다 3cm 이상 아래인 봉우리만, 위(가까운 것)부터 검사
     - 평면 점이 POI에서 수평으로 충분히 넓게 퍼져 있어야 한다 (95% 지점이 25cm 이상) — 사물의 작은 계단면 제외
    """
    horiz = (np.abs(n_up) > 0.85) & (hdist <= local_r)
    nh = int(horiz.sum())
    if nh < 30:
        return None
    ys, hd = h[horiz], hdist[horiz]
    lo, hi = float(ys.min()), float(ys.max())
    edges = np.array([lo - 0.005, lo + 0.005]) if hi - lo < 0.01 else np.arange(lo, hi + 0.01, 0.01)
    hist, edges = np.histogram(ys, bins=edges)
    sm = np.convolve(np.pad(hist, 1), np.ones(3), mode="valid")  # 길이 보존 (빈이 3개 미만이어도 hist와 같은 길이)  # 1cm 빈 3개 합 = 3cm 창
    thresh = max(25.0, 0.06 * nh)
    centers = 0.5 * (edges[:-1] + edges[1:])
    peaks = []
    for i in range(len(sm)):
        left = sm[i - 1] if i > 0 else -1
        right = sm[i + 1] if i < len(sm) - 1 else -1
        if sm[i] >= thresh and sm[i] >= left and sm[i] >= right and centers[i] < poi_h - SUPPORT_BELOW_POI_M:
            peaks.append(float(centers[i]))
    for c in sorted(peaks, reverse=True):  # 가까운(높은) 봉우리부터
        near = ys[np.abs(ys - c) < 0.015]
        spread = hd[np.abs(ys - c) < 0.015]
        if len(near) >= 25 and float(np.percentile(spread, 95)) >= SUPPORT_MIN_SPREAD_M:
            return float(np.median(near))  # 빈 중심 대신 봉우리 주변 점의 중앙값
    return None


def _ransac_vertical_plane(
    P: np.ndarray, normals: np.ndarray, valid: np.ndarray, poi: np.ndarray, up_axis: int, rng: np.random.Generator
) -> tuple[np.ndarray, dict] | None:
    """벽 같은 큰 수직 평면 하나. POI를 지나지 않고(>5cm) 크기가 큰 것만."""
    n_up = normals[:, up_axis]
    cand = np.where(valid & (np.abs(n_up) < 0.35))[0]
    if len(cand) < 80:
        return None
    X = P[cand]
    NX = normals[cand]
    best_cnt, best = 0, None
    for _ in range(150):
        a, b, c = X[rng.choice(len(X), 3, replace=False)]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n /= ln
        if abs(n[up_axis]) > 0.35:  # 수직 평면만
            continue
        # 거리뿐 아니라 법선도 평면과 일치해야 인라이어 (다른 평면이 이 평면을 스치는 띠는 제외)
        d = np.abs((X - a) @ n)
        cnt = int(((d < 0.015) & (np.abs(NX @ n) > 0.85)).sum())
        if cnt > best_cnt:
            best_cnt, best = cnt, (a, n)
    if best is None or best_cnt < max(60, int(0.15 * len(cand))):
        return None
    a, n = best
    dist_all = np.abs((P - a) @ n)
    inl = valid & (dist_all < 0.02) & (np.abs(normals @ n) > 0.85)
    if abs(float((poi - a) @ n)) < 0.05:  # 사물 자신의 큰 면 — 지우지 않는다
        return None
    pts = P[inl]
    spread = np.linalg.svd(pts - pts.mean(axis=0), compute_uv=False)[0] / np.sqrt(len(pts))
    if spread * np.sqrt(12) < 0.5:  # 최대 길이(균일분포 가정) 0.5m 미만이면 사물일 수 있음
        return None
    return inl, {"kind": "vertical", "inliers": int(inl.sum()), "distFromPoiM": float(abs((poi - a) @ n))}


def _components(Q: np.ndarray, r: float) -> np.ndarray:
    tree = cKDTree(Q)
    pairs = tree.query_pairs(r, output_type="ndarray")
    n = len(Q)
    if len(pairs) == 0:
        return np.arange(n)
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, labels = connected_components(g, directed=False)
    return labels


def segment_object(
    points: np.ndarray,
    poi: np.ndarray,
    *,
    max_radius: float = 1.0,
    up_axis: int = 1,
    conn_radius: float | None = None,
    remove_planes: bool = True,
    denoise: bool = True,
    seed: int = 0,
) -> SegmentResult:
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    poi = np.asarray(poi, dtype=np.float64).reshape(3)
    N = len(P)
    warnings: list[str] = []
    empty = SegmentResult(
        mask=np.zeros(N, dtype=bool), center=poi.copy(), aabb_min=poi.copy(), aabb_max=poi.copy(),
        n_in=0, n_removed=N, support_h=None, quality="poor",
        warnings=["점이 너무 적어 사물을 분리할 수 없습니다"],
    )
    if N < 30:
        return empty

    rng = np.random.default_rng(seed)
    tree = cKDTree(P)
    normals = _estimate_normals(P, tree, 12)
    n_up = normals[:, up_axis]
    h = P[:, up_axis]

    removed = np.zeros(N, dtype=bool)
    support_mask = np.zeros(N, dtype=bool)  # 지지면 대역으로 지워진 점 (옆면 아래쪽은 나중에 복원)
    planes: list[dict] = []
    support_h = None
    if remove_planes:
        pa_, pb_ = [i for i in range(3) if i != up_axis]
        hdist = np.linalg.norm(P[:, [pa_, pb_]] - poi[[pa_, pb_]], axis=1)  # POI까지 수평 거리
        support_h = _find_support_plane(h, n_up, float(poi[up_axis]), hdist, max(0.45, 0.8 * max_radius))
        if support_h is not None:
            band = np.abs(h - support_h) < SUPPORT_BAND_M
            removed |= band
            support_mask = band
            planes.append({"kind": "support", "heightM": support_h, "inliers": int(band.sum())})
        for _ in range(2):
            res = _ransac_vertical_plane(P, normals, ~removed, poi, up_axis, rng)
            if res is None:
                break
            inl, info = res
            removed |= inl
            planes.append(info)

    keep_idx = np.where(~removed)[0]
    if len(keep_idx) < 20:
        empty.warnings = ["배경 제거 후 남은 점이 너무 적습니다 — 중심 지정 위치를 확인하세요"]
        empty.planes, empty.support_h = planes, support_h
        return empty
    Q = P[keep_idx]

    # 연결 반경 적응 선택: 점 간격(최근접 거리 중앙값)의 2배에서 시작해 늘려 가며 사물 성분의 크기를 본다.
    #  - 성장이 멈추는(≤12%) 지점 = 사물이 하나로 이어진 상태 → 거기서 멈춘다 (성긴 부분이 끊기지 않게)
    #  - 직전 대비 1.8배 이상 갑자기 커지면 옆 사물/배경과 합쳐진 것 → 직전 단계를 쓴다
    d2, _ = cKDTree(Q).query(Q, k=2)
    med = float(np.median(d2[:, 1]))
    dq = np.linalg.norm(Q - poi, axis=1)
    seeds = np.where(dq < SEED_RADIUS_M)[0]
    if len(seeds) == 0:
        seeds = np.argsort(dq)[:3]
        warnings.append("POI 가까이에 점이 없어 가장 가까운 점에서 시작했습니다 — 사물 표면에 더 가깝게 지정하세요")

    def grow(radius: float) -> np.ndarray:
        labels = _components(Q, radius)
        return np.isin(labels, np.unique(labels[seeds]))

    if conn_radius:
        r = conn_radius
        obj = grow(r)
    else:
        base = max(0.015, 2.0 * med)
        radii = sorted({min(base * f, 0.06) for f in (1.0, 1.3, 1.7, 2.2, 2.8)})

        def leaks(comp: np.ndarray) -> bool:
            if not comp.any():
                return False
            diag_ = float(np.linalg.norm(Q[comp].max(axis=0) - Q[comp].min(axis=0)))
            return diag_ > 2.2 * max_radius or float(dq[comp].max()) > 1.6 * max_radius

        obj, r = grow(radii[0]), radii[0]
        calm = 0  # 연속으로 성장이 멈춘(≤12%) 단계 수 — 조각 상태의 우연한 정체와 구분하려고 2단계를 요구
        for nxt in radii[1:]:
            cand = grow(nxt)
            if leaks(cand):  # 범위를 넘어 번졌다 = 옆 물체/배경과 합쳐짐 → 직전 단계를 쓴다
                warnings.append("사물이 옆 물체와 이어져 연결 반경을 줄여 분리했습니다")
                break
            n_prev, n_new = int(obj.sum()), int(cand.sum())
            calm = calm + 1 if n_new <= 1.12 * n_prev else 0
            obj, r = cand, nxt
            if calm >= 2 and n_new >= 30:
                break
        # 범위를 크게 넘는 누출이면 반경을 줄여 재시도
        for _ in range(3):
            if not obj.any():
                break
            diag = float(np.linalg.norm(Q[obj].max(axis=0) - Q[obj].min(axis=0)))
            reach_all = float(dq[obj].max())
            if diag <= 2.2 * max_radius and reach_all <= 1.6 * max_radius:
                break
            r *= 0.7
            obj = grow(r)
            warnings.append("사물이 배경과 이어져 연결 반경을 줄여 다시 분리했습니다")

    # 최대 반경 절단 + 이상점 제거
    obj &= dq <= max_radius
    if obj.sum() > 50:
        O = Q[obj]
        k = min(8, len(O) - 1)
        dk, _ = cKDTree(O).query(O, k=k + 1)
        md = dk[:, 1:].mean(axis=1)
        thr = md.mean() + 2.5 * md.std()
        sel = np.where(obj)[0]
        obj[sel[md > thr]] = False

    mask = np.zeros(N, dtype=bool)
    mask[keep_idx[obj]] = True

    # 지지면 대역에서 함께 지워진 사물 옆면 아래쪽 복원: 법선이 수평이 아닌(옆면) 점 중 사물에 2cm 이내
    if support_mask.any() and mask.any():
        cand = np.where(support_mask & (np.abs(n_up) < 0.6))[0]
        if len(cand):
            dd, _ = cKDTree(P[mask]).query(P[cand])
            mask[cand[dd < 0.02]] = True
    # 3면(앞·옆·위) 밀도 합의 — 사물 밖에 떠다니는 점 제거 (사물이 충분히 클 때만)
    if denoise and mask.sum() >= 150:
        idx = np.where(mask)[0]
        keep = denoise_tri_view(P[idx])
        mask[idx[~keep]] = False
        if (~keep).any():
            planes.append({"kind": "denoise", "removed": int((~keep).sum())})
    n_in = int(mask.sum())
    if n_in == 0:
        empty.planes, empty.support_h = planes, support_h
        empty.warnings = warnings + ["사물 점을 찾지 못했습니다"]
        return empty

    O = P[mask]
    amin, amax = O.min(axis=0), O.max(axis=0)
    reach = float(np.linalg.norm(O - poi, axis=1).max())
    if n_in >= MIN_GOOD_POINTS and reach < 0.97 * max_radius:
        quality = "good"
    elif n_in >= MIN_FAIR_POINTS:
        quality = "fair"
    else:
        quality = "poor"
    if n_in < MIN_GOOD_POINTS:
        warnings.append(f"사물 점이 {n_in}개로 적습니다 — 사물 주위를 더 스캔하면 정확해집니다")
    if reach >= 0.97 * max_radius:
        warnings.append("사물이 범위 반경 끝까지 이어집니다 — 범위 반경을 늘리거나 중심을 다시 지정하세요")

    return SegmentResult(
        mask=mask, center=(amin + amax) / 2.0, aabb_min=amin, aabb_max=amax,
        n_in=n_in, n_removed=N - n_in, support_h=support_h, planes=planes,
        conn_radius=r, seeds=int(len(seeds)), quality=quality, warnings=warnings,
    )
