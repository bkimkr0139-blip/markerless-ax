"""사물 분리 API + 자산 삭제(단건/일괄) 테스트."""

import base64
import json

import numpy as np
from fastapi.testclient import TestClient

from app.main import create_app
from spatial_core.registration import write_glb


def _client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    return TestClient(app)


def _cloud(P):
    return {"count": len(P), "base64": base64.b64encode(P.astype(np.float32).tobytes()).decode()}


def _scene(rng):
    table = np.stack([rng.uniform(-0.7, 0.7, 1500), np.zeros(1500), rng.uniform(-0.7, 0.7, 1500)], 1)
    box = []
    for _ in range(400):
        ax = rng.integers(0, 3)
        p = (rng.random(3) - 0.5) * np.array([0.2, 0.2, 0.15])
        p[ax] = rng.choice([-1, 1]) * np.array([0.2, 0.2, 0.15])[ax] / 2
        box.append(p + np.array([0, 0.1, 0]))
    P = np.vstack([table, np.array(box)]) + rng.normal(0, 0.002, (1900, 3))
    return P


def test_segment_endpoint_isolates_object(tmp_path):
    rng = np.random.default_rng(1)
    P = _scene(rng)
    with _client(tmp_path) as c:
        r = c.post("/api/v1/scan/segment", json={"cloud": _cloud(P), "poi": {"x": 0, "y": 0.2, "z": 0}, "maxRadius": 0.6})
        assert r.status_code == 200, r.text
        out = r.json()
        mask = np.frombuffer(base64.b64decode(out["mask"]), dtype=np.uint8).astype(bool)
        assert len(mask) == len(P) == out["count"]
        assert out["quality"] in ("good", "fair")
        assert 300 < out["nIn"] < 450          # 박스 점만 (탁자 1500점 제외)
        assert P[mask][:, 1].min() > -0.03     # 탁자(y≈0) 아래/위 평면이 섞이지 않음
        assert out["supportHeightM"] is not None


def test_segment_rejects_bad_cloud(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/api/v1/scan/segment", json={"cloud": {"count": 5, "base64": "AAAA"}, "poi": {"x": 0, "y": 0, "z": 0}})
        assert r.status_code == 422


def _register(c, aid, pts, **extra):
    verts = np.vstack([pts, pts + [0.01, 0, 0], pts + [0, 0.01, 0]]).astype(np.float32)
    n = len(pts)
    tris = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], 1).astype(np.uint32)
    meta = {"assetId": aid, "projectId": "P1", "spaceId": "S1",
            "designPose": {"positionM": {"x": 0, "y": 0, "z": 0}, "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}, **extra}
    r = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)},
               files={"model": ("a.glb", write_glb(verts, tris), "model/gltf-binary")})
    assert r.status_code == 201, r.text


def test_delete_single_and_bulk(tmp_path):
    rng = np.random.default_rng(3)
    with _client(tmp_path) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()
        pts = rng.uniform(-0.2, 0.2, (200, 3))
        for aid in ("A1", "A2", "A3"):
            _register(c, aid, pts + rng.normal(0, 0.001, pts.shape))
        # A1에 관측 하나 생성 → 삭제 시 함께 정리되는지
        cloud = (pts + rng.normal(0, 0.002, pts.shape)).astype(np.float32)
        m = c.post("/api/v1/measurements", json={"assetId": "A1", "alignment": "object",
                   "cloud": _cloud(cloud), "trackingNormal": True})
        assert m.status_code == 201, m.text

        assert c.delete("/api/v1/assets/A1").status_code == 204
        assert c.get("/api/v1/assets/A1").status_code == 404
        assert c.delete("/api/v1/assets/A1").status_code == 404  # 이미 없음

        r = c.post("/api/v1/assets/bulk-delete", json={"assetIds": ["A2", "A3", "NOPE", "A2"]})
        assert r.status_code == 200
        assert sorted(r.json()["deleted"]) == ["A2", "A3"] and r.json()["notFound"] == ["NOPE"]
        assert c.get(f"/api/v1/spaces/{sp['id']}/assets").json() == []


