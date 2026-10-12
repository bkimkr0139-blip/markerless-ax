"""디지털 트윈 E2E: 합성 방 프레임 → 청크 업로드 → 재구성 → 커버리지/구조/외피/투어/CAD 치환."""

import hashlib
import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from spatial_core.reconstruction.camera import to_mat16
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames

from app.main import create_app


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def frame_payload(f, seg=0):
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(f.rgb).save(buf, "JPEG", quality=90)
    rgb = buf.getvalue()
    depth = f.depth.astype("<f4").tobytes()
    conf = f.conf.astype(np.uint8).tobytes()
    K = f.K_rgb
    meta = {"frameIndex": f.index, "segmentIndex": seg, "timestampNs": f.index * 33_000_000, "cameraTransform": to_mat16(f.T_world_cam),
            "intrinsics": [K[0, 0], 0, 0, 0, K[1, 1], 0, K[0, 2], K[1, 2], 1], "imageWidth": f.rgb_size[0], "imageHeight": f.rgb_size[1],
            "depthWidth": f.depth.shape[1], "depthHeight": f.depth.shape[0], "trackingState": "normal", "motionBlurScore": 0.0}
    parts = {"rgb": rgb, "depth": depth, "conf": conf}
    return meta, parts


def upload_frame(c, sid, f, chunk=20_000):
    meta, parts = frame_payload(f)
    decl = {k: {"bytes": len(v), "sha256": sha(v)} for k, v in parts.items()}
    r = c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": decl, "idempotencyKey": f"{sid}-{f.index}"})
    assert r.status_code == 200, r.text
    if r.json()["state"] == "DONE":
        return
    for k, v in parts.items():
        off = r.json()["received"][k] if r.json().get("received") else 0
        while off < len(v):
            piece = v[off: off + chunk]
            rr = c.put(f"/api/v1/scan-sessions/{sid}/frames/{f.index}/parts/{k}?offset={off}", content=piece, headers={"X-Sha256": decl[k]["sha256"]})
            assert rr.status_code == 200, rr.text
            off += len(piece)
    rc = c.post(f"/api/v1/scan-sessions/{sid}/frames/complete-upload", json={"frameIndex": f.index})
    assert rc.status_code == 200, rc.text


