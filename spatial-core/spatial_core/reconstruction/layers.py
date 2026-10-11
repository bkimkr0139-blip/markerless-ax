"""공간 메시 의미 분리 — 구조(바닥·벽·천장)와 그 밖의 '시설/사물 후보'.

클래스를 단정하지 않는다: 구조 평면에서 떨어진 면을 연결 성분으로 묶어 UNKNOWN_OBJECT 후보로 두고(지시서 §6.3),
등록 자산과 겹치면 *후보 연결*만 제안한다(자동 확정 금지). 벽에 붙은 설비도 구조와 따로 후보로 남는다(벽 평면에서 6cm 이상 돌출).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .mesh import TriMesh, face_components
from .structure import Structure


@dataclass
class ObjectCandidate:
    id: int
    bbox_min: list[float]
    bbox_max: list[float]
    area_m2: float
    face_count: int
    height_m: float
    label: str = "UNKNOWN_OBJECT"
    attached_to_wall: int | None = None
    asset_candidates: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"id": self.id, "bboxMin": self.bbox_min, "bboxMax": self.bbox_max, "areaM2": round(self.area_m2, 3), "faces": self.face_count,
                "heightM": round(self.height_m, 3), "label": self.label, "attachedToWall": self.attached_to_wall, "assetCandidates": self.asset_candidates,
                "sourceKind": "OBSERVED", "reviewStatus": "REVIEW_REQUIRED"}


def structure_face_mask(mesh: TriMesh, st: Structure, tol: float = 0.06) -> np.ndarray:
    """바닥·천장·벽 평면(±tol)에 놓인 면 = 구조."""
    c = mesh.face_centers()
    n = mesh.face_normals()
    is_struct = np.zeros(len(c), bool)
    if st.floor_y is not None:
        is_struct |= (np.abs(c[:, 1] - st.floor_y) < tol) & (n[:, 1] > 0.8)
    if st.ceiling_y is not None:
        is_struct |= (np.abs(c[:, 1] - st.ceiling_y) < tol) & (n[:, 1] < -0.8)
    for w in st.walls:
        nv = np.array(w.normal)
        d = np.abs((c[:, [0, 2]] - np.array(w.p0)) @ nv)
        tang = np.array([-nv[1], nv[0]])
        t = (c[:, [0, 2]] - np.array(w.p0)) @ tang
        L = w.length
        is_struct |= (d < tol) & (t > -0.1) & (t < L + 0.1) & (np.abs(n[:, 1]) < 0.5)
    return is_struct


def object_candidates(mesh: TriMesh, st: Structure, assets: list[dict] | None = None, min_area: float = 0.04, tol: float = 0.06) -> list[ObjectCandidate]:
    """assets: [{assetId, min:[3], max:[3]}] (공간 프레임 AABB). 겹침(IoU)이 큰 것을 후보로 제안한다."""
    if mesh.empty:
        return []
    keep = ~structure_face_mask(mesh, st, tol)
    if not keep.any():
        return []
    sub = mesh.subset_faces(keep)
    lab = face_components(sub)
    area = sub.face_areas()
    ctr = sub.face_centers()
    out: list[ObjectCandidate] = []
    for k in range(lab.max() + 1):
        sel = lab == k
        a = float(area[sel].sum())
        if a < min_area:
            continue
        verts = sub.v[np.unique(sub.f[sel])]
        lo, hi = verts.min(0), verts.max(0)
        att = None
        for w in st.walls:  # 벽에 붙은 후보인지 (벽 평면 근처)
            nv = np.array(w.normal)
            d = ((verts[:, [0, 2]] - np.array(w.p0)) @ nv)
            if np.percentile(np.abs(d), 10) < 0.15:
                att = w.id
                break
        oc = ObjectCandidate(len(out) + 1, lo.round(3).tolist(), hi.round(3).tolist(), a, int(sel.sum()), float(hi[1] - lo[1]), attached_to_wall=att)
        for ast in assets or []:
            alo, ahi = np.array(ast["min"]), np.array(ast["max"])
            inter = np.maximum(0, np.minimum(hi, ahi) - np.maximum(lo, alo))
            iv = float(np.prod(inter))
            uv = float(np.prod(hi - lo) + np.prod(ahi - alo) - iv)
            iou = iv / uv if uv > 0 else 0.0
            if iou > 0.2:
                oc.asset_candidates.append({"assetId": ast["assetId"], "iou": round(iou, 3), "autoConfirmed": False})
        oc.asset_candidates.sort(key=lambda x: -x["iou"])
        out.append(oc)
    out.sort(key=lambda o: -o.area_m2)
    for i, o in enumerate(out, 1):
        o.id = i
    return out
