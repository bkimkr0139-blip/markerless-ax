"""최소 binary glTF(GLB) 리더/라이터 + 메시 샘플링.

앱(iOS)이 생성하는 GLB와 개발지시서 6.2절의 모바일 모델(GLB)을 다룬다.
지원 범위 (PoC용 최소 집합):
- 메시 primitive: POSITION(VEC3/FLOAT32) + indices(SCALAR/U8/U16/U32), mode=TRIANGLES
- 노드 변환: TRS(translation/rotation/scale) 또는 matrix (씬 트리 1단 누적)
- 텍스처/머티리얼/스킨 등은 무시 (오버레이 렌더링은 RealityKit이 자체 처리)
"""

from __future__ import annotations

import json
import struct

import numpy as np

_GLB_MAGIC = 0x46546C67  # 'glTF'
_CHUNK_JSON = 0x4E4F534A  # 'JSON'
_CHUNK_BIN = 0x004E4942  # 'BIN\0'

_COMPONENT_DTYPES = {
    5120: "<i1",
    5121: "<u1",
    5122: "<i2",
    5123: "<u2",
    5125: "<u4",
    5126: "<f4",
}
_TYPE_NUMS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def write_glb(positions: np.ndarray, indices: np.ndarray, name: str = "mesh") -> bytes:
    """정점/삼각형만 담는 최소 GLB 생성 (PoC 스캔 메시용).

    positions: (N,3) float32 — 미터, Y-up 권장
    indices  : (3*T,) 또는 (T,3) 정점 인덱스 — uint32로 저장
    """
    pos = np.asarray(positions, dtype="<f4").reshape(-1, 3)
    idx = np.asarray(indices, dtype="<u4").reshape(-1)

    pos_bytes = pos.tobytes()
    idx_bytes = idx.tobytes()
    pos_len = len(pos_bytes)
    idx_len = len(idx_bytes)
    pad = lambda x: (4 - x % 4) % 4  # noqa: E731
    idx_offset = pos_len + pad(pos_len)
    bin_len = idx_offset + idx_len + pad(idx_len)

    gltf = {
        "asset": {"version": "2.0", "generator": "markerless-poc"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [
            {
                "name": name,
                "primitives": [
                    {"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}
                ],
            }
        ],
        "buffers": [{"byteLength": bin_len}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": pos_len, "target": 34962},
            {
                "buffer": 0,
                "byteOffset": idx_offset,
                "byteLength": idx_len,
                "target": 34963,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": int(len(pos)),
                "type": "VEC3",
                "min": [float(v) for v in pos.min(axis=0)],
                "max": [float(v) for v in pos.max(axis=0)],
            },
            {
                "bufferView": 1,
                "componentType": 5125,
                "count": int(len(idx)),
                "type": "SCALAR",
            },
        ],
    }
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_len = len(json_bytes) + pad(len(json_bytes))
    json_bytes = json_bytes + b" " * (json_len - len(json_bytes))

    header = struct.pack("<III", _GLB_MAGIC, 2, 12 + 8 + json_len + 8 + bin_len)
    json_header = struct.pack("<II", json_len, _CHUNK_JSON)
    bin_header = struct.pack("<II", bin_len, _CHUNK_BIN)
    bin_data = (
        pos_bytes
        + b"\x00" * pad(pos_len)
        + idx_bytes
        + b"\x00" * (bin_len - idx_offset - idx_len)
    )
    return header + json_header + json_bytes + bin_header + bin_data


def _read_accessor(
    spec: dict, accessors: list[dict], buffer_views: list[dict], bin_chunk: bytes
) -> np.ndarray:
    acc = accessors[spec]
    bv = buffer_views[acc["bufferView"]]
    offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    num = acc["count"] * _TYPE_NUMS[acc["type"]]
    dtype = np.dtype(_COMPONENT_DTYPES[acc["componentType"]])
    # 정점 버퍼는 count×12바이트로 빈틈없이 채워져야 한다 (byteStride가 없을 때). iOS의 SIMD3<Float>는 메모리에서
    # 16바이트라, 그대로 쓰면 버퍼가 count×16이 되어 좌표가 전부 뒤섞인다 — 그런 파일은 거부한다.
    if acc["type"] == "VEC3" and "byteStride" not in bv and "byteLength" in bv and bv["byteLength"] != num * dtype.itemsize:
        raise ValueError(
            f"GLB 정점 버퍼 길이가 맞지 않습니다 (bufferView {bv['byteLength']}B ≠ {acc['count']}×{3 * dtype.itemsize}B) "
            "— 앱을 최신 빌드로 업데이트한 뒤 다시 스캔하세요"
        )
    arr = np.frombuffer(bin_chunk, dtype=dtype, count=num, offset=offset)
    return arr.reshape(acc["count"], _TYPE_NUMS[acc["type"]])


def _node_matrix(node: dict) -> np.ndarray:
    if "matrix" in node:  # column-major 16개
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    T = np.eye(4)
    if "translation" in node:
        T[:3, 3] = node["translation"]
    R = np.eye(3)
    if "rotation" in node:  # {x,y,z,w}
        x, y, z, w = node["rotation"]
        R = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ]
        )
    S = np.eye(3)
    if "scale" in node:
        S = np.diag(node["scale"])
    T[:3, :3] = R @ S
    return T