def wait_ready(c, sid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/v1/scan-sessions/{sid}/status").json()
        if s["status"] in ("READY", "NEED_MORE_CAPTURE", "FAILED"):
            return s
        time.sleep(0.5)
    raise AssertionError("재구성 시간 초과")


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        yield c


def scan_room(c, space_ref, room, n=70, seed=0):
    sid = c.post(f"/api/v1/spaces/{space_ref}/scan-sessions", json={"mode": "SPACE", "deviceId": "t", "relocalized": True}).json()["scanSessionId"]
    for f in orbit_frames(room, n=n, seed=seed):
        upload_frame(c, sid, f)
    r = c.post(f"/api/v1/scan-sessions/{sid}/finalize", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PROCESSING", r.text
    r = c.post(f"/api/v1/scan-sessions/{sid}/reconstruct", json={"voxelM": 0.05, "textureMaxPx": 1024})
    assert r.status_code == 202, r.text
    st = wait_ready(c, sid)
    return sid, st


def test_full_space_pipeline(client):
    c = client
    c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
    spA = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "A101", "name": "방A"}).json()["id"]
    spB = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "A102", "name": "방B"}).json()["id"]
    b = c.post("/api/v1/buildings", json={"projectCode": "P1", "name": "본관", "buildingType": "concrete"}).json()
    f1 = c.post("/api/v1/floors", json={"buildingId": b["id"], "level": "1F", "elevationM": 0.0, "heightM": 3.0}).json()
    # 배치: A는 원점, B는 오른쪽 5.2m (이웃 방 — 벽 간격 0.2m)
    T = np.eye(4)
    TB = np.eye(4); TB[0, 3] = 5.2
    assert c.put("/api/v1/spaces/A101/placement", json={"buildingId": b["id"], "floorId": f1["id"], "tBuildingSpace": to_mat16(T)}).status_code == 200
    assert c.put(f"/api/v1/spaces/{spB}/placement", json={"buildingId": b["id"], "floorId": f1["id"], "tBuildingSpace": to_mat16(TB)}).status_code == 200

    roomA = SynthRoom(size=(5.0, 2.7, 4.0), door=("z0", 1.0, 2.0, 2.0))
    roomB = SynthRoom(size=(4.0, 2.7, 4.0), boxes=())
    sidA, stA = scan_room(c, "A101", roomA)
    assert stA["status"] in ("READY", "NEED_MORE_CAPTURE"), stA
    assert stA["revisionId"]
    sidB, stB = scan_room(c, str(spB), roomB, seed=3)
    assert stB["revisionId"]

    cov = c.get("/api/v1/spaces/A101/coverage").json()
    assert 0.4 < cov["overall"] <= 1.0 and cov["heatmapUrl"]
    revs = c.get("/api/v1/spaces/A101/geometry/revisions").json()
    assert revs and "textured" in revs[0]["files"] and revs[0]["current"]
    g = c.get(revs[0]["files"]["textured"])
    assert g.status_code == 200 and g.content[:4] == b"glTF"

    # 같은 공간 재스캔 → 새 리비전, 이전 리비전 보존
    sidA2, _ = scan_room(c, "A101", roomA, seed=5)
    revs2 = c.get("/api/v1/spaces/A101/geometry/revisions").json()
    assert len(revs2) == 2 and sum(r["current"] for r in revs2) == 1

    # 외피
    env = c.post(f"/api/v1/buildings/{b['id']}/generate-envelope", json={}).json()
    assert env["exterior"] > 0 and env["interior"] >= 1, env
    man = c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()
    assert len(man["spaces"]) == 2 and man["envelope"]["glbUrl"] and man["building"]["notice"]
    assert any(w["thicknessMethod"] == "MEASURED" for w in man["envelope"]["walls"]), "내벽 두께는 마주보는 벽 간격으로 측정"
    assert all(w["reviewStatus"] in ("REVIEW_REQUIRED", "AUTO", "APPROVED") for w in man["envelope"]["walls"])
    assert man["entrances"], "외벽 문 개구부에서 출입구 후보"
    # 벽 두께 수정 → 출처 MEASURED/BIM + 승인, 외피 재생성
    wid = next(w["id"] for w in man["envelope"]["walls"] if w["kind"] == "EXTERIOR")
    r = c.patch(f"/api/v1/walls/{wid}/thickness", json={"thicknessM": 0.35, "method": "BIM", "note": "도면"})
    assert r.status_code == 200 and r.json()["method"] == "BIM"
    man2 = c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()
    w2 = next(w for w in man2["envelope"]["walls"] if w["id"] == wid)
    assert abs(w2["thicknessM"] - 0.35) < 1e-6 and w2["thicknessMethod"] == "BIM"
    # 재생성해도 사람이 정한 값은 유지
    c.post(f"/api/v1/buildings/{b['id']}/generate-envelope", json={})
    w3 = next(w for w in c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()["envelope"]["walls"] if w["id"] == wid)
    assert abs(w3["thicknessM"] - 0.35) < 1e-6
    # 포털 추가
    tour = man["tourId"]
    p = c.post(f"/api/v1/tours/{tour}/portals", json={"fromSpace": "A101", "toSpace": "A102", "positionBuilding": [5.1, 0, 1.5], "yawDeg": 90})
    assert p.status_code == 201 and p.json()["reviewStatus"] == "REVIEW_REQUIRED"


