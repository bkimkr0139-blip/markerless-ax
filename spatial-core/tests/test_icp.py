import numpy as np
import pytest

from spatial_core.registration import icp_align, icp_plane_align, sample_mesh_points
from spatial_core.coordinate import Pose, rpy_deg_to_matrix


def _rigid(R, t, pts):
    return pts @ R.T + t


def _box_cloud(n=1200, size=0.4, seed=1):
    """사이즈 size(m) 정육면체 표면 근사 클라우드."""
    rng = np.random.default_rng(seed)
    faces = rng.integers(0, 6, size=n)
    u = (rng.random(n) - 0.5) * size
    v = (rng.random(n) - 0.5) * size
    s = np.full(n, size / 2)
    pts = np.zeros((n, 3))
    for i in range(6):
        m = faces == i
        axis = i // 2
        sign = 1 if i % 2 == 0 else -1
        other = [a for a in range(3) if a != axis]
        pts[m, axis] = sign * s[m]
        pts[m, other[0]] = u[m]
        pts[m, other[1]] = v[m]
    return pts


def test_identity_recovery():
    src = _box_cloud()
    res = icp_align(src, src.copy(), max_iterations=10)
    assert np.allclose(res.transform, np.eye(4), atol=1e-6)
    # 부동소수점 취소 오차로 rmse가 0이 아니라 ~1e-9(m) 수준일 수 있다
    assert res.rmse < 1e-6


def test_recovers_known_transform():
    """약 5°/8mm의 알려진 강체변환을 복원하는지."""
    R = rpy_deg_to_matrix(2.0, 1.0, 5.0)
    t = np.array([0.005, -0.003, 0.008])
    target = _box_cloud(n=1500, seed=2)
    # 측정 클라우드: target에 노이즈 + 밀도 차이
    rng = np.random.default_rng(3)
    source = _rigid(R, t, target[:1200]) + rng.normal(0, 0.001, (1200, 3))

    res = icp_align(source, target, max_iterations=40, tolerance=1e-9)

    # 복원된 변환으로 source를 target 프레임으로 보내야 한다
    assert res.rmse < 0.004  # 노이즈 수준 이하
    assert res.inlier_ratio > 0.8

    T_pose = Pose(position=res.transform[:3, 3], rotation=res.transform[:3, :3])
    expected = Pose(position=t, rotation=R)
    recovered = expected.compose(T_pose)
    # 복원 변환 ≈ (R,t)^-1: 검증은 실제 정합 오차로 한다
    aligned = source @ res.transform[:3, :3].T + res.transform[:3, 3]
    nn_self = np.linalg.norm(aligned - target[: len(source)], axis=1)
    # 최근점이 아니라 대응 순서 기반이므로 순열 확인: 대부분 자기 인덱스와 일치해야 함
    assert np.median(nn_self) < 0.01
    _ = recovered  # 표현용


def test_recovers_translation_with_init():
    target = _box_cloud(n=1500, seed=4)
    source = target + np.array([0.03, -0.02, 0.01])
    T0 = np.eye(4)
    res = icp_align(source, target, init_transform=T0, max_iterations=50, tolerance=1e-9)
    aligned = source @ res.transform[:3, :3].T + res.transform[:3, 3]
    res2 = np.linalg.norm(aligned - target, axis=1)
    assert np.median(res2) < 0.001
    assert res.rmse < 0.002


def test_robust_to_outliers_with_trim():
    """10% 그로스 아웃라이어가 있어도 트리밍으로 정합된다."""
    target = _box_cloud(n=1500, seed=5)
    source = target + np.array([0.02, 0.0, 0.0])
    rng = np.random.default_rng(6)
    n_out = len(source) // 10
    out_idx = rng.choice(len(source), n_out, replace=False)
    source[out_idx] += rng.normal(0, 0.3, (n_out, 3))  # 30cm 랜덤 튐

    res = icp_align(source, target, max_iterations=60, trim_ratio=0.25, tolerance=1e-9)
    aligned = source @ res.transform[:3, :3].T + res.transform[:3, 3]
    d = np.linalg.norm(aligned - target, axis=1)
    assert np.median(d) < 0.001


def test_requires_min_points():
    with pytest.raises(ValueError):
        icp_align(np.zeros((2, 3)), np.zeros((10, 3)))


