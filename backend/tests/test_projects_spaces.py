def test_create_and_list_projects(client):
    r = client.post("/api/v1/projects", json={"projectId": "PJT001", "name": "테스트 현장"})
    assert r.status_code == 201
    assert r.json()["projectId"] == "PJT001"

    r = client.post("/api/v1/projects", json={"projectId": "PJT001"})
    assert r.status_code == 409

    r = client.get("/api/v1/projects")
    assert r.status_code == 200
    assert [p["projectId"] for p in r.json()] == ["PJT001"]

    r = client.get("/api/v1/projects/PJT001")
    assert r.status_code == 200
    r = client.get("/api/v1/projects/NOPE")
    assert r.status_code == 404


def test_space_lifecycle(client):
    r = client.post(
        "/api/v1/projects/PJT001/spaces",
        json={"spaceId": "MECH-301", "name": "3층 기계실", "level": "3F"},
    )
    assert r.status_code == 201
    space = r.json()
    assert space["code"] == "MECH-301"
    assert space["hasWorldMap"] is False
    sid = space["id"]

    r = client.get("/api/v1/projects/PJT001/spaces")
    assert [s["code"] for s in r.json()] == ["MECH-301"]

    r = client.get(f"/api/v1/spaces/{sid}")
    assert r.status_code == 200
    r = client.get("/api/v1/spaces/9999")
    assert r.status_code == 404


def test_worldmap_roundtrip(client):
    sid = client.post(
        "/api/v1/projects/PJT001/spaces", json={"spaceId": "MECH-301"}
    ).json()["id"]

    # bplist 시그니처 검사
    r = client.put(f"/api/v1/spaces/{sid}/worldmap", content=b"not-a-plist")
    assert r.status_code == 422

    data = b"bplist00" + b"\x00" * 64
    r = client.put(
        f"/api/v1/spaces/{sid}/worldmap",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200
    assert r.json()["hasWorldMap"] is True
    sha = r.json()["worldmapSha256"]

    r = client.get(f"/api/v1/spaces/{sid}/worldmap")
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["X-Sha256"] == sha
