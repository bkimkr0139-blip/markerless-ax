"""보안 선행 과제: 약한 기본 비밀번호 변경 강제, 비밀번호 정책, 신규 API의 입력 방어."""

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app

H = {"X-Requested-With": "markerless-admin"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKERLESS_ENFORCE_PW_CHANGE", "1")
    auth.throttle_reset()
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        yield c


def login(c, pw="admin123!@#"):
    return c.post("/admin/api/login", json={"username": "admin", "password": pw}, headers=H)


def test_default_password_forces_change_before_anything_else(env):
    r = login(env)
    assert r.status_code == 200 and r.json()["user"]["mustChangePassword"] is True
    blocked = env.get("/admin/api/dashboard")
    assert blocked.status_code == 403 and "PASSWORD_CHANGE_REQUIRED" in blocked.json()["detail"]
    assert env.get("/admin/api/twin/buildings").status_code == 403, "신규 트윈 API도 같은 규칙"
    assert env.get("/admin/api/me").status_code == 200  # 변경 화면에 필요한 것만 허용
    # 정책 위반
    for bad in ("short1!", "alllowercase12", "admin123!@#x", "Admin-Password1"):
        r = env.post("/admin/api/me/password", json={"current": "admin123!@#", "new": bad}, headers=H)
        assert r.status_code == 422, bad
    # 같은 비밀번호 거절
    assert env.post("/admin/api/me/password", json={"current": "admin123!@#", "new": "admin123!@#"}, headers=H).status_code == 422
    ok = env.post("/admin/api/me/password", json={"current": "admin123!@#", "new": "T7#vRq-91zLm"}, headers=H)
    assert ok.status_code == 200
    assert env.get("/admin/api/dashboard").status_code == 200
    assert env.get("/admin/api/me").json()["user"]["mustChangePassword"] is False


def test_created_and_reset_accounts_must_change(env):
    login(env)
    env.post("/admin/api/me/password", json={"current": "admin123!@#", "new": "T7#vRq-91zLm"}, headers=H)
    weak = env.post("/admin/api/users", json={"username": "kim", "password": "kim12345", "role": "viewer"}, headers=H)
    assert weak.status_code == 422
    ok = env.post("/admin/api/users", json={"username": "kim", "password": "Zx9$kLmP-22q", "role": "viewer"}, headers=H)
    assert ok.status_code == 201 and ok.json()["mustChangePassword"] is True
    uid = ok.json()["id"]
    c2 = TestClient(env.app)
    r = c2.post("/admin/api/login", json={"username": "kim", "password": "Zx9$kLmP-22q"}, headers=H)
    assert r.json()["user"]["mustChangePassword"] is True
    assert c2.get("/admin/api/dashboard").status_code == 403
    # 초기화한 임시 비밀번호도 다시 변경 강제
    assert env.post(f"/admin/api/users/{uid}/password", json={"password": "Qw3!rTy-8890"}, headers=H).status_code == 200
    assert env.post(f"/admin/api/users/{uid}/password", json={"password": "weakweak"}, headers=H).status_code == 422


def test_existing_weak_account_is_flagged_at_startup(tmp_path, monkeypatch):
    """이미 운영 중이던 DB의 약한 기본 계정도 시작할 때 표시된다."""
    monkeypatch.setenv("MARKERLESS_ENFORCE_PW_CHANGE", "1")
    url = f"sqlite:///{tmp_path}/t.db"
    app = create_app(database_url=url, storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        login(c)
        c.post("/admin/api/me/password", json={"current": "admin123!@#", "new": "T7#vRq-91zLm"}, headers=H)
    # 강한 비밀번호로 바꾼 계정은 재시작해도 강제되지 않는다
    app2 = create_app(database_url=url, storage_dir=str(tmp_path / "s"))
    with TestClient(app2) as c2:
        r = c2.post("/admin/api/login", json={"username": "admin", "password": "T7#vRq-91zLm"}, headers=H)
        assert r.json()["user"]["mustChangePassword"] is False


def test_twin_api_input_defenses(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
        sid = c.post("/api/v1/spaces/S1/scan-sessions", json={"mode": "SPACE"}).json()["scanSessionId"]
        assert c.post("/api/v1/spaces/S1/scan-sessions", json={"mode": "HACK"}).status_code == 422
        assert c.put(f"/api/v1/scan-sessions/{sid}/frames/0/parts/../../etc/passwd?offset=0", content=b"x").status_code in (404, 405, 409, 422)
        assert c.put(f"/api/v1/scan-sessions/{sid}/frames/0/parts/evil?offset=0", content=b"x").status_code == 404
        # 구조 JSON 검증
        assert c.post(f"/api/v1/scan-sessions/{sid}/structure", content=b"not json").status_code == 422
        assert c.post(f"/api/v1/scan-sessions/{sid}/structure", content=b'{"walls": [{"x": 1}]}').status_code == 422
        # 파일 라우트는 알려진 종류만
        assert c.get("/api/v1/twin/files/../../etc/x.glb").status_code in (404, 422)
        assert c.get("/api/v1/twin/files/observed/nope.glb").status_code == 404
        assert c.get("/api/v1/twin/files/bogus/1.glb").status_code == 404
        # 해시 형식·크기 방어
        meta = {"frameIndex": 0, "cameraTransform": [0] * 16, "intrinsics": [0] * 9, "imageWidth": 10, "imageHeight": 10, "depthWidth": 4, "depthHeight": 4}
        bad = {"rgb": {"bytes": 10, "sha256": "zz"}, "depth": {"bytes": 64, "sha256": "0" * 64}, "conf": {"bytes": 16, "sha256": "0" * 64}}
        assert c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": bad}).status_code == 422
        huge = {**bad, "rgb": {"bytes": 999_999_999, "sha256": "0" * 64}}
        assert c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": huge}).status_code == 413
