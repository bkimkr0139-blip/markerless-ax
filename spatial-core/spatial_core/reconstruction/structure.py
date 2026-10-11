"""융합 메시에서 방 구조(바닥·천장·벽·개구부 후보)를 뽑고 면적 기준 커버리지를 계산한다.

가정: 중력축 = +Y, 벽은 수직. 비-맨해튼(비스듬한) 벽도 방향 피크 검출로 처리한다.
직접 보지 못한 부분은 '미관측'으로 두고, 개구부(문·창)는 미관측 구멍에서 추정한 INFERRED 후보일 뿐이다.
RoomPlan JSON이 있으면 `from_roomplan`으로 구조 prior를 대체하되 커버리지는 같은 방식으로 계산한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .mesh import TriMesh

CELL = 0.10  # 커버리지 격자(m)


@dataclass
class Wall:
    id: int
    p0: tuple[float, float]  # XZ
    p1: tuple[float, float]
    y_min: float
    y_max: float
    normal: tuple[float, float]  # XZ, 방 안쪽
    coverage: float = 0.0
    kind: str = "INTERIOR"  # EXTERIOR/INTERIOR는 건물 단계(envelope)에서 확정
    source_kind: str = "OBSERVED"
    confidence: float = 0.7
    cells: np.ndarray | None = field(default=None, repr=False)  # (nt, ny) bool 관측 격자

    @property
    def length(self) -> float:
        return float(np.hypot(self.p1[0] - self.p0[0], self.p1[1] - self.p0[1]))

    @property
    def height(self) -> float:
        return float(self.y_max - self.y_min)

    def to_json(self) -> dict:
        return {"id": self.id, "p0": list(map(float, self.p0)), "p1": list(map(float, self.p1)), "yMin": float(self.y_min),
                "yMax": float(self.y_max), "normal": list(map(float, self.normal)), "lengthM": round(self.length, 3),
                "heightM": round(self.height, 3), "coverage": round(float(self.coverage), 3), "kind": self.kind,
                "sourceKind": self.source_kind, "confidence": round(float(self.confidence), 3)}


@dataclass
class Opening:
    kind: str  # DOOR | WINDOW | HOLE
    wall_id: int
    t0: float
    t1: float
    y0: float
    y1: float
    source_kind: str = "INFERRED"
    review_status: str = "REVIEW_REQUIRED"
    confidence: float = 0.4

    def to_json(self) -> dict:
        return {"kind": self.kind, "wallId": self.wall_id, "t0": round(float(self.t0), 3), "t1": round(float(self.t1), 3),
                "y0": round(float(self.y0), 3), "y1": round(float(self.y1), 3), "widthM": round(float(self.t1 - self.t0), 3),
                "heightM": round(float(self.y1 - self.y0), 3),
                "sourceKind": self.source_kind, "reviewStatus": self.review_status, "confidence": self.confidence}


@dataclass
class Structure:
    floor_y: float | None
    ceiling_y: float | None
    walls: list[Wall]
    openings: list[Opening]
    polygon: list[tuple[float, float]]
    floor_coverage: float = 0.0
    ceiling_coverage: float = 0.0
    overall_coverage: float = 0.0
    unobserved_area_m2: float = 0.0
    floor_cells: np.ndarray | None = field(default=None, repr=False)
    ceiling_cells: np.ndarray | None = field(default=None, repr=False)
    grid_origin: tuple[float, float] = (0.0, 0.0)
    source: str = "MESH"  # MESH | ROOMPLAN
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"floorY": self.floor_y, "ceilingY": self.ceiling_y, "walls": [w.to_json() for w in self.walls],
                "openings": [o.to_json() for o in self.openings], "polygon": [list(map(float, p)) for p in self.polygon],
                "coverage": {"overall": round(self.overall_coverage, 3), "floor": round(self.floor_coverage, 3),
                             "ceiling": round(self.ceiling_coverage, 3),
                             "walls": [{"wallId": w.id, "coverage": round(float(w.coverage), 3)} for w in self.walls],
                             "unobservedAreaM2": round(self.unobserved_area_m2, 2)},
                "source": self.source, "notes": self.notes}


def _smooth(h: np.ndarray, k: int) -> np.ndarray:
    """길이를 유지하는 상자 평활 (np.convolve same은 짧은 배열에서 길이가 늘어난다)."""
    return ndimage.uniform_filter1d(h.astype(float), size=k, mode="constant") * k


def _peak(hist: np.ndarray, min_val: float) -> list[int]:
    out = []
    for i in range(len(hist)):
        l = hist[i - 1] if i > 0 else -1
        r = hist[i + 1] if i + 1 < len(hist) else -1
        if hist[i] >= min_val and hist[i] >= l and hist[i] > r:
            out.append(i)
    return out


def _horizontal_planes(mesh: TriMesh, up: bool, bin_m: float = 0.02):
    n = mesh.face_normals()
    sel = (n[:, 1] > 0.9) if up else (n[:, 1] < -0.9)
    if not sel.any():
        return None
    y = mesh.face_centers()[sel, 1]
    a = mesh.face_areas()[sel]
    lo, hi = y.min(), y.max()
    bins = np.arange(lo - bin_m, hi + 2 * bin_m, bin_m)
    h, e = np.histogram(y, bins=bins, weights=a)
    h = _smooth(h, 3)
    return h, e


def _find_floor_ceiling(mesh: TriMesh):
    floor = ceiling = None
    up = _horizontal_planes(mesh, True)
    if up is not None:
        h, e = up
        thr = max(0.8, 0.25 * h.max())
        pk = _peak(h, thr)
        if pk:
            i = min(pk)  # 가장 낮은 유의 평면 = 바닥 (테이블 상판보다 아래)
            floor = float(0.5 * (e[i] + e[i + 1]))
    dn = _horizontal_planes(mesh, False)
    if dn is not None:
        h, e = dn
        thr = max(0.8, 0.25 * h.max())
        pk = _peak(h, thr)
        if pk:
            i = max(pk)
            c = float(0.5 * (e[i] + e[i + 1]))
            if floor is None or c - floor > 1.8:
                ceiling = c
    return floor, ceiling


def _wall_orientations(theta: np.ndarray, area: np.ndarray, min_frac: float = 0.08):
    """수평 법선각 분포에서 지배 방향들 (rad). 맨해튼이면 φ+k·90° 4개를 돌려준다."""
    nb = 360
    h, _ = np.histogram(theta % (2 * np.pi), bins=nb, range=(0, 2 * np.pi), weights=area)
    hs = np.convolve(np.r_[h[-2:], h, h[:2]], np.ones(5), mode="valid")  # 순환 평활
    tot = hs.sum() / 5 if hs.sum() > 0 else 1
    # 4방향 접힘으로 맨해튼 판정
    fold = hs.reshape(4, 90).sum(0)
    phi_bin = int(np.argmax(fold))
    near = 0.0
    for k in range(4):
        for d in range(-8, 9):
            near += h[(phi_bin + 90 * k + d) % nb]
    if near / max(h.sum(), 1e-9) > 0.6:
        ang = (theta % (np.pi / 2))
        ref = (phi_bin + 0.5) * np.pi / 180.0
        dd = ((ang - ref + np.pi / 4) % (np.pi / 2)) - np.pi / 4
        m = np.abs(dd) < np.radians(8)
        phi = ref + float(np.average(dd[m], weights=area[m])) if m.any() else ref
        return [(phi + k * np.pi / 2) % (2 * np.pi) for k in range(4)]
    peaks = []
    for i in range(nb):
        if hs[i] >= min_frac * tot * 5 and hs[i] >= hs[i - 1] and hs[i] > hs[(i + 1) % nb]:
            peaks.append((i + 0.5) * np.pi / 180.0)
    return peaks


def extract_structure(mesh: TriMesh, observed_pts: np.ndarray | None = None) -> Structure:
    """메시로부터 구조 추출. observed_pts(TSDF 표면 복셀)가 있으면 커버리지를 그것으로 더 정확히 계산."""
    notes: list[str] = []
    if mesh.empty:
        return Structure(None, None, [], [], [], notes=["메시가 비어 있음"])
    floor, ceiling = _find_floor_ceiling(mesh)
    if floor is None:
        floor = float(np.percentile(mesh.v[:, 1], 1))
        notes.append("바닥 평면을 찾지 못해 하단 1%를 바닥으로 가정")
    if ceiling is None:
        notes.append("천장이 충분히 관측되지 않음 — 벽 높이는 관측 상단 기준")
    n = mesh.face_normals()
    ctr = mesh.face_centers()
    area = mesh.face_areas()
    top = ceiling if ceiling is not None else float(np.percentile(mesh.v[:, 1], 99))
    vert = (np.abs(n[:, 1]) < 0.3) & (ctr[:, 1] > floor + 0.1) & (ctr[:, 1] < top - 0.1)
    walls: list[Wall] = []
    if vert.any():
        th = np.arctan2(n[vert, 2], n[vert, 0])
        ar = area[vert]
        c = ctr[vert]
        oris = _wall_orientations(th, ar)
        wid = 0
        for ori in oris:
            nv = np.array([np.cos(ori), np.sin(ori)])
            dang = np.abs(((th - ori + np.pi) % (2 * np.pi)) - np.pi)
            sel = dang < np.radians(12)
            if sel.sum() < 20:
                continue
            d = c[sel][:, [0, 2]] @ nv
            aa = ar[sel]
            lo, hi = d.min(), d.max()
            bins = np.arange(lo - 0.02, hi + 0.04, 0.02)
            h, e = np.histogram(d, bins=bins, weights=aa)
            hs = _smooth(h, 5)
            for pk in _peak(hs, max(0.6, 0.08 * hs.sum() if len(hs) else 0.6)):
                off0 = 0.5 * (e[pk] + e[pk + 1])
                near = np.abs(d - off0) < 0.05
                # 평면 위치는 근방 면들의 면적 가중 중앙값으로 정밀화 (빈 중심만 쓰면 최대 1cm 치우친다)
                o_ = np.argsort(d[near])
                cw = np.cumsum(aa[near][o_])
                off = float(d[near][o_][np.searchsorted(cw, cw[-1] / 2)]) if near.any() else off0
                inl = np.abs(d - off) < 0.07
                if inl.sum() < 10:
                    continue
                tang = np.array([-nv[1], nv[0]])
                t = c[sel][inl][:, [0, 2]] @ tang
                yy = c[sel][inl][:, 1]
                order = np.argsort(t)
                ts, ys = t[order], yy[order]
                cut = np.nonzero(np.diff(ts) > 0.45)[0]
                for s0, s1 in zip(np.r_[0, cut + 1], np.r_[cut + 1, len(ts)]):
                    if s1 - s0 < 8:
                        continue
                    seg_t = ts[s0:s1]
                    seg_y = ys[s0:s1]
                    L = seg_t[-1] - seg_t[0]
                    y0 = float(np.percentile(seg_y, 1))
                    y1 = float(np.percentile(seg_y, 99))
                    if L < 0.8:
                        continue
                    full_h = (top - floor)
                    # 가구 옆면 같은 낮은 수직면은 벽에서 제외 (바닥에서 시작해 높이의 상당 부분을 덮어야 벽)
                    if (y1 - y0) < 0.5 * full_h or y0 > floor + 0.7:
                        continue
                    p0 = off * nv + seg_t[0] * tang
                    p1 = off * nv + seg_t[-1] * tang
                    wid += 1
                    walls.append(Wall(wid, (float(p0[0]), float(p0[1])), (float(p1[0]), float(p1[1])), floor, top,
                                      (float(nv[0]), float(nv[1]))))
    # 정렬: 방 중심 기준 각도순 → 다각형
    poly: list[tuple[float, float]] = []
    if len(walls) >= 3:
        mid = np.array([[(w.p0[0] + w.p1[0]) / 2, (w.p0[1] + w.p1[1]) / 2] for w in walls])
        cen = mid.mean(0)
        order = np.argsort(np.arctan2(mid[:, 1] - cen[1], mid[:, 0] - cen[0]))
        ws = [walls[i] for i in order]
        for i in range(len(ws)):
            a, b = ws[i], ws[(i + 1) % len(ws)]
            pt = _intersect(a, b)
            if pt is not None:
                poly.append(pt)
    elif walls:
        notes.append("벽이 3개 미만이라 방 외곽(다각형)을 닫을 수 없음 — 추가 촬영 필요")
    st = Structure(floor, ceiling, walls, [], poly, notes=notes)
    _coverage(st, mesh, observed_pts)
    return st


def _intersect(a: Wall, b: Wall):
    d1 = np.array(a.p1) - np.array(a.p0)
    d2 = np.array(b.p1) - np.array(b.p0)
    M = np.array([d1, -d2]).T
    if abs(np.linalg.det(M)) < 1e-6:
        return None
    s = np.linalg.solve(M, np.array(b.p0) - np.array(a.p0))
    p = np.array(a.p0) + d1 * s[0]
    return float(p[0]), float(p[1])


def _coverage(st: Structure, mesh: TriMesh, obs: np.ndarray | None) -> None:
    """면적 기준 커버리지. 관측 = 메시 표면(또는 관측 복셀)이 해당 평면 ±8cm 안에 있는 격자 칸."""
    pts = obs if obs is not None and len(obs) else mesh.face_centers()
    wts = None
    if obs is None or not len(obs):
        wts = mesh.face_areas()
    total_area = 0.0
    seen_area = 0.0
    for w in st.walls:
        nv = np.array(w.normal)
        tang = np.array([-nv[1], nv[0]])
        base = np.array(w.p0)
        L = w.length
        t_of = lambda xz: (xz - base) @ tang  # noqa: E731
        nt = max(1, int(np.ceil(L / CELL)))
        ny = max(1, int(np.ceil((w.y_max - w.y_min) / CELL)))
        grid = np.zeros((nt, ny), bool)
        dist = np.abs((pts[:, [0, 2]] - base) @ nv)
        m = dist < 0.08
        if m.any():
            tt = t_of(pts[m][:, [0, 2]])
            ii = np.floor(tt / CELL).astype(int)
            jj = np.floor((pts[m][:, 1] - w.y_min) / CELL).astype(int)
            ok = (ii >= 0) & (ii < nt) & (jj >= 0) & (jj < ny)
            grid[ii[ok], jj[ok]] = True
            if wts is None:  # 복셀 점은 밀도가 있어 1칸 팽창해 구멍 메움
                grid = ndimage.binary_closing(grid, iterations=1)
        w.cells = grid
        w.coverage = float(grid.mean())
        a = L * (w.y_max - w.y_min)
        total_area += a
        seen_area += a * w.coverage
        _find_openings(st, w, grid)
    # 바닥·천장: 방 다각형 바운딩 격자
    if st.polygon:
        P = np.array(st.polygon)
        lo, hi = P.min(0), P.max(0)
        st.grid_origin = (float(lo[0]), float(lo[1]))
        nx = max(1, int(np.ceil((hi[0] - lo[0]) / CELL)))
        nz = max(1, int(np.ceil((hi[1] - lo[1]) / CELL)))
        gi, gj = np.meshgrid(np.arange(nx), np.arange(nz), indexing="ij")
        cx = lo[0] + (gi + 0.5) * CELL
        cz = lo[1] + (gj + 0.5) * CELL
        inside = _in_poly(np.stack([cx, cz], -1).reshape(-1, 2), P).reshape(nx, nz)
        for name, yv in (("floor", st.floor_y), ("ceiling", st.ceiling_y)):
            if yv is None:
                setattr(st, f"{name}_cells", np.zeros((nx, nz), bool))
                setattr(st, f"{name}_coverage", 0.0)
                if inside.any():
                    total_area += inside.sum() * CELL * CELL
                continue
            g = np.zeros((nx, nz), bool)
            m = np.abs(pts[:, 1] - yv) < 0.08
            if m.any():
                ii = np.floor((pts[m][:, 0] - lo[0]) / CELL).astype(int)
                jj = np.floor((pts[m][:, 2] - lo[1]) / CELL).astype(int)
                ok = (ii >= 0) & (ii < nx) & (jj >= 0) & (jj < nz)
                g[ii[ok], jj[ok]] = True
                if wts is None:
                    g = ndimage.binary_closing(g, iterations=1)
            g &= inside
            setattr(st, f"{name}_cells", g)
            cov = float(g.sum() / max(inside.sum(), 1))
            setattr(st, f"{name}_coverage", cov)
            a = inside.sum() * CELL * CELL
            total_area += a
            seen_area += a * cov
    st.overall_coverage = seen_area / total_area if total_area > 0 else 0.0
    st.unobserved_area_m2 = total_area - seen_area


def _in_poly(p: np.ndarray, poly: np.ndarray) -> np.ndarray:
    x, y = p[:, 0], p[:, 1]
    inside = np.zeros(len(p), bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = poly[i, 0], poly[i, 1], poly[j, 0], poly[j, 1]
        cross = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi)
        inside ^= cross
        j = i
    return inside


def _find_openings(st: Structure, w: Wall, grid: np.ndarray) -> None:
    """벽 격자의 미관측 구멍 중 문/창 크기의 직사각형을 INFERRED 후보로 올린다 (형태학적 열기로 직사각 덩어리만 추출)."""
    un = ~grid
    nt, ny = grid.shape
    used = np.zeros_like(un)

    def blobs(mask, se_w, se_h):
        op = ndimage.binary_opening(mask, structure=np.ones((se_w, se_h), bool))
        lab, k = ndimage.label(op)
        for i in range(1, k + 1):
            xs, ys = np.nonzero(lab == i)
            yield xs.min(), xs.max() + 1, ys.min(), ys.max() + 1

    for x0, x1, j0, j1 in blobs(un, 6, 16):  # 문: 폭 ≥0.6m, 높이 ≥1.6m, 바닥에 닿음
        if j0 <= 1:
            wd = (x1 - x0) * CELL
            if wd <= 2.4:
                st.openings.append(Opening("DOOR", w.id, x0 * CELL, x1 * CELL, w.y_min, w.y_min + j1 * CELL, confidence=0.35))
                used[x0:x1, j0:j1] = True
    rest = un & ~used
    for x0, x1, j0, j1 in blobs(rest, 4, 4):  # 창: 폭·높이 ≥0.4m, 바닥에서 떨어짐
        if j0 >= 3 and j1 < ny:
            st.openings.append(Opening("WINDOW", w.id, x0 * CELL, x1 * CELL, w.y_min + j0 * CELL, w.y_min + j1 * CELL, confidence=0.3))
            used[x0:x1, j0:j1] = True


def from_roomplan(js: dict, mesh: TriMesh | None = None, observed_pts: np.ndarray | None = None) -> Structure:
    """RoomPlan에서 변환한 `space-structure.json` → Structure (wall transform16 열 우선, widthM/heightM/thicknessM)."""
    from .camera import mat16

    walls: list[Wall] = []
    ys = []
    for i, w in enumerate(js.get("walls", []), 1):
        T = mat16(w["transform16"])
        x_axis = T[:3, 0]
        z_axis = T[:3, 2]
        c = T[:3, 3]
        half = w["widthM"] / 2
        p0 = c - x_axis * half
        p1 = c + x_axis * half
        nrm = np.array([z_axis[0], z_axis[2]])
        nrm /= max(np.linalg.norm(nrm), 1e-9)
        ymin, ymax = c[1] - w["heightM"] / 2, c[1] + w["heightM"] / 2
        ys += [ymin, ymax]
        wl = Wall(int(w.get("id", i)) if str(w.get("id", i)).isdigit() else i, (float(p0[0]), float(p0[2])), (float(p1[0]), float(p1[2])),
                  float(ymin), float(ymax), (float(nrm[0]), float(nrm[1])), source_kind="OBSERVED", confidence=0.6)
        walls.append(wl)
    floor = js.get("floorY", min(ys) if ys else None)
    ceiling = js.get("ceilingY", max(ys) if ys else None)
    poly: list[tuple[float, float]] = []
    if len(walls) >= 3:
        mid = np.array([[(w.p0[0] + w.p1[0]) / 2, (w.p0[1] + w.p1[1]) / 2] for w in walls])
        cen = mid.mean(0)
        ws = [walls[i] for i in np.argsort(np.arctan2(mid[:, 1] - cen[1], mid[:, 0] - cen[0]))]
        for i in range(len(ws)):
            pt = _intersect(ws[i], ws[(i + 1) % len(ws)])
            if pt:
                poly.append(pt)
    st = Structure(floor, ceiling, walls, [], poly, source="ROOMPLAN", notes=["RoomPlan 구조 사용"])
    for kind, key in (("DOOR", "doors"), ("WINDOW", "windows"), ("HOLE", "openings")):
        for o in js.get(key, []):
            T = mat16(o["transform16"])
            c = T[:3, 3]
            # 가장 가까운 벽에 부착
            best, bd = None, 1e9
            for w in walls:
                nv = np.array(w.normal)
                dd = abs((np.array([c[0], c[2]]) - np.array(w.p0)) @ nv)
                if dd < bd:
                    best, bd = w, dd
            if best is None or bd > 0.4:
                continue
            tang = np.array([-best.normal[1], best.normal[0]])
            tc = (np.array([c[0], c[2]]) - np.array(best.p0)) @ tang
            st.openings.append(Opening(kind, best.id, tc - o["widthM"] / 2, tc + o["widthM"] / 2, c[1] - o["heightM"] / 2,
                                       c[1] + o["heightM"] / 2, source_kind="OBSERVED", review_status="AUTO", confidence=0.6))
    if mesh is not None and not mesh.empty:
        _coverage(st, mesh, observed_pts)
    return st


# ───────────── RoomPlan 구조(다른 세션 프레임)를 RGB-D 메시 프레임에 정렬 ─────────────

def _ry(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def align_prior_to_mesh(prior: dict, ref: Structure, max_rms: float = 0.12):
    """RoomPlan 벽(자기 세션 프레임)을 메시에서 추출한 벽(ref)에 맞추는 yaw+이동 4자유도 정렬.

    반환 (aligned_json | None, info). 정렬이 불확실(평행 벽뿐·잔차 큼·후보 모호)하면 None — 잘못된 구조를 쓰지 않는다.
    """
    from .camera import mat16, to_mat16

    if len(ref.walls) < 2 or len(prior.get("walls", [])) < 2:
        return None, {"reason": "벽이 부족해 정렬할 수 없음"}
    PW = []
    for w in prior["walls"]:
        T = mat16(w["transform16"])
        n = np.array([T[0, 2], T[2, 2]])
        n /= max(np.linalg.norm(n), 1e-9)
        PW.append((np.array([T[0, 3], T[2, 3]]), n, float(w["widthM"])))
    MW = []
    for w in ref.walls:
        MW.append((0.5 * (np.array(w.p0) + np.array(w.p1)), np.array(w.normal), w.length))
    mcen = np.mean([m[0] for m in MW], axis=0)
    pcen = np.mean([p[0] for p in PW], axis=0)
    cands = []
    for deg in np.arange(0, 360, 1.0):
        th = np.radians(deg)
        R2 = np.array([[np.cos(th), np.sin(th)], [-np.sin(th), np.cos(th)]])  # _ry(θ)의 XZ 부분
        cost = 0.0
        for m, n, L in PW:
            nr = R2 @ n
            ang = min(np.degrees(np.arccos(np.clip(abs(float(nr @ mn[1])), 0, 1))) for mn in MW)
            cost += L * ang * ang
        cands.append((cost / sum(p[2] for p in PW), deg))
    cands.sort()
    picks = []
    for c, d in cands:
        if all(min(abs(d - e), 360 - abs(d - e)) > 15 for _, e in picks):
            picks.append((c, d))
        if len(picks) >= 4:
            break
    best = None
    results = []
    for _, deg in picks:
        th = np.radians(deg)
        R2 = np.array([[np.cos(th), np.sin(th)], [-np.sin(th), np.cos(th)]])
        t = mcen - R2 @ pcen
        for _ in range(8):
            A = np.zeros((2, 2))
            b = np.zeros(2)
            resid = []
            wsum = 0.0
            for m, n, L in PW:
                nr = R2 @ n
                mp = R2 @ m + t
                bestj, bd = None, 1e9
                for mm, nn, _ in MW:
                    if abs(float(nr @ nn)) < np.cos(np.radians(12)):
                        continue
                    d = abs(float(nn @ (mp - mm)))
                    if d < bd:
                        bestj, bd = (mm, nn), d
                if bestj is None or bd > 1.0:
                    continue
                mm, nn = bestj
                A += L * np.outer(nn, nn)
                b += L * nn * float(nn @ (mm - R2 @ m))
                resid.append((L, bd))
                wsum += L
            if wsum == 0 or np.linalg.det(A) < 0.05 * wsum * wsum:
                resid = None
                break
            t = np.linalg.solve(A, b)
        if not resid:
            continue
        # 최종 잔차
        tot = 0.0
        ws = 0.0
        for m, n, L in PW:
            nr = R2 @ n
            mp = R2 @ m + t
            ds = [abs(float(nn @ (mp - mm))) for mm, nn, _ in MW if abs(float(nr @ nn)) >= np.cos(np.radians(12))]
            if ds:
                tot += L * min(ds) ** 2
                ws += L
        rms = float(np.sqrt(tot / ws)) if ws else 9.0
        results.append((rms, deg, R2, t, ws))
    if not results:
        return None, {"reason": "평행하지 않은 벽 대응을 찾지 못함"}
    results.sort(key=lambda x: x[0])
    rms, deg, R2, t, ws = results[0]
    if rms > max_rms:
        return None, {"reason": f"정렬 잔차 {rms * 100:.0f}cm > {max_rms * 100:.0f}cm", "rms": rms}
    tied = [r for r in results if r[0] <= 1.5 * rms + 0.01]
    if len(tied) > 1:
        # 벽 배치만으로는 모호(직사각형의 180° 대칭 등) — 문·창 위치로 판별한다. 근거가 없으면 정렬하지 않는다.
        feats = []
        for key in ("doors", "windows"):
            for o in prior.get(key, []):
                To = mat16(o["transform16"])
                feats.append((np.array([To[0, 3], To[2, 3]]), float(To[1, 3])))
        mopen = []
        for o in ref.openings:
            w = next((x for x in ref.walls if x.id == o.wall_id), None)
            if w is None:
                continue
            L = max(w.length, 1e-9)
            tang = (np.array(w.p1) - np.array(w.p0)) / L
            mopen.append((np.array(w.p0) + tang * 0.5 * (o.t0 + o.t1), 0.5 * (o.y0 + o.y1)))
        if feats and mopen:
            scored = []
            for cand in tied:
                _r, _d, R2c, tc, _w = cand
                ds = [min(float(np.linalg.norm(R2c @ f[0] + tc - m[0])) for m in mopen) for f in feats]
                scored.append((float(np.mean(ds)), cand))
            scored.sort(key=lambda x: x[0])
            if len(scored) == 1 or scored[1][0] - scored[0][0] >= 0.3:
                rms, deg, R2, t, ws = scored[0][1]
            else:
                return None, {"reason": f"정렬 후보가 모호함({deg:.0f}° vs {tied[1][1]:.0f}°) — 문·창으로도 구분 안 됨", "rms": rms, "ambiguous": True}
        else:
            return None, {"reason": f"정렬 후보가 모호함({deg:.0f}° vs {tied[1][1]:.0f}°) — 대칭 방이고 문·창 정보 없음", "rms": rms, "ambiguous": True}
    dy = (ref.floor_y - prior["floorY"]) if (ref.floor_y is not None and prior.get("floorY") is not None) else 0.0
    G = np.eye(4)
    G[:3, :3] = _ry(np.radians(deg))
    G[0, 3], G[2, 3], G[1, 3] = t[0], t[1], dy
    out = dict(prior)
    for key in ("walls", "doors", "windows", "openings"):
        out[key] = [dict(w, transform16=to_mat16(G @ mat16(w["transform16"]))) for w in prior.get(key, [])]
    if prior.get("floorY") is not None:
        out["floorY"] = prior["floorY"] + dy
    if prior.get("ceilingY") is not None:
        out["ceilingY"] = prior["ceilingY"] + dy
    return out, {"yawDeg": float(deg), "translation": [float(t[0]), float(dy), float(t[1])], "rms": rms}