def test_segment_returns_outline_and_asset_outline_endpoint(tmp_path):
    rng = np.random.default_rng(1)
    P = _scene(rng)
    with _client(tmp_path) as c:
        r = c.post("/api/v1/scan/segment", json={"cloud": _cloud(P), "poi": {"x": 0, "y": 0.2, "z": 0}, "maxRadius": 0.6})
        o = r.json()["outline"]
        assert o is not None and len(o["lines"]) > 30 and o["levels"] >= 3
        assert set(o["views"]) == {"front", "right", "back", "left"}
        size = sorted(o["obb"]["size"])
        assert abs(size[0] - 0.15) < 0.04 and abs(size[2] - 0.20) < 0.04  # 박스 0.2×0.2×0.15

        # 등록 후 자산 외곽선 (로컬 프레임) + 삭제하면 404
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
        box = P[1500:] - P[1500:].mean(axis=0)
        _register(c, "BOX", box)
        ol = c.get("/api/v1/assets/BOX/outline")
        assert ol.status_code == 200 and len(ol.json()["lines"]) > 20
        assert c.delete("/api/v1/assets/BOX").status_code == 204
        assert c.get("/api/v1/assets/BOX/outline").status_code == 404


def test_segment_refine_and_assessment(tmp_path):
    rng = np.random.default_rng(1)
    P = _scene(rng)
    with _client(tmp_path) as c:
        r = c.post("/api/v1/scan/segment", json={"cloud": _cloud(P), "poi": {"x": 0, "y": 0.2, "z": 0},
                                                 "maxRadius": 0.6, "refine": True})
        assert r.status_code == 200, r.text
        out = r.json()
        a, ref = out["assessment"], out["refined"]
        assert a is not None and 0 <= a["score"] <= 100 and set(a["sides"]) == {"front", "right", "back", "left"}
        assert isinstance(a["tips"], list) and "sizeM" in a
        pts = np.frombuffer(base64.b64decode(ref["points"]), dtype="<f4").reshape(-1, 3)
        nrm = np.frombuffer(base64.b64decode(ref["normals"]), dtype="<f4").reshape(-1, 3)
        assert len(pts) == len(nrm) == ref["count"] == out["nIn"]
        assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0, atol=1e-3)
        # refine=false면 refined는 없다
        r2 = c.post("/api/v1/scan/segment", json={"cloud": _cloud(P), "poi": {"x": 0, "y": 0.2, "z": 0}, "maxRadius": 0.6})
        assert r2.json()["refined"] is None and r2.json()["assessment"] is not None


def _png(w=8, h=8):
    """최소 PNG (zlib로 직접 생성) — 서버는 시그니처·크기만 검증한다."""
    import struct
    import zlib

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x80\x80\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_photo_cards_upload_list_download_and_cleanup(tmp_path):
    rng = np.random.default_rng(3)
    meta = {"center": [0.0, 0.05, 0.0], "right": [1, 0, 0], "up": [0, 1, 0], "forward": [0, 0, -1],
            "widthM": 0.3, "heightM": 0.2}
    with _client(tmp_path) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
        _register(c, "A1", rng.uniform(-0.2, 0.2, (200, 3)))
        assert c.get("/api/v1/assets/A1/photos").json() == {"photos": []}

        files = {"image": ("front.png", _png(), "image/png")}
        assert c.put("/api/v1/assets/A1/photos/front", data={"meta": json.dumps(meta)}, files=files).status_code == 204
        assert c.put("/api/v1/assets/A1/photos/back", data={"meta": json.dumps(meta)}, files=files).status_code == 204
        lst = c.get("/api/v1/assets/A1/photos").json()["photos"]
        assert [p["side"] for p in lst] == ["front", "back"] and lst[0]["widthM"] == 0.3
        png = c.get("/api/v1/assets/A1/photos/front.png")
        assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
        assert c.get("/api/v1/assets/A1/photos/left.png").status_code == 404

        # 검증: 잘못된 방향 / PNG 아님 / 메타 오류
        assert c.put("/api/v1/assets/A1/photos/top", data={"meta": json.dumps(meta)}, files=files).status_code == 422
        bad = {"image": ("x.png", b"not a png", "image/png")}
        assert c.put("/api/v1/assets/A1/photos/left", data={"meta": json.dumps(meta)}, files=bad).status_code == 422
        assert c.put("/api/v1/assets/A1/photos/left", data={"meta": json.dumps({**meta, "widthM": 99})}, files=files).status_code == 422
        assert c.put("/api/v1/assets/NOPE/photos/front", data={"meta": json.dumps(meta)}, files=files).status_code == 404

        # 재등록하면 이전 사진은 지워진다, 삭제해도 정리된다
        _register(c, "A1", rng.uniform(-0.2, 0.2, (200, 3)))
        assert c.get("/api/v1/assets/A1/photos").json() == {"photos": []}
        c.put("/api/v1/assets/A1/photos/front", data={"meta": json.dumps(meta)}, files=files)
        assert c.delete("/api/v1/assets/A1").status_code == 204
        assert not (tmp_path / "s" / "photos" / "A1").exists()


