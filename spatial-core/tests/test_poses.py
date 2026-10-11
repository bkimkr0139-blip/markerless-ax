import numpy as np
import pytest

from spatial_core.coordinate import (
    Pose,
    Z_UP_TO_Y_UP,
    matrix_to_rpy_deg,
    pose_delta,
    quat_to_matrix,
    rpy_deg_to_matrix,
)


def test_rpy_identity():
    R = rpy_deg_to_matrix(0.0, 0.0, 0.0)
    assert np.allclose(R, np.eye(3), atol=1e-12)


@pytest.mark.parametrize(
    "roll,pitch,yaw",
    [(10.0, 20.0, 30.0), (-15.0, 5.0, 200.0), (0.0, -35.0, 90.0), (179.0, 0.5, -179.0)],
)
def test_rpy_roundtrip(roll, pitch, yaw):
    """오일러각은 표현이 여러 개(예: yaw 200° ≡ -160°)이므로 회전행렬로 비교한다."""
    R = rpy_deg_to_matrix(roll, pitch, yaw)
    r2, p2, y2 = matrix_to_rpy_deg(R)
    assert np.allclose(rpy_deg_to_matrix(r2, p2, y2), R, atol=1e-9)


def test_quat_vs_rpy_90_yaw():
    # yaw 90도: +X(1,0,0)가 +Y(0,1,0)로 회전해야 한다
    import math

    R_q = quat_to_matrix(0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))
    R_r = rpy_deg_to_matrix(0.0, 0.0, 90.0)
    assert np.allclose(R_q, R_r, atol=1e-9)


def test_pose_compose_inverse_roundtrip():
    a = Pose.from_rpy_deg({"x": 1.0, "y": 2.0, "z": 3.0}, {"roll": 5, "pitch": -10, "yaw": 45})
    b = Pose.from_rpy_deg({"x": -0.5, "y": 0.2, "z": 1.1}, {"roll": 0, "pitch": 15, "yaw": -90})
    T = a.compose(b)
    back = T.compose(b.inverse()).compose(a.inverse())
    assert np.allclose(back.position, np.zeros(3), atol=1e-9)
    assert np.allclose(back.rotation, np.eye(3), atol=1e-9)


def test_pose_apply_translates_points():
    p = Pose.from_rpy_deg({"x": 10.0, "y": 0.0, "z": 0.0}, {"roll": 0, "pitch": 0, "yaw": 0})
    pts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    out = p.apply(pts)
    assert np.allclose(out, pts + np.array([10.0, 0.0, 0.0]))


def test_pose_delta_known_translation():
    design = Pose.from_rpy_deg({"x": 2.0, "y": 1.0, "z": 0.5}, {"roll": 0, "pitch": 0, "yaw": 0})
    actual = Pose.from_rpy_deg(
        {"x": 2.01, "y": 1.0, "z": 0.503}, {"roll": 0, "pitch": 0, "yaw": 0}
    )
    d = pose_delta(design, actual)
    assert d["dxMm"] == pytest.approx(10.0, abs=1e-6)
    assert d["dyMm"] == pytest.approx(0.0, abs=1e-6)
    assert d["dzMm"] == pytest.approx(3.0, abs=1e-6)
    assert d["distMm"] == pytest.approx(np.hypot(10.0, 3.0), abs=1e-6)
    assert d["maxRotDeg"] == pytest.approx(0.0, abs=1e-6)


def test_pose_delta_rotation():
    design = Pose.from_rpy_deg({"x": 0, "y": 0, "z": 0}, {"roll": 0, "pitch": 0, "yaw": 0})
    actual = Pose.from_rpy_deg({"x": 0, "y": 0, "z": 0}, {"roll": 0, "pitch": 0, "yaw": 2.0})
    d = pose_delta(design, actual)
    assert d["yawDeg"] == pytest.approx(2.0, abs=1e-6)
    assert d["maxRotDeg"] == pytest.approx(2.0, abs=1e-6)


def test_pose_delta_reported_in_space_frame_not_object_frame():
    # design이 yaw 90도 회전돼 있어도 위치 편차는 공간(X/Y) 축으로 보고된다
    design = Pose.from_rpy_deg({"x": 0, "y": 0, "z": 0}, {"roll": 0, "pitch": 0, "yaw": 90})
    actual = Pose.from_rpy_deg({"x": 0.05, "y": 0, "z": 0}, {"roll": 0, "pitch": 0, "yaw": 90})
    d = pose_delta(design, actual)
    assert d["dxMm"] == pytest.approx(50.0, abs=1e-6)
    assert d["dyMm"] == pytest.approx(0.0, abs=1e-6)


def test_pose_json_roundtrip():
    p = Pose.from_rpy_deg({"x": 1.25, "y": -2.5, "z": 0.8}, {"roll": 3, "pitch": -2, "yaw": 91})
    j = p.to_json()
    p2 = Pose.from_json(j)
    assert np.allclose(p2.position, p.position, atol=1e-9)
    assert np.allclose(p2.rotation, p.rotation, atol=1e-9)


def test_pose_json_accepts_quat():
    import math

    q = {"x": 0.0, "y": 0.0, "z": math.sin(math.pi / 4), "w": math.cos(math.pi / 4)}
    p = Pose.from_json({"positionM": {"x": 0, "y": 0, "z": 0}, "quat": q})
    assert np.allclose(p.rotation, rpy_deg_to_matrix(0, 0, 90), atol=1e-9)


def test_zup_to_yup():
    up_bim = np.array([0.0, 0.0, 1.0])
    assert np.allclose(Z_UP_TO_Y_UP @ up_bim, [0.0, 1.0, 0.0])  # BIM 위쪽 → glTF 위쪽
    north_bim = np.array([0.0, 1.0, 0.0])
    assert np.allclose(Z_UP_TO_Y_UP @ north_bim, [0.0, 0.0, -1.0])  # BIM 북쪽 → glTF 전방
    assert np.isclose(np.linalg.det(Z_UP_TO_Y_UP), 1.0)  # 오른손 유지 (반사 없음)
