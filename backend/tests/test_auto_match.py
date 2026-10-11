"""검수 핵심 E2E: 등록(사물 분리 → 모델) → 다른 장면(위치·방위·배경·방해 사물이 다름)에서 자동 매칭 + 정합 정확도."""

import base64
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from spatial_core.registration import write_glb


def _ry(deg):
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _box(rng, c, size, n, noise=0.002):
    c, size = np.array(c, float), np.array(size, float)
    out = []
    for _ in range(n):
        ax = rng.integers(0, 3)
        p = (rng.random(3) - 0.5) * size
        p[ax] = rng.choice([-1, 1]) * size[ax] / 2
        out.append(c + p)
    return np.array(out) + rng.normal(0, noise, (n, 3))


def _l_object(rng, n=900):
    """비대칭 L자 사물, 바닥면이 y=0 (탁자 위에 놓임)."""
    return np.vstack([_box(rng, [0, 0.05, 0], [0.30, 0.10, 0.12], n // 2),
                      _box(rng, [-0.09, 0.13, 0], [0.12, 0.16, 0.12], n // 2)])


def _slab(rng, n=700):
    return _box(rng, [0, 0.025, 0], [0.42, 0.05, 0.26], n)


def _background(rng):
    table = np.stack([rng.uniform(-0.8, 0.8, 1800), np.zeros(1800), rng.uniform(-0.8, 0.8, 1800)], 1)
    wall = np.stack([rng.uniform(-0.9, 0.9, 900), rng.uniform(0, 1.0, 900), np.full(900, -0.9)], 1)
    return np.vstack([table, wall]) + rng.normal(0, 0.003, (2700, 3))


def _cloud(P):
    return {"count": len(P), "base64": base64.b64encode(P.astype(np.float32).tobytes()).decode()}


def _nearest_point(P, q):
    return P[np.argmin(np.linalg.norm(P - q, axis=1))]


def _register(c, aid, obj, rng, place):
    """앱과 같은 흐름: 장면 → POI 사물 분리 → 사물 중심 원점 GLB(점마다 작은 삼각형) → 등록."""
    scene = np.vstack([_background(rng), obj + place])
    poi = _nearest_point(obj + place, (obj + place).mean(axis=0))
    r = c.post("/api/v1/scan/segment", json={"cloud": _cloud(scene), "poi": dict(zip("xyz", poi)), "maxRadius": 0.6}).json()
    mask = np.frombuffer(base64.b64decode(r["mask"]), dtype=np.uint8).astype(bool)
    center = np.array([r["center"][k] for k in "xyz"])
    kept = scene[mask] - center
    n = len(kept)
    verts = np.vstack([kept + [0.012, 0, 0], kept + [-0.006, 0.0104, 0], kept + [-0.006, -0.0104, 0]]).astype(np.float32)
    tris = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], 1).astype(np.uint32)
    meta = {"assetId": aid, "projectId": "P1", "spaceId": "S1",
            "designPose": {"positionM": dict(zip("xyz", center)), "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}}
    resp = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)},
                  files={"model": ("a.glb", write_glb(verts, tris), "model/gltf-binary")})
    assert resp.status_code == 201, resp.text
    return center


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKERLESS_API_KEY", raising=False)
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()
        c.space_id = sp["id"]
        yield c


def _inspect(c, scene, hint, **extra):
    body = {"alignment": "object", "hintCenter": dict(zip("xyz", hint)), "cloud": _cloud(scene),
            "trackingNormal": True, "worldMapRestored": False, **extra}
    return c.post("/api/v1/measurements", json=body)


@pytest.mark.parametrize("yaw", [137.0, -60.0, 20.0])
def test_auto_match_picks_right_asset_and_pose_is_accurate(client, yaw):
    rng = np.random.default_rng(int(abs(yaw)) + 7)
    _register(client, "L-OBJ", _l_object(rng), rng, place=np.array([0.10, 0.0, 0.05]))
    _register(client, "SLAB", _slab(rng), rng, place=np.array([-0.2, 0.0, 0.2]))  # 방해(다른 크기) 자산

    # 검수 장면: 탁자 위 다른 자리 + 다른 방위 + 방해 사물(상자) + 65%만 관측
    obj = _l_object(rng)
    c0 = (obj.min(axis=0) + obj.max(axis=0)) / 2  # 등록 시 모델 원점(AABB 중심)과 같은 기준
    R = _ry(yaw)
    pos = np.array([-0.30, 0.0, 0.25])
    placed = (obj - c0) @ R.T + c0 + pos  # c0를 축으로 회전 후 이동
    seen = placed[rng.random(len(placed)) < 0.65]
    clutter = _box(rng, [0.45, 0.10, -0.2], [0.2, 0.2, 0.2], 400)
    scene = np.vstack([_background(rng), seen, clutter])
    true_center = c0 + pos  # AABB 중심의 실제 위치 (모델 원점이 놓이는 곳)

    hint = _nearest_point(seen, seen.mean(axis=0)) + np.array([0.03, 0.02, -0.03])  # 조준은 조금 어긋남
    r = _inspect(client, scene, hint, spaceId=client.space_id, autoMatch=True)
    assert r.status_code == 201, r.text
    out = r.json()
    m = out["match"]
    assert m["matchedBy"] == "auto" and m["assetId"] == "L-OBJ", m["ranking"]

    pose = out["observation"]["actualPose"]
    got = np.array([pose["positionM"][k] for k in "xyz"])
    # 모델 원점(=등록 때 AABB 중심)이 새 장면에서 놓이는 위치와 비교 — 등록 시 중심 기준이 달라 약간의 오프셋 허용
    assert np.linalg.norm(got - true_center) < 0.04, f"pos err {np.linalg.norm(got - true_center):.3f}"
    q = pose["quat"]
    yaw_got = np.degrees(2 * np.arctan2(q["y"], q["w"]))
    assert abs(((yaw_got - yaw) + 180) % 360 - 180) < 6.0, f"yaw {yaw_got:.1f} vs {yaw}"
    assert m["sizeRatio"]["footprint"] < 1.15 and m["sizeRatio"]["height"] < 1.15  # 관측이 모델보다 커지면 안 됨
    assert m["observedOutline"] is not None and len(m["observedOutline"]["lines"]) > 20
    assert out["result"]["decision"] in ("PASS", "REVIEW")


def test_auto_match_rejects_unrelated_object(client):
    rng = np.random.default_rng(3)
    _register(client, "L-OBJ", _l_object(rng), rng, place=np.array([0.1, 0, 0.05]))
    ball = rng.normal(0, 1, (700, 3))
    ball = 0.08 * ball / np.linalg.norm(ball, axis=1, keepdims=True) + [0.0, 0.08, 0.0]  # 지름 16cm 구
    scene = np.vstack([_background(rng), ball])
    r = _inspect(client, scene, np.array([0.0, 0.16, 0.0]), spaceId=client.space_id, autoMatch=True)
    assert r.status_code == 422 and "닮은" in r.json()["detail"], r.text


def test_pinned_asset_still_works_and_no_assets_message(client):
    rng = np.random.default_rng(4)
    r = _inspect(client, _background(rng), np.zeros(3), spaceId=client.space_id, autoMatch=True)
    assert r.status_code == 422 and "등록된 자산이 없습니다" in r.json()["detail"]
    _register(client, "L-OBJ", _l_object(rng), rng, place=np.array([0.1, 0, 0.05]))
    obj = _l_object(rng)
    scene = np.vstack([_background(rng), (obj + [-0.2, 0, 0.2])[rng.random(len(obj)) < 0.7]])
    r = _inspect(client, scene, np.array([-0.2, 0.08, 0.2]), assetId="L-OBJ")
    assert r.status_code == 201, r.text
    assert r.json()["match"]["matchedBy"] == "pinned"


def _match(c, scene, hint, **extra):
    body = {"spaceId": c.space_id, "hintCenter": dict(zip("xyz", hint)), "cloud": _cloud(scene), **extra}
    return c.post("/api/v1/match", json=body)


def test_match_endpoint_ranks_candidates_without_saving(client):
    """자산 찾기: 판정·저장 없이 후보 순위(유사도)만. 올바른 자산이 1위·accepted, 방해 자산은 뒤."""
    rng = np.random.default_rng(21)
    _register(client, "L-OBJ", _l_object(rng), rng, place=np.array([0.10, 0.0, 0.05]))
    _register(client, "SLAB", _slab(rng), rng, place=np.array([-0.2, 0.0, 0.2]))
    obj = _l_object(rng)
    placed = (obj - (obj.min(0) + obj.max(0)) / 2) @ _ry(75.0).T + [-0.3, 0.105, 0.25]
    scene = np.vstack([_background(rng), placed[rng.random(len(placed)) < 0.7]])
    r = _match(client, scene, np.array([-0.3, 0.1, 0.25]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] and out["best"] == "L-OBJ"
    top = out["candidates"][0]
    assert top["assetId"] == "L-OBJ" and top["similarity"] > 60 and top["accepted"]
    assert all(set(("assetId", "similarity", "inlier", "sizeRatio", "modelSizeM")) <= set(c) for c in out["candidates"])
    sims = [c["similarity"] for c in out["candidates"]]
    assert sims == sorted(sims, reverse=True)
    assert out["observedOutline"] is not None and out["segmentation"]["segmented"]
    # 저장 없음: 관측/판정 레코드가 생기지 않았다 (자산 삭제가 관측 때문에 막히지 않고 목록만 그대로)
    assert len(client.get(f"/api/v1/spaces/{client.space_id}/assets").json()) == 2


def test_match_endpoint_marks_unrelated_object_not_accepted(client):
    rng = np.random.default_rng(22)
    _register(client, "L-OBJ", _l_object(rng), rng, place=np.array([0.1, 0, 0.05]))
    ball = rng.normal(0, 1, (700, 3))
    ball = 0.08 * ball / np.linalg.norm(ball, axis=1, keepdims=True) + [0.0, 0.08, 0.0]
    r = _match(client, np.vstack([_background(rng), ball]), np.array([0.0, 0.16, 0.0]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] is False and out["best"] is None
    assert out["candidates"], "후보는 보여 줘서 사용자가 직접 고를 수 있어야 한다"
    assert r.json()["candidates"][0]["accepted"] is False


def test_match_endpoint_without_assets(client):
    rng = np.random.default_rng(23)
    r = _match(client, _background(rng), np.zeros(3))
    assert r.status_code == 422 and "등록된 자산이 없습니다" in r.json()["detail"]
