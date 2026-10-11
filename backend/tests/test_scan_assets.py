"""스캔 → 자가 에셋 생성 경로 (Scan-to-BIM/CAD)."""

from conftest import make_box_glb

META = {
    "assetId": "SCAN-PUMP-001",
    "projectId": "PJT001",
    "spaceId": "MECH-301",
    "bimRevision": "GEN-001",
    "family": "Pump-A",
    "designPose": {
        "positionM": {"x": 2.0, "y": 1.0, "z": 0.5},
        "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 15},
    },
}


def _upload(client, meta=None, glb=None):
    return client.post(
        "/api/v1/assets/scan",
        data={"meta": __import__("json").dumps(meta or META)},
        files={"model": ("scan.glb", glb or make_box_glb(), "model/gltf-binary")},
    )


def test_scan_asset_lifecycle(client):
    r = client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    assert r.status_code == 201

    r = _upload(client)
    assert r.status_code == 201, r.text
    asset = r.json()
    assert asset["assetId"] == "SCAN-PUMP-001"
    assert asset["bimRevision"] == "GEN-001"
    assert asset["designPose"]["positionM"] == {"x": 2.0, "y": 1.0, "z": 0.5}
    # 기본 공차 Rule 자동 적용
    assert asset["tolerance"]["positionMm"] == 15.0
    assert asset["tolerance"]["rotationDeg"] == 1.0
    # 자산 로컬 AABB (0.4m 박스)
    aabb = asset["aabb"]
    assert abs(aabb["minM"][0] - (-0.2)) < 1e-5
    assert abs(aabb["maxM"][0] - 0.2) < 1e-5

    # GLB 다운로드 = 업로드 원본
    r = client.get("/api/v1/assets/SCAN-PUMP-001/model.glb")
    assert r.status_code == 200
    assert r.content == make_box_glb()


def test_rescan_replaces_asset(client):
    client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    _upload(client)
    glb2 = make_box_glb(size_m=0.6)
    r = _upload(client, glb=glb2)
    assert r.status_code == 201
    r = client.get("/api/v1/assets/SCAN-PUMP-001/model.glb")
    assert r.content == glb2


def test_missing_space_404(client):
    r = _upload(client)
    assert r.status_code == 404


def test_tiny_scan_rejected(client):
    client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    r = _upload(client, glb=make_box_glb(size_m=0.01))
    assert r.status_code == 422
    assert "too small" in r.json()["detail"]


def test_set_tolerance_rule(client):
    client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    _upload(client)
    r = client.put(
        "/api/v1/assets/SCAN-PUMP-001/tolerance",
        json={"positionMm": 10, "zToleranceMm": 8, "rotationDeg": 0.5},
    )
    assert r.status_code == 200
    assert r.json()["tolerance"]["positionMm"] == 10.0