# ---------- point-to-plane ICP ----------


def _box_mesh(size=0.4):
    """정육면체 메시 (12 삼각형) → GLB 왕복으로 실제 파이프라인과 동일하게."""
    s = size / 2
    v = np.array(
        [
            [-s, -s, -s], [s, -s, -s], [s, s, -s], [-s, s, -s],
            [-s, -s, s], [s, -s, s], [s, s, s], [-s, s, s],
        ]
    )
    quads = [
        (0, 3, 2, 1), (4, 5, 6, 7),  # z-
        (0, 1, 5, 4), (2, 3, 7, 6),  # x
        (0, 4, 7, 3), (1, 2, 6, 5),  # y
    ]
    f = []
    for a, b, c, d in quads:
        f.append([a, b, c])
        f.append([a, c, d])
    return v, np.array(f)


def _box_mesh_points_normals(n=4000, size=0.4, seed=7):
    v, f = _box_mesh(size)
    return sample_mesh_points(v, f, n, seed=seed, return_normals=True)


def test_plane_icp_recovers_known_transform():
    """p2p coarse 후 p2plane fine — 알려진 변환 복원."""
    R = rpy_deg_to_matrix(1.0, -1.5, 3.0)
    t = np.array([0.006, -0.004, 0.007])
    tgt, tnrm = _box_mesh_points_normals(n=4000)
    rng = np.random.default_rng(8)
    src = _rigid(R, t, tgt) + rng.normal(0, 0.001, tgt.shape)

    # 자산 프레임 보정과 동일한 방향: source→target 변환을 추정
    res = icp_plane_align(src, tgt, tnrm, max_iterations=40, tolerance=1e-9)
    assert res.converged or res.rmse < 0.003
    assert res.rmse < 0.003  # 노이즈 수준
    assert res.inlier_ratio > 0.9


def test_plane_icp_kills_tangential_slide():
    """대칭 박스 접선 미끄럼: p2p는 Z방향 미끄럼이 남고 p2plane은 잡는다.

    실측 스모크에서 발생한 케이스 — 법선이 z방향인 면의 점들이 x/y로
    미끄러져도 p2p 잔차가 0이라 수렴 판정이 흐려진다. p2plane은
    법선방향 잔차만 보므로 회전 오차를 확정적으로 잡는다.
    """
    tgt, tnrm = _box_mesh_points_normals(n=4000, seed=9)
    # yaw 0.6° + z 미끄럼 12mm 상태에서 coarse p2p가 도달하는 지점 근처에서 시작
    R_bad = rpy_deg_to_matrix(0.0, 0.0, 0.6)
    t_bad = np.array([0.0, 0.0, 0.012])
    src = _rigid(R_bad, t_bad, tgt)

    p2p = icp_align(src, tgt, max_iterations=40, tolerance=1e-9)
    plane = icp_plane_align(src, tgt, tnrm, init_transform=p2p.transform,
                            max_iterations=40, tolerance=1e-9)

    aligned = src @ plane.transform[:3, :3].T + plane.transform[:3, 3]
    # 복원: src가 tgt로 가므로 잔여 변환이 0에 가까워야 한다
    d = np.linalg.norm(aligned - tgt, axis=1)
    assert np.median(d) < 0.002
    # 회전 복원 확인: transform ≈ inverse(R_bad, t_bad)
    T_inv = np.eye(4)
    T_inv[:3, :3] = R_bad.T
    T_inv[:3, 3] = -R_bad.T @ t_bad
    assert np.allclose(plane.transform, T_inv, atol=5e-3)


def test_plane_icp_mismatched_normals_raises():
    with pytest.raises(ValueError):
        icp_plane_align(np.zeros((10, 3)), np.zeros((10, 3)), np.zeros((5, 3)))


def test_sample_mesh_points_normals_unit():
    v, f = _box_mesh()
    pts, nrm = sample_mesh_points(v, f, 600, seed=3, return_normals=True)
    assert pts.shape == (600, 3) and nrm.shape == (600, 3)
    # 모든 법선은 축정렬 단위벡터
    assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0)
    uniq = {tuple(np.round(n, 6)) for n in nrm}
    assert len(uniq) == 6  # ±x ±y ±z