def test_upload_resume_and_integrity(client):
    c = client
    c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
    sid = c.post("/api/v1/spaces/S1/scan-sessions", json={"mode": "SPACE", "relocalized": True}).json()["scanSessionId"]
    f = orbit_frames(SynthRoom(), n=1)[0]
    meta, parts = frame_payload(f)
    decl = {k: {"bytes": len(v), "sha256": sha(v)} for k, v in parts.items()}
    r = c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": decl})
    assert r.json()["state"] == "PENDING"
    d = parts["depth"]
    assert c.put(f"/api/v1/scan-sessions/{sid}/frames/0/parts/depth?offset=0", content=d[:1000]).status_code == 200
    # 어긋난 offset은 거절 + 서버가 받은 위치 알려줌 (재개)
    bad = c.put(f"/api/v1/scan-sessions/{sid}/frames/0/parts/depth?offset=5", content=d[5:10])
    assert bad.status_code == 409 and "1000" in bad.json()["detail"]
    # 재선언하면 받은 위치가 이어진다
    r2 = c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": decl}).json()
    assert r2["received"]["depth"] == 1000
    # 손상된 마지막 조각 → sha256 불일치로 거절, 처음부터
    junk = bytes(len(d) - 1000)
    bad2 = c.put(f"/api/v1/scan-sessions/{sid}/frames/0/parts/depth?offset=1000", content=junk)
    assert bad2.status_code == 422 and "sha256" in bad2.json()["detail"]
    r3 = c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": decl}).json()
    assert r3["received"]["depth"] == 0
    # 덜 올라온 프레임은 완료 불가
    assert c.post(f"/api/v1/scan-sessions/{sid}/frames/complete-upload", json={"frameIndex": 0}).status_code == 409
    # 잘못된 메타는 거절
    badmeta = dict(meta, cameraTransform=[1, 2, 3])
    assert c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": badmeta, "parts": decl}).status_code == 422
    # 크기 불일치
    wrong = {**decl, "depth": {"bytes": 10, "sha256": "0" * 64}}
    assert c.post(f"/api/v1/scan-sessions/{sid}/frames/init-upload", json={"meta": meta, "parts": wrong}).status_code == 422


def test_unregistered_session_not_merged(client):
    c = client
    c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
    sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()["id"]
    c.put(f"/api/v1/spaces/{sp}/worldmap", content=b"bplist00" + b"x" * 64, headers={"X-Sha256": sha(b"bplist00" + b"x" * 64)})
    s = c.post(f"/api/v1/spaces/{sp}/scan-sessions", json={"mode": "SPACE", "relocalized": False}).json()
    assert s["registered"] is False
    c.post(f"/api/v1/scan-sessions/{s['scanSessionId']}/finalize", json={})
    r = c.post(f"/api/v1/spaces/{sp}/merge-sessions", json={"sessionIds": [s["scanSessionId"]]})
    assert r.status_code == 409 and "UNREGISTERED" in r.json()["detail"]


def test_not_enough_frames_needs_more_capture(client):
    c = client
    c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"})
    sid = c.post("/api/v1/spaces/S1/scan-sessions", json={"mode": "SPACE", "relocalized": True}).json()["scanSessionId"]
    upload_frame(c, sid, orbit_frames(SynthRoom(), n=1)[0])
    r = c.post(f"/api/v1/scan-sessions/{sid}/finalize", json={}).json()
    assert r["status"] == "NEED_MORE_CAPTURE" and r["warnings"]
    assert c.post(f"/api/v1/scan-sessions/{sid}/reconstruct", json={}).status_code == 409


