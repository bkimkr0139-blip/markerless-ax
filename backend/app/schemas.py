"""API 스키마 (docs/API.md와 1:1 대응, 개발지시서 6.1/18/19절).

JSON 키는 지시서 예시와 동일한 camelCase를 사용한다.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field, model_validator


class Vector3(BaseModel):
    x: float
    y: float
    z: float


class RpyDeg(BaseModel):
    roll: float
    pitch: float
    yaw: float


class Quat(BaseModel):
    x: float
    y: float
    z: float
    w: float


class PoseIn(BaseModel):
    positionM: Vector3
    rpyDeg: RpyDeg | None = None
    quat: Quat | None = None

    @model_validator(mode="after")
    def _need_orientation(self) -> "PoseIn":
        if self.rpyDeg is None and self.quat is None:
            raise ValueError("pose requires rpyDeg or quat")
        return self

    def to_json(self) -> dict[str, Any]:
        from spatial_core.coordinate import Pose

        return Pose.from_json(self.model_dump()).to_json()


class Tolerance(BaseModel):
    positionMm: float = 15.0
    zToleranceMm: float | None = None
    rotationDeg: float = 1.0


class Uncertainty(BaseModel):
    positionMm: float = 0.0
    rotationDeg: float = 0.0


class CloudIn(BaseModel):
    """float32 [x,y,z]*N little-endian의 base64. 공간(space) 프레임 좌표."""

    base64: str
    count: int = Field(gt=0)
    frame: str = "space"

    def decode(self, max_points: int) -> np.ndarray:
        if self.frame != "space":
            raise ValueError(f"unsupported cloud frame: {self.frame} (only 'space')")
        raw = base64.b64decode(self.base64, validate=True)
        expected = self.count * 12
        if len(raw) != expected:
            raise ValueError(
                f"cloud byte length mismatch: got {len(raw)}, expected {expected}"
            )
        pts = np.frombuffer(raw, dtype="<f4").reshape(-1, 3).astype(np.float64)
        if len(pts) > max_points:
            idx = np.linspace(0, len(pts) - 1, max_points).astype(int)
            pts = pts[idx]
        return pts


# ---------- 생성/입력 ----------


class ProjectIn(BaseModel):
    projectId: str = Field(min_length=1, max_length=64)
    name: str = ""


class SpaceIn(BaseModel):
    spaceId: str = Field(min_length=1, max_length=64)
    name: str = ""
    level: str = ""


class PackageAssetIn(BaseModel):
    assetId: str = Field(min_length=1, max_length=64)
    ifcGuid: str | None = None
    family: str | None = None
    type: str | None = None
    designPose: PoseIn
    tolerance: Tolerance
    cad: str | None = None  # 패키지 내 GLB 경로 (PoC에서는 메타만 저장)


class PackageIn(BaseModel):
    projectId: str = Field(min_length=1, max_length=64)
    revision: str = Field(min_length=1, max_length=32)
    spaceId: str = Field(min_length=1, max_length=64)
    spaceName: str = ""
    level: str = ""
    assets: list[PackageAssetIn] = Field(min_length=1)


class GeoIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altM: float | None = None
    accuracyM: float | None = Field(default=None, ge=0)


class ScanAssetIn(BaseModel):
    """앱 스캔 → 자산 생성 메타 (GLB는 multipart의 별도 파트).

    GLB는 자산 로컬 프레임(중심=design origin) 좌표여야 하고,
    designPose는 공간 프레임에서의 자산 원점 포즈다.
    """

    assetId: str = Field(min_length=1, max_length=64)
    projectId: str = Field(min_length=1, max_length=64)
    spaceId: str = Field(min_length=1, max_length=64)
    bimRevision: str = Field(default="GEN-001", max_length=32)
    family: str | None = None
    type: str | None = None
    designPose: PoseIn
    tolerance: Tolerance | None = None  # 없으면 서버 기본 Rule 적용
    notes: str | None = None
    deviceId: str | None = Field(default=None, max_length=128)  # 등록한 단말 (백오피스에서 단말별 조회)
    name: str | None = Field(default=None, max_length=200)  # 표시 이름 (백오피스에서 수정 가능)
    scannedAt: datetime | None = None  # 단말에서 스캔한 시각 (없으면 서버 수신 시각)
    geo: GeoIn | None = None  # 스캔 위치 (단말 GPS)


class MeasurementIn(BaseModel):
    assetId: str | None = None  # autoMatch면 생략 — 서버가 공간의 등록 자산 중 가장 닮은 것을 고른다
    autoMatch: bool = False
    spaceId: int | None = None  # autoMatch 대상 공간(숫자 ID)
    deviceId: str = "unknown"
    bimRevision: str | None = None  # None이면 자산의 현재 revision 사용
    taskId: str | None = None
    observationId: str | None = None  # 기존 관측에 결과 부착
    # 기기가 보고하는 캡처 품질 (cloud 경로가 아닐 때 사용)
    confidence: str | None = None
    depthValidRatio: float | None = None
    trackingNormal: bool = True
    uncertainty: Uncertainty | None = None
    # 둘 중 하나는 반드시: 정합 완료 포즈(수동/기기 정합) 또는 원본 포인트 클라우드
    actualPose: PoseIn | None = None
    cloud: CloudIn | None = None
    # 정합 방식: "space" = 클라우드가 공간 프레임이고 초깃값 항등(월드맵 복원 전제, 기존 동작)
    #            "object" = 사물 앵커 정합 — 초기 포즈를 모르므로 yaw 탐색 후 ICP.
    #                       클라우드는 현재 세션 좌표, 결과 actualPose도 현재 세션 좌표의 사물 포즈
    alignment: Literal["space", "object"] = "space"
    hintCenter: Vector3 | None = None  # object 모드: 세션 좌표의 사물 중심 추정(사용자 조준점)
    worldMapRestored: bool = False  # object 모드: 세션이 공간 프레임과 일치하는가 (편차 판정 가능 여부)

    @model_validator(mode="after")
    def _need_asset_or_automatch(self) -> "MeasurementIn":
        if self.autoMatch:
            if self.spaceId is None or self.cloud is None or self.alignment != "object":
                raise ValueError("autoMatch requires spaceId, cloud and alignment='object'")
        elif not self.assetId:
            raise ValueError("assetId is required unless autoMatch")
        return self

    @model_validator(mode="after")
    def _need_pose_or_cloud(self) -> "MeasurementIn":
        if self.actualPose is None and self.cloud is None:
            raise ValueError("measurement requires actualPose or cloud")
        return self


class ObservationCreateIn(BaseModel):
    assetId: str
    deviceId: str = "unknown"
    taskId: str | None = None
    evidenceUris: list[str] = Field(default_factory=list)
    captureMeta: dict[str, Any] = Field(default_factory=dict)


class TolerancePut(BaseModel):
    positionMm: float = Field(gt=0)
    zToleranceMm: float | None = None
    rotationDeg: float = Field(ge=0)


class ReportIn(BaseModel):
    taskId: str | None = None
    observationIds: list[str] = Field(default_factory=list)
    title: str = "검수 리포트"


# ---------- 응답 ----------


class ProjectOut(BaseModel):
    projectId: str
    name: str
    createdAt: str | None = None


class SpaceOut(BaseModel):
    id: int
    projectId: str
    code: str
    name: str
    level: str
    hasWorldMap: bool = False
    worldmapSha256: str | None = None


class SegmentIn(BaseModel):
    """POI 기반 사물 분리 요청. cloud는 세션(=space) 좌표 float32 xyz*N."""

    cloud: CloudIn
    poi: Vector3
    maxRadius: float = Field(default=1.0, gt=0.05, le=3.0)  # 사물 최대 반경(m)
    upAxis: int = Field(default=1, ge=0, le=2)  # ARKit Y-up
    refine: bool = False  # true면 사물 점을 스무딩하고 법선을 계산해 돌려준다 (VIO 점용 — LiDAR 메시는 불필요)


class SegmentOut(BaseModel):
    count: int
    mask: str  # uint8 * N (1=사물) base64 — 요청 점 순서와 동일
    center: Vector3  # 사물 AABB 중심
    aabbMin: Vector3
    aabbMax: Vector3
    nIn: int
    nRemoved: int
    quality: str  # good | fair | poor
    warnings: list[str] = Field(default_factory=list)
    planes: list[dict[str, Any]] = Field(default_factory=list)
    supportHeightM: float | None = None
    connRadiusM: float = 0.0
    # 분리된 사물의 외곽선 (세션 좌표) — 점 조각 대신 선으로 미리보기: lines/obb/views
    outline: dict[str, Any] | None = None
    # 등록 품질 평가: score(0~100), 방향별 면 확보(sides), 방위 커버리지, 밀도, 노이즈, 상자형 정도, 보완 안내(tips)
    assessment: dict[str, Any] | None = None
    # refine=true 요청 시: 마스크가 true인 점들(요청 순서)의 정제 좌표+법선 — float32 xyz*K base64
    refined: dict[str, Any] | None = None


class MatchIn(BaseModel):
    """자산 찾기(자동 매칭만, 판정·저장 없음): 비춘 사물과 닮은 등록 자산 후보 순위."""

    spaceId: int
    cloud: CloudIn
    hintCenter: Vector3 | None = None
    worldMapRestored: bool = False


class BulkDeleteIn(BaseModel):
    assetIds: list[str] = Field(min_length=1, max_length=500)


class BulkDeleteOut(BaseModel):
    deleted: list[str]
    notFound: list[str]


class AssetOut(BaseModel):
    assetId: str
    projectId: str
    spaceId: int
    bimRevision: str
    ifcGuid: str | None = None
    family: str | None = None
    type: str | None = None
    designPose: dict[str, Any]
    tolerance: dict[str, Any] | None = None
    cadUri: str | None = None
    aabb: dict[str, Any] | None = None
    scannedAt: str | None = None
    geo: dict[str, Any] | None = None
    deviceId: str | None = None
    inspectView: dict[str, bool] | None = None  # 검수 때 보여 줄 레이어 (관리자 설정)
    name: str | None = None  # 표시 이름 (관리자가 수정, 비면 assetId)
    status: str | None = None  # ACTIVE | ARCHIVED


class ResultOut(BaseModel):
    resultId: str
    observationId: str
    assetId: str
    decision: str
    confidence: str
    deviation: dict[str, Any] | None = None
    reasons: list[str] = Field(default_factory=list)
    margins: dict[str, Any] = Field(default_factory=dict)
    createdAt: str | None = None


class ObservationOut(BaseModel):
    observationId: str
    taskId: str | None = None
    assetId: str
    deviceId: str
    bimRevision: str
    designPose: dict[str, Any]
    actualPose: dict[str, Any] | None = None
    deviation: dict[str, Any] | None = None
    uncertainty: dict[str, Any] | None = None
    confidence: str
    decision: str | None = None
    evidence: list[str] = Field(default_factory=list)
    createdAt: str | None = None


class MeasurementOut(BaseModel):
    observation: ObservationOut
    result: ResultOut
    # 사물 대 사물 검수 상세: 매칭 순위, 분리·정합 지표, 관측 사물 외곽선(세션 좌표)
    match: dict[str, Any] | None = None


class EvidenceOut(BaseModel):
    evidenceId: str
    kind: str
    uri: str
    sha256: str
    sizeBytes: int


class ReportOut(BaseModel):
    reportId: str
    taskId: str | None = None
    title: str
    summary: list[str] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    createdAt: str | None = None
