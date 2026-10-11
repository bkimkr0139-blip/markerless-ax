"""건물 외피(추정) 생성 — 실내에서 본 벽면에서 외벽/내벽을 구분하고 두께를 부여한다.

중요: 실내 한쪽 면만으로는 실제 벽 두께·외관을 알 수 없다. 두께 출처 우선순위는 BIM > MEASURED > CONFIG_DEFAULT > INFERRED 이고
각 벽에 method/confidence/reviewStatus를 남긴다. 결과 지오메트리는 모두 sourceKind=INFERRED, reviewStatus=REVIEW_REQUIRED.
- 외벽 판정: 방 안쪽 평면에서 바깥쪽으로 30cm 나간 점이 다른 방 안에 들어가지 않으면 외벽.
- 내벽 두께: 두 방이 같은 건물 좌표에 있고 평행한 벽 평면이 마주보면 그 간격(5~60cm)을 MEASURED(낮은 신뢰)로 쓴다.
- 개구부(문·창)는 벽을 조각 박스로 나눠 비운다(불리언 연산 없이). 외벽의 DOOR 개구부는 출입구 후보가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .mesh import TriMesh
from .glb_export import box_mesh
from .structure import Structure, _in_poly

DEFAULT_THICKNESS = {"concrete": 0.20, "masonry": 0.30, "light": 0.12, "default": 0.20}
THICKNESS_PRIORITY = {"BIM": 4, "MEASURED": 3, "CONFIG_DEFAULT": 2, "INFERRED": 1}


@dataclass
class SpaceInput:
    key: str  # spaceId 코드
    T_building_space: np.ndarray
    structure: Structure
    floor_elevation: float = 0.0  # 건물 좌표 Y (T에 이미 포함되면 0)


@dataclass
class WallRecord:
    space_key: str
    wall_id: int
    kind: str  # EXTERIOR | INTERIOR
    p0: tuple[float, float]  # 건물 XZ
    p1: tuple[float, float]
    normal: tuple[float, float]  # 건물 XZ, 방 안쪽
    y0: float
    y1: float
    thickness: float
    method: str
    confidence: float
    note: str = ""
    neighbor: str | None = None


@dataclass
class EntranceRecord:
    id: str
    space_key: str
    wall_id: int
    position: tuple[float, float, float]
    yaw_deg: float
    exterior_viewpoint: tuple[float, float, float]
    width: float
    height: float


@dataclass
class EnvelopeResult:
    mesh: TriMesh
    walls: list[WallRecord]
    entrances: list[EntranceRecord]
    notices: list[str] = field(default_factory=list)


def _to_building(T: np.ndarray, xz: tuple[float, float], y: float) -> np.ndarray:
    p = T @ np.array([xz[0], y, xz[1], 1.0])
    return p[:3]


def _dir_to_building(T: np.ndarray, d: tuple[float, float]) -> np.ndarray:
    v = T[:3, :3] @ np.array([d[0], 0.0, d[1]])
    n = np.hypot(v[0], v[2])
    return np.array([v[0] / max(n, 1e-9), v[2] / max(n, 1e-9)])


def _ear_clip(poly: np.ndarray) -> list[tuple[int, int, int]]:
    """단순 다각형 삼각분할(귀 자르기). 반시계 정렬 가정 없이 면적 부호로 처리."""
    n = len(poly)
    idx = list(range(n))
    area = 0.5 * sum(poly[i, 0] * poly[(i + 1) % n, 1] - poly[(i + 1) % n, 0] * poly[i, 1] for i in range(n))
    if area < 0:
        idx.reverse()
    tris = []
    guard = 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        ear = False
        for k in range(len(idx)):
            a, b, c = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            pa, pb, pc = poly[a], poly[b], poly[c]
            cross = (pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0])
            if cross <= 1e-12:
                continue
            ok = True
            for j in idx:
                if j in (a, b, c):
                    continue
                p = poly[j]
                d1 = (pb[0] - pa[0]) * (p[1] - pa[1]) - (pb[1] - pa[1]) * (p[0] - pa[0])
                d2 = (pc[0] - pb[0]) * (p[1] - pb[1]) - (pc[1] - pb[1]) * (p[0] - pb[0])
                d3 = (pa[0] - pc[0]) * (p[1] - pc[1]) - (pa[1] - pc[1]) * (p[0] - pc[0])
                if d1 >= -1e-12 and d2 >= -1e-12 and d3 >= -1e-12:
                    ok = False
                    break
            if ok:
                tris.append((a, b, c))
                idx.pop(k)
                ear = True
                break
        if not ear:
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


def slab_mesh(poly: np.ndarray, y0: float, y1: float, color) -> TriMesh:
    """다각형 슬래브(두께 y1−y0) 메시."""
    n = len(poly)
    v = np.vstack([np.c_[poly[:, 0], np.full(n, y0), poly[:, 1]], np.c_[poly[:, 0], np.full(n, y1), poly[:, 1]]])
    f = []
    for a, b, c in _ear_clip(poly):
        f.append((n + a, n + b, n + c))  # 윗면
        f.append((a, c, b))  # 아랫면
    for i in range(n):
        j = (i + 1) % n
        f += [(i, j, n + j), (i, n + j, n + i)]
    return TriMesh(v, np.array(f), np.tile(np.asarray(color, np.uint8), (len(v), 1)))


def _wall_boxes(p0, p1, normal, y0, y1, thick, openings: list[tuple[float, float, float, float]], color) -> list[TriMesh]:
    """벽(안쪽 평면 p0→p1, 바깥쪽으로 thick)을 개구부(t0,t1,y0,y1)를 비운 박스 조각들로."""
    p0, p1 = np.array(p0, float), np.array(p1, float)
    L = np.linalg.norm(p1 - p0)
    if L < 1e-3:
        return []
    tang = (p1 - p0) / L
    nrm = np.array(normal, float)
    out = -nrm  # 바깥 방향
    bps = sorted({0.0, L, *[min(max(o[0], 0), L) for o in openings], *[min(max(o[1], 0), L) for o in openings]})
    pieces = []
    for a, b in zip(bps[:-1], bps[1:]):
        if b - a < 0.01:
            continue
        mid = 0.5 * (a + b)
        cov = sorted([(max(o[2], y0), min(o[3], y1)) for o in openings if o[0] <= mid <= o[1]])
        cur = y0
        segs = []
        for c0, c1 in cov:
            if c0 > cur + 0.01:
                segs.append((cur, c0))
            cur = max(cur, c1)
        if y1 > cur + 0.01:
            segs.append((cur, y1))
        for s0, s1 in segs:
            cxz = p0 + tang * mid + out * (thick / 2)
            c = np.array([cxz[0], 0.5 * (s0 + s1), cxz[1]])
            # 박스를 tang/out 축에 맞춰 회전: 로컬 축 박스를 만들고 회전
            bx = box_mesh([0, 0, 0], [b - a, s1 - s0, thick])
            R = np.array([[tang[0], 0, out[0]], [0, 1, 0], [tang[1], 0, out[1]]])
            v = bx.v @ R.T + c
            m = TriMesh(v, bx.f.copy(), np.tile(np.asarray(color, np.uint8), (len(v), 1)))
            pieces.append(m)
    return pieces


def _merge(meshes: list[TriMesh]) -> TriMesh:
    if not meshes:
        return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64))
    off = 0
    V, F, C = [], [], []
    for m in meshes:
        V.append(m.v)
        F.append(m.f + off)
        C.append(m.colors if m.colors is not None else np.full((len(m.v), 3), 200, np.uint8))
        off += len(m.v)
    return TriMesh(np.vstack(V), np.vstack(F), np.vstack(C))


def generate_envelope(spaces: list[SpaceInput], building_type: str = "default", default_thickness: float | None = None,
                      interior_default: float = 0.10, overrides: dict | None = None, slab_thickness: float = 0.20) -> EnvelopeResult:
    """overrides: {(spaceKey, wallId): (thicknessM, method, note)} — 관리자가 정한 값은 자동값보다 우선(BIM>MEASURED>…)."""
    overrides = overrides or {}
    ext_t = default_thickness or DEFAULT_THICKNESS.get(building_type, DEFAULT_THICKNESS["default"])
    polys: dict[str, np.ndarray] = {}
    for sp in spaces:
        st = sp.structure
        if len(st.polygon) >= 3:
            polys[sp.key] = np.array([_to_building(sp.T_building_space, p, st.floor_y or 0.0)[[0, 2]] for p in st.polygon])
    walls: list[WallRecord] = []
    meshes: list[TriMesh] = []
    entrances: list[EntranceRecord] = []
    notices = ["외관·벽 두께 일부 추정 — 실제 외벽 두께/재질/외관은 BIM 또는 실측 확인 필요"]
    col_ext, col_int = (176, 168, 150), (200, 196, 186)
    # 모든 방의 벽을 건물 좌표로
    recs: list[dict] = []
    for sp in spaces:
        st = sp.structure
        T = sp.T_building_space
        fy = st.floor_y if st.floor_y is not None else 0.0
        cy = st.ceiling_y if st.ceiling_y is not None else fy + 2.7
        for w in st.walls:
            b0 = _to_building(T, w.p0, fy)
            b1 = _to_building(T, w.p1, fy)
            nrm = _dir_to_building(T, w.normal)
            recs.append(dict(sp=sp, w=w, p0=b0[[0, 2]], p1=b1[[0, 2]], nrm=nrm, y0=float(b0[1]), y1=float(b0[1] + (cy - fy))))
    for r in recs:
        sp, w = r["sp"], r["w"]
        mid = 0.5 * (r["p0"] + r["p1"])
        probe = mid - r["nrm"] * 0.3
        neighbor = None
        for key, poly in polys.items():
            if key != sp.key and _in_poly(probe[None, :], poly)[0]:
                neighbor = key
                break
        kind = "INTERIOR" if neighbor else "EXTERIOR"
        t, method, conf, note = (ext_t, "CONFIG_DEFAULT", 0.3, f"기본값({building_type})") if kind == "EXTERIOR" else (interior_default, "CONFIG_DEFAULT", 0.3, "기본값")
        if kind == "INTERIOR":  # 마주보는 벽 평면 간격으로 두께 측정
            best = None
            tang = (r["p1"] - r["p0"]) / max(np.linalg.norm(r["p1"] - r["p0"]), 1e-9)
            for o in recs:
                if o["sp"].key != neighbor:
                    continue
                if np.dot(o["nrm"], r["nrm"]) > -0.95:
                    continue
                gap = float(np.dot(o["p0"] - r["p0"], -r["nrm"]))  # 바깥쪽(−nrm) 방향 거리
                if not (0.03 <= gap <= 0.6):
                    continue
                # 접선 방향 겹침
                a0, a1 = sorted([float(np.dot(r["p0"] - r["p0"], tang)), float(np.dot(r["p1"] - r["p0"], tang))])
                b0_, b1_ = sorted([float(np.dot(o["p0"] - r["p0"], tang)), float(np.dot(o["p1"] - r["p0"], tang))])
                if min(a1, b1_) - max(a0, b0_) < 0.5:
                    continue
                if best is None or gap < best:
                    best = gap
            if best is not None:
                t, method, conf, note = best, "MEASURED", 0.5, f"마주보는 벽 간격 {best * 100:.0f}cm"
        ov = overrides.get((sp.key, w.id))
        if ov and THICKNESS_PRIORITY.get(ov[1], 0) >= THICKNESS_PRIORITY[method]:
            t, method, conf, note = float(ov[0]), ov[1], 0.9 if ov[1] in ("BIM", "MEASURED") else 0.5, ov[2] if len(ov) > 2 else "관리자 지정"
        walls.append(WallRecord(sp.key, w.id, kind, tuple(r["p0"]), tuple(r["p1"]), tuple(r["nrm"]), r["y0"], r["y1"], float(t), method, conf, note, neighbor))
        if kind == "EXTERIOR":
            ops = [(o.t0, o.t1, r["y0"] + (o.y0 - (sp.structure.floor_y or 0.0)), r["y0"] + (o.y1 - (sp.structure.floor_y or 0.0)))
                   for o in sp.structure.openings if o.wall_id == w.id]
            meshes += _wall_boxes(r["p0"], r["p1"], r["nrm"], r["y0"], r["y1"], t, ops, col_ext)
            tang = (r["p1"] - r["p0"]) / max(np.linalg.norm(r["p1"] - r["p0"]), 1e-9)
            for o in sp.structure.openings:
                if o.wall_id == w.id and o.kind == "DOOR":
                    c = r["p0"] + tang * 0.5 * (o.t0 + o.t1)
                    out = -r["nrm"]
                    yaw = float(np.degrees(np.arctan2(out[0], out[1])))
                    y = r["y0"]
                    entrances.append(EntranceRecord(f"E{len(entrances) + 1}", sp.key, w.id, (float(c[0]), float(y), float(c[1])), yaw,
                                                    (float(c[0] + out[0] * 6), float(y + 1.6), float(c[1] + out[1] * 6)),
                                                    float(o.t1 - o.t0), float(o.y1 - o.y0)))
        else:
            ops = [(o.t0, o.t1, r["y0"] + (o.y0 - (sp.structure.floor_y or 0.0)), r["y0"] + (o.y1 - (sp.structure.floor_y or 0.0)))
                   for o in sp.structure.openings if o.wall_id == w.id]
            if neighbor and sp.key < neighbor:  # 칸막이는 한 번만 그린다
                meshes += _wall_boxes(r["p0"], r["p1"], r["nrm"], r["y0"], r["y1"], t, ops, col_int)
    # 슬래브: 각 방 바닥 아래, 천장 위(지붕/층간)
    for sp in spaces:
        st = sp.structure
        if sp.key in polys:
            fy = _to_building(sp.T_building_space, (0, 0), st.floor_y or 0.0)[1]
            cy = fy + ((st.ceiling_y - st.floor_y) if st.ceiling_y is not None and st.floor_y is not None else 2.7)
            outer = polys[sp.key]
            meshes.append(slab_mesh(outer, fy - slab_thickness, fy, (150, 150, 150)))
            meshes.append(slab_mesh(outer, cy, cy + slab_thickness, (160, 160, 160)))
    if not entrances:
        notices.append("외벽에서 출입문 개구부를 찾지 못함 — 관리자 웹에서 출입구 포털을 직접 지정하세요")
    return EnvelopeResult(_merge(meshes), walls, entrances, notices)
