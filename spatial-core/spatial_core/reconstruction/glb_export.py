"""glTF 2.0 바이너리(GLB) 내보내기 — 텍스처 메시 / 정점색 메시 / 단순 박스 모음."""

from __future__ import annotations

import io
import json
import struct

import numpy as np

from .mesh import TriMesh


def _pad4(b: bytes, pad: bytes = b"\x00") -> bytes:
    return b + pad * ((4 - len(b) % 4) % 4)


class _Builder:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.views: list[dict] = []
        self.accessors: list[dict] = []

    def view(self, data: bytes, target: int | None = None) -> int:
        while len(self.buf) % 4:
            self.buf += b"\x00"
        self.views.append({"buffer": 0, "byteOffset": len(self.buf), "byteLength": len(data), **({"target": target} if target else {})})
        self.buf += data
        return len(self.views) - 1

    def accessor(self, arr: np.ndarray, ctype: int, typ: str, target: int | None = None, minmax: bool = False) -> int:
        a = {"bufferView": self.view(arr.tobytes(), target), "componentType": ctype, "count": int(len(arr)), "type": typ}
        if minmax:
            a["min"] = arr.min(0).astype(float).tolist()
            a["max"] = arr.max(0).astype(float).tolist()
        self.accessors.append(a)
        return len(self.accessors) - 1


def _pack(doc: dict, b: _Builder) -> bytes:
    doc["buffers"] = [{"byteLength": len(b.buf)}]
    doc["bufferViews"] = b.views
    doc["accessors"] = b.accessors
    js = _pad4(json.dumps(doc, separators=(",", ":")).encode("utf-8"), b" ")
    bn = _pad4(bytes(b.buf))
    return struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bn)) + struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(bn), 0x004E4942) + bn


def write_textured_glb(tm, extras: dict | None = None, jpeg_quality: int = 88, name: str = "space") -> bytes:
    from PIL import Image

    b = _Builder()
    pos = tm.v.astype("<f4")
    nrm = tm.n.astype("<f4")
    uv = tm.uv.astype("<f4")
    idx = tm.f.reshape(-1).astype("<u4")
    attrs = {"POSITION": b.accessor(pos, 5126, "VEC3", 34962, True), "NORMAL": b.accessor(nrm, 5126, "VEC3", 34962),
             "TEXCOORD_0": b.accessor(uv, 5126, "VEC2", 34962)}
    ai = b.accessor(idx.reshape(-1, 1), 5125, "SCALAR", 34963)
    img = io.BytesIO()
    Image.fromarray(tm.atlas).save(img, "JPEG", quality=jpeg_quality)
    iv = b.view(img.getvalue())
    doc = {"asset": {"version": "2.0", "generator": "markerless-spatial-core"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0, "name": name}],
           "meshes": [{"primitives": [{"attributes": attrs, "indices": ai, "material": 0, "mode": 4}], "extras": extras or {}}],
           "materials": [{"name": "photo", "doubleSided": True,
                          "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0.0, "roughnessFactor": 1.0}}],
           "textures": [{"source": 0, "sampler": 0}], "images": [{"bufferView": iv, "mimeType": "image/jpeg"}],
           "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]}
    return _pack(doc, b)


def write_colored_glb(mesh: TriMesh, extras: dict | None = None, name: str = "mesh", flat_color: tuple[int, int, int] | None = None,
                      alpha: float = 1.0) -> bytes:
    """정점색(없으면 단색) 메시. 추정(INFERRED)/커버리지 히트맵/치환 모델용."""
    b = _Builder()
    pos = mesh.v.astype("<f4")
    nrm = mesh.vertex_normals().astype("<f4")
    attrs = {"POSITION": b.accessor(pos, 5126, "VEC3", 34962, True), "NORMAL": b.accessor(nrm, 5126, "VEC3", 34962)}
    if mesh.colors is not None:
        col = np.concatenate([mesh.colors.astype(np.float32) / 255.0, np.full((len(mesh.colors), 1), alpha, np.float32)], 1).astype("<f4")
        attrs["COLOR_0"] = b.accessor(col, 5126, "VEC4", 34962)
    ai = b.accessor(mesh.f.reshape(-1, 1).astype("<u4"), 5125, "SCALAR", 34963)
    base = [c / 255.0 for c in (flat_color or (200, 200, 200))] + [alpha]
    mat = {"name": "m", "doubleSided": True, "pbrMetallicRoughness": {"baseColorFactor": base, "metallicFactor": 0.0, "roughnessFactor": 0.9}}
    if alpha < 1.0:
        mat["alphaMode"] = "BLEND"
    doc = {"asset": {"version": "2.0", "generator": "markerless-spatial-core"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0, "name": name}], "meshes": [{"primitives": [{"attributes": attrs, "indices": ai, "material": 0, "mode": 4}],
                                                                 "extras": extras or {}}], "materials": [mat]}
    return _pack(doc, b)


def box_mesh(center, size, color=None) -> TriMesh:
    c = np.asarray(center, float)
    h = np.asarray(size, float) / 2
    sg = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)
    v = c + sg * h
    # 8 코너 인덱스: bit(x,y,z) → index = x*4+y*2+z (x,y,z∈{0,1})
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    f = []
    for a, b_, c_, d in quads:
        f += [(a, b_, c_), (a, c_, d)]
    f = np.array(f)
    # 바깥쪽을 향하도록 방향 보정
    cen = v.mean(0)
    for i, tri in enumerate(f):
        n = np.cross(v[tri[1]] - v[tri[0]], v[tri[2]] - v[tri[0]])
        if np.dot(n, v[tri].mean(0) - cen) < 0:
            f[i] = tri[[0, 2, 1]]
    col = None if color is None else np.tile(np.asarray(color, np.uint8), (len(v), 1))
    return TriMesh(v, f, col)