def test_scan_time_and_gps_are_recorded_and_listed(tmp_path):
    rng = np.random.default_rng(5)
    with _client(tmp_path) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()
        pts = rng.uniform(-0.2, 0.2, (200, 3))
        _register(c, "G1", pts, scannedAt="2026-10-07T21:09:03+09:00", deviceId="phone-1",
                  geo={"lat": 37.5665, "lon": 126.978, "altM": 38.5, "accuracyM": 6.0})
        _register(c, "G2", pts)  # GPS 없는 등록(실내/권한 거부)
        rows = {a["assetId"]: a for a in c.get(f"/api/v1/spaces/{sp['id']}/assets").json()}
        assert rows["G1"]["scannedAt"].startswith("2026-10-07T12:09:03")  # UTC로 정규화
        assert rows["G1"]["geo"] == {"lat": 37.5665, "lon": 126.978, "altM": 38.5, "accuracyM": 6.0}
        assert rows["G1"]["deviceId"] == "phone-1"
        assert rows["G2"]["geo"] is None and rows["G2"]["scannedAt"]  # 시각은 서버 수신 시각으로 채움
        bad = c.post("/api/v1/assets/scan", data={"meta": "{}"}, files={"model": ("a.glb", b"x")})
        assert bad.status_code == 422


def test_photo_cards_24_directions_and_legacy_names(tmp_path):
    import io
    rng = np.random.default_rng(7)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    meta = {"center": [0, 0, 0], "right": [1, 0, 0], "up": [0, 1, 0], "forward": [0, 0, -1], "widthM": 0.2, "heightM": 0.2}
    with _client(tmp_path) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
        _register(c, "C1", rng.uniform(-0.2, 0.2, (200, 3)))
        for k in (5, 0, 23, 12):  # 순서 뒤섞어 업로드
            r = c.put(f"/api/v1/assets/C1/photos/a{k:02d}", data={"meta": json.dumps({**meta, "azimuthDeg": k * 15.0})},
                      files={"image": ("x.png", png, "image/png")})
            assert r.status_code == 204, r.text
        got = c.get("/api/v1/assets/C1/photos").json()["photos"]
        assert [p["side"] for p in got] == ["a00", "a05", "a12", "a23"]  # 방위각 순으로 정렬
        assert c.get("/api/v1/assets/C1/photos/a05.png").status_code == 200
        assert c.get("/api/v1/assets/C1/photos/front.png").status_code == 200  # 대표 썸네일(a00로 대체)
        bad = c.put("/api/v1/assets/C1/photos/a99", data={"meta": json.dumps(meta)}, files={"image": ("x.png", png, "image/png")})
        assert bad.status_code == 422
        legacy = c.put("/api/v1/assets/C1/photos/left", data={"meta": json.dumps(meta)}, files={"image": ("x.png", png, "image/png")})
        assert legacy.status_code == 204  # 구버전 이름도 계속 받는다


def _corrupt_glb_like_old_ios(verts: np.ndarray, tris: np.ndarray) -> bytes:
    """구버전 iOS 앱이 만들던 GLB: 정점을 SIMD3<Float>(16바이트)로 써 놓고 VEC3(12바이트)라고 선언."""
    import struct
    pos = b"".join(struct.pack("<ffff", *v, 0.0) for v in verts.astype(np.float32))
    idx = tris.astype(np.uint32).tobytes()
    js = json.dumps({
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(pos) + len(idx)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)}, {"buffer": 0, "byteOffset": len(pos), "byteLength": len(idx)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3"},
                      {"bufferView": 1, "componentType": 5125, "count": tris.size, "type": "SCALAR"}],
    }, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    bin_ = pos + idx
    bin_ += b"\0" * ((4 - len(bin_) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    return struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(bin_), 0x004E4942) + bin_


def test_corrupt_glb_from_old_app_is_rejected_with_clear_message(tmp_path):
    rng = np.random.default_rng(11)
    pts = rng.uniform(-0.2, 0.2, (200, 3))
    verts = np.vstack([pts, pts + [0.01, 0, 0], pts + [0, 0.01, 0]]).astype(np.float32)
    n = len(pts)
    tris = np.stack([np.arange(n), np.arange(n) + n, np.arange(n) + 2 * n], 1)
    with _client(tmp_path) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
        meta = {"assetId": "BAD", "projectId": "P1", "spaceId": "S1",
                "designPose": {"positionM": {"x": 0, "y": 0, "z": 0}, "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}}
        r = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)},
                   files={"model": ("a.glb", _corrupt_glb_like_old_ios(verts, tris), "model/gltf-binary")})
        assert r.status_code == 422 and "최신 빌드" in r.text, r.text
        _register(c, "GOOD", pts)  # 정상 GLB는 계속 받는다
        assert c.get("/api/v1/assets/GOOD").status_code == 200
