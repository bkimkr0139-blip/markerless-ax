"""디지털 트윈 API 권한 시험 — API 키, 세션 인증, 역할(뷰어/검수자/현장관리자/관리자), CSRF, 경로 입력."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spatial_core.reconstruction.camera import to_mat16
from spatial_core.reconstruction.synthetic import SynthRoom

from app import auth
from app.main import create_app
from tests.test_twin import scan_room  # 합성 방 스캔 헬퍼 재사용

H = {"X-Requested-With": "markerless-admin"}
PW = "Zq8$wLm-4417x"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tw")
    auth.throttle_reset()
    app = create_app(database_url=f"sqlite:///{tmp}/t.db", storage_dir=str(tmp / "s"))
    with TestClient(app) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "R1"}).json()["id"]
        b = c.post("/api/v1/buildings", json={"projectCode": "P1", "name": "본관"}).json()
        fl = c.post("/api/v1/floors", json={"buildingId": b["id"], "level": "1F"}).json()
        c.put("/api/v1/spaces/R1/placement", json={"buildingId": b["id"], "floorId": fl["id"], "tBuildingSpace": to_mat16(np.eye(4))})
        scan_room(c, "R1", SynthRoom(size=(5.0, 2.7, 4.0), door=("z0", 1.0, 2.0, 2.0)), n=60)
        assert c.post(f"/api/v1/buildings/{b['id']}/generate-envelope", json={}).status_code == 200
        man = c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()
        yield app, b["id"], man


def client_as(app, role):
    """역할별 로그인 클라이언트 (관리자가 계정을 만들고 첫 비밀번호 변경까지 마친 상태)."""
    adm = TestClient(app)
    adm.post("/admin/api/login", json={"username": "admin", "password": "admin123!@#"}, headers=H)
    if role == "admin":
        return adm
    adm.post("/admin/api/users", json={"username": role, "role": role, "password": PW}, headers=H)
    c = TestClient(app)
    assert c.post("/admin/api/login", json={"username": role, "password": PW}, headers=H).status_code == 200
    return c


def test_api_key_protects_twin_endpoints(world, monkeypatch):
    app, bid, man = world
    monkeypatch.setenv("MARKERLESS_API_KEY", "k" * 24)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        for path in (f"/api/v1/buildings/{bid}/tour-manifest", "/api/v1/buildings", "/api/v1/cad-catalog", f"/api/v1/twin/files/envelope/{bid}.glb"):
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers={"X-API-Key": "wrong" * 5}).status_code == 401
            assert c.get(path, headers={"X-API-Key": "k" * 24}).status_code in (200, 404)
        assert c.post("/api/v1/spaces/R1/scan-sessions", json={"mode": "SPACE"}).status_code == 401
        # 관리자 웹 API는 키가 아니라 세션 인증 — 키가 있어도 미로그인은 401
        assert c.get("/admin/api/twin/buildings").status_code == 401


@pytest.mark.parametrize("role,can_write", [("viewer", False), ("inspector", False), ("manager", True), ("admin", True)])
def test_admin_twin_role_matrix(world, role, can_write):
    app, bid, man = world
    c = client_as(app, role)
    # 읽기: 로그인한 누구나
    assert c.get("/admin/api/twin/buildings").status_code == 200
    scene = c.get(f"/admin/api/twin/buildings/{bid}/scene")
    assert scene.status_code == 200 and scene.json()["spaces"]
    g = c.get(scene.json()["spaces"][0]["texturedGlbUrl"])
    assert g.status_code == 200 and g.content[:4] == b"glTF"
    wall = next(w for w in man["envelope"]["walls"] if w["kind"] == "EXTERIOR")
    code = lambda r: r.status_code  # noqa: E731
    thick = c.patch(f"/admin/api/twin/walls/{wall['id']}/thickness", json={"thicknessM": 0.3, "method": "MEASURED"}, headers=H)
    review = c.post(f"/admin/api/twin/walls/{wall['id']}/review", json={"reviewStatus": "APPROVED"}, headers=H)
    env = c.post(f"/admin/api/twin/buildings/{bid}/generate-envelope", json={}, headers=H)
    portal = c.post("/admin/api/twin/portals", json={"fromSpace": "R1", "toSpace": None, "positionBuilding": [1, 0, 0], "yawDeg": 0}, headers=H)
    for r in (thick, review, env, portal):
        assert (r.status_code in (200, 201)) == can_write, (role, r.request.url, r.status_code, r.text[:120])
        if not can_write:
            assert r.status_code == 403


def test_csrf_and_unauthenticated_admin_twin(world):
    app, bid, man = world
    anon = TestClient(app)
    for path in ("/admin/api/twin/buildings", f"/admin/api/twin/buildings/{bid}/scene", "/admin/api/twin/cad-catalog"):
        assert anon.get(path).status_code == 401
    adm = client_as(app, "admin")
    wall = man["envelope"]["walls"][0]["id"]
    # CSRF 헤더 없는 변경 요청은 거절
    assert adm.patch(f"/admin/api/twin/walls/{wall}/thickness", json={"thicknessM": 0.2, "method": "MEASURED"}).status_code == 403
    # 범위 밖 입력
    assert adm.patch(f"/admin/api/twin/walls/{wall}/thickness", json={"thicknessM": 9.0, "method": "MEASURED"}, headers=H).status_code == 422
    assert adm.patch(f"/admin/api/twin/walls/{wall}/thickness", json={"thicknessM": 0.2, "method": "HACK"}, headers=H).status_code == 422
    assert adm.post(f"/admin/api/twin/walls/{wall}/review", json={"reviewStatus": "WHATEVER"}, headers=H).status_code == 422
    # 파일 경로: 알려진 종류만, 숫자/ID만
    for bad in ("/admin/api/twin/files/../../../etc/passwd.glb", "/admin/api/twin/files/observed/..%2F..%2Fx.glb", "/admin/api/twin/files/secret/1.glb"):
        assert adm.get(bad).status_code in (404, 422)
    # 감사 로그에 변경이 남는다
    adm.patch(f"/admin/api/twin/walls/{wall}/thickness", json={"thicknessM": 0.25, "method": "BIM", "note": "도면 A-101"}, headers=H)
    logs = adm.get("/admin/api/audit?limit=50").json()["logs"]
    assert any(l["action"] == "twin.wall_thickness" for l in logs)
