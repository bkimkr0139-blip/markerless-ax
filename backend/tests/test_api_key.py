from fastapi.testclient import TestClient

from app.main import create_app


def test_api_key_required_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKERLESS_API_KEY", "secret-key")
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200  # 연결 확인은 열어 둔다
        assert c.get("/api/v1/projects").status_code == 401
        assert c.get("/api/v1/projects", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/v1/projects", headers={"X-API-Key": "secret-key"}).status_code == 200


def test_no_key_configured_means_open(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKERLESS_API_KEY", raising=False)
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        assert c.get("/api/v1/projects").status_code == 200
