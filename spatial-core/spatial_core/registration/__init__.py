"""정합 엔진 (개발지시서 9절).

PoC v1: numpy 전용 ICP + 최소 GLB 파서/라이터.
OpenCV(PnP)·Open3D 연동은 Week 3-4에 확장 (pyproject의 heavy extra).
"""

from spatial_core.registration.icp import ICPResult, estimate_normals, icp_align, icp_plane_align, icp_yaw_align, icp_yaw_plane_refine
from spatial_core.registration.global_align import GlobalAlignResult, ObjectAlignResult, align_object, yaw_search_align
from spatial_core.registration.glb import (
    read_glb_mesh,
    write_glb,
    sample_mesh_points,
    mesh_aabb,
    mesh_extent,
)

__all__ = [
    "ICPResult",
    "icp_align",
    "icp_plane_align",
    "icp_yaw_align",
    "icp_yaw_plane_refine",
    "estimate_normals",
    "ObjectAlignResult",
    "align_object",
    "GlobalAlignResult",
    "yaw_search_align",
    "read_glb_mesh",
    "write_glb",
    "sample_mesh_points",
    "mesh_aabb",
    "mesh_extent",
]
