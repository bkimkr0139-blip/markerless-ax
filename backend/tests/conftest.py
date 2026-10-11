from __future__ import annotations

import os

# 기존 테스트는 기본 관리자 비밀번호로 로그인한다 — 강제 변경은 전용 테스트(test_security.py)에서 켠다
os.environ.setdefault("MARKERLESS_ENFORCE_PW_CHANGE", "0")

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    with TestClient(app) as c:
        yield c


def make_box_glb(size_m: float = 0.4, center=(0.0, 0.0, 0.0)):
    """테스트용 정육면체 GLB (자산 로컬 프레임, 중심 center)."""
    from spatial_core.registration import write_glb

    s = size_m / 2
    cx, cy, cz = center
    v = np.array(
        [
            [cx - s, cy - s, cz - s],
            [cx + s, cy - s, cz - s],
            [cx + s, cy + s, cz - s],
            [cx - s, cy + s, cz - s],
            [cx - s, cy - s, cz + s],
            [cx + s, cy - s, cz + s],
            [cx + s, cy + s, cz + s],
            [cx - s, cy + s, cz + s],
        ]
    )
    f = np.array(
        [
            [0, 2, 1], [0, 3, 2],  # back
            [4, 5, 6], [4, 6, 7],  # front
            [0, 1, 5], [0, 5, 4],  # bottom
            [2, 3, 7], [2, 7, 6],  # top
            [1, 2, 6], [1, 6, 5],  # right
            [0, 4, 7], [0, 7, 3],  # left
        ]
    )
    return write_glb(v, f, name="test-box")


def box_cloud(size_m: float, center, n: int = 1200, noise_m: float = 0.001, seed: int = 7):
    """정육면체 표면 포인트 클라우드 (공간 프레임)."""
    rng = np.random.default_rng(seed)
    s = size_m / 2
    faces = rng.integers(0, 6, size=n)
    u = (rng.random(n) - 0.5) * size_m
    v = (rng.random(n) - 0.5) * size_m
    pts = np.zeros((n, 3))
    for i in range(6):
        m = faces == i
        axis, sign = i // 2, (1 if i % 2 == 0 else -1)
        other = [a for a in range(3) if a != axis]
        pts[m, axis] = sign * s
        pts[m, other[0]] = u[m]
        pts[m, other[1]] = v[m]
    pts += rng.normal(0, noise_m, pts.shape)
    pts += np.asarray(center)
    return pts


def cloud_payload(pts) -> dict:
    import base64

    f32 = pts.astype("<f4").reshape(-1)
    return {"base64": base64.b64encode(f32.tobytes()).decode(), "count": len(pts), "frame": "space"}
