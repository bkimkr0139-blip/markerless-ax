"""등록 품질: 분리된 사물 점의 정제(스무딩·법선)와 완성도 평가.

완성도는 카메라 궤적이 아니라 '실제로 복원된 점이 사물 둘레 어느 면에 있는가'로 판단한다.
한쪽 면만 스캔한 모델은 다른 방향에서 검수할 때 맞지 않으므로, 어느 면이 비었는지 알려 준다.
"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from spatial_core.segmentation.outline import _min_area_rect

SIDE_NAMES = ("front", "right", "back", "left")  # 외형 뷰와 같은 정의: 관찰자가 −v / +u / +v / −u 쪽
SIDE_LABELS = {"front": "앞쪽", "right": "오른쪽", "back": "뒤쪽", "left": "왼쪽"}  # "쪽"이 겹치지 않게


def refine_points(P: np.ndarray, k: int = 12, max_move_m: float = 0.012) -> tuple[np.ndarray, np.ndarray]:
    """국소 평면 스무딩 + 바깥쪽 법선. 반환: (정제된 점, 단위 법선).

    - 각 점을 이웃 k개의 최소제곱 평면 쪽으로 70%만큼 당긴다 (이동량 상한 max_move_m) → 표면 두께(노이즈) 감소.
    - 법선은 사물 무게중심에서 바깥을 향하게 정렬한다 (소형 사물은 거의 볼록하다는 가정).
    """
    P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
    n = len(P)
    if n < 8:
        return P.copy(), np.tile([0.0, 1.0, 0.0], (n, 1))
    kk = min(k, n)
    tree = cKDTree(P)
    _, idx = tree.query(P, k=kk, workers=-1)
    nb = P[idx]
    mean = nb.mean(axis=1)
    cen = nb - mean[:, None, :]
    cov = np.einsum("nki,nkj->nij", cen, cen) / kk
    _, vecs = np.linalg.eigh(cov)
    N = vecs[:, :, 0]
    off = np.einsum("ni,ni->n", P - mean, N)  # 점의 국소 평면까지 부호 있는 거리
    move = np.clip(0.7 * off, -max_move_m, max_move_m)
    Q = P - move[:, None] * N
    centroid = Q.mean(axis=0)
    flip = np.einsum("ni,ni->n", N, Q - centroid) < 0
    N[flip] *= -1.0
    return Q, N


def assess_object(P: np.ndarray, up_axis: int = 1) -> dict:
    """사물 점의 완성도 평가. score(0~100), 방향별 면 확보, 방위 커버리지, 밀도, 노이즈, 상자형 정도, 보완 안내."""
    P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
    n = len(P)
    if n < 20:
        return {"score": 0, "grade": "poor", "points": n, "sides": {s: 0 for s in SIDE_NAMES},
                "sidesCovered": 0, "azimuthCoverage": 0.0, "densityPerM2": 0.0, "noiseMm": None,
                "boxness": 0.0, "topCovered": False, "tips": ["사물 점이 너무 적습니다 — 사물 둘레를 더 스캔하세요"]}
    pa, pb = [i for i in range(3) if i != up_axis]
    th, ctr2, size2 = _min_area_rect(P[:, [pa, pb]])
    u = np.zeros(3); u[pa], u[pb] = np.cos(th), np.sin(th)
    v = np.zeros(3); v[pa], v[pb] = -np.sin(th), np.cos(th)
    h = P[:, up_axis]
    h_lo, h_hi = float(h.min()), float(h.max())
    height = max(h_hi - h_lo, 1e-3)
    rel = P.copy()
    rel[:, pa] -= ctr2[0]; rel[:, pb] -= ctr2[1]
    a, b = rel @ u, rel @ v
    half_u, half_v = max(size2[0] / 2, 1e-3), max(size2[1] / 2, 1e-3)

    # 방향별 면 확보: 그 면 자체의 '중앙부'에 점이 있어야 한다.
    # (바깥 띠만 보면 인접한 면의 모서리 점이 비어 있는 면으로 잡힌다 — 모서리는 중앙에 닿지 않는다)
    in_mid_h = (h > h_lo + 0.2 * height) & (h < h_hi - 0.2 * height)
    shell_v = max(0.02, 0.12 * 2 * half_v)  # 면 평면에서 이 안쪽까지를 그 면의 점으로 본다
    shell_u = max(0.02, 0.12 * 2 * half_u)
    mid_a = np.abs(a) < 0.5 * half_u  # 면 폭의 가운데 절반
    mid_b = np.abs(b) < 0.5 * half_v
    side_pts = {
        "front": int(np.sum((b < -half_v + shell_v) & mid_a & in_mid_h)),
        "right": int(np.sum((a > half_u - shell_u) & mid_b & in_mid_h)),
        "back": int(np.sum((b > half_v - shell_v) & mid_a & in_mid_h)),
        "left": int(np.sum((a < -half_u + shell_u) & mid_b & in_mid_h)),
    }
    # 두께가 4cm 미만인 축에는 마주 보는 두 면이 존재할 수 없다 — 단면 한 장만 스캔한 경우다.
    #  · 얇은 축에 수직인 면(가로 끝면)은 면이 아니라 판의 가장자리이므로 0
    #  · 얇은 축 방향의 두 면(앞/뒤 또는 좌/우)은 같은 판이므로 점이 더 많은 쪽 하나만 인정
    THIN = 0.04
    if size2[1] < THIN:  # v 방향이 얇다 → 좌/우 끝면 없음, 앞/뒤는 하나로
        side_pts["left"] = side_pts["right"] = 0
        keep = "front" if side_pts["front"] >= side_pts["back"] else "back"
        side_pts["back" if keep == "front" else "front"] = 0
    if size2[0] < THIN:  # u 방향이 얇다 → 앞/뒤 끝면 없음, 좌/우는 하나로
        side_pts["front"] = side_pts["back"] = 0
        keep = "right" if side_pts["right"] >= side_pts["left"] else "left"
        side_pts["left" if keep == "right" else "right"] = 0
    need = max(10, int(0.02 * n))
    covered = {s: c >= need for s, c in side_pts.items()}
    n_cov = sum(covered.values())

    # 방위 커버리지: 무게중심 둘레 36개 방위 중 점이 있는 비율 (얇은 사물도 둘레를 돌았는지 보여 준다)
    cx, cz = P[:, pa].mean(), P[:, pb].mean()
    ang = np.arctan2(P[:, pb] - cz, P[:, pa] - cx)
    bins = np.floor((ang + np.pi) / (2 * np.pi) * 36).astype(int) % 36
    counts = np.bincount(bins, minlength=36)
    az_cov = float(np.mean(counts >= 2))

    top_covered = bool(np.sum(h > h_hi - 0.15 * height) >= max(12, int(0.03 * n)))

    L, W = size2[0], size2[1]
    area = max(2 * (L * W + L * height + W * height) - L * W, 1e-4)  # 바닥면 제외 표면적
    density = n / area

    # 노이즈: 이웃 평면 잔차 중앙값
    _, idx = cKDTree(P).query(P, k=min(10, n), workers=-1)
    nb = P[idx] - P[idx].mean(axis=1, keepdims=True)
    sv = np.linalg.svd(nb, compute_uv=False)  # (n,3) 특이값, 최소값 = 평면 이탈 크기
    noise_mm = float(np.median(sv[:, -1] / np.sqrt(min(10, n))) * 1000.0)

    # 상자형 정도: OBB 겉면(6면) 1.2cm 안의 점 비율
    d_side = np.minimum.reduce([np.abs(np.abs(a) - half_u), np.abs(np.abs(b) - half_v)])
    d_top, d_bot = np.abs(h - h_hi), np.abs(h - h_lo)
    boxness = float(np.mean((d_side < 0.012) | (d_top < 0.012) | (d_bot < 0.012)))

    score = 100.0 * (
        0.45 * (n_cov / 4.0) + 0.25 * az_cov + 0.10 * min(1.0, density / 1500.0)
        + 0.10 * (1.0 if top_covered else 0.0) + 0.10 * max(0.0, 1.0 - noise_mm / 8.0)
    )
    tips: list[str] = []
    for s in SIDE_NAMES:
        if not covered[s]:
            tips.append(f"{SIDE_LABELS[s]} 면이 비어 있습니다 — 사물 {SIDE_LABELS[s]}에서 천천히 스캔하세요")
    if not top_covered:
        tips.append("윗면이 비어 있습니다 — 위에서 내려다보며 스캔하세요")
    if density < 600:
        tips.append("점이 성깁니다 — 사물에 더 가까이서, 천천히 움직이며 스캔하세요")
    if noise_mm > 10:
        tips.append("표면 노이즈가 큽니다 — 조명이 충분한 곳에서 흔들림 없이 스캔하세요")
    grade = "good" if score >= 70 and n_cov >= 3 else ("fair" if score >= 45 else "poor")
    return {
        "score": round(score), "grade": grade, "points": n,
        "sides": side_pts, "sidesCovered": n_cov, "sideCoveredFlags": covered,
        "azimuthCoverage": round(az_cov, 3), "densityPerM2": round(density, 1),
        "noiseMm": round(noise_mm, 2), "boxness": round(boxness, 3), "topCovered": top_covered,
        "sizeM": [float(size2[0]), float(height), float(size2[1])], "tips": tips,
    }
