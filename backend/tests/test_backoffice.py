"""통합 백오피스: 인증·권한·자산 관리·검수 리뷰·이슈 워크플로우."""

import base64
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app
from app.models import Observation
import test_auto_match as T  # 등록/검수 합성 장면 헬퍼 재사용

H = {"X-Requested-With": "markerless-admin"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKERLESS_API_KEY", raising=False)
    auth.throttle_reset()
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1", "name": "지하 기계실"}).json()
        c.space_id = sp["id"]
        c.app_ = app
        yield c


def login(c, username="admin", password="admin123!@#"):
    r = c.post("/admin/api/login", json={"username": username, "password": password})
    return r


def as_user(app, username, password):
    """별도 세션(쿠키)을 가진 클라이언트."""
    c = TestClient(app)
    c.__enter__()
    assert login(c, username, password).status_code == 200
    return c


def mk_user(admin, username, role, pw="Pw-Test-4821x"):
    r = admin.post("/admin/api/users", json={"username": username, "role": role, "password": pw, "displayName": username}, headers=H)
    assert r.status_code == 201, r.text
    return pw


def register_asset(c, aid="L-OBJ", seed=1):
    rng = np.random.default_rng(seed)
    T._register(c, aid, T._l_object(rng), rng, place=np.array([0.1, 0.0, 0.05]))
    return rng


# ───────────────────────── 인증 ─────────────────────────

def test_default_admin_login_and_session(env):
    assert env.get("/admin/api/assets").status_code == 401  # 미로그인
    r = login(env)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    me = env.get("/admin/api/me").json()
    assert me["user"]["username"] == "admin" and me["permissions"]["manageUsers"] is True
    assert env.post("/admin/api/logout", headers=H).status_code == 200
    assert env.get("/admin/api/me").status_code == 401  # 로그아웃하면 세션이 폐기됨


def test_wrong_password_and_lockout(env):
    for _ in range(5):
        assert login(env, "admin", "wrong").status_code == 401
    r = login(env, "admin", "admin123!@#")  # 맞는 비밀번호여도 잠금 중
    assert r.status_code == 429


def test_csrf_header_required_for_mutations(env):
    login(env)
    r = env.post("/admin/api/users", json={"username": "x1234", "role": "viewer", "password": "Pw-Test-4821x"})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]


