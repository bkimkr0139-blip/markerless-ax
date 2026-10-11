"""사물 외곽선(와이어프레임) 추출 — 점 조각 대신 CAD/BIM처럼 선으로 표현한다.

입력: 분리된 사물 점 (중력 정렬 좌표, 기본 Y-up).
출력:
 - OBB: 수직축 기준 최소 면적 회전 박스 (yaw만) — 박스 12모서리
 - levels: 높이별 단면 윤곽선 (alpha-shape 경계) — 쌓아 올린 등고선 형태의 와이어프레임
 - views: 앞/오른쪽/뒤/왼쪽 4방향 외형 선화 (그 방향에서 보이는 면의 윤곽, 2D)

또한 3면 밀도 합의(tri-view) 노이즈 제거를 제공한다: 앞·옆·위 투영 모두에서 밀도가 낮은 점은 떠다니는 노이즈.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import uniform_filter
from scipy.spatial import ConvexHull, Delaunay, QhullError, cKDTree

MAX_LINES = 9000  # 응답/렌더 상한 (가로 단면 + 수직 단면)


@dataclass
class OutlineResult:
    obb_center: np.ndarray
    obb_size: np.ndarray  # (u 길이, 높이, v 길이)
    obb_yaw_rad: float  # u축이 평면 첫 축(a)에서 두번째 축(b) 방향으로 돈 각
    obb_corners: np.ndarray  # (8,3)
    lines: np.ndarray  # (M,2,3) 높이별 가로 단면 + 수직 단면 윤곽선 (입력 좌표계)
    n_levels: int
    n_vsections: int = 0  # 수직 단면 수 (u축·v축에 수직인 판)
    views: dict[str, dict] = field(default_factory=dict)  # name -> {"w","h","segments"(M,2,2)}


# ---------------------------------------------------------------- 노이즈 제거

def denoise_tri_view(P: np.ndarray, cell: float = 0.02, min_points: int = 150) -> np.ndarray:
    """앞·옆·위 3개 투영 모두에서 밀도가 충분한 점만 남기는 마스크 (떠다니는 점 제거).

    각 투영에서 cell(2cm) 격자 + 3×3 이웃 합이 4 미만인 칸의 점은 고립으로 본다.
    점이 적으면(<min_points) 판단 근거가 부족해 그대로 둔다.
    """
    n = len(P)
    keep = np.ones(n, dtype=bool)
    if n < min_points:
        return keep
    lo = P.min(axis=0)
    ij = np.floor((P - lo) / cell).astype(int)
    dims = ij.max(axis=0) + 1
    for a, b in ((0, 1), (0, 2), (1, 2)):
        H = np.zeros((dims[a], dims[b]))
        np.add.at(H, (ij[:, a], ij[:, b]), 1.0)
        S = uniform_filter(H, size=3, mode="constant") * 9.0  # 3×3 합
        keep &= S[ij[:, a], ij[:, b]] >= 4.0
    return keep


# ---------------------------------------------------------------- 2D 윤곽 (alpha shape)

def _alpha_segments(pts2: np.ndarray, alpha: float) -> np.ndarray:
    """2D 점의 alpha-shape 경계 선분 (M,2,2). 오목한 윤곽도 따라간다."""
    if len(pts2) < 4:
        return np.zeros((0, 2, 2))
    try:
        tri = Delaunay(pts2)
    except QhullError:
        return np.zeros((0, 2, 2))
    s = tri.simplices
    A, B, C = pts2[s[:, 0]], pts2[s[:, 1]], pts2[s[:, 2]]
    a = np.linalg.norm(B - C, axis=1)
    b = np.linalg.norm(A - C, axis=1)
    c = np.linalg.norm(A - B, axis=1)
    area2 = np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (C[:, 0] - A[:, 0]) * (B[:, 1] - A[:, 1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        R = a * b * c / (2.0 * area2)  # 외접원 반지름 = abc/(4·면적), 면적 = area2/2
    ok = np.isfinite(R) & (R < alpha)
    if not ok.any():
        return np.zeros((0, 2, 2))
    edges = np.concatenate([s[ok][:, [0, 1]], s[ok][:, [1, 2]], s[ok][:, [2, 0]]])
    edges.sort(axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = uniq[counts == 1]  # 한 삼각형에만 속한 변 = 경계
    return pts2[boundary]


def _median_nn(pts2: np.ndarray) -> float:
    if len(pts2) < 3:
        return 0.02
    d, _ = cKDTree(pts2).query(pts2, k=2)
    return float(np.median(d[:, 1]))


def _alpha_for(pts2: np.ndarray) -> float:
    return float(np.clip(3.0 * _median_nn(pts2), 0.015, 0.12))


# ---------------------------------------------------------------- OBB (yaw)

def _min_area_rect(xy: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """2D 점의 최소 면적 회전 사각형. (각도 θ[rad], 중심(2), 크기(2: u,v 길이))."""
    if len(xy) < 3:
        c = xy.mean(axis=0) if len(xy) else np.zeros(2)
        return 0.0, c, np.zeros(2)
    try:
        hull = xy[ConvexHull(xy).vertices]
    except QhullError:
        c = (xy.min(axis=0) + xy.max(axis=0)) / 2
        return 0.0, c, xy.max(axis=0) - xy.min(axis=0)
    best = None
    edges = np.roll(hull, -1, axis=0) - hull
    for th in np.arctan2(edges[:, 1], edges[:, 0]):
        c, s = np.cos(th), np.sin(th)
        R = np.array([[c, s], [-s, c]])  # 점을 (u,v)로
        uv = hull @ R.T
        size = uv.max(axis=0) - uv.min(axis=0)
        area = size[0] * size[1]
        if best is None or area < best[0]:
            ctr_uv = (uv.max(axis=0) + uv.min(axis=0)) / 2
            best = (area, th, R.T @ ctr_uv, size)
    _, th, ctr, size = best
    return float(th), ctr, size


# ---------------------------------------------------------------- 외곽선

def extract_outline(points: np.ndarray, up_axis: int = 1) -> OutlineResult:
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(P) < 8:
        raise ValueError("outline requires >= 8 points")
    pa, pb = [i for i in range(3) if i != up_axis]  # 수평면 두 축

    # OBB: 수평면 최소 면적 사각형 + 높이 범위
    th, ctr2, size2 = _min_area_rect(P[:, [pa, pb]])
    h_lo, h_hi = float(P[:, up_axis].min()), float(P[:, up_axis].max())
    u_dir = np.zeros(3); u_dir[pa], u_dir[pb] = np.cos(th), np.sin(th)
    v_dir = np.zeros(3); v_dir[pa], v_dir[pb] = -np.sin(th), np.cos(th)
    up = np.zeros(3); up[up_axis] = 1.0
    center = np.zeros(3)
    center[pa], center[pb], center[up_axis] = ctr2[0], ctr2[1], (h_lo + h_hi) / 2
    size = np.array([size2[0], h_hi - h_lo, size2[1]])
    corners = np.array([
        center + sx * u_dir * size[0] / 2 + sy * up * size[1] / 2 + sz * v_dir * size[2] / 2
        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)
    ])

    # 높이별 단면 윤곽선 (등고선 와이어프레임)
    ext_h = max(h_hi - h_lo, 1e-6)
    step = float(np.clip(ext_h / 12.0, 0.02, 0.08))
    levels = np.arange(h_lo + step / 2, h_hi, step) if ext_h > step else np.array([(h_lo + h_hi) / 2])
    # 맨 아래/위 윤곽도 포함 (바닥·윗면 외곽)
    levels = np.unique(np.concatenate([[h_lo + 0.005], levels, [h_hi - 0.005]])) if ext_h > 0.02 else levels
    all_lines: list[np.ndarray] = []
    used = 0
    for lv in levels:
        sel = np.abs(P[:, up_axis] - lv) <= step / 2
        if sel.sum() < 8:
            continue
        s2 = P[sel][:, [pa, pb]]
        seg2 = _alpha_segments(s2, _alpha_for(s2))
        if len(seg2) == 0:
            continue
        seg3 = np.zeros((len(seg2), 2, 3))
        seg3[:, :, pa] = seg2[:, :, 0]
        seg3[:, :, pb] = seg2[:, :, 1]
        seg3[:, :, up_axis] = lv
        all_lines.append(seg3)
        used += 1
    # 수직 단면: u축·v축에 수직인 얇은 판으로 잘라 그 안의 윤곽을 그린다 — 벽·모서리 같은 수직선이 선으로 나온다.
    # (높이별 가로 단면만으로는 수직선이 전혀 생기지 않는다. 스캔 방향과는 무관하다.)
    rel0 = P - center
    pu_, pv_, py_ = rel0 @ u_dir, rel0 @ v_dir, rel0 @ up
    n_vert = 0
    for n_dir, a_dir, along, across in ((u_dir, v_dir, pu_, pv_), (v_dir, u_dir, pv_, pu_)):
        lo, hi = float(along.min()), float(along.max())
        ext = hi - lo
        if ext < 0.03:
            continue
        vstep = float(np.clip(ext / 6.0, 0.03, 0.10))
        pos = np.arange(lo + vstep / 2, hi, vstep) if ext > vstep else np.array([(lo + hi) / 2])
        pos = np.unique(np.concatenate([[lo + 0.005], pos, [hi - 0.005]]))  # 양끝 면도 포함
        for s in pos:
            sel = np.abs(along - s) <= vstep / 2
            if sel.sum() < 8:
                continue
            s2 = np.stack([across[sel], py_[sel]], axis=1)
            seg2 = _alpha_segments(s2, _alpha_for(s2))
            if len(seg2) == 0:
                continue
            # 판 안 좌표 (a: 가로, b: 높이) → 3D
            a = seg2[:, :, 0][..., None] * a_dir
            b = seg2[:, :, 1][..., None] * up
            all_lines.append(center + s * n_dir + a + b)
            n_vert += 1
    lines = np.concatenate(all_lines) if all_lines else np.zeros((0, 2, 3))
    if len(lines) > MAX_LINES:
        lines = lines[np.linspace(0, len(lines) - 1, MAX_LINES).astype(int)]

    # 4방향 외형: 그 방향에서 보이는 절반(가까운 쪽)의 정사영 윤곽
    views: dict[str, dict] = {}
    rel = P - center
    specs = {  # 이름: 바라보는 방향(forward) — 관찰자는 반대편에서 봄
        "front": v_dir, "right": -u_dir, "back": -v_dir, "left": u_dir,
    }
    for name, fwd in specs.items():
        depth = rel @ fwd
        near = depth <= np.median(depth)
        right = np.cross(fwd, up)
        pts2 = np.stack([rel[near] @ right, rel[near] @ up], axis=1)
        seg = _alpha_segments(pts2, _alpha_for(pts2)) if len(pts2) >= 8 else np.zeros((0, 2, 2))
        if len(seg) > 1500:
            seg = seg[np.linspace(0, len(seg) - 1, 1500).astype(int)]
        views[name] = {"w": float(pts2[:, 0].max() - pts2[:, 0].min()) if len(pts2) else 0.0,
                       "h": float(ext_h), "segments": seg}

    return OutlineResult(obb_center=center, obb_size=size, obb_yaw_rad=float(th),
                         obb_corners=corners, lines=lines, n_levels=used, views=views,
                         n_vsections=n_vert)


def outline_to_json(o: OutlineResult, decimals: int = 3) -> dict:
    """JSON 직렬화 (미터, 소수 3자리). lines: [x1,y1,z1,x2,y2,z2]*M, views.segments: [x1,y1,x2,y2]*M."""
    r = lambda a: np.round(np.asarray(a, dtype=float), decimals).tolist()  # noqa: E731
    return {
        "lines": r(o.lines.reshape(-1, 6)),
        "obb": {
            "center": r(o.obb_center), "size": r(o.obb_size),
            "yawDeg": round(float(np.degrees(o.obb_yaw_rad)), 2), "corners": r(o.obb_corners),
        },
        "levels": int(o.n_levels),
        "vsections": int(o.n_vsections),
        "views": {
            k: {"w": round(v["w"], decimals), "h": round(v["h"], decimals), "segments": r(v["segments"].reshape(-1, 4))}
            for k, v in o.views.items()
        },
    }


def obb_of(points: np.ndarray, up_axis: int = 1) -> tuple[np.ndarray, float]:
    """가벼운 OBB: (크기 (u길이, 높이, v길이), yaw[rad]). 외곽선 전체 계산 없이 크기 비교용."""
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    pa, pb = [i for i in range(3) if i != up_axis]
    th, _, size2 = _min_area_rect(P[:, [pa, pb]])
    return np.array([size2[0], float(P[:, up_axis].max() - P[:, up_axis].min()), size2[1]]), float(th)
