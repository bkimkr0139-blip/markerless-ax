"""사물 앵커 정합 E2E: 월드맵 없이(세션 좌표가 임의) 저장된 자산에 붙는지."""

import base64

import numpy as np
from fastapi.testclient import TestClient

from app.main import create_app
from spatial_core.registration import write_glb


def _ry(deg):
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def _surfel_object(seed=5):
    """앱이 만드는 것과 같은 형태: 점마다 작은 삼각형(surfel). L자 사물의 표면 점."""
    rng = np.random.default_rng(seed)

    def box(c, size, n):
        c, size = np.array(c), np.array(size)
        out = []
        for _ in range(n):
            ax = rng.integers(0, 3)
            p = (rng.random(3) - 0.5) * size
            p[ax] = rng.choice([-1, 1]) * size[ax] / 2
            out.append(c + p)
        return np.array(out)

    pts = np.vstack([box([0, 0.15, 0], [0.5, 0.3, 0.2], 600), box([0.2, 0.45, 0], [0.1, 0.3, 0.2], 250)])
    pts -= pts.mean(axis=0)
    verts, idx = [], []
    for p in pts:
        b = len(verts)
        verts += [p + [0.012, 0, 0], p + [-0.006, 0.0104, 0], p + [-0.006, -0.0104, 0]]
        idx += [b, b + 1, b + 2]
    return pts, np.array(verts, dtype=np.float32), np.array(idx, dtype=np.uint32).reshape(-1, 3)


def test_object_anchor_attaches_without_worldmap(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db", storage_dir=str(tmp_path / "s"))
    with TestClient(app) as c:
        c.post("/api/v1/projects", json={"projectId": "P1", "name": "p"})
        sp = c.post("/api/v1/projects/P1/spaces", json={"spaceId": "S1"}).json()
        pts, verts, tris = _surfel_object()
        glb = write_glb(verts, tris)
        meta = {
            "assetId": "OBJ-1", "projectId": "P1", "spaceId": "S1",
            "designPose": {"positionM": {"x": 1.0, "y": 0.2, "z": -1.0}, "rpyDeg": {"roll": 0, "pitch": 0, "yaw": 0}},
        }
        import json
        r = c.post("/api/v1/assets/scan", data={"meta": json.dumps(meta)},
                   files={"model": ("scan.glb", glb, "model/gltf-binary")})
        assert r.status_code == 201, r.text

        # 다른 세션: 사물이 전혀 다른 위치·방위 (월드맵 없음), 75%만 관측 + 노이즈
        rng = np.random.default_rng(9)
        true_yaw, true_pos = 205.0, np.array([-0.7, 0.0, 2.4])
        seen = pts[rng.random(len(pts)) < 0.75]
        cloud = seen @ _ry(true_yaw).T + true_pos + rng.normal(0, 0.003, seen.shape)
        body = {
            "assetId": "OBJ-1", "alignment": "object", "worldMapRestored": False,
            "hintCenter": {"x": true_pos[0] + 0.05, "y": true_pos[1], "z": true_pos[2] - 0.04},
            "cloud": {"count": len(cloud), "base64": base64.b64encode(cloud.astype(np.float32).tobytes()).decode()},
            "trackingNormal": True,
        }
        r = c.post("/api/v1/measurements", json=body)
        assert r.status_code == 201, r.text
        out = r.json()
        pose = out["observation"]["actualPose"]
        got = np.array([pose["positionM"][k] for k in "xyz"])
        assert np.linalg.norm(got - true_pos) < 0.04
        assert "quat" in pose
        assert out["result"]["decision"] in ("PASS", "REVIEW")  # FAIL/RETAKE면 안 붙은 것
        assert any("월드맵" in x for x in out["result"]["reasons"])
