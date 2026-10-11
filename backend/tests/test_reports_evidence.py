import json

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


def _setup_and_measure(client):
    client.post("/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"})
    client.post(
        "/api/v1/assets/scan",
        data={"meta": json.dumps(META)},
        files={"model": ("scan.glb", make_box_glb(), "model/gltf-binary")},
    )
    obs_ids = []
    # PASS 1건
    pts = box_cloud(0.4, center=(2.005, 1.0, 0.5), n=1200)
    r = client.post(
        "/api/v1/measurements",
        json={
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
            "depthValidRatio": 0.9,
        },
    )
    obs_ids.append(r.json()["observation"]["observationId"])
    # FAIL 1건
    pts = box_cloud(0.4, center=(2.05, 1.0, 0.5), n=1200)
    r = client.post(
        "/api/v1/measurements",
        json={
            "assetId": "SCAN-PUMP-001",
            "deviceId": "IPHONE-TEST",
            "cloud": cloud_payload(pts),
            "depthValidRatio": 0.9,
        },
    )
    obs_ids.append(r.json()["observation"]["observationId"])
    return obs_ids


def test_report_summary_korean(client):
    obs_ids = _setup_and_measure(client)
    r = client.post("/api/v1/reports", json={"observationIds": obs_ids})
    assert r.status_code == 201, r.text
    report = r.json()
    assert "검수 대상 2건" in report["summary"][0]
    assert "PASS 1건" in report["summary"][0]
    assert "FAIL 1건" in report["summary"][0]
    fail_line = [s for s in report["summary"] if "공차를 초과" in s]
    assert len(fail_line) == 1
    assert fail_line[0].startswith("SCAN-PUMP-001")

    # 재조회
    r = client.get(f"/api/v1/reports/{report['reportId']}")
    assert r.status_code == 200
    assert r.json()["summary"] == report["summary"]


def test_report_requires_scope(client):
    r = client.post("/api/v1/reports", json={})
    assert r.status_code == 422
    r = client.post("/api/v1/reports", json={"observationIds": ["nope"]})
    assert r.status_code == 422


def test_evidence_roundtrip(client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    r = client.post(
        "/api/v1/evidence",
        files={"file": ("capture.png", png, "image/png")},
        data={"kind": "image"},
    )
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["sizeBytes"] == len(png)

    r = client.get(f"/api/v1/evidence/{ev['evidenceId']}")
    assert r.status_code == 200
    assert r.json()["sha256"] == ev["sha256"]

    r = client.get(f"/api/v1/evidence/{ev['evidenceId']}/download")
    assert r.status_code == 200
    assert r.content == png  # 원본 보존 (원칙 13)


def test_evidence_rejects_unknown_type(client):
    r = client.post(
        "/api/v1/evidence",
        files={"file": ("evil.exe", b"MZ...", "application/octet-stream")},
    )
    assert r.status_code == 422
