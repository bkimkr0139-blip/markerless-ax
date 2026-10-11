"""메시 정규화 — 벽·바닥·천장 정점을 추정 평면에 스냅하고(코너는 2~3 평면의 교점), 나머지 표면은 부피 보존 평활(Taubin)한다.

실물 재연 품질(투어에서 벽이 반듯하고 모서리가 깨끗하게 보이는 것)을 위한 처리다. 원본 관측 메시(`observed.glb`)는 그대로 두고,
정규화 결과는 텍스처/LOD 메시에만 쓴다. 평면에서 `tol`(기본 5cm) 넘게 떨어진 정점은 건드리지 않으므로 휘어진 벽·돌출 설비는 보존된다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix

from .mesh import TriMesh
from .structure import Structure


@dataclass
class RefineStats:
    snapped: int
    corners: int
    smoothed: int
    max_move_cm: float
    mean_move_mm: float

    def to_json(self) -> dict:
        return {"snappedVertices": self.snapped, "cornerVertices": self.corners, "smoothedVertices": self.smoothed,
                "maxMoveCm": round(self.max_move_cm, 2), "meanMoveMm": round(self.mean_move_mm, 2)}


def planes_of(st: Structure) -> list[tuple[np.ndarray, float]]:
    """(법선, 오프셋) — 법선은 방 안쪽, n·x = d."""
    out = []
    if st.floor_y is not None:
        out.append((np.array([0.0, 1.0, 0.0]), float(st.floor_y)))
    if st.ceiling_y is not None:
        out.append((np.array([0.0, -1.0, 0.0]), float(-st.ceiling_y)))
    for w in st.walls:
        n = np.array([w.normal[0], 0.0, w.normal[1]])
        out.append((n, float(n @ np.array([w.p0[0], 0.0, w.p0[1]]))))
    return out


def _adjacency(mesh: TriMesh):
    n = len(mesh.v)
    f = mesh.f
    r = np.concatenate([f[:, 0], f[:, 1], f[:, 2], f[:, 1], f[:, 2], f[:, 0]])
    c = np.concatenate([f[:, 1], f[:, 2], f[:, 0], f[:, 0], f[:, 1], f[:, 2]])
    A = coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n)).tocsr()
    A.data[:] = 1.0
    deg = np.asarray(A.sum(1)).ravel()
    return A, deg


def taubin(mesh: TriMesh, free: np.ndarray, iters: int = 3, lam: float = 0.5, mu: float = -0.53) -> np.ndarray:
    """부피 보존 평활. free=움직여도 되는 정점(평면 스냅된 정점은 고정)."""
    A, deg = _adjacency(mesh)
    v = mesh.v.copy()
    inv = 1.0 / np.maximum(deg, 1)
    for _ in range(iters):
        for k in (lam, mu):
            avg = (A @ v) * inv[:, None]
            v[free] += k * (avg[free] - v[free])
    return v


def refit_planes(mesh: TriMesh, st: Structure, band: float = 0.03, iters: int = 3) -> Structure:
    """추정 평면을 메시 정점으로 다시 정밀 피팅한다(벽: 2D 전체최소제곱 직선, 바닥/천장: 면적 가중 평균). 구조 추출의 ±수 mm 편향 제거."""
    import copy

    st2 = copy.deepcopy(st)
    V = mesh.v
    vn = mesh.vertex_normals()
    for _ in range(iters):
        for w in st2.walls:
            n = np.array([w.normal[0], 0.0, w.normal[1]])
            d = float(n @ np.array([w.p0[0], 0.0, w.p0[1]]))
            tang = np.array([-w.normal[1], w.normal[0]])
            t = (V[:, [0, 2]] - np.array(w.p0)) @ tang
            ok = (np.abs(V @ n - d) < band) & (np.abs(vn @ n) > 0.9) & (t > 0.05) & (t < w.length - 0.05) & (V[:, 1] > w.y_min + 0.05) & (V[:, 1] < w.y_max - 0.05)
            if ok.sum() < 200:
                continue
            P = V[ok][:, [0, 2]]
            c = P.mean(0)
            u, s_, vt = np.linalg.svd(P - c, full_matrices=False)
            direction = vt[0] * (1 if vt[0] @ tang > 0 else -1)
            normal = np.array([-direction[1], direction[0]])
            if normal @ np.array(w.normal) < 0:
                normal = -normal
            # 끝점은 같은 접선 범위를 새 직선 위로 투영
            p0 = np.array(w.p0)
            p1 = np.array(w.p1)
            def onto(p):
                return c + direction * float((p - c) @ direction)
            w.p0, w.p1 = tuple(onto(p0)), tuple(onto(p1))
            w.normal = (float(normal[0]), float(normal[1]))
        for name in ("floor_y", "ceiling_y"):
            y0 = getattr(st2, name)
            if y0 is None:
                continue
            ok = (np.abs(V[:, 1] - y0) < band) & (np.abs(vn[:, 1]) > 0.9)
            if ok.sum() > 200:
                setattr(st2, name, float(V[ok, 1].mean()))
    return st2


def regularize(mesh: TriMesh, st: Structure, tol: float = 0.05, align_cos: float = 0.55, lam: float = 0.02, smooth_iters: int = 3):
    """반환 (정규화된 TriMesh, RefineStats)."""
    if mesh.empty:
        return mesh, RefineStats(0, 0, 0, 0.0, 0.0)
    st = refit_planes(mesh, st)
    planes = planes_of(st)
    V = mesh.v
    vn = mesh.vertex_normals()
    n_v = len(V)
    # 정점별 후보 평면: 거리 < tol, 법선 정렬, 그리고 벽이면 벽 범위 안
    cand: list[list[int]] = [[] for _ in range(n_v)]
    for pi, (n, d) in enumerate(planes):
        dist = V @ n - d
        ok = (np.abs(dist) < tol) & (vn @ n > align_cos)
        if pi >= (2 if st.floor_y is not None and st.ceiling_y is not None else (1 if (st.floor_y is not None or st.ceiling_y is not None) else 0)):
            w = st.walls[pi - (int(st.floor_y is not None) + int(st.ceiling_y is not None))]
            tang = np.array([-w.normal[1], w.normal[0]])
            t = (V[:, [0, 2]] - np.array(w.p0)) @ tang
            ok &= (t > -0.08) & (t < w.length + 0.08) & (V[:, 1] > w.y_min - 0.08) & (V[:, 1] < w.y_max + 0.08)
        for i in np.nonzero(ok)[0]:
            cand[i].append(pi)
    newV = V.copy()
    snapped = np.zeros(n_v, bool)
    corners = 0
    for i, ps in enumerate(cand):
        if not ps:
            continue
        # 코너 정점이 서로 거의 평행한 평면(법선 거의 같음)을 만나면 특이 → 가장 가까운 것만
        use = [ps[0]]
        for p in ps[1:]:
            if all(abs(float(planes[p][0] @ planes[q][0])) < 0.95 for q in use):
                use.append(p)
        ns = np.array([planes[p][0] for p in use])
        ds = np.array([planes[p][1] for p in use])
        # min Σ (n·x − d)² + λ|x − x0|²  → (λI + ΣnnT) x = λ x0 + Σ n d
        M = lam * np.eye(3) + ns.T @ ns
        b = lam * V[i] + ns.T @ ds
        newV[i] = np.linalg.solve(M, b)
        snapped[i] = True
        corners += len(use) >= 2
    free = ~snapped
    tmp = TriMesh(newV, mesh.f, mesh.colors, mesh.extras)
    sm = taubin(tmp, free, iters=smooth_iters) if smooth_iters else newV
    move = np.linalg.norm(sm - V, axis=1)
    out = TriMesh(sm, mesh.f.copy(), None if mesh.colors is None else mesh.colors.copy(), dict(mesh.extras))
    return out, RefineStats(int(snapped.sum()), int(corners), int(free.sum()), float(move.max() * 100), float(move.mean() * 1000))
