"""ORM 모델 (개발지시서 19절 데이터 모델 + 자가 에셋 생성 흐름).

JSON 컬럼 구조는 docs/API.md의 canonical JSON과 일치한다.
Pose canonical: {"positionM": {x,y,z}, "rpyDeg": {roll,pitch,yaw}} (미터/도)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (UniqueConstraint("project_code", "code", name="uq_space_code"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_code: Mapped[str] = mapped_column(ForeignKey("projects.code"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200), default="")
    level: Mapped[str] = mapped_column(String(64), default="")
    # ARWorldMap 저장 (공간 좌표 복원 — docs/COORDINATE.md T_space_session)
    worldmap_uri: Mapped[str | None] = mapped_column(String(500), default=None)
    worldmap_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)  # 관리자 수정 시각 (모바일 동기화 판단용)
    # 디지털 트윈 배치 (docs/INTERFACES.md §5) — 건물/층/공간 종류와 공간→건물 변환(열 우선 16)
    building_id: Mapped[int | None] = mapped_column(Integer, default=None)
    floor_id: Mapped[int | None] = mapped_column(Integer, default=None)
    t_building_space: Mapped[list | None] = mapped_column(JSON, default=None)
    kind: Mapped[str] = mapped_column(String(16), default="ROOM")  # ROOM | CORRIDOR | FACILITY_ZONE


class Asset(Base):
    """검수 대상 자산 = CAD/BIM 모델 + 설계 Pose + 공차 Rule.

    자가 생성(스캔) 경로와 Revit 패키지 임포트 경로가 같은 테이블에 저장된다.
    bim_revision: Revit 경로는 모델 리비전(R12 등), 자가 생성은 "GEN-001" 식.
    """

    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(ForeignKey("projects.code"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    bim_revision: Mapped[str] = mapped_column(String(32))
    ifc_guid: Mapped[str | None] = mapped_column(String(64), default=None)
    family: Mapped[str | None] = mapped_column(String(128), default=None)
    asset_type: Mapped[str | None] = mapped_column(String(128), default=None)
    design_pose: Mapped[dict] = mapped_column(JSON)
    tolerance: Mapped[dict | None] = mapped_column(JSON, default=None)
    cad_uri: Mapped[str | None] = mapped_column(String(500), default=None)
    cad_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # 자산 로컬 프레임 AABB {"minM": [..], "maxM": [..]} — 기기 오버레이/필터용
    aabb: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # --- 백오피스 관리 정보 (기존 DB는 ensure_schema가 컬럼을 추가한다) ---
    name: Mapped[str] = mapped_column(String(200), default="")  # 표시 이름 (비면 asset_id)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | ARCHIVED
    device_id: Mapped[str | None] = mapped_column(String(128), default=None)  # 등록한 단말
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # 스캔 시각·위치 — 재스캔하면 갱신. geo = {"lat","lon","altM","accuracyM"} (GPS 없으면 None)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    geo: Mapped[dict | None] = mapped_column(JSON, default=None)
    # 검수 때 앱이 보여 줄 레이어 선택 (통합관리자가 설정). None이면 기본값(외곽 박스·치수·사진 실루엣)
    inspect_view: Mapped[dict | None] = mapped_column(JSON, default=None)


class InspectionTask(Base):
    __tablename__ = "inspection_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_code: Mapped[str] = mapped_column(ForeignKey("projects.code"))
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id"))
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    assignee: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Observation(Base):
    """현장 관측 1건 = 자산 1개의 측정 결과 (개발지시서 19절).

    설계 Pose는 측정 시점 값으로 스냅샷한다(원칙 12: 모든 검사에 BIM Revision 저장).
    """

    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(String(36), default=None)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.asset_id"))
    device_id: Mapped[str] = mapped_column(String(128), default="unknown")
    bim_revision: Mapped[str] = mapped_column(String(32), default="")
    design_pose: Mapped[dict] = mapped_column(JSON, default=dict)
    actual_pose: Mapped[dict | None] = mapped_column(JSON, default=None)
    deviation: Mapped[dict | None] = mapped_column(JSON, default=None)
    uncertainty: Mapped[dict | None] = mapped_column(JSON, default=None)
    confidence: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    decision: Mapped[str | None] = mapped_column(String(16), default=None)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InspectionResult(Base):
    """판정 결과 레코드 — 판정 근거(reasons/margins)를 포함한다."""

    __tablename__ = "inspection_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    observation_id: Mapped[str] = mapped_column(ForeignKey("observations.id"))
    asset_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    deviation: Mapped[dict | None] = mapped_column(JSON, default=None)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    margins: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvidenceRecord(Base):
    """원본 증거 보존 (원칙 13: 원본 RGB/Depth/Pose Evidence 보존)."""

    __tablename__ = "evidence_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32), default="image")
    uri: Mapped[str] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str | None] = mapped_column(String(36), default=None)
    title: Mapped[str] = mapped_column(String(255), default="검수 리포트")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ───────────────────────── 백오피스: 계정·세션·이슈 워크플로우·감사 ─────────────────────────

ROLES = ("admin", "manager", "inspector", "viewer")  # 통합관리자 / 현장관리자 / 현장 검수자 / 조회 전용


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(100), default="")
    role: Mapped[str] = mapped_column(String(16), default="viewer")
    pw_salt: Mapped[str] = mapped_column(String(64))
    pw_hash: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # 기본/임시 비밀번호이면 첫 로그인 후 바꿀 때까지 다른 관리자 API를 막는다 (auth.current_user)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)


class AuthSession(Base):
    """로그인 세션 — DB에 두어 로그아웃·계정 비활성화·비밀번호 변경 때 즉시 폐기할 수 있다."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


ISSUE_STATUSES = ("OPEN", "CONFIRMED", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "REINSPECT", "CLOSED", "DISMISSED")


class Issue(Base):
    """검수 결과에서 나온 문제(부적합) — 확인 → 조치 지시 → 조치 → 재검수 → 종결 워크플로우."""

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # 코드 = ISS-0001
    asset_id: Mapped[str | None] = mapped_column(String(64), default=None)
    observation_id: Mapped[str | None] = mapped_column(String(36), default=None)
    space_id: Mapped[int | None] = mapped_column(Integer, default=None)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(12), default="MEDIUM")  # LOW | MEDIUM | HIGH | CRITICAL
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    source: Mapped[str] = mapped_column(String(12), default="manual")  # auto(검수 결과 자동) | manual
    reporter: Mapped[str] = mapped_column(String(64), default="system")
    assignee: Mapped[str | None] = mapped_column(String(64), default=None)
    due_date: Mapped[str | None] = mapped_column(String(10), default=None)  # YYYY-MM-DD
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class IssueEvent(Base):
    """이슈 이력: 생성·상태 전이·배정·코멘트·재측정 결과 (감사 추적)."""

    __tablename__ = "issue_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    type: Mapped[str] = mapped_column(String(16))  # created | status | comment | assign | edit | remeasure
    from_status: Mapped[str | None] = mapped_column(String(16), default=None)
    to_status: Mapped[str | None] = mapped_column(String(16), default=None)
    comment: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    """관리자 행위 감사 로그 (누가·언제·무엇을)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[str] = mapped_column(String(24), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
