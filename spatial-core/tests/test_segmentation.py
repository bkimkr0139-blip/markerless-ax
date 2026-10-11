"""POI 기반 사물 분리: 탁자 위 사물 + 벽 + 옆 사물이 섞인 장면에서 사물만 분리되는지."""

import numpy as np

from spatial_core.segmentation import segment_object


def _plane(rng, n, x, y, z, noise=0.003):
    """축 정렬 평면 샘플: x,y,z 중 (lo,hi) 튜플이 범위, 스칼라가 고정값."""
    cols = []
    for v in (x, y, z):
        cols.append(rng.uniform(*v, n) if isinstance(v, tuple) else np.full(n, v))
    P = np.stack(cols, axis=1)
    return P + rng.normal(0, noise, P.shape)


def _box_surface(rng, c, size, n, noise=0.002):
    c, size = np.array(c), np.array(size)
    out = []
    for _ in range(n):
        ax = rng.integers(0, 3)
        p = (rng.random(3) - 0.5) * size
        p[ax] = rng.choice([-1, 1]) * size[ax] / 2
        out.append(c + p)
    return np.array(out) + rng.normal(0, noise, (n, 3))


def _scene(seed=1):
    rng = np.random.default_rng(seed)
    table = _plane(rng, 1800, (-0.7, 0.7), 0.0, (-0.7, 0.7))           # 탁자 윗면 y=0
    floor = _plane(rng, 900, (-1.2, 1.2), -0.75, (-1.2, 1.2))          # 바닥 y=-0.75
    wall = _plane(rng, 1000, (-1.2, 1.2), (-0.75, 1.0), -0.85)         # 뒷벽 z=-0.85
    target = _box_surface(rng, [0.0, 0.10, 0.0], [0.20, 0.20, 0.15], 500)   # 대상(탁자 위)
    other = _box_surface(rng, [0.45, 0.07, 0.3], [0.14, 0.14, 0.14], 300)   # 옆 사물
    P = np.vstack([table, floor, wall, target, other])
    label = np.concatenate([np.zeros(len(table) + len(floor) + len(wall), int), np.ones(len(target), int),
                            np.full(len(other), 2)])
    return P, label


def test_segments_object_from_table_wall_and_neighbor():
    P, label = _scene()
    poi = np.array([0.0, 0.20, 0.0])  # 대상 윗면 중심을 조준
    r = segment_object(P, poi, max_radius=0.6)
    visible = (label == 1) & (P[:, 1] > 0.008)  # 탁자에 닿은 바닥면은 실제로도 보이지 않는다
    tp = (r.mask & (label == 1)).sum() / max(r.mask.sum(), 1)  # precision
    rc = (r.mask & visible).sum() / visible.sum()              # recall (보이는 면 기준)
    assert tp > 0.93, f"precision {tp:.2f}"
    assert rc > 0.85, f"recall {rc:.2f}"
    assert (r.mask & (label == 2)).sum() == 0  # 옆 사물 미포함
    assert r.support_h is not None and abs(r.support_h - 0.0) < 0.02
    assert np.linalg.norm(r.center - np.array([0, 0.10, 0])) < 0.04
    assert r.quality in ("good", "fair")


def test_poi_on_the_side_face_also_works():
    P, label = _scene(seed=2)
    poi = np.array([0.10, 0.10, 0.0])  # 대상 옆면(x=+0.10) 중앙
    r = segment_object(P, poi, max_radius=0.6)
    visible = (label == 1) & (P[:, 1] > 0.008)
    assert (r.mask & visible).sum() / visible.sum() > 0.8
    assert (r.mask & (label == 0)).sum() / max(r.mask.sum(), 1) < 0.05  # 배경 혼입 5% 미만


def test_max_radius_limits_object():
    P, label = _scene(seed=3)
    r = segment_object(P, np.array([0.0, 0.20, 0.0]), max_radius=0.08)
    assert np.linalg.norm(P[r.mask] - np.array([0.0, 0.20, 0.0]), axis=1).max() <= 0.08 + 1e-9


def test_too_few_points_is_poor():
    r = segment_object(np.zeros((5, 3)), np.zeros(3))
    assert r.quality == "poor" and r.n_in == 0


def test_support_plane_is_stable_across_context_radius():
    """같은 사물·POI에서 문맥 반경(입력 점 범위)이 달라도 같은 지지면·같은 사물 크기가 나와야 한다."""
    rng = np.random.default_rng(11)
    P, label = _scene(seed=11)
    # 사물 옆에 낮은 선반(작은 수평면, y=0.12 — POI 아래)을 추가: 지지면으로 오인되면 안 된다
    shelf = _plane(rng, 120, (0.5, 0.62), 0.12, (-0.2, -0.08))
    P = np.vstack([P, shelf])
    poi = np.array([0.0, 0.20, 0.0])
    sizes, supports = [], []
    for ctx in (0.5, 0.8, 1.2):
        sel = np.linalg.norm(P - poi, axis=1) <= ctx
        r = segment_object(P[sel], poi, max_radius=0.6)
        supports.append(r.support_h)
        sizes.append(r.n_in)
    assert all(s is not None and abs(s) < 0.02 for s in supports), supports
    assert max(sizes) - min(sizes) < 0.15 * max(sizes), sizes  # 사물 점 수가 반경에 따라 15% 이상 달라지면 불안정


def test_support_plane_histogram_with_few_bins_does_not_crash():
    """수평면 점의 높이 폭이 1~2cm라 히스토그램 빈이 3개 미만일 때 IndexError가 나던 회귀."""
    from spatial_core.segmentation.object_segment import _find_support_plane

    rng = np.random.default_rng(0)
    h = np.concatenate([rng.uniform(0.0, 0.015, 200), rng.uniform(0.3, 0.5, 50)])
    n_up = np.concatenate([np.ones(200), np.zeros(50)])
    hdist = rng.uniform(0, 0.5, 250)
    _find_support_plane(h, n_up, 0.4, hdist, 0.6)  # 예외 없이 끝나면 된다
    h2 = rng.uniform(0.0, 0.012, 100)
    _find_support_plane(h2, np.ones(100), 0.4, rng.uniform(0, 0.5, 100), 0.6)
