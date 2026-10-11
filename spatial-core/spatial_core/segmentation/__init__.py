"""POI(사용자가 지정한 사물 중심) 기반 사물 분리 + 외곽선 추출 + 등록 품질 평가."""

from spatial_core.segmentation.object_segment import SegmentResult, segment_object
from spatial_core.segmentation.outline import (
    OutlineResult,
    denoise_tri_view,
    extract_outline,
    obb_of,
    outline_to_json,
)
from spatial_core.segmentation.quality import assess_object, refine_points

__all__ = [
    "SegmentResult", "segment_object", "OutlineResult", "extract_outline", "denoise_tri_view",
    "outline_to_json", "obb_of", "assess_object", "refine_points",
]
