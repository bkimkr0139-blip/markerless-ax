"""디지털 트윈 모바일/연동 API (`/api/v1`) — docs/INTERFACES.md §5."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db, get_storage
from ..models import Asset, Space
from ..models_twin import Building, CadCatalog, CadReplacement, Floor, MeshRevision, ScanSession, SpaceGeometry, TourScene, Wall
from ..services import twin as svc
from ..services.twin import TwinError
from ..storage import FileStorage

router = APIRouter(prefix="/api/v1", tags=["twin"])

FILE_KINDS = {"observed", "textured", "inferred", "coverage", "lodweb", "lodmobile"}


def _err(e: TwinError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def api_url(kind: str, ident: str) -> str:
    return f"/api/v1/twin/files/{kind}/{ident}.glb"


# ── 건물·층·배치 ──

class BuildingIn(BaseModel):
    projectCode: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    gps: dict | None = None
    buildingType: str = "default"
    defaultThicknessM: float | None = Field(default=None, ge=0.03, le=2.0)
    exteriorPoints: list = []


class FloorIn(BaseModel):
    buildingId: int
    level: str = "1F"
    elevationM: float = 0.0
    heightM: float = Field(default=3.0, gt=0.5, le=20)


class PlacementIn(BaseModel):
    buildingId: int
    floorId: int | None = None
    tBuildingSpace: list[float] | None = None
    kind: str = Field(default="ROOM", pattern="^(ROOM|CORRIDOR|FACILITY_ZONE)$")


def building_json(db: Session, b: Building) -> dict:
    floors = db.scalars(select(Floor).where(Floor.building_id == b.id).order_by(Floor.elevation_m)).all()
    return {"id": b.id, "projectCode": b.project_code, "name": b.name, "gps": b.gps, "buildingType": b.building_type,
            "defaultThicknessM": b.default_thickness_m, "notice": b.notice, "envelopeRevision": b.envelope_revision,
            "floors": [{"id": f.id, "level": f.level, "elevationM": f.elevation_m, "heightM": f.height_m} for f in floors]}


@router.post("/buildings", status_code=201)
def create_building(body: BuildingIn, db: Session = Depends(get_db)) -> dict:
    b = svc.create_building(db, body.projectCode, body.name, body.gps, body.buildingType, body.defaultThicknessM, body.exteriorPoints)
    db.commit()
    return building_json(db, b)


@router.get("/buildings")
def list_buildings(db: Session = Depends(get_db)) -> list[dict]:
    return [building_json(db, b) for b in db.scalars(select(Building).order_by(Building.id)).all()]


@router.get("/buildings/{bid}")
def get_building(bid: int, db: Session = Depends(get_db)) -> dict:
    b = db.get(Building, bid)
    if b is None:
        raise HTTPException(404, "building not found")
    return building_json(db, b)


@router.post("/floors", status_code=201)
def create_floor(body: FloorIn, db: Session = Depends(get_db)) -> dict:
    if db.get(Building, body.buildingId) is None:
        raise HTTPException(404, "building not found")
    f = Floor(building_id=body.buildingId, level=body.level, elevation_m=body.elevationM, height_m=body.heightM)
    db.add(f)
    db.commit()
    return {"id": f.id, "buildingId": f.building_id, "level": f.level, "elevationM": f.elevation_m, "heightM": f.height_m}


@router.put("/spaces/{ref}/placement")
def put_placement(ref: str, body: PlacementIn, db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.place_space(db, svc.resolve_space(db, ref), body.buildingId, body.floorId, body.tBuildingSpace, body.kind)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


# ── 스캔 세션 ──

class SessionIn(BaseModel):
    mode: str = "SPACE"
    deviceId: str = "unknown"
    deviceProfile: dict = {}
    capabilities: dict = {}
    worldMapRevision: str | None = None
    relocalized: bool = False


def session_json(s: ScanSession) -> dict:
    return {"scanSessionId": s.id, "spaceId": s.space_id, "mode": s.mode, "status": s.status, "stage": s.stage, "progress": s.progress,
            "message": s.message, "quality": s.quality, "coverage": s.coverage, "revisionId": s.revision_id, "frames": s.frame_count,
            "registered": s.registered, "createdAt": svc.iso(s.created_at)}


@router.post("/spaces/{ref}/scan-sessions", status_code=201)
def create_session(ref: str, body: SessionIn, db: Session = Depends(get_db)) -> dict:
    try:
        sp = svc.resolve_space(db, ref)
        s = svc.create_session(db, sp, body.mode, body.deviceId, body.deviceProfile, body.capabilities, body.worldMapRevision, body.relocalized)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return session_json(s)


class InitUploadIn(BaseModel):
    meta: dict
    parts: dict
    idempotencyKey: str | None = Field(default=None, max_length=100)


@router.post("/scan-sessions/{sid}/frames/init-upload")
def init_upload(sid: str, body: InitUploadIn, db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.init_upload(db, svc.get_session(db, sid), body.meta, body.parts, body.idempotencyKey)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


@router.put("/scan-sessions/{sid}/frames/{idx}/parts/{part}")
async def put_part(sid: str, idx: int, part: str, request: Request, offset: int = 0, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    data = await request.body()
    try:
        out = svc.put_part(db, storage, svc.get_session(db, sid), idx, part, offset, data, request.headers.get("x-sha256"))
    except TwinError as e:
        db.commit()  # 해시 불일치 시 received 초기화 반영
        raise _err(e) from e
    db.commit()
    return out


class CompleteIn(BaseModel):
    frameIndex: int


@router.post("/scan-sessions/{sid}/frames/complete-upload")
def complete_upload(sid: str, body: CompleteIn, db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.complete_upload(db, svc.get_session(db, sid), body.frameIndex)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


@router.post("/scan-sessions/{sid}/structure", status_code=201)
async def upload_structure(sid: str, request: Request, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        js = json.loads(await request.body())
        svc.save_structure(db, storage, svc.get_session(db, sid), js)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"JSON 오류: {e}") from e
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"ok": True, "walls": len(js["walls"])}


class FinalizeIn(BaseModel):
    endedReason: str = "user"


@router.post("/scan-sessions/{sid}/finalize")
def finalize(sid: str, body: FinalizeIn | None = None, db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.finalize_session(db, svc.get_session(db, sid), (body.endedReason if body else "user"))
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


class ReconstructIn(BaseModel):
    voxelM: float | None = Field(default=None, ge=0.01, le=0.1)
    textureMaxPx: int | None = Field(default=None, ge=512, le=8192)
    force: bool = False


@router.post("/scan-sessions/{sid}/reconstruct", status_code=202)
def reconstruct(sid: str, request: Request, body: ReconstructIn | None = None, db: Session = Depends(get_db)) -> dict:
    body = body or ReconstructIn()
    try:
        s = svc.get_session(db, sid)
    except TwinError as e:
        raise _err(e) from e
    if s.status == "PROCESSING" and s.stage.startswith("RUNNING") and not body.force:
        raise HTTPException(409, "이미 재구성 중입니다")
    if s.status in svc.OPEN_STATES:
        raise HTTPException(409, "촬영이 끝나지 않았습니다 — finalize 먼저")
    if s.status == "NEED_MORE_CAPTURE" and not body.force:
        raise HTTPException(409, f"추가 촬영이 필요합니다: {s.message}")
    params = {}
    if body.voxelM:
        params["voxel_m"] = body.voxelM
    if body.textureMaxPx:
        params["texture_px"] = body.textureMaxPx
    s.status, s.stage, s.progress, s.message = "PROCESSING", "QUEUED", 0.0, "대기열"
    db.commit()
    worker = request.app.state.twin_worker
    ok = worker.submit(f"session:{sid}", svc.run_reconstruction, request.app.state.session_factory, request.app.state.storage, s.space_id, [sid], params, sid)
    if not ok:
        raise HTTPException(409, "이미 재구성 중입니다")
    return {"scanSessionId": sid, "status": "QUEUED"}


@router.get("/scan-sessions/{sid}/status")
def status(sid: str, db: Session = Depends(get_db)) -> dict:
    try:
        return session_json(svc.get_session(db, sid))
    except TwinError as e:
        raise _err(e) from e


# ── 공간 지오메트리 ──

@router.get("/spaces/{ref}/coverage")
def coverage(ref: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.coverage_info(db, svc.resolve_space(db, ref), api_url)
    except TwinError as e:
        raise _err(e) from e


@router.get("/spaces/{ref}/geometry/revisions")
def revisions(ref: str, db: Session = Depends(get_db)) -> list[dict]:
    try:
        return svc.revisions_info(db, svc.resolve_space(db, ref), api_url)
    except TwinError as e:
        raise _err(e) from e


class MergeIn(BaseModel):
    sessionIds: list[str] = Field(min_length=1, max_length=8)
    includeCurrent: bool = True
    voxelM: float | None = Field(default=None, ge=0.01, le=0.1)


@router.post("/spaces/{ref}/merge-sessions", status_code=202)
def merge_sessions(ref: str, body: MergeIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """여러 세션을 같은 공간으로 병합: 기존 프레임을 버리지 않고 전체를 다시 융합한다 (구간 정합이 불확실하면 그 구간은 제외·REVIEW_REQUIRED)."""
    try:
        sp = svc.resolve_space(db, ref)
        ids = list(body.sessionIds)
        for sid in ids:
            s = svc.get_session(db, sid)
            if s.space_id != sp.id:
                raise TwinError(f"세션 {sid}은(는) 다른 공간의 것입니다", 409)
            if not s.registered:
                raise TwinError(f"세션 {sid}은(는) 월드맵 재위치에 실패한 UNREGISTERED 세션 — 자동 병합할 수 없습니다 (관리자가 수동 정합 확인)", 409)
            if s.status in svc.OPEN_STATES:
                raise TwinError(f"세션 {sid}의 촬영이 끝나지 않았습니다", 409)
        if body.includeCurrent:
            cur = svc.current_revision(db, sp.id)
            if cur:
                ids = [x for x in cur.session_ids if x not in ids] + ids
    except TwinError as e:
        raise _err(e) from e
    label = ids[-1]
    s = db.get(ScanSession, label)
    s.status, s.stage, s.progress, s.message = "PROCESSING", "QUEUED", 0.0, f"{len(ids)}개 세션 병합 대기"
    db.commit()
    params = {"voxel_m": body.voxelM} if body.voxelM else {}
    ok = request.app.state.twin_worker.submit(f"merge:{sp.id}", svc.run_reconstruction, request.app.state.session_factory, request.app.state.storage, sp.id, ids, params, label)
    if not ok:
        raise HTTPException(409, "이 공간의 병합이 이미 진행 중입니다")
    return {"spaceId": sp.code, "sessionIds": ids, "statusSession": label, "status": "QUEUED"}


# ── 외피·벽 ──

class EnvelopeIn(BaseModel):
    defaultThicknessM: float | None = Field(default=None, ge=0.03, le=2.0)
    buildingType: str | None = None


@router.post("/buildings/{bid}/generate-envelope")
def generate_envelope(bid: int, body: EnvelopeIn | None = None, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    b = db.get(Building, bid)
    if b is None:
        raise HTTPException(404, "building not found")
    body = body or EnvelopeIn()
    try:
        out = svc.generate_building_envelope(db, storage, b, body.defaultThicknessM, body.buildingType, "api")
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


class ThicknessIn(BaseModel):
    thicknessM: float = Field(ge=0.03, le=2.0)
    method: str = "MEASURED"
    note: str = Field(default="", max_length=300)


@router.patch("/walls/{wid}/thickness")
def patch_thickness(wid: int, body: ThicknessIn, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    w = db.get(Wall, wid)
    if w is None:
        raise HTTPException(404, "wall not found")
    try:
        svc.set_wall_thickness(db, storage, w, body.thicknessM, body.method, body.note, "api")
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"id": w.id, "thicknessM": w.thickness_m, "method": w.thickness_method, "reviewStatus": w.review_status}


# ── CAD 카탈로그·치환 ──

@router.post("/cad-catalog", status_code=201)
async def add_catalog(meta: str = Form(...), model: UploadFile = File(...), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        m = json.loads(meta)
        row = svc.register_catalog(db, storage, str(m["code"]), m.get("name", ""), m.get("family", ""), await model.read(), m.get("ifcGuid"), m.get("symmetryDeg"),
                                   m.get("revision", "R1"))
    except (KeyError, json.JSONDecodeError) as e:
        raise HTTPException(422, f"meta 오류: {e}") from e
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"id": row.id, "code": row.code, "dimsM": row.dims_m, "revision": row.revision}


@router.get("/cad-catalog")
def list_catalog(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": c.id, "code": c.code, "name": c.name, "family": c.family, "dimsM": c.dims_m, "revision": c.revision, "symmetryDeg": c.symmetry}
            for c in db.scalars(select(CadCatalog).order_by(CadCatalog.code)).all()]


def _asset(db: Session, aid: str) -> Asset:
    a = db.get(Asset, aid)
    if a is None:
        raise HTTPException(404, f"asset not found: {aid}")
    return a


@router.post("/assets/{aid}/cad-replacements/candidates")
def cad_candidates(aid: str, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        cands = svc.cad_candidates(db, storage, _asset(db, aid), "api")
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"assetId": aid, "candidates": cands, "autoApprove": False,
            "note": "후보는 REVIEW_REQUIRED — 관리자가 승인해야 치환 표시 레이어에 반영됩니다. UNDETERMINED는 승인할 수 없습니다."}


class ApproveIn(BaseModel):
    comment: str = Field(default="", max_length=300)
    acknowledge: bool = False  # UNDETERMINED 후보를 사람이 확인하고 승인할 때


@router.post("/assets/{aid}/cad-replacements/{rid}/approve")
def cad_approve(aid: str, rid: int, request: Request, body: ApproveIn | None = None, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        out = svc.approve_replacement(db, storage, _asset(db, aid), rid, "api:" + request.headers.get("x-actor", "api-key"), (body.comment if body else ""), bool(body and body.acknowledge))
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


@router.delete("/assets/{aid}/cad-replacements/{rid}")
def cad_revert(aid: str, rid: int, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.revert_replacement(db, _asset(db, aid), rid, "api:" + request.headers.get("x-actor", "api-key"))
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return out


# ── 투어 ──

@router.get("/buildings/{bid}/tour-manifest")
def tour_manifest(bid: int, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    b = db.get(Building, bid)
    if b is None:
        raise HTTPException(404, "building not found")
    return svc.tour_manifest(db, storage, b, api_url)


@router.get("/tours/{tid}/scene")
def tour_scene(tid: str, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    t = db.get(TourScene, tid)
    if t is None:
        raise HTTPException(404, "tour not found")
    return svc.tour_manifest(db, storage, db.get(Building, t.building_id), api_url)


class PortalIn(BaseModel):
    fromSpace: str | None = None
    toSpace: str | None = None
    positionBuilding: list[float]
    yawDeg: float = 0.0
    widthM: float = Field(default=0.9, gt=0.2, le=6)
    heightM: float = Field(default=2.1, gt=0.5, le=6)
    name: str = Field(default="", max_length=100)
    kind: str = "DOOR"


@router.post("/tours/{tid}/portals", status_code=201)
def add_portal(tid: str, body: PortalIn, db: Session = Depends(get_db)) -> dict:
    t = db.get(TourScene, tid)
    if t is None:
        raise HTTPException(404, "tour not found")
    b = db.get(Building, t.building_id)
    try:
        c = svc.create_portal(db, b, body.fromSpace, body.toSpace, body.positionBuilding, body.yawDeg, body.widthM, body.heightM, body.kind, body.name, t)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    code_of = {s.id: s.code for s in db.scalars(select(Space)).all()}
    return svc.connector_json(c, code_of)


# ── 파일 ──

def twin_file_path(db: Session, storage: FileStorage, kind: str, ident: str):
    if kind in FILE_KINDS:
        rev = db.get(MeshRevision, ident)
        uri = rev.files.get(kind) if rev else None
    elif kind == "envelope":
        b = db.get(Building, int(ident)) if ident.isdigit() else None
        uri = b.envelope_uri if b else None
    elif kind == "design":
        g = db.get(SpaceGeometry, int(ident)) if ident.isdigit() else None
        uri = g.uri if g and g.kind == "DESIGN" else None
    elif kind == "replacement":
        r = db.get(CadReplacement, int(ident)) if ident.isdigit() else None
        uri = f"twin/replacement/{r.id}.glb" if r and r.status == "APPROVED" else None
    else:
        uri = None
    if not uri:
        raise HTTPException(404, "file not found")
    p = storage.open_path(uri)
    if not p.exists():
        raise HTTPException(404, "file missing")
    return p


@router.get("/twin/files/{kind}/{ident}.glb")
def twin_file(kind: str, ident: str, db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)):
    return FileResponse(twin_file_path(db, storage, kind, ident), media_type="model/gltf-binary")


@router.post("/spaces/{ref}/design-geometry", status_code=201)
async def add_design(ref: str, meta: str = Form("{}"), model: UploadFile = File(...), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    """설계(BIM/CAD) 지오메트리 GLB를 공간에 붙인다 — DESIGN 레이어, REVIEW_REQUIRED로 시작."""
    try:
        m = json.loads(meta)
        g = svc.add_design_geometry(db, storage, svc.resolve_space(db, ref), await model.read(), str(m.get("designRevision", "R1")), "api", m.get("ifcGuid"))
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"meta 오류: {e}") from e
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"id": g.id, "sourceKind": "DESIGN", "reviewStatus": g.review_status, "designRevision": g.design_revision, "url": api_url("design", str(g.id))}
