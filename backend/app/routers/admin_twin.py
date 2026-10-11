"""관리자 웹용 디지털 트윈 API (`/admin/api/twin`) — 세션 쿠키 인증, 읽기는 로그인 사용자, 수정·승인은 현장관리자 이상.

모든 변경은 감사 로그(audit_logs)와 model_approvals에 남는다. JSON 모양은 docs/INTERFACES.md §6 (`/scene` = tour manifest).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import auth
from ..auth import current_user, require_role
from ..deps import get_db, get_storage
from ..models import Asset, Space, User
from ..models_twin import (
    Building, CadCatalog, CadReplacement, Floor, MeshRevision, Opening, ScanSession, SpaceConnector, SpaceGeometry, TourScene, Wall,
)
from ..services import twin as svc
from ..services.twin import TwinError
from ..storage import FileStorage
from .twin import FILE_KINDS, twin_file_path

router = APIRouter(prefix="/admin/api/twin", tags=["admin-twin"])


def admin_url(kind: str, ident: str) -> str:
    return f"/admin/api/twin/files/{kind}/{ident}.glb"


def _err(e: TwinError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


@router.get("/buildings")
def buildings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    out = []
    for b in db.scalars(select(Building).order_by(Building.id)).all():
        floors = []
        spaces = db.scalars(select(Space).where(Space.building_id == b.id)).all()
        for f in db.scalars(select(Floor).where(Floor.building_id == b.id).order_by(Floor.elevation_m)).all():
            rows = []
            for s in [x for x in spaces if x.floor_id == f.id]:
                rev = svc.current_revision(db, s.id)
                rows.append({"spaceId": s.code, "numericId": s.id, "name": s.name or s.code, "status": rev.status if rev else "NO_SCAN", "hasMesh": rev is not None,
                             "coverage": rev.coverage if rev else 0.0, "quality": rev.quality if rev else None,
                             "reviewStatus": rev.review_status if rev else None})
            floors.append({"id": f.id, "level": f.level, "elevationM": f.elevation_m, "heightM": f.height_m, "spaces": rows})
        ents = db.scalars(select(SpaceConnector).where(SpaceConnector.building_id == b.id, SpaceConnector.kind == "ENTRANCE")).all()
        out.append({"id": b.id, "name": b.name, "projectCode": b.project_code, "floors": floors, "hasEnvelope": bool(b.envelope_uri), "entranceCount": len(ents)})
    return out


@router.get("/buildings/{bid}/scene")
def scene(bid: int, user: User = Depends(current_user), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    b = db.get(Building, bid)
    if b is None:
        raise HTTPException(404, "building not found")
    return svc.tour_manifest(db, storage, b, admin_url)


@router.get("/spaces/{ref}/revisions")
def revisions(ref: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    try:
        return svc.revisions_info(db, svc.resolve_space(db, ref), admin_url)
    except TwinError as e:
        raise _err(e) from e


@router.get("/spaces/{ref}/coverage")
def coverage(ref: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.coverage_info(db, svc.resolve_space(db, ref), admin_url)
    except TwinError as e:
        raise _err(e) from e


@router.get("/spaces/{ref}/sessions")
def sessions(ref: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    from .twin import session_json

    try:
        sp = svc.resolve_space(db, ref)
    except TwinError as e:
        raise _err(e) from e
    return [session_json(s) | {"mode": s.mode, "deviceId": s.device_id} for s in db.scalars(select(ScanSession).where(ScanSession.space_id == sp.id).order_by(ScanSession.created_at.desc())).all()]


@router.get("/files/{kind}/{ident}.glb")
def files(kind: str, ident: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)):
    return FileResponse(twin_file_path(db, storage, kind, ident), media_type="model/gltf-binary", headers={"Cache-Control": "private, max-age=300"})


# ── 수정·승인 (현장관리자 이상) ──

class ThicknessIn(BaseModel):
    thicknessM: float = Field(ge=0.03, le=2.0)
    method: str = "MEASURED"
    note: str = Field(default="", max_length=300)


@router.patch("/walls/{wid}/thickness")
def wall_thickness(wid: int, body: ThicknessIn, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db),
                   storage: FileStorage = Depends(get_storage)) -> dict:
    w = db.get(Wall, wid)
    if w is None:
        raise HTTPException(404, "wall not found")
    try:
        svc.set_wall_thickness(db, storage, w, body.thicknessM, body.method, body.note, user.username)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.wall_thickness", "wall", str(wid), {"thicknessM": body.thicknessM, "method": body.method}, auth.client_ip(request))
    db.commit()
    return {"id": w.id, "thicknessM": w.thickness_m, "thicknessMethod": w.thickness_method, "confidence": w.confidence, "reviewStatus": w.review_status}


class ReviewIn(BaseModel):
    reviewStatus: str
    comment: str = Field(default="", max_length=300)


@router.post("/walls/{wid}/review")
def wall_review(wid: int, body: ReviewIn, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    w = db.get(Wall, wid)
    if w is None:
        raise HTTPException(404, "wall not found")
    try:
        svc.set_review(db, "wall", w, body.reviewStatus, user.username, body.comment)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.wall_review", "wall", str(wid), {"reviewStatus": body.reviewStatus}, auth.client_ip(request))
    db.commit()
    return {"id": w.id, "reviewStatus": w.review_status}


class GenIn(BaseModel):
    defaultThicknessM: float | None = Field(default=None, ge=0.03, le=2.0)
    buildingType: str | None = None


@router.post("/buildings/{bid}/generate-envelope")
def generate(bid: int, request: Request, body: GenIn | None = None, user: User = Depends(require_role("manager")), db: Session = Depends(get_db),
             storage: FileStorage = Depends(get_storage)) -> dict:
    b = db.get(Building, bid)
    if b is None:
        raise HTTPException(404, "building not found")
    body = body or GenIn()
    try:
        out = svc.generate_building_envelope(db, storage, b, body.defaultThicknessM, body.buildingType, user.username)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.envelope", "building", str(bid), out, auth.client_ip(request))
    db.commit()
    return out


class PortalIn(BaseModel):
    fromSpace: str | None = None
    toSpace: str | None = None
    positionBuilding: list[float]
    yawDeg: float = 0.0
    widthM: float = Field(default=0.9, gt=0.2, le=6)
    heightM: float = Field(default=2.1, gt=0.5, le=6)
    name: str = Field(default="", max_length=100)
    kind: str = "DOOR"


@router.post("/portals", status_code=201)
def add_portal(body: PortalIn, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    try:
        ref = body.fromSpace or body.toSpace
        if not ref:
            raise TwinError("fromSpace 또는 toSpace가 필요합니다 (건물을 정하는 데 사용)")
        sp = svc.resolve_space(db, ref)
        if sp.building_id is None:
            raise TwinError(f"공간 {sp.code}이(가) 건물에 배치되지 않았습니다", 409)
        b = db.get(Building, sp.building_id)
        tour = db.scalar(select(TourScene).where(TourScene.building_id == b.id))
        c = svc.create_portal(db, b, body.fromSpace, body.toSpace, body.positionBuilding, body.yawDeg, body.widthM, body.heightM, body.kind, body.name, tour)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.portal_create", "portal", c.id, {"from": body.fromSpace, "to": body.toSpace}, auth.client_ip(request))
    db.commit()
    return svc.connector_json(c, {s.id: s.code for s in db.scalars(select(Space)).all()})


class PortalPatch(BaseModel):
    positionBuilding: list[float] | None = None
    yawDeg: float | None = None
    widthM: float | None = Field(default=None, gt=0.2, le=6)
    heightM: float | None = Field(default=None, gt=0.5, le=6)
    state: str | None = Field(default=None, pattern="^(OPEN|CLOSED|UNKNOWN)$")
    reviewStatus: str | None = None
    name: str | None = Field(default=None, max_length=100)


@router.patch("/portals/{pid}")
def patch_portal(pid: str, body: PortalPatch, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    c = db.get(SpaceConnector, pid)
    if c is None:
        raise HTTPException(404, "portal not found")
    if body.positionBuilding is not None:
        if len(body.positionBuilding) != 3:
            raise HTTPException(422, "positionBuilding은 [x,y,z]")
        c.position = [float(x) for x in body.positionBuilding]
    for src, dst in (("yawDeg", "yaw_deg"), ("widthM", "width_m"), ("heightM", "height_m"), ("state", "state"), ("name", "name")):
        v = getattr(body, src)
        if v is not None:
            setattr(c, dst, v)
    if body.reviewStatus is not None:
        try:
            svc.set_review(db, "portal", c, body.reviewStatus, user.username)
        except TwinError as e:
            raise _err(e) from e
    elif body.positionBuilding is not None:
        c.review_status = "REVIEW_REQUIRED"  # 위치가 바뀌면 다시 확인
    auth.audit(db, user.username, "twin.portal_update", "portal", pid, body.model_dump(exclude_none=True), auth.client_ip(request))
    db.commit()
    return svc.connector_json(c, {s.id: s.code for s in db.scalars(select(Space)).all()})


@router.post("/revisions/{rid}/review")
def revision_review(rid: str, body: ReviewIn, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    r = db.get(MeshRevision, rid)
    if r is None:
        raise HTTPException(404, "revision not found")
    try:
        svc.set_review(db, "revision", r, body.reviewStatus, user.username, body.comment)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.revision_review", "revision", rid, {"reviewStatus": body.reviewStatus}, auth.client_ip(request))
    db.commit()
    return {"revisionId": r.id, "reviewStatus": r.review_status}


@router.post("/geometry/{gid}/review")
def geometry_review(gid: int, body: ReviewIn, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    g = db.get(SpaceGeometry, gid)
    if g is None:
        raise HTTPException(404, "geometry not found")
    try:
        svc.set_review(db, "design_geometry", g, body.reviewStatus, user.username, body.comment)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.geometry_review", "geometry", str(gid), {"reviewStatus": body.reviewStatus}, auth.client_ip(request))
    db.commit()
    return {"id": g.id, "reviewStatus": g.review_status}


@router.post("/revisions/{rid}/make-current")
def make_current(rid: str, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    """이전 리비전으로 되돌리기 — 기존 리비전은 지워지지 않으므로 언제든 복원할 수 있다."""
    r = db.get(MeshRevision, rid)
    if r is None:
        raise HTTPException(404, "revision not found")
    for o in db.scalars(select(MeshRevision).where(MeshRevision.space_id == r.space_id, MeshRevision.is_current.is_(True))).all():
        o.is_current = False
    r.is_current = True
    auth.audit(db, user.username, "twin.revision_restore", "revision", rid, {}, auth.client_ip(request))
    db.commit()
    return {"revisionId": r.id, "current": True}


# ── CAD 치환 (관리자 승인) ──

def _asset(db: Session, aid: str) -> Asset:
    a = db.get(Asset, aid)
    if a is None:
        raise HTTPException(404, f"asset not found: {aid}")
    return a


@router.get("/cad-catalog")
def catalog(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": c.id, "code": c.code, "name": c.name, "dimsM": c.dims_m, "revision": c.revision} for c in db.scalars(select(CadCatalog)).all()]


@router.get("/assets/{aid}/cad")
def asset_cad(aid: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(CadReplacement).where(CadReplacement.asset_id == aid).order_by(CadReplacement.created_at.desc())).all()
    cat = {c.id: c.code for c in db.scalars(select(CadCatalog)).all()}
    return {"assetId": aid, "replacements": [{"id": r.id, "catalogCode": cat.get(r.catalog_id), "status": r.status, "reviewStatus": r.review_status, "fitness": r.fitness,
                                              "rmseMm": round(r.rmse_m * 1000, 1), "dimErrorPct": round((r.dim_error_m or 0) * 100, 1), "symmetryAmbiguous": r.symmetry_ambiguous,
                                              "note": r.note, "approvedBy": r.approved_by} for r in rows]}


@router.post("/assets/{aid}/cad/candidates")
def cad_candidates(aid: str, user: User = Depends(require_role("manager")), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        c = svc.cad_candidates(db, storage, _asset(db, aid), user.username)
    except TwinError as e:
        raise _err(e) from e
    db.commit()
    return {"assetId": aid, "candidates": c}


class ApproveIn(BaseModel):
    comment: str = Field(default="", max_length=300)
    acknowledge: bool = False


@router.post("/assets/{aid}/cad/{rid}/approve")
def cad_approve(aid: str, rid: int, request: Request, body: ApproveIn | None = None, user: User = Depends(require_role("manager")), db: Session = Depends(get_db), storage: FileStorage = Depends(get_storage)) -> dict:
    try:
        out = svc.approve_replacement(db, storage, _asset(db, aid), rid, user.username, (body.comment if body else ""), bool(body and body.acknowledge))
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.cad_approve", "asset", aid, {"replacementId": rid}, auth.client_ip(request))
    db.commit()
    return out


@router.post("/assets/{aid}/cad/{rid}/revert")
def cad_revert(aid: str, rid: int, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.revert_replacement(db, _asset(db, aid), rid, user.username)
    except TwinError as e:
        raise _err(e) from e
    auth.audit(db, user.username, "twin.cad_revert", "asset", aid, {"replacementId": rid}, auth.client_ip(request))
    db.commit()
    return out
