"""디지털 트윈 확장 모델 (지시서 §11.1). 기존 spaces/assets의 PK·FK를 그대로 참조하고 기존 테이블은 건드리지 않는다.

spaces에 추가되는 배치 정보(building_id, floor_id, t_building_space, kind)는 db.py `_ADDED_COLUMNS`로 보강한다.
sourceKind: OBSERVED | INFERRED | DESIGN | REPLACEMENT — OBSERVED 원본은 덮어쓰지 않는다.
reviewStatus: AUTO | REVIEW_REQUIRED | APPROVED | REJECTED
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import _now, _uuid


def _ts():
    return mapped_column(DateTime(timezone=True), default=_now)


class Building(Base):
    __tablename__ = "buildings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_code: Mapped[str] = mapped_column(ForeignKey("projects.code"))
    name: Mapped[str] = mapped_column(String(200), default="")
    gps: Mapped[dict | None] = mapped_column(JSON, default=None)  # {lat,lon,accuracyM}
    building_type: Mapped[str] = mapped_column(String(32), default="default")  # concrete|masonry|light|default
    default_thickness_m: Mapped[float | None] = mapped_column(Float, default=None)
    exterior_points: Mapped[list] = mapped_column(JSON, default=list)  # 외부 진입점 후보 [{name, position[3]}]
    notice: Mapped[str] = mapped_column(String(300), default="")
    envelope_revision: Mapped[int] = mapped_column(Integer, default=0)
    envelope_uri: Mapped[str | None] = mapped_column(String(500), default=None)
    envelope_review: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    created_at: Mapped[datetime] = _ts()


class Floor(Base):
    __tablename__ = "floors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    level: Mapped[str] = mapped_column(String(32), default="1F")
    elevation_m: Mapped[float] = mapped_column(Float, default=0.0)
    height_m: Mapped[float] = mapped_column(Float, default=3.0)


class SpaceConnector(Base):
    """문 포털 — 방↔방 / 방↔외부(to_space_id=None)."""
    __tablename__ = "space_connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    from_space_id: Mapped[int | None] = mapped_column(Integer, default=None)
    to_space_id: Mapped[int | None] = mapped_column(Integer, default=None)
    kind: Mapped[str] = mapped_column(String(16), default="DOOR")  # DOOR | ENTRANCE | OPENING
    position: Mapped[list] = mapped_column(JSON, default=list)  # 건물 좌표 [x,y,z]
    yaw_deg: Mapped[float] = mapped_column(Float, default=0.0)
    width_m: Mapped[float] = mapped_column(Float, default=0.9)
    height_m: Mapped[float] = mapped_column(Float, default=2.1)
    state: Mapped[str] = mapped_column(String(12), default="UNKNOWN")  # OPEN|CLOSED|UNKNOWN
    source_kind: Mapped[str] = mapped_column(String(12), default="INFERRED")
    review_status: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    exterior_viewpoint: Mapped[list | None] = mapped_column(JSON, default=None)
    name: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _ts()


class RoomTopology(Base):
    __tablename__ = "room_topology"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    space_a: Mapped[int] = mapped_column(Integer)
    space_b: Mapped[int | None] = mapped_column(Integer, default=None)
    relation: Mapped[str] = mapped_column(String(16), default="ADJACENT")  # ADJACENT | DOOR | EXTERIOR
    connector_id: Mapped[str | None] = mapped_column(String(36), default=None)
    shared_wall_gap_m: Mapped[float | None] = mapped_column(Float, default=None)


class ScanSession(Base):
    __tablename__ = "scan_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    mode: Mapped[str] = mapped_column(String(12), default="SPACE")  # SPACE | FACILITY | OBJECT
    device_id: Mapped[str] = mapped_column(String(128), default="unknown")
    device_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    world_map_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    relocalized: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="SCANNING")
    stage: Mapped[str] = mapped_column(String(24), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(500), default="")
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    structure_uri: Mapped[str | None] = mapped_column(String(500), default=None)
    ended_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    quality: Mapped[str | None] = mapped_column(String(12), default=None)
    coverage: Mapped[float | None] = mapped_column(Float, default=None)
    revision_id: Mapped[str | None] = mapped_column(String(36), default=None)
    registered: Mapped[bool] = mapped_column(Boolean, default=True)  # 월드맵 복원 실패(UNREGISTERED)면 False — 자동 병합 금지
    created_at: Mapped[datetime] = _ts()


class ScanFrame(Base):
    __tablename__ = "scan_frames"
    __table_args__ = (UniqueConstraint("session_id", "frame_index", name="uq_frame"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("scan_sessions.id"))
    frame_index: Mapped[int] = mapped_column(Integer)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    parts: Mapped[dict] = mapped_column(JSON, default=dict)  # {rgb:{bytes,sha256,received,uri}, depth:…, conf:…}
    state: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | DONE
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), default=None)
    created_at: Mapped[datetime] = _ts()


class ScanSegment(Base):
    __tablename__ = "scan_segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("scan_sessions.id"))
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    aligned: Mapped[bool] = mapped_column(Boolean, default=True)
    fitness: Mapped[float | None] = mapped_column(Float, default=None)
    transform: Mapped[list | None] = mapped_column(JSON, default=None)
    reason: Mapped[str] = mapped_column(String(200), default="")


class SpatialMap(Base):
    """ARWorldMap 리비전 기록 (파일 자체는 spaces.worldmap_* 와 worldmaps/ 에 있다)."""
    __tablename__ = "spatial_maps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    revision: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = _ts()


class MeshRevision(Base):
    __tablename__ = "mesh_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    session_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="READY")
    quality: Mapped[str] = mapped_column(String(12), default="REVIEW")
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    bbox: Mapped[list | None] = mapped_column(JSON, default=None)
    files: Mapped[dict] = mapped_column(JSON, default=dict)  # kind → storage uri
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    axes_yaw: Mapped[float] = mapped_column(Float, default=0.0)
    review_status: Mapped[str] = mapped_column(String(20), default="AUTO")
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()


class TextureAtlas(Base):
    __tablename__ = "texture_atlases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("mesh_revisions.id"))
    px: Mapped[int] = mapped_column(Integer, default=0)
    texel_cm: Mapped[float] = mapped_column(Float, default=0.0)
    texture_coverage: Mapped[float] = mapped_column(Float, default=0.0)
    unobserved_faces: Mapped[int] = mapped_column(Integer, default=0)
    frames_used: Mapped[int] = mapped_column(Integer, default=0)


class ScanCoverage(Base):
    __tablename__ = "scan_coverage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("mesh_revisions.id"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    overall: Mapped[float] = mapped_column(Float, default=0.0)
    floor: Mapped[float] = mapped_column(Float, default=0.0)
    ceiling: Mapped[float] = mapped_column(Float, default=0.0)
    unobserved_area_m2: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class SpaceGeometry(Base):
    """공간 지오메트리 출처 레지스트리 — 같은 공간에 OBSERVED/INFERRED/DESIGN/REPLACEMENT가 공존한다."""
    __tablename__ = "space_geometry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    revision_id: Mapped[str | None] = mapped_column(String(36), default=None)
    source_kind: Mapped[str] = mapped_column(String(12), default="OBSERVED")
    kind: Mapped[str] = mapped_column(String(24), default="MESH")  # MESH TEXTURED INFERRED COVERAGE STRUCTURE ENVELOPE DESIGN REPLACEMENT
    uri: Mapped[str | None] = mapped_column(String(500), default=None)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    uncertainty_m: Mapped[float | None] = mapped_column(Float, default=None)
    session_ids: Mapped[list] = mapped_column(JSON, default=list)
    design_revision: Mapped[str | None] = mapped_column(String(32), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(32), default=None)
    review_status: Mapped[str] = mapped_column(String(20), default="AUTO")
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = _ts()


class Wall(Base):
    __tablename__ = "walls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    revision_id: Mapped[str | None] = mapped_column(String(36), default=None)
    wall_no: Mapped[int] = mapped_column(Integer, default=0)  # 구조 추출 내 번호 (Structure.walls[].id)
    geom: Mapped[dict] = mapped_column(JSON, default=dict)  # space 프레임 {p0,p1,yMin,yMax,normal,lengthM,heightM}
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    kind: Mapped[str] = mapped_column(String(10), default="INTERIOR")
    neighbor_space_id: Mapped[int | None] = mapped_column(Integer, default=None)
    thickness_m: Mapped[float | None] = mapped_column(Float, default=None)
    thickness_method: Mapped[str] = mapped_column(String(16), default="CONFIG_DEFAULT")
    confidence: Mapped[float] = mapped_column(Float, default=0.3)
    source_kind: Mapped[str] = mapped_column(String(12), default="OBSERVED")
    review_status: Mapped[str] = mapped_column(String(20), default="AUTO")
    note: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = _ts()


class WallThicknessAssumption(Base):
    __tablename__ = "wall_thickness_assumptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wall_id: Mapped[int] = mapped_column(ForeignKey("walls.id"))
    thickness_m: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    at: Mapped[datetime] = _ts()


class Opening(Base):
    __tablename__ = "openings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wall_id: Mapped[int] = mapped_column(ForeignKey("walls.id"))
    kind: Mapped[str] = mapped_column(String(10), default="DOOR")
    t0: Mapped[float] = mapped_column(Float, default=0.0)
    t1: Mapped[float] = mapped_column(Float, default=0.0)
    y0: Mapped[float] = mapped_column(Float, default=0.0)
    y1: Mapped[float] = mapped_column(Float, default=0.0)
    source_kind: Mapped[str] = mapped_column(String(12), default="INFERRED")
    review_status: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    confidence: Mapped[float] = mapped_column(Float, default=0.4)


class SpatialTransformEdge(Base):
    __tablename__ = "spatial_transform_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    building_id: Mapped[int | None] = mapped_column(Integer, default=None)
    from_frame: Mapped[str] = mapped_column(String(80))
    to_frame: Mapped[str] = mapped_column(String(80))
    matrix: Mapped[list] = mapped_column(JSON, default=list)  # 열 우선 16
    kind: Mapped[str] = mapped_column(String(24), default="PLACEMENT")  # PLACEMENT | REGISTRATION | ENTRANCE
    uncertainty_m: Mapped[float | None] = mapped_column(Float, default=None)
    review_status: Mapped[str] = mapped_column(String(20), default="AUTO")
    created_at: Mapped[datetime] = _ts()


class RegistrationRun(Base):
    __tablename__ = "registration_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    src_session_id: Mapped[str | None] = mapped_column(String(36), default=None)
    dst_ref: Mapped[str | None] = mapped_column(String(64), default=None)  # 기준 세션/리비전
    fitness: Mapped[float] = mapped_column(Float, default=0.0)
    rmse_m: Mapped[float] = mapped_column(Float, default=0.0)
    overlap_points: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    transform: Mapped[list | None] = mapped_column(JSON, default=None)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = _ts()


class ScanQualityReport(Base):
    __tablename__ = "scan_quality_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("scan_sessions.id"))
    quality: Mapped[str] = mapped_column(String(12), default="REVIEW")
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class AssetGeometryLink(Base):
    """자산(사물)과 공간 메시의 연결 — 이중 표면 방지용 영역(bbox)과 마스크 방식."""
    __tablename__ = "asset_geometry_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    revision_id: Mapped[str | None] = mapped_column(String(36), default=None)
    bbox_space: Mapped[dict | None] = mapped_column(JSON, default=None)
    mask_mode: Mapped[str] = mapped_column(String(24), default="space-background-only")  # | asset-extracted
    source_kind: Mapped[str] = mapped_column(String(12), default="OBSERVED")


class CadCatalog(Base):
    __tablename__ = "cad_catalog"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    family: Mapped[str] = mapped_column(String(100), default="")
    ifc_guid: Mapped[str | None] = mapped_column(String(64), default=None)
    dims_m: Mapped[list] = mapped_column(JSON, default=list)  # [x,y,z] 로컬 프레임 크기
    glb_uri: Mapped[str | None] = mapped_column(String(500), default=None)
    ports: Mapped[list] = mapped_column(JSON, default=list)  # 연결 포트·기능 좌표 [{name, position[3]}]
    symmetry: Mapped[list] = mapped_column(JSON, default=list)  # 대칭 yaw 각(도) 목록, 예 [0,180]
    revision: Mapped[str] = mapped_column(String(32), default="R1")
    created_at: Mapped[datetime] = _ts()


class CadReplacement(Base):
    __tablename__ = "cad_replacements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    catalog_id: Mapped[int | None] = mapped_column(Integer, default=None)
    transform: Mapped[list | None] = mapped_column(JSON, default=None)  # 카탈로그 로컬 → 자산 로컬, 열 우선 16
    fitness: Mapped[float] = mapped_column(Float, default=0.0)
    rmse_m: Mapped[float] = mapped_column(Float, default=0.0)
    dim_error_m: Mapped[float | None] = mapped_column(Float, default=None)
    symmetry_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(14), default="CANDIDATE")  # CANDIDATE|UNDETERMINED|APPROVED|REVERTED|REJECTED
    review_status: Mapped[str] = mapped_column(String(20), default="REVIEW_REQUIRED")
    design_revision: Mapped[str | None] = mapped_column(String(32), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(32), default=None)
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = _ts()


class ModelApproval(Base):
    __tablename__ = "model_approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(24))  # wall | portal | revision | envelope | cad_replacement
    target_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))  # APPROVE | REJECT | REVERT | EDIT
    actor: Mapped[str] = mapped_column(String(64), default="system")
    comment: Mapped[str] = mapped_column(String(300), default="")
    at: Mapped[datetime] = _ts()


class TourScene(Base):
    __tablename__ = "tour_scenes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    building_id: Mapped[int] = mapped_column(ForeignKey("buildings.id"))
    name: Mapped[str] = mapped_column(String(200), default="기본 투어")
    entry: Mapped[dict] = mapped_column(JSON, default=dict)  # 시작 뷰포인트
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class TourPortal(Base):
    __tablename__ = "tour_portals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tour_id: Mapped[str] = mapped_column(ForeignKey("tour_scenes.id"))
    connector_id: Mapped[str] = mapped_column(String(36))
    label: Mapped[str] = mapped_column(String(100), default="")
    order_no: Mapped[int] = mapped_column(Integer, default=0)


class TourViewpoint(Base):
    __tablename__ = "tour_viewpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tour_id: Mapped[str] = mapped_column(ForeignKey("tour_scenes.id"))
    name: Mapped[str] = mapped_column(String(100), default="")
    position: Mapped[list] = mapped_column(JSON, default=list)
    target: Mapped[list] = mapped_column(JSON, default=list)
    space_id: Mapped[int | None] = mapped_column(Integer, default=None)
