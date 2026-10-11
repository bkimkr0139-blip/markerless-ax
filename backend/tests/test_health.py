def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version(client):
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    assert "spatialCore" in r.json()
