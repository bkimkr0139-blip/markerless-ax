"""스캔 보조 API — 자산 등록 전 사물 분리(POI 기반)."""

from __future__ import annotations

import base64

import numpy as np
from fastapi import APIRouter, HTTPException

from spatial_core.segmentation import assess_object, extract_outline, outline_to_json, refine_points, segment_object

from ..schemas import SegmentIn, SegmentOut, Vector3

router = APIRouter(prefix="/api/v1/scan", tags=["scan"])

MAX_SEGMENT_POINTS = 120_000  # LiDAR 메시 정점까지 수용


def _v3(a: np.ndarray) -> Vector3:
    return Vector3(x=float(a[0]), y=float(a[1]), z=float(a[2]))


@router.post("/segment", response_model=SegmentOut)
def segment(body: SegmentIn) -> SegmentOut:
    """스캔 점에서 POI의 사물만 분리한다 (지지면·벽 제거 + 연결 성분 + 이상점 제거).

    마스크는 요청 점과 같은 순서의 uint8 배열(base64). 클라이언트는 마스크로 GLB를 만든다.
    """
    if body.cloud.count > MAX_SEGMENT_POINTS:
        raise HTTPException(status_code=422, detail=f"too many points: {body.cloud.count} > {MAX_SEGMENT_POINTS}")
    try:
        pts = body.cloud.decode(MAX_SEGMENT_POINTS)  # 상한 이하이므로 샘플링 없이 원 순서 유지
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    r = segment_object(
        pts, np.array([body.poi.x, body.poi.y, body.poi.z]),
        max_radius=body.maxRadius, up_axis=body.upAxis,
    )
    outline = None
    if r.n_in >= 30:
        try:
            outline = outline_to_json(extract_outline(pts[r.mask], up_axis=body.upAxis))
        except ValueError:
            outline = None  # 점이 너무 적어 외곽선을 못 만듦 — 분리 결과는 그대로 반환
    assessment = None
    refined = None
    if r.n_in >= 20:
        obj_pts = pts[r.mask]
        if body.refine:
            # 스무딩·법선은 한 번만 계산해 품질 평가와 외곽선에도 같은 정제 점을 쓴다 (노이즈가 줄어 외곽선이 매끈해진다)
            obj_pts, obj_nrm = refine_points(obj_pts)
            refined = {
                "count": len(obj_pts),
                "points": base64.b64encode(obj_pts.astype("<f4").tobytes()).decode(),
                "normals": base64.b64encode(obj_nrm.astype("<f4").tobytes()).decode(),
            }
        assessment = assess_object(obj_pts, up_axis=body.upAxis)
        if outline is not None and body.refine:
            try:
                outline = outline_to_json(extract_outline(obj_pts, up_axis=body.upAxis))
            except ValueError:
                pass
    return SegmentOut(
        count=len(pts),
        mask=base64.b64encode(r.mask.astype(np.uint8).tobytes()).decode(),
        center=_v3(r.center), aabbMin=_v3(r.aabb_min), aabbMax=_v3(r.aabb_max),
        nIn=r.n_in, nRemoved=r.n_removed, quality=r.quality, warnings=r.warnings,
        planes=r.planes, supportHeightM=r.support_h, connRadiusM=r.conn_radius,
        outline=outline, assessment=assessment, refined=refined,
    )
