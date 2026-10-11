"""Revit 패키지 임포트 경로 (개발지시서 6.1)."""

PACKAGE = {
    "projectId": "PJT001",
    "revision": "R12",
    "spaceId": "MECH-301",
    "spaceName": "3층 기계실",
    "level": "3F",
    "assets": [
        {
            "assetId": "PUMP-301",
            "ifcGuid": "1kZ$L8v9X7KfO9GprP6mL$",
            "family": "Pump-A",
            "type": "PA-50",
            "designPose": {
                "positionM": {"x": 12.35, "y": 8.22, "z": 1.25},
                "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 90},
            },
            "tolerance": {"positionMm": 15, "zToleranceMm": 10, "rotationDeg": 1.0},
        },
        {
            "assetId": "PANEL-302",
            "family": "Panel-B",
            "designPose": {
                "positionM": {"x": 4.0, "y": 2.0, "z": 0.9},
                "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0},
            },
            "tolerance": {"positionMm": 20, "rotationDeg": 2.0},
        },
    ],
}


def test_import_and_query(client):
    r = client.post("/api/v1/packages", json=PACKAGE)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["imported"] == 2
    assert body["assetIds"] == ["PUMP-301", "PANEL-302"]

    r = client.get("/api/v1/assets/PUMP-301")
    asset = r.json()
    assert asset["bimRevision"] == "R12"
    assert asset["designPose"]["positionM"]["x"] == 12.35
    assert asset["tolerance"]["positionMm"] == 15
    assert asset["ifcGuid"] == "1kZ$L8v9X7KfO9GprP6mL$"

    sid = client.get("/api/v1/projects/PJT001/spaces").json()[0]["id"]
    r = client.get(f"/api/v1/spaces/{sid}/assets")
    assert {a["assetId"] for a in r.json()} == {"PUMP-301", "PANEL-302"}


def test_import_revalidates_revision(client):
    client.post("/api/v1/packages", json=PACKAGE)
    pkg = {**PACKAGE, "revision": "R13", "assets": [PACKAGE["assets"][0]]}
    r = client.post("/api/v1/packages", json=pkg)
    assert r.status_code == 201
    assert client.get("/api/v1/assets/PUMP-301").json()["bimRevision"] == "R13"


def test_missing_tolerance_is_bim_data_error(client):
    """공차 누락 = BIM 데이터 오류 → 422 + 검사 중단 (12.2)."""
    bad = {
        **PACKAGE,
        "assets": [
            {
                "assetId": "BAD-001",
                "designPose": {
                    "positionM": {"x": 0, "y": 0, "z": 0},
                    "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0},
                },
            }
        ],
    }
    r = client.post("/api/v1/packages", json=bad)
    assert r.status_code == 422


def test_missing_orientation_is_rejected(client):
    bad = {
        **PACKAGE,
        "assets": [
            {
                "assetId": "BAD-002",
                "designPose": {"positionM": {"x": 0, "y": 0, "z": 0}},
                "tolerance": {"positionMm": 15, "rotationDeg": 1},
            }
        ],
    }
    r = client.post("/api/v1/packages", json=bad)
    assert r.status_code == 422


def test_quat_orientation_accepted(client):
    import math

    pkg = {
        **PACKAGE,
        "assets": [
            {
                "assetId": "Q-001",
                "designPose": {
                    "positionM": {"x": 1, "y": 2, "z": 3},
                    "quat": {
                        "x": 0,
                        "y": 0,
                        "z": math.sin(math.pi / 4),
                        "w": math.cos(math.pi / 4),
                    },
                },
                "tolerance": {"positionMm": 15, "rotationDeg": 1},
            }
        ],
    }
    r = client.post("/api/v1/packages", json=pkg)
    assert r.status_code == 201, r.text
    asset = client.get("/api/v1/assets/Q-001").json()
    # quat 90° yaw → canonical rpy로 저장됨
    assert abs(asset["designPose"]["rpyDeg"]["yaw"]) == pytest_approx(90.0)


def pytest_approx(v):
    import pytest

    return pytest.approx(v, abs=1e-6)
