import numpy as np

from spatial_core.registration import (
    mesh_aabb,
    mesh_extent,
    read_glb_mesh,
    sample_mesh_points,
    write_glb,
)


def _tetrahedron():
    v = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    f = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    return v, f


def test_write_read_roundtrip():
    v, f = _tetrahedron()
    glb = write_glb(v, f, name="pump")
    v2, f2 = read_glb_mesh(glb)
    assert v2.shape == v.shape
    assert np.allclose(np.sort(v2, axis=0), np.sort(v, axis=0), atol=1e-6)
    assert f2.shape == f.shape


def test_read_glb_float16_indices():
    """u16 인덱스 GLB도 읽을 수 있어야 한다 (기기별 호환)."""
    import json
    import struct

    v, f = _tetrahedron()
    pos = v.astype("<f4").tobytes()
    idx = f.reshape(-1).astype("<u2").tobytes()
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(pos) + len(idx)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
            {"buffer": 0, "byteOffset": len(pos), "byteLength": len(idx)},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 4,
                "type": "VEC3",
                "min": [0, 0, 0],
                "max": [1, 1, 1],
            },
            {"bufferView": 1, "componentType": 5123, "count": 12, "type": "SCALAR"},
        ],
    }
    jb = json.dumps(gltf).encode()
    pad = lambda x: (4 - x % 4) % 4  # noqa: E731
    jb += b" " * pad(len(jb))
    total = 12 + 8 + len(jb) + 8 + len(pos) + len(idx)
    blob = (
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(jb), 0x4E4F534A)
        + jb
        + struct.pack("<II", len(pos) + len(idx), 0x004E4942)
        + pos
        + idx
    )
    v2, f2 = read_glb_mesh(blob)
    assert v2.shape == (4, 3)
    assert f2.shape == (4, 3)


def test_node_translation_applied():
    """노드에 translation이 있으면 정점에 적용돼야 한다."""
    import json
    import struct

    v, f = _tetrahedron()
    # write_glb는 노드 변환 없이 원본 좌표를 그대로 저장한다
    v2, _ = read_glb_mesh(write_glb(v, f))
    assert np.allclose(v2.max(axis=0), [1, 1, 1], atol=1e-5)

    # translation이 있는 GLB 수동 구성
    pos = v.astype("<f4").tobytes()
    idx = f.reshape(-1).astype("<u4").tobytes()
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [5.0, 0.0, 0.0]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(pos) + len(idx)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
            {"buffer": 0, "byteOffset": len(pos), "byteLength": len(idx)},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 4,
                "type": "VEC3",
                "min": [0, 0, 0],
                "max": [1, 1, 1],
            },
            {"bufferView": 1, "componentType": 5125, "count": 12, "type": "SCALAR"},
        ],
    }
    jb = json.dumps(gltf).encode()
    jb += b" " * ((4 - len(jb) % 4) % 4)
    total = 12 + 8 + len(jb) + 8 + len(pos) + len(idx)
    blob = (
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(jb), 0x4E4F534A)
        + jb
        + struct.pack("<II", len(pos) + len(idx), 0x004E4942)
        + pos
        + idx
    )
    v3, _ = read_glb_mesh(blob)
    assert np.allclose(v3.max(axis=0), [6, 1, 1], atol=1e-5)


def test_sampling_area_proportional():
    """큰 면에서 더 많이 샘플링되는지 — 두 직사각형 판."""
    v = np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [1, 1, 0],
            [0, 1, 0],  # 1x1 판 (면적 1)
            [0, 0, 1],
            [10, 0, 1],
            [10, 10, 1],
            [0, 10, 1],  # 10x10 판 (면적 100)
        ]
    )
    f = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]])
    pts = sample_mesh_points(v, f, 2000, seed=42)
    assert pts.shape == (2000, 3)
    in_big = np.mean(pts[:, 2] > 0.5)
    assert 0.94 < in_big < 0.996  # 면적 비 100/101


def test_aabb_and_extent():
    v, _ = _tetrahedron()
    aabb = mesh_aabb(v)
    assert aabb["minM"] == [0.0, 0.0, 0.0]
    assert aabb["maxM"] == [1.0, 1.0, 1.0]
    assert mesh_extent(v) == np.sqrt(3.0)