def test_cad_replacement_flow(client, tmp_path):
    """스캔 자산 → CAD 후보(자동 승인 없음) → 승인 → 원본 보존 → 되돌리기. 미등록 사물은 후보 없음."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "tools"))
    import match_bench as mb
    from spatial_core.registration import write_glb

    rng = np.random.default_rng(0)
    c = client
    c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
    sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()["id"]

    def glb_of(dims, n=2500):
        p, _ = mb.box(rng, dims, n)
        # 점-삼각형 GLB (앱이 만드는 형식과 동일)
        idx = np.arange(n * 3, dtype=np.uint32).reshape(-1, 3)
        pos = np.repeat(p, 3, axis=0) + rng.normal(0, 1e-4, (n * 3, 3))
        return write_glb(pos.astype("float32"), idx)

    def reg_asset(aid, dims):
        meta = {"assetId": aid, "projectId": "P1", "spaceId": "S1", "bimRevision": "GEN-001", "designPose": {"positionM": {"x": 1, "y": dims[1] / 2, "z": 1}, "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}}
        r = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)}, files={"model": ("m.glb", glb_of(dims), "model/gltf-binary")})
        assert r.status_code == 201, r.text

    for code, dims in (("BOX-A", (0.4, 0.25, 0.3)), ("TALL", (0.2, 0.6, 0.2))):
        r = c.post("/api/v1/cad-catalog", data={"meta": json.dumps({"code": code, "name": code, "symmetryDeg": [0, 180]})},
                   files={"model": ("m.glb", glb_of(dims), "model/gltf-binary")})
        assert r.status_code == 201, r.text
    reg_asset("A-1", (0.4, 0.25, 0.3))
    reg_asset("UNK", (0.9, 0.9, 0.9))
    cand = c.post("/api/v1/assets/A-1/cad-replacements/candidates").json()
    top = cand["candidates"][0]
    assert top["catalogCode"] == "BOX-A" and top["decision"] in ("CANDIDATE", "UNDETERMINED")
    assert cand["autoApprove"] is False
    unk = c.post("/api/v1/assets/UNK/cad-replacements/candidates").json()
    assert unk["candidates"] == [], "미등록 사물은 억지로 후보를 만들지 않는다"
    if top["decision"] == "UNDETERMINED":  # 대칭 등으로 미확정 — 사람이 근거 없이는 승인 불가, 근거+확인이면 가능
        assert c.post(f"/api/v1/assets/A-1/cad-replacements/{top['id']}/approve", json={}).status_code == 409
        assert c.post(f"/api/v1/assets/A-1/cad-replacements/{top['id']}/approve", json={"acknowledge": True}).status_code == 409
        ok = c.post(f"/api/v1/assets/A-1/cad-replacements/{top['id']}/approve", json={"acknowledge": True, "comment": "정면 라벨로 방향 확인"})
        assert ok.status_code == 200
        c.delete(f"/api/v1/assets/A-1/cad-replacements/{top['id']}")
    if top["decision"] == "CANDIDATE":
        before = c.get("/api/v1/assets/A-1/model.glb").content
        ap = c.post(f"/api/v1/assets/A-1/cad-replacements/{top['id']}/approve", json={"comment": "확인"})
        assert ap.status_code == 200 and ap.json()["observedPreserved"] is True
        assert c.get("/api/v1/assets/A-1/model.glb").content == before, "승인해도 OBSERVED 원본 모델은 그대로"
        rv = c.delete(f"/api/v1/assets/A-1/cad-replacements/{top['id']}")
        assert rv.status_code == 200 and rv.json()["observedRestored"] is True


def test_object_candidates_assets_link_and_design_layer(client):
    """공간 메시의 사물 후보(자동 확정 없음) + 자산 연결 + 설계 레이어(별도 보관)."""
    c = client
    c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
    spid = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "R1"}).json()["id"]
    b = c.post("/api/v1/buildings", json={"projectCode": "P1", "name": "본관"}).json()
    fl = c.post("/api/v1/floors", json={"buildingId": b["id"], "level": "1F"}).json()
    c.put("/api/v1/spaces/R1/placement", json={"buildingId": b["id"], "floorId": fl["id"], "tBuildingSpace": to_mat16(np.eye(4))})
    # 방 안 박스(1.2,0,1.0)-(1.7,0.8,1.5)와 같은 위치에 자산을 먼저 등록 (겹치면 '후보'로만 제안되어야 한다)
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
    import match_bench as mb
    from spatial_core.registration import write_glb

    rng = np.random.default_rng(1)
    p, _ = mb.box(rng, (0.5, 0.8, 0.5), 2000)
    idx = np.arange(6000, dtype=np.uint32).reshape(-1, 3)
    glb = write_glb((np.repeat(p, 3, axis=0) + rng.normal(0, 1e-4, (6000, 3))).astype("float32"), idx)
    meta = {"assetId": "BOX1", "projectId": "P1", "spaceId": "R1", "bimRevision": "GEN-001",
            "designPose": {"positionM": {"x": 1.45, "y": 0.4, "z": 1.25}, "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}}}
    assert c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)}, files={"model": ("m.glb", glb, "model/gltf-binary")}).status_code == 201
    room = SynthRoom(size=(5.0, 2.7, 4.0), boxes=(((1.2, 0.0, 1.0), (0.5, 0.8, 0.5)),))
    sid, st = scan_room(c, "R1", room, n=80)
    assert st["revisionId"]
    man = c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()
    sp = man["spaces"][0]
    objs = sp["objectCandidates"]
    assert objs, "사물 후보가 분리되어야 한다"
    assert all(o["label"] == "UNKNOWN_OBJECT" and o["reviewStatus"] == "REVIEW_REQUIRED" for o in objs), "클래스를 단정하지 않는다"
    linked = [o for o in objs if o["assetCandidates"]]
    assert linked and all(a["autoConfirmed"] is False for o in linked for a in o["assetCandidates"]), "자산 연결은 후보 제안만"
    assert any(a["assetId"] == "BOX1" and a["extractedMask"] for a in man["assets"]), "이중 표면 방지 영역"
    # 설계 지오메트리: 별도 레이어, 검토 전
    d = c.post("/api/v1/spaces/R1/design-geometry", data={"meta": json.dumps({"designRevision": "R7"})}, files={"model": ("d.glb", glb, "model/gltf-binary")})
    assert d.status_code == 201 and d.json()["reviewStatus"] == "REVIEW_REQUIRED"
    man2 = c.get(f"/api/v1/buildings/{b['id']}/tour-manifest").json()
    sp2 = man2["spaces"][0]
    assert sp2["layers"]["design"] and sp2["designRevision"] == "R7" and "DESIGN" in sp2["sourceKinds"]
    assert "OBSERVED" in sp2["sourceKinds"] and sp2["layers"]["observed"], "관측 레이어는 그대로"
    assert c.get(sp2["layers"]["design"]).status_code == 200
    assert c.post("/api/v1/spaces/R1/design-geometry", files={"model": ("d.glb", b"garbage", "model/gltf-binary")}).status_code == 422


def test_unplaced_space_is_auto_placed_for_tour(client):
    """배치하지 않은 공간도 재구성이 끝나면 임시 건물·1층에 자동 배치되어 웹 투어에 나타난다(검토 필요)."""
    c = client
    c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
    c.post("/api/v1/projects/P1/spaces", json={"spaceId": "AUTO1"})
    sid, st = scan_room(c, "AUTO1", SynthRoom(size=(4.0, 2.7, 3.5)), n=60)
    assert st["revisionId"]
    bs = c.get("/api/v1/buildings").json()
    assert len(bs) == 1 and bs[0]["name"] == "미배정 건물 (자동)" and bs[0]["floors"]
    man = c.get(f"/api/v1/buildings/{bs[0]['id']}/tour-manifest").json()
    assert man["spaces"][0]["spaceId"] == "AUTO1" and man["spaces"][0]["texturedGlbUrl"]
    # 두 번째 방도 같은 건물에 (겹치지 않게 옆으로)
    c.post("/api/v1/projects/P1/spaces", json={"spaceId": "AUTO2"})
    scan_room(c, "AUTO2", SynthRoom(size=(3.0, 2.7, 3.0)), n=60, seed=2)
    assert len(c.get("/api/v1/buildings").json()) == 1
    m2 = c.get(f"/api/v1/buildings/{bs[0]['id']}/tour-manifest").json()
    xs = sorted(mat[12] for mat in (s["tBuildingSpace"] for s in m2["spaces"]))
    assert len(xs) == 2 and xs[1] - xs[0] > 3.0, "두 방이 겹치지 않게 배치"
