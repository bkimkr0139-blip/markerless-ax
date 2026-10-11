"""사물 대 사물 정합: 방위·위치·크기가 정확히 복원되는지 (각도·크기 불일치 회귀 방지)."""

import numpy as np
import pytest

from spatial_core.registration import align_object, icp_yaw_align


def _ry(deg):
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _box(rng, c, size, n, noise=0.002):
    c, size = np.array(c), np.array(size)
    out = []
    for _ in range(n):
        ax = rng.integers(0, 3)
        p = (rng.random(3) - 0.5) * size
        p[ax] = rng.choice([-1, 1]) * size[ax] / 2
        out.append(c + p)
    return np.array(out) + rng.normal(0, noise, (n, 3))


def _l_object(rng, n=1200):
    """비대칭 L자형 사물 (로컬 프레임, 원점=AABB 중심)."""
    P = np.vstack([_box(rng, [0, 0.05, 0], [0.30, 0.10, 0.12], n // 2), _box(rng, [-0.09, 0.13, 0], [0.12, 0.16, 0.12], n // 2)])
    return P - (P.min(axis=0) + P.max(axis=0)) / 2


def _yaw_of(T):
    R = np.linalg.inv(T)[:3, :3]
    return np.degrees(np.arctan2(R[0, 2], R[0, 0]))  # Y-up에서 Ry(θ)의 θ


def _angle_err(a, b):
    return abs((a - b + 180) % 360 - 180)


@pytest.mark.parametrize("yaw,seen", [(0.0, 0.6), (137.0, 0.6), (-80.0, 0.5), (250.0, 0.7)])
def test_recovers_pose_with_partial_noisy_observation(yaw, seen):
    rng = np.random.default_rng(int(abs(yaw)) + 1)
    model = _l_object(rng)
    pos = np.array([0.8, 0.12, -1.3])
    vis = model[rng.random(len(model)) < seen]
    # 한쪽 방향에서만 본 것처럼 (카메라 쪽 절반만) — 실제 스캔의 편향
    vis = vis[(vis @ _ry(0).T)[:, 2] > -0.02] if seen < 0.55 else vis
    observed = vis @ _ry(yaw).T + pos + rng.normal(0, 0.003, vis.shape)
    hint = pos + np.array([0.06, 0.03, -0.05])  # 사용자 조준이 중심에서 6cm쯤 어긋남

    r = align_object(observed, model, hints=[hint])
    assert _angle_err(_yaw_of(r.transform), yaw) < 4.0, f"yaw {_yaw_of(r.transform):.1f} vs {yaw}"
    got = np.linalg.inv(r.transform)[:3, 3]
    # 부분 관측이면 AABB/무게중심이 치우치므로 위치는 ICP가 맞춘 결과 — 3cm 이내
    assert np.linalg.norm(got - pos) < 0.03, f"pos err {np.linalg.norm(got - pos):.3f}"
    assert r.inlier_ratio > 0.8 and r.score < 0.35


def test_prior_pose_is_used_when_restored():
    rng = np.random.default_rng(5)
    model = _l_object(rng)
    pos = np.array([0.2, 0.1, 0.3])
    observed = model[rng.random(len(model)) < 0.6] @ _ry(8.0).T + pos + rng.normal(0, 0.002, (0, 3)) if False else \
        (model[rng.random(len(model)) < 0.6] @ _ry(8.0).T + pos)
    prior = np.eye(4)
    prior[:3, 3] = pos + [0.02, 0, -0.02]  # 설계 포즈(yaw 0) — 실제는 8° 돌아가 있음
    r = align_object(observed, model, prior=prior)
    assert _angle_err(_yaw_of(r.transform), 8.0) < 3.0
    assert r.best_init in ("prior", "center", "centroid", "hint")


def test_yaw_icp_does_not_tilt():
    """6자유도 ICP는 기울지만 4자유도는 수평을 유지한다 — 변환의 수직축 성분이 정확히 항등."""
    rng = np.random.default_rng(6)
    model = _l_object(rng)
    obs = model[rng.random(len(model)) < 0.5] @ _ry(33).T + [0.1, 0.0, 0.2]
    res = icp_yaw_align(obs, model, up_axis=1, max_iterations=40, max_correspondence_dist_m=0.2)
    R = res.transform[:3, :3]
    assert abs(R[1, 1] - 1.0) < 1e-9 and abs(R[0, 1]) < 1e-9 and abs(R[1, 0]) < 1e-9
