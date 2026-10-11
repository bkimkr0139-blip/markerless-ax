"""사물 앵커 정합: 초기 포즈 없이 yaw+이동을 복원하는지 (월드맵 없는 재스캔 상황 모사)."""

import numpy as np

from spatial_core.registration import yaw_search_align


def _object(seed=3):
    """비대칭 L자형 사물의 표면 점 (로컬 프레임, 원점=중심 근처). Y-up."""
    rng = np.random.default_rng(seed)

    def box(c, size, n):
        c, size = np.array(c), np.array(size)
        pts = []
        for _ in range(n):
            ax = rng.integers(0, 3)
            sign = rng.choice([-1, 1])
            p = (rng.random(3) - 0.5) * size
            p[ax] = sign * size[ax] / 2
            pts.append(c + p)
        return np.array(pts)

    pts = np.vstack([box([0, 0.15, 0], [0.5, 0.3, 0.2], 900), box([0.2, 0.45, 0], [0.1, 0.3, 0.2], 400)])
    # 법선: 가장 가까운 면 방향 근사(박스라서 정확한 값 불필요 — p2plane용 대략 법선)
    normals = pts - pts.mean(axis=0)
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    return pts - pts.mean(axis=0), normals


def _ry(deg):
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _yaw_err(a, b):
    return abs((a - b + 180) % 360 - 180)


def test_recovers_yaw_and_translation_without_prior():
    model, normals = _object()
    rng = np.random.default_rng(7)
    true_yaw, true_pos = 128.0, np.array([1.3, 0.2, -2.1])
    # 현재 세션에서 관측: 일부(70%)만 보이고 노이즈 + 주변 배경 점 포함
    seen = model[rng.random(len(model)) < 0.7]
    cloud = seen @ _ry(true_yaw).T + true_pos + rng.normal(0, 0.003, seen.shape)
    clutter = rng.uniform(-0.8, 0.8, (80, 3)) + true_pos
    cloud = np.vstack([cloud, clutter])

    hint = true_pos + np.array([0.08, 0.03, -0.05])  # 사용자 조준은 수 cm 부정확
    res = yaw_search_align(cloud, model, normals, hint)

    pose_R = np.linalg.inv(res.transform)[:3, :3]
    pose_t = np.linalg.inv(res.transform)[:3, 3]
    assert _yaw_err(res.yaw_deg, true_yaw) < 5.0
    assert np.linalg.norm(pose_t - true_pos) < 0.03
    assert res.inlier_ratio > 0.6
    assert res.score < 0.5
    assert np.allclose(pose_R @ pose_R.T, np.eye(3), atol=1e-6)


def test_wrong_object_scores_worse_than_right_object():
    model, normals = _object()
    rng = np.random.default_rng(11)
    cloud_same = model @ _ry(40.0).T + np.array([0.5, 0, 0.5]) + rng.normal(0, 0.003, model.shape)
    other = rng.uniform(-0.3, 0.3, (700, 3))  # 전혀 다른 구름 형상
    good = yaw_search_align(cloud_same, model, normals, np.array([0.5, 0, 0.5]))
    bad = yaw_search_align(other, model, normals, np.zeros(3))
    assert good.score < bad.score
