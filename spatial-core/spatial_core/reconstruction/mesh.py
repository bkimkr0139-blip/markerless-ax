"""삼각형 메시 자료구조와 정리(연결 성분 필터·용접·LOD)."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


@dataclass
class TriMesh:
    v: np.ndarray  # (N,3) float
    f: np.ndarray  # (M,3) int
    colors: np.ndarray | None = None  # (N,3) uint8
    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.v = np.asarray(self.v, dtype=np.float64).reshape(-1, 3)
        self.f = np.asarray(self.f, dtype=np.int64).reshape(-1, 3)

    @property
    def empty(self) -> bool:
        return len(self.f) == 0

    def face_normals(self) -> np.ndarray:
        a, b, c = self.v[self.f[:, 0]], self.v[self.f[:, 1]], self.v[self.f[:, 2]]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        return n / np.maximum(ln, 1e-12)

    def face_areas(self) -> np.ndarray:
        a, b, c = self.v[self.f[:, 0]], self.v[self.f[:, 1]], self.v[self.f[:, 2]]
        return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)

    def face_centers(self) -> np.ndarray:
        return self.v[self.f].mean(axis=1)

    def vertex_normals(self) -> np.ndarray:
        fn = self.face_normals() * self.face_areas()[:, None]
        vn = np.zeros_like(self.v)
        for k in range(3):
            np.add.at(vn, self.f[:, k], fn)
        ln = np.linalg.norm(vn, axis=1, keepdims=True)
        return vn / np.maximum(ln, 1e-12)

    def aabb(self) -> tuple[np.ndarray, np.ndarray]:
        return self.v.min(0), self.v.max(0)

    def area(self) -> float:
        return float(self.face_areas().sum())

    def transformed(self, T: np.ndarray) -> "TriMesh":
        return TriMesh(self.v @ T[:3, :3].T + T[:3, 3], self.f.copy(),
                       None if self.colors is None else self.colors.copy(), dict(self.extras))

    def subset_faces(self, keep: np.ndarray) -> "TriMesh":
        f = self.f[keep]
        used = np.unique(f)
        remap = -np.ones(len(self.v), dtype=np.int64)
        remap[used] = np.arange(len(used))
        return TriMesh(self.v[used], remap[f], None if self.colors is None else self.colors[used], dict(self.extras))


def weld(mesh: TriMesh, tol: float = 1e-4) -> TriMesh:
    """tol 격자로 정점을 합치고 퇴화 삼각형을 제거."""
    if mesh.empty:
        return mesh
    q = np.round(mesh.v / tol).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    v = mesh.v[first]
    f = inv[mesh.f]
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    col = None if mesh.colors is None else mesh.colors[first]
    return TriMesh(v, f[ok], col, dict(mesh.extras))


def face_components(mesh: TriMesh) -> np.ndarray:
    """정점을 공유하는 face끼리의 연결 성분 번호 (face별)."""
    if mesh.empty:
        return np.zeros(0, dtype=int)
    n = len(mesh.v)
    f = mesh.f
    rows = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    cols = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    g = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n, n))
    _, lab = connected_components(g, directed=False)
    return lab[f[:, 0]]


def remove_small_components(mesh: TriMesh, min_area_m2: float = 0.05, min_faces: int = 30) -> TriMesh:
    """작은 떠다니는 조각(노이즈)을 제거한다. 큰 표면은 건드리지 않는다."""
    if mesh.empty:
        return mesh
    lab = face_components(mesh)
    area = mesh.face_areas()
    sums = np.bincount(lab, weights=area)
    cnt = np.bincount(lab)
    good = (sums >= min_area_m2) & (cnt >= min_faces)
    if not good.any():
        return mesh
    return mesh.subset_faces(good[lab])


def decimate_clustering(mesh: TriMesh, cell: float) -> TriMesh:
    """정점 클러스터링 LOD — cell(m) 격자마다 정점 1개(평균). 빠르고 단순하며 토폴로지를 보존하지는 않는다."""
    if mesh.empty:
        return mesh
    q = np.floor(mesh.v / cell).astype(np.int64)
    uniq, inv = np.unique(q, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    cnt = np.bincount(inv, minlength=len(uniq)).astype(np.float64)
    v = np.stack([np.bincount(inv, weights=mesh.v[:, k], minlength=len(uniq)) for k in range(3)], 1) / cnt[:, None]
    col = None
    if mesh.colors is not None:
        col = np.stack([np.bincount(inv, weights=mesh.colors[:, k].astype(float), minlength=len(uniq)) for k in range(3)], 1)
        col = (col / cnt[:, None]).clip(0, 255).astype(np.uint8)
    f = inv[mesh.f]
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    return TriMesh(v, f[ok], col, dict(mesh.extras))