def read_glb_mesh(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """GLB -> (vertices (N,3) float64 미터, triangles (T,3) int64). 노드 변환 적용."""
    magic, version, _total = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC or version != 2:
        raise ValueError("not a glTF 2.0 binary (GLB) file")

    offset = 12
    json_doc: dict = {}
    bin_chunk = b""
    while offset < len(data):
        clen, ctype = struct.unpack_from("<II", data, offset)
        payload = data[offset + 8 : offset + 8 + clen]
        if ctype == _CHUNK_JSON:
            json_doc = json.loads(payload.decode("utf-8"))
        elif ctype == _CHUNK_BIN:
            bin_chunk = payload
        offset += 8 + clen + ((4 - clen % 4) % 4)

    accessors = json_doc["accessors"]
    buffer_views = json_doc["bufferViews"]
    meshes = json_doc["meshes"]
    nodes = json_doc.get("nodes", [])

    all_v: list[np.ndarray] = []
    all_f: list[np.ndarray] = []
    v_base = 0

    def add_primitive(mesh_index: int, world: np.ndarray) -> None:
        nonlocal v_base
        for prim in meshes[mesh_index]["primitives"]:
            v = _read_accessor(
                prim["attributes"]["POSITION"], accessors, buffer_views, bin_chunk
            ).astype(np.float64)
            f = _read_accessor(
                prim["indices"], accessors, buffer_views, bin_chunk
            ).astype(np.int64).reshape(-1, 3)
            v = (world[:3, :3] @ v.T).T + world[:3, 3]
            all_v.append(v)
            all_f.append(f + v_base)
            v_base += len(v)

    scene = json_doc["scenes"][json_doc.get("scene", 0)]
    for root in scene["nodes"]:
        world = _node_matrix(nodes[root])
        node = nodes[root]
        if "mesh" in node:
            add_primitive(node["mesh"], world)
        for child in node.get("children", []):
            child_node = nodes[child]
            child_world = world @ _node_matrix(child_node)
            if "mesh" in child_node:
                add_primitive(child_node["mesh"], child_world)

    if not all_v:
        raise ValueError("GLB contains no mesh primitives")
    vertices = np.vstack(all_v)
    triangles = np.vstack(all_f)
    return vertices, triangles


def mesh_aabb(vertices: np.ndarray) -> dict[str, list[float]]:
    """로컬 프레임 AABB {minM: [..], maxM: [..]}."""
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    return {
        "minM": [float(x) for x in v.min(axis=0)],
        "maxM": [float(x) for x in v.max(axis=0)],
    }


def mesh_extent(vertices: np.ndarray) -> float:
    """AABB 대각선 길이(미터) — ICP 잔차 정규화/Confidence 기준용."""
    aabb = mesh_aabb(vertices)
    d = np.array(aabb["maxM"]) - np.array(aabb["minM"])
    return float(np.linalg.norm(d))


def sample_mesh_points(
    vertices: np.ndarray,
    triangles: np.ndarray,
    n_samples: int,
    seed: int | None = 0,
    return_normals: bool = False,
):
    """삼각형 면적 비례 표면 샘플링 (ICP target용).

    return_normals=True이면 (points (N,3), normals (N,3))을 반환한다.
    법선은 샘플이 뽑힌 삼각형의 단위 법선 — point-to-plane ICP용.
    """
    rng = np.random.default_rng(seed)
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(triangles, dtype=np.int64)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cross = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    total = areas.sum()
    if total <= 0:
        raise ValueError("mesh has zero surface area")
    probs = areas / total
    tri_idx = rng.choice(len(f), size=n_samples, p=probs)

    r1 = np.sqrt(rng.random(n_samples))
    r2 = rng.random(n_samples)
    A, B, C = a[tri_idx], b[tri_idx], c[tri_idx]
    p1 = 1.0 - r1
    p2 = r1 * (1.0 - r2)
    p3 = r1 * r2
    pts = p1[:, None] * A + p2[:, None] * B + p3[:, None] * C
    if not return_normals:
        return pts
    tri_n = cross[tri_idx]
    tri_n /= np.linalg.norm(tri_n, axis=1, keepdims=True)
    return pts, tri_n
