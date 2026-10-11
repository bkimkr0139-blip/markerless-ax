"""외곽선 추출: OBB·단면 윤곽·4방향 외형·tri-view 노이즈 제거."""

import numpy as np

from spatial_core.segmentation import denoise_tri_view, extract_outline


def _rot(yaw_deg):
    t = np.radians(yaw_deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])  # Y축 회전


def _box(rng, size, n, yaw=0.0, noise=0.002):
    size = np.array(size)
    pts = []
    for _ in range(n):
        ax = rng.integers(0, 3)
        p = (rng.random(3) - 0.5) * size
        p[ax] = rng.choice([-1, 1]) * size[ax] / 2
        pts.append(p)
    P = np.array(pts) @ _rot(yaw).T + rng.normal(0, noise, (n, 3))
    return P


def test_obb_recovers_size_and_yaw():
    rng = np.random.default_rng(1)
    P = _box(rng, (0.30, 0.10, 0.20), 1500, yaw=30.0)  # 가로 30 × 높이 10 × 깊이 20 cm, 30° 회전
    o = extract_outline(P)
    dims = sorted([o.obb_size[0], o.obb_size[2]])
    assert abs(dims[0] - 0.20) < 0.02 and abs(dims[1] - 0.30) < 0.02
    assert abs(o.obb_size[1] - 0.10) < 0.015
    assert o.obb_corners.shape == (8, 3)
    # yaw: u축 각도는 90° 주기로만 정해진다. Y축 +30° 회전은 (x,z) 평면에서는 −30° 방향이다.
    err = abs(((np.degrees(o.obb_yaw_rad) + 30.0 + 45) % 90) - 45)
    assert err < 4.0


def test_level_contours_follow_surface_and_views_exist():
    rng = np.random.default_rng(2)
    P = _box(rng, (0.30, 0.20, 0.20), 3000)
    o = extract_outline(P)
    assert o.n_levels >= 5 and len(o.lines) > 50
    # 모든 윤곽 선분 끝점이 박스 겉면 근처 (노이즈 수 mm 이내): 면 중 하나에 가까워야 한다
    q = o.lines.reshape(-1, 3)
    half = np.array([0.15, 0.10, 0.10])
    dist_to_surface = np.min(half - np.abs(q), axis=1)  # 가장 가까운 면까지 거리
    assert np.percentile(dist_to_surface, 95) < 0.02
    assert set(o.views) == {"front", "right", "back", "left"}
    for v in o.views.values():
        assert len(v["segments"]) > 10 and v["h"] > 0.15


def test_concave_shape_gets_concave_outline():
    """L자 단면: 볼록껍질로는 안쪽 모서리가 사라진다 — alpha-shape은 오목부를 따라가야 한다."""
    rng = np.random.default_rng(3)
    parts = [_box(rng, (0.30, 0.10, 0.10), 900), _box(rng, (0.10, 0.10, 0.30), 900)]
    parts[0] += [0.0, 0.0, 0.0]
    parts[1] += [-0.10, 0.0, 0.10]  # 한쪽 끝에서 수직으로 뻗어 L자
    P = np.vstack(parts)
    o = extract_outline(P)
    q = o.lines.reshape(-1, 3)
    # L의 오목 모서리 근방(x≈-0.05, z≈+0.05 바깥쪽 빈 공간 x>-0.05&z>0.05)에는 선이 거의 없어야 한다
    hole = (q[:, 0] > -0.03) & (q[:, 2] > 0.08)
    assert hole.sum() / max(len(q), 1) < 0.03


def test_tri_view_removes_floaters_but_keeps_body():
    rng = np.random.default_rng(4)
    body = _box(rng, (0.25, 0.15, 0.2), 800)
    floaters = rng.uniform(-0.6, 0.6, (25, 3))  # 멀리 떠 있는 고립 점
    P = np.vstack([body, floaters])
    keep = denoise_tri_view(P)
    assert keep[:800].mean() > 0.9                 # 몸통은 대부분 보존
    assert keep[800:].mean() < 0.2                 # 떠다니는 점은 거의 제거
