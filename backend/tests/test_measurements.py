"""측정 흐름 E2E: 자산 생성 → 측정 제출(cloud/pose) → 판정 저장 (개발지시서 9/13/14절)."""

import numpy as np
import pytest

from conftest import box_cloud, cloud_payload, make_box_glb

META = {
    "assetId": "SCAN-PUMP-001",
    "projectId": "PJT001",
    "spaceId": "MECH-301",
    "designPose": {
        "positionM": {"x": 2.0, "y": 1.0, "z": 0.5},
        "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0},
    },
}


def _setup(client):
    client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    client.post(
        "/api/v1/assets/scan",
        data={"meta": __import__("json").dumps(META)},
        files={"model": ("scan.glb", make_box_glb(), "model/gltf-binary")},
    )


def _measure(client, body):
    return client.post("/api/v1/measurements", json=body)


def test_cloud_path_pass(client):
    """설계 위치 근처(+5mm) 스캔 → ICP → PASS (HIGH 신뢰도)."""
    _setup(client)
    pts = box_cloud(
        0.4, center=(2.005, 1.0, 0.5), n=1200, noise_m=0.001
    )
    body = {
        "assetId": "SCAN-PUMP-001",
        "deviceId": "IPHONE-TEST",
        "cloud": cloud_payload(pts),
        "depthValidRatio": 0.9,
    }
    r = _measure(client, body)
    assert r.status_code == 201, r.text
    out = r.json()
    obs, result = out["observation"], out["result"]
    assert result["decision"] == "PASS"
    assert result["confidence"] == "HIGH"
    assert abs(result["deviation"]["dxMm"] - 5.0) < 2.0
    assert result["margins"]["position"]["certainlyIn"] is True
    # ICP 부가정보 저장 확인
    assert obs["deviation"]["distMm"] < 10.0
    # Observation 스냅샷에 BIM revision (원칙 12)
    assert obs["bimRevision"] == "GEN-001"


def test_cloud_path_fail(client):
    """지시서 Demo 2: X +27mm → FAIL."""
    _setup(client)
    pts = box_cloud(0.4, center=(2.027, 1.0, 0.5), n=1200, noise_m=0.001)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
            "depthValidRatio": 0.9,
        },
    )
    assert r.status_code == 201
    result = r.json()["result"]
    assert result["decision"] == "FAIL"
    assert abs(result["deviation"]["dxMm"] - 27.0) < 2.5


def test_low_quality_cloud_is_retake(client):
    """점이 극소 → 신뢰도 LOW → RETAKE (원칙: LOW는 자동 판정 불가)."""
    _setup(client)
    pts = box_cloud(0.4, center=(2.0, 1.0, 0.5), n=40, noise_m=0.003)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
        },
    )
    assert r.status_code == 201
    result = r.json()["result"]
    assert result["decision"] == "RETAKE"


def test_tracking_lost_is_retake(client):
    _setup(client)
    pts = box_cloud(0.4, center=(2.0, 1.0, 0.5), n=1200)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
            "trackingNormal": False,
        },
    )
    assert r.status_code == 201
    assert r.json()["result"]["decision"] == "RETAKE"


def test_explicit_pose_path(client):
    """기기가 정합 완료 포즈를 직접 보고하는 경로."""
    _setup(client)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "actualPose": {
                "positionM": {"x": 2.0, "y": 1.0, "z": 0.5},
                "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0.4},
            },
            "confidence": "HIGH",
            "uncertainty": {"positionMm": 2, "rotationDeg": 0.1},
        },
    )
    assert r.status_code == 201
    result = r.json()["result"]
    assert result["decision"] == "PASS"
    assert result["deviation"]["maxRotDeg"] == pytest_half(0.4)


def pytest_half(v):
    import pytest

    return pytest.approx(v, abs=1e-6)


def test_explicit_pose_without_confidence_is_retake(client):
    """신뢰도 미보고 → UNKNOWN → RETAKE (원칙 11)."""
    _setup(client)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "actualPose": {
                "positionM": {"x": 2.0, "y": 1.0, "z": 0.5},
                "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0},
            },
        },
    )
    assert r.status_code == 201
    assert r.json()["result"]["decision"] == "RETAKE"


def test_rotation_fail(client):
    """Yaw 2.0° > 공차 1° → FAIL (ICP 잔차 반영해도 확정 초과)."""
    _setup(client)
    # yaw 회전된 클라우드
    from spatial_core.coordinate import Pose

    pts_local = box_cloud(0.4, center=(0.0, 0.0, 0.0), n=1500, noise_m=0.0005)
    pose = Pose.from_rpy_deg(
        {"x": 2.0, "y": 1.0, "z": 0.5}, {"roll": 0, "pitch": 0, "yaw": 2.0}
    )
    pts = pose.apply(pts_local)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
            "depthValidRatio": 0.9,
        },
    )
    assert r.status_code == 201
    result = r.json()["result"]
    assert result["decision"] == "FAIL"
    # ICP 정합 정밀도 범위(±0.3°) 내에서 2° 회전을 복원하는지
    assert abs(result["deviation"]["yawDeg"]) == pytest.approx(2.0, abs=0.3)


def test_measurement_attaches_to_observation(client):
    """원본 관측 생성 후 측정 부착 → 관측에 판정 반영."""
    _setup(client)
    r = client.post(
        "/api/v1/observations",
        json={
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "evidenceUris": [],
            "captureMeta": {"frames": 90},
        },
    )
    assert r.status_code == 201
    obs_id = r.json()["observationId"]

    pts = box_cloud(0.4, center=(2.05, 1.0, 0.5), n=1200)
    r = _measure(
        client,
        {
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "observationId": obs_id,
            "cloud": cloud_payload(pts),
            "depthValidRatio": 0.9,
        },
    )
    assert r.status_code == 201
    assert r.json()["observation"]["observationId"] == obs_id

    r = client.get(f"/api/v1/observations/{obs_id}")
    assert r.json()["decision"] == "FAIL"  # +50mm

    r = client.get("/api/v1/inspection-results", params={"observationId": obs_id})
    assert r.status_code == 200
    assert len(r.json()) == 1