def test_static_pages_and_security_headers(env):
    r = env.get("/admin/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert env.get("/admin/static/css/app.css").status_code == 200
    assert env.get("/admin/static/vendor/three.module.js").status_code == 200
    assert env.get("/api/v1/projects").status_code == 200  # 단말용 API는 영향 없음 (키 미설정)


# ───────────────────────── 역할 권한 ─────────────────────────

def test_role_permissions_on_assets(env):
    register_asset(env)
    login(env)
    mk_user(env, "mgr1", "manager"); mk_user(env, "insp1", "inspector"); mk_user(env, "view1", "viewer")
    viewer = as_user(env.app_, "view1", "Pw-Test-4821x")
    manager = as_user(env.app_, "mgr1", "Pw-Test-4821x")
    inspector = as_user(env.app_, "insp1", "Pw-Test-4821x")

    assert viewer.get("/admin/api/assets").status_code == 200  # 조회는 누구나
    assert viewer.patch("/admin/api/assets/L-OBJ", json={"name": "x"}, headers=H).status_code == 403
    assert inspector.patch("/admin/api/assets/L-OBJ", json={"name": "x"}, headers=H).status_code == 403
    r = manager.patch("/admin/api/assets/L-OBJ", json={"name": "1호기 펌프", "tolerance": {"positionMm": 20, "rotationDeg": 2}}, headers=H)
    assert r.status_code == 200 and r.json()["name"] == "1호기 펌프" and r.json()["tolerance"]["positionMm"] == 20
    assert manager.delete("/admin/api/assets/L-OBJ", headers=H).status_code == 403  # 삭제는 통합관리자만
    assert env.delete("/admin/api/assets/L-OBJ", headers=H).status_code == 200
    assert env.get("/admin/api/assets/L-OBJ").status_code == 404

    # 감사 로그: 수정과 삭제가 남는다 (관리자·현장관리자만 조회)
    acts = [a["action"] for a in env.get("/admin/api/audit").json()["logs"]]
    assert "asset.update" in acts and "asset.delete" in acts and "login" in acts
    assert inspector.get("/admin/api/audit").status_code == 403


def test_user_management(env):
    login(env)
    mk_user(env, "insp1", "inspector")
    assert env.post("/admin/api/users", json={"username": "insp1", "role": "viewer", "password": "Pw-Test-4821x"}, headers=H).status_code == 409
    assert env.post("/admin/api/users", json={"username": "a!", "role": "viewer", "password": "Pw-Test-4821x"}, headers=H).status_code == 422
    insp = as_user(env.app_, "insp1", "Pw-Test-4821x")
    assert insp.get("/admin/api/users").status_code == 403
    assert len(insp.get("/admin/api/users?assignable=true").json()["users"]) >= 2  # 담당자 선택용은 허용
    uid = next(u["id"] for u in env.get("/admin/api/users").json()["users"] if u["username"] == "insp1")
    # 비활성화하면 이미 로그인한 세션도 즉시 끊긴다
    assert env.patch(f"/admin/api/users/{uid}", json={"active": False}, headers=H).status_code == 200
    assert insp.get("/admin/api/me").status_code == 401
    assert login(TestClient(env.app_), "insp1", "Pw-Test-4821x").status_code == 401
    admin_id = next(u["id"] for u in env.get("/admin/api/users").json()["users"] if u["username"] == "admin")
    assert env.patch(f"/admin/api/users/{admin_id}", json={"role": "viewer"}, headers=H).status_code == 409  # 자기 강등 금지


def test_password_change_revokes_other_sessions(env):
    login(env)
    other = as_user(env.app_, "admin", "admin123!@#")
    r = env.post("/admin/api/me/password", json={"current": "admin123!@#", "new": "Nw-Pass-7391xz"}, headers=H)
    assert r.status_code == 200
    assert env.get("/admin/api/me").status_code == 200  # 지금 세션은 유지
    assert other.get("/admin/api/me").status_code == 401  # 다른 기기는 로그아웃
    assert login(TestClient(env.app_), "admin", "admin123!@#").status_code == 401
    assert login(TestClient(env.app_), "admin", "Nw-Pass-7391xz").status_code == 200


# ───────────────────────── 검수 리뷰 · 자동 이슈 ─────────────────────────

def _inspect_auto(c, rng, yaw=40.0):
    obj = T._l_object(rng)
    placed = (obj - (obj.min(0) + obj.max(0)) / 2) @ T._ry(yaw).T + [-0.3, 0.105, 0.25]
    scene = np.vstack([T._background(rng), placed[rng.random(len(placed)) < 0.7]])
    return T._inspect(c, scene, np.array([-0.3, 0.1, 0.25]), spaceId=c.space_id, autoMatch=True)


def test_inspection_review_and_dashboard(env):
    rng = register_asset(env)
    login(env)
    r = _inspect_auto(env, rng)
    assert r.status_code == 201, r.text
    oid = r.json()["observation"]["observationId"]

    lst = env.get("/admin/api/inspections").json()
    assert lst["total"] == 1 and lst["inspections"][0]["assetId"] == "L-OBJ" and lst["inspections"][0]["matchedBy"] == "auto"
    d = env.get(f"/admin/api/inspections/{oid}").json()
    assert d["hasReview"] and d["match"]["ranking"] and d["asset"]["assetId"] == "L-OBJ" and d["icp"]["inlierRatio"] > 0.5
    rv = env.get(f"/admin/api/inspections/{oid}/review").json()
    pts = np.frombuffer(base64.b64decode(rv["observedPoints"]["points"]), dtype="<f4").reshape(-1, 3)
    assert len(pts) == rv["observedPoints"]["count"] > 50 and rv["observedOutline"]["lines"] and rv["actualPose"]["quat"]

    csv = env.get("/admin/api/inspections.csv")
    assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf") and b"L-OBJ" in csv.content
    assert env.get("/admin/api/inspections?decision=NOPE").json()["total"] == 0

    dash = env.get("/admin/api/dashboard").json()
    assert dash["kpi"]["assets"] == 1 and dash["kpi"]["inspections"] == 1 and len(dash["daily"]) == 14
    assert sum(dash["decisions"].values()) == 1 and dash["recentInspections"][0]["id"] == oid

    # 3D 뷰어 자원: 모델/외곽선/사진
    glb = env.get("/admin/api/assets/L-OBJ/model.glb")
    assert glb.status_code == 200 and glb.content[:4] == b"glTF"
    assert env.get("/admin/api/assets/L-OBJ/outline").json()["lines"]
    assert env.get("/admin/api/assets/L-OBJ/photos/front.png").status_code == 404  # 아직 사진 카드 없음


def test_review_missing_for_legacy_observation(env):
    register_asset(env)
    login(env)
    with env.app_.state.session_factory() as db:
        db.add(Observation(id="legacy-1", asset_id="L-OBJ", device_id="old", decision="PASS", confidence="HIGH"))
        db.commit()
    assert env.get("/admin/api/inspections/legacy-1/review").status_code == 404
    assert env.get("/admin/api/inspections/legacy-1").json()["hasReview"] is False


def test_size_mismatch_cannot_pass_and_creates_issue(env):
    """고정 자산에 크기가 전혀 다른 작은 물체 → 합격하면 안 되고(FAIL/REVIEW), 이슈가 자동 생성된다."""
    rng = register_asset(env)
    login(env)
    ball = rng.normal(0, 1, (700, 3))
    ball = 0.08 * ball / np.linalg.norm(ball, axis=1, keepdims=True) + [0.0, 0.08, 0.0]
    r = T._inspect(env, np.vstack([T._background(rng), ball]), np.array([0.0, 0.16, 0.0]), assetId="L-OBJ")
    assert r.status_code == 201, r.text
    res = r.json()["result"]
    assert res["decision"] in ("FAIL", "REVIEW") and any("크기 불일치" in x for x in res["reasons"])
    issues = env.get("/admin/api/issues").json()
    assert issues["total"] == 1 and issues["issues"][0]["source"] == "auto" and issues["issues"][0]["status"] == "OPEN"
    first = issues["issues"][0]
    # 같은 자산을 다시 재면 새 이슈를 만들지 않고 '재측정' 이력만 쌓인다
    T._inspect(env, np.vstack([T._background(rng), ball]), np.array([0.0, 0.16, 0.0]), assetId="L-OBJ")
    assert env.get("/admin/api/issues").json()["total"] == 1
    ev = env.get(f"/admin/api/issues/{first['id']}").json()["events"]
    assert [e["type"] for e in ev].count("remeasure") == 1 and ev[0]["type"] == "created"


# ───────────────────────── 이슈 워크플로우 ─────────────────────────

def _issue(c, **kw):
    body = {"assetId": "L-OBJ", "title": "배관 위치 이탈", "description": "설계 대비 40mm", "severity": "HIGH", **kw}
    r = c.post("/admin/api/issues", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def _tr(c, iid, to, **kw):
    return c.post(f"/admin/api/issues/{iid}/transition", json={"to": to, **kw}, headers=H)


def test_issue_full_workflow_with_roles(env):
    register_asset(env)
    login(env)
    for u, role in (("insp1", "inspector"), ("mgr1", "manager"), ("tech1", "inspector"), ("view1", "viewer")):
        mk_user(env, u, role)
    insp, mgr, tech, view = (as_user(env.app_, u, "Pw-Test-4821x") for u in ("insp1", "mgr1", "tech1", "view1"))

    assert view.post("/admin/api/issues", json={"title": "x" * 3}, headers=H).status_code == 403  # 조회 전용은 불가
    iss = _issue(insp)
    iid = iss["id"]
    assert iss["status"] == "OPEN" and iss["code"].startswith("ISS-") and iss["source"] == "manual"

    # 검수자: 확인 가능, 조치 지시는 불가
    assert [t["to"] for t in iss["allowedTransitions"]] == ["CONFIRMED", "DISMISSED"]
    assert _tr(insp, iid, "CONFIRMED", comment="현장 확인함").status_code == 200
    assert _tr(insp, iid, "ASSIGNED", assignee="tech1").status_code == 403
    assert _tr(mgr, iid, "ASSIGNED").status_code == 422  # 담당자 필수
    assert _tr(mgr, iid, "ASSIGNED", assignee="view1").status_code == 422  # 조회 전용은 담당자 불가
    r = _tr(mgr, iid, "ASSIGNED", assignee="tech1", dueDate="2030-01-31", comment="2주 내 조치")
    assert r.status_code == 200 and r.json()["assignee"] == "tech1" and r.json()["dueDate"] == "2030-01-31"

    # 담당자 본인만(또는 관리자) 조치 시작/완료
    assert _tr(insp, iid, "IN_PROGRESS").status_code == 403
    assert _tr(tech, iid, "IN_PROGRESS").status_code == 200
    assert _tr(tech, iid, "RESOLVED").status_code == 422  # 조치 내용 필수
    assert _tr(tech, iid, "RESOLVED", resolution="배관 지지대 재설치").status_code == 200
    assert _tr(insp, iid, "REINSPECT", comment="재검수 요청").status_code == 200

    # 재검수 PASS가 없으면 종결 불가 → PASS 기록 후 종결
    assert _tr(insp, iid, "CLOSED").status_code == 409
    assert _tr(insp, iid, "CLOSED", force=True, comment="x").status_code == 409  # 강제 종결은 현장관리자 이상
    with env.app_.state.session_factory() as db:
        db.add(Observation(asset_id="L-OBJ", device_id="d1", decision="PASS", confidence="HIGH",
                           created_at=datetime.now(timezone.utc) + timedelta(seconds=5)))
        db.commit()
    done = _tr(insp, iid, "CLOSED", comment="재검수 합격 확인")
    assert done.status_code == 200 and done.json()["status"] == "CLOSED" and done.json()["closedAt"]
    types = [(e["type"], e["to"]) for e in done.json()["events"]]
    assert types[0] == ("created", "OPEN") and ("status", "CLOSED") in types and len(types) == 7
    assert env.get("/admin/api/issues?status=OPEN,CONFIRMED").json()["total"] == 0


def test_force_close_dismiss_and_comment(env):
    register_asset(env)
    login(env)
    iid = _issue(env)["id"]
    assert _tr(env, iid, "DISMISSED").status_code == 422  # 사유 필수
    assert _tr(env, iid, "DISMISSED", comment="센서 오탐 — 현장 확인 결과 정상").json()["status"] == "DISMISSED"
    assert _tr(env, iid, "CLOSED").status_code == 422  # 허용되지 않는 전이
    assert _tr(env, iid, "OPEN").json()["status"] == "OPEN"  # 재개
    for to, kw in (("CONFIRMED", {}), ("ASSIGNED", {"assignee": "admin"}), ("IN_PROGRESS", {}), ("RESOLVED", {"resolution": "조치"}), ("REINSPECT", {})):
        assert _tr(env, iid, to, **kw).status_code == 200, to
    r = _tr(env, iid, "CLOSED", force=True, comment="재검수 불가 장비 — 관리자 승인")  # 관리자는 사유 있는 강제 종결 가능
    assert r.status_code == 200 and r.json()["events"][-1]["data"].get("forced") is True
    c = env.post(f"/admin/api/issues/{iid}/comment", json={"comment": "사후 메모"}, headers=H).json()
    assert c["events"][-1]["type"] == "comment"


def test_issue_filters_overdue_and_patch_permissions(env):
    register_asset(env)
    login(env)
    mk_user(env, "insp1", "inspector")
    insp = as_user(env.app_, "insp1", "Pw-Test-4821x")
    late = _issue(env, title="기한 초과 건", dueDate="2020-01-01")
    _issue(env, title="정상 건", dueDate="2999-01-01")
    assert env.get("/admin/api/issues?overdue=true").json()["total"] == 1
    assert env.get("/admin/api/issues?q=기한").json()["issues"][0]["id"] == late["id"]
    assert env.get("/admin/api/issues").json()["counts"]["OPEN"] == 2
    assert insp.patch(f"/admin/api/issues/{late['id']}", json={"assignee": "admin"}, headers=H).status_code == 403  # 배정은 현장관리자 이상
    assert insp.patch(f"/admin/api/issues/{late['id']}", json={"severity": "CRITICAL"}, headers=H).json()["severity"] == "CRITICAL"


def test_spaces_devices_and_bulk(env):
    rng = register_asset(env, "A1", 1)
    T._register(env, "A2", T._slab(rng), rng, place=np.array([-0.2, 0, 0.2]))
    login(env)
    sp = env.get("/admin/api/spaces").json()["spaces"][0]
    assert sp["assets"] == 2 and sp["name"] == "지하 기계실"
    assert env.get("/admin/api/assets?q=a1").json()["total"] == 1
    r = env.post("/admin/api/assets/bulk", json={"action": "archive", "ids": ["A1", "A2", "NOPE"]}, headers=H).json()
    assert sorted(r["done"]) == ["A1", "A2"] and r["notFound"] == ["NOPE"]
    assert env.get("/admin/api/assets?status=ARCHIVED").json()["total"] == 2
    assert env.post("/admin/api/assets/bulk", json={"action": "delete", "ids": ["A1"]}, headers=H).json()["done"] == ["A1"]
    assert env.get("/admin/api/devices").status_code == 200
    assert env.get("/admin/api/system").json()["counts"]["assets"] == 1


def test_inspect_view_defaults_save_and_bulk_apply(env):
    register_asset(env, "V1", seed=1)
    register_asset(env, "V2", seed=2)
    # 앱(단말 API)이 보는 기본값 = 외곽 박스·치수·사진 실루엣
    base = env.get("/api/v1/assets/V1").json()["inspectView"]
    assert base == {"surface": False, "points": False, "outline": True, "box": True, "dimensions": True, "silhouette": True}
    login(env)
    assert env.get("/admin/api/assets/V1").json()["inspectView"] == base
    r = env.patch("/admin/api/assets/V1", json={"inspectView": {"outline": True, "silhouette": False}}, headers=H)
    assert r.status_code == 200, r.text
    got = env.get("/api/v1/assets/V1").json()["inspectView"]  # 단말이 검수 때 읽는 값에 반영
    assert got["outline"] is True and got["silhouette"] is False and got["box"] is True  # 빠진 키는 기본값
    assert env.get("/api/v1/assets/V2").json()["inspectView"] == base  # 다른 자산은 그대로
    assert env.patch("/admin/api/assets/V1", json={"inspectView": {"nope": True}}, headers=H).status_code == 422
    full = {"surface": True, "points": False, "outline": False, "box": False, "dimensions": True, "silhouette": True}
    r = env.post("/admin/api/assets/inspect-view", json={"inspectView": full}, headers=H)  # 전체 자산에 적용
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert env.get("/api/v1/assets/V1").json()["inspectView"] == full
    assert env.get("/api/v1/assets/V2").json()["inspectView"] == full
    one = env.post("/admin/api/assets/inspect-view", json={"ids": ["V2"], "inspectView": base}, headers=H).json()
    assert one["updated"] == 1 and env.get("/api/v1/assets/V1").json()["inspectView"] == full
    assert any(x["action"] == "asset.bulk_inspect_view" for x in env.get("/admin/api/audit").json()["logs"])
    # 권한: 조회 전용은 설정을 바꿀 수 없다
    mk_user(env, "viewer1", "viewer")
    v = as_user(env.app_, "viewer1", "Pw-Test-4821x")
    assert v.patch("/admin/api/assets/V1", json={"inspectView": base}, headers=H).status_code == 403


def test_space_crud_and_mobile_sync_revision(env):
    register_asset(env, "SP-A", seed=3)
    dev = lambda path: env.get(f"/api/v1/{path}")  # 단말 API (인증 없는 테스트 환경)
    login(env)
    rev0 = dev("sync/state").json()["revision"]
    # 추가
    r = env.post("/admin/api/spaces", json={"code": "B-201", "name": "배관실", "level": "B2"}, headers=H)
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert env.post("/admin/api/spaces", json={"code": "B-201"}, headers=H).status_code == 409  # 중복 ID
    rev1 = dev("sync/state").json()["revision"]
    assert rev1 != rev0  # 모바일이 감지
    assert any(sp["code"] == "B-201" and sp["name"] == "배관실" for sp in dev("projects/P1/spaces").json())
    # 수정 (이름·층·ID)
    assert env.patch(f"/admin/api/spaces/{new_id}", json={"name": "배관실(개정)", "level": "B3", "code": "B-202"}, headers=H).status_code == 200
    got = {sp["id"]: sp for sp in dev("projects/P1/spaces").json()}[new_id]
    assert (got["code"], got["name"], got["level"]) == ("B-202", "배관실(개정)", "B3")
    assert env.patch(f"/admin/api/spaces/{new_id}", json={"code": "S1"}, headers=H).status_code == 409  # 다른 공간과 충돌
    rev2 = dev("sync/state").json()["revision"]
    assert rev2 != rev1
    assert dev("sync/state").json()["revision"] == rev2  # 바뀐 게 없으면 그대로
    # 자산 이름·보관 상태도 모바일에 반영
    assert env.patch("/admin/api/assets/SP-A", json={"name": "펌프 A동"}, headers=H).status_code == 200
    a = dev("assets/SP-A").json()
    assert a["name"] == "펌프 A동" and a["status"] == "ACTIVE"
    assert dev("sync/state").json()["revision"] != rev2
    sid = env.space_id
    assert any(x["assetId"] == "SP-A" for x in dev(f"spaces/{sid}/assets").json())
    assert env.patch("/admin/api/assets/SP-A", json={"status": "ARCHIVED"}, headers=H).status_code == 200
    assert not any(x["assetId"] == "SP-A" for x in dev(f"spaces/{sid}/assets").json())  # 보관 자산은 현장 목록에서 빠진다
    # 삭제: 빈 공간은 바로, 자산이 있으면 확인(cascade) 필요, 권한은 통합관리자만
    mk_user(env, "mgr1", "manager")
    m = as_user(env.app_, "mgr1", "Pw-Test-4821x")
    assert m.delete(f"/admin/api/spaces/{new_id}", headers=H).status_code == 403
    assert env.delete(f"/admin/api/spaces/{new_id}", headers=H).status_code == 200
    assert dev(f"spaces/{new_id}").status_code == 404
    blocked = env.delete(f"/admin/api/spaces/{sid}", headers=H)
    assert blocked.status_code == 409 and "자산이 1개" in blocked.json()["detail"]
    ok = env.delete(f"/admin/api/spaces/{sid}?cascade=true", headers=H)
    assert ok.status_code == 200 and ok.json()["assetsDeleted"] == 1
    assert dev("assets/SP-A").status_code == 404 and dev(f"spaces/{sid}").status_code == 404
    acts = [x["action"] for x in env.get("/admin/api/audit").json()["logs"]]
    assert {"space.create", "space.update", "space.delete"} <= set(acts)


def test_change_events_fire_on_commit_not_before_and_coalesce(env):
    """저장(커밋) 시점에만 이벤트가 나가고, 연속 변경은 한 번으로 합쳐지며, 롤백은 알리지 않는다."""
    import time
    from app.events import broker

    got: list[set] = []
    remove = broker.add_listener(lambda sc: got.append(sc))
    try:
        register_asset(env, "EV-1", seed=4)   # 자산 등록 → assets
        time.sleep(0.7)
        assert got and "assets" in got[-1]
        got.clear()
        login(env)
        # 연속 저장 3번 → 합쳐서 한 번
        for n in ("a", "b", "c"):
            env.patch("/admin/api/assets/EV-1", json={"name": n}, headers=H)
        env.post("/admin/api/spaces", json={"code": "EV-S"}, headers=H)
        time.sleep(0.8)
        assert len(got) == 1 and {"assets", "spaces"} <= got[0], got
        got.clear()
        # 아무것도 안 바뀌는 조회/로그인은 알리지 않는다
        env.get("/admin/api/assets"); env.get("/admin/api/dashboard")
        time.sleep(0.6)
        assert got == []
        # 롤백된 변경은 알리지 않는다
        from app.models import Space
        sf = env.app_.state.session_factory
        with sf() as db:
            db.add(Space(project_code="P1", code="ROLLBACK-ME"))
            db.flush()
            db.rollback()
        time.sleep(0.6)
        assert got == []
    finally:
        remove()


def test_bulk_delete_inspections_cleans_results_review_and_keeps_issues(env):
    rng = register_asset(env)
    login(env)
    ids = []
    for _ in range(2):
        r = _inspect_auto(env, rng)
        assert r.status_code == 201, r.text
        ids.append(r.json()["observation"]["observationId"])
    assert env.get("/admin/api/inspections").json()["total"] == 2
    review = env.app_.state.storage.root / "reviews" / f"{ids[0]}.json"
    had_review = review.exists()
    issues = env.get("/admin/api/issues").json()["issues"]
    linked = [i for i in issues if i.get("observationId") in ids]
    # 권한: 통합관리자만
    mk_user(env, "mgr2", "manager")
    m = as_user(env.app_, "mgr2", "Pw-Test-4821x")
    assert m.post("/admin/api/inspections/bulk-delete", json={"ids": ids}, headers=H).status_code == 403
    r = env.post("/admin/api/inspections/bulk-delete", json={"ids": [ids[0], "no-such-id"]}, headers=H)
    assert r.status_code == 200 and r.json()["deleted"] == [ids[0]] and r.json()["notFound"] == ["no-such-id"]
    assert not review.exists() and had_review is True
    assert env.get(f"/admin/api/inspections/{ids[0]}").status_code == 404
    assert env.get("/admin/api/inspections").json()["total"] == 1
    # 이슈는 남고, 삭제된 검수와의 연결만 끊긴다
    after = env.get("/admin/api/issues").json()["issues"]
    assert len(after) == len(issues)
    assert not any(i.get("observationId") == ids[0] for i in after)
    assert env.post("/admin/api/inspections/bulk-delete", json={"ids": []}, headers=H).status_code == 422
    assert any(x["action"] == "inspection.bulk_delete" for x in env.get("/admin/api/audit").json()["logs"])
