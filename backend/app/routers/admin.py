"""통합 백오피스 API (/admin/api/*) — 세션 쿠키 인증, 역할 기반 권한.

화면: 대시보드 · 자산 관리(3D) · 검수 이력/리뷰 · 이슈(조치 워크플로우) · 공간 · 단말기 · 사용자 · 감사 로그.
단말(앱)용 /api/v1 은 API 키, 백오피스 /admin/api 는 로그인 세션 — 인증 체계가 서로 독립이다.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth
from ..auth import current_user, require_role
from ..deps import get_db, get_storage
from ..models import (
    Asset, AuditLog, AuthSession, InspectionResult, Issue, IssueEvent, Observation, Space, User,
)
from ..services import issues as wf
from ..services.assets import (
    DEFAULT_INSPECT_VIEW,
    INSPECT_VIEW_KEYS,
    asset_outline,
    delete_assets,
    effective_inspect_view,
    list_photos,
    model_ok,
    photo_path,
)

router = APIRouter(prefix="/admin/api", tags=["admin"])
_STARTED = time.time()

DECISIONS = ("PASS", "FAIL", "REVIEW", "RETAKE")


# ───────────────────────── 공통 ─────────────────────────

def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def local_date(dt: datetime) -> date:
    return _aware(dt).astimezone().date()


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "displayName": u.display_name or u.username, "role": u.role,
            "roleLabel": auth.ROLE_LABEL.get(u.role, u.role), "active": u.active, "mustChangePassword": bool(u.must_change_password) and auth.enforce_pw_change(),
            "createdAt": iso(u.created_at), "lastLoginAt": iso(u.last_login_at)}


def _size_cm(aabb: dict | None) -> list[float] | None:
    try:
        mn, mx = aabb["minM"], aabb["maxM"]  # type: ignore[index]
        return [round((mx[i] - mn[i]) * 100, 1) for i in range(3)]
    except (KeyError, TypeError, IndexError):
        return None


# ───────────────────────── 인증 ─────────────────────────

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip = auth.client_ip(request)
    auth.throttle_check(ip, body.username)
    user = db.scalar(select(User).where(User.username == body.username))
    ok = user is not None and user.active and auth.verify_password(body.password, user.pw_salt, user.pw_hash)
    if not ok:
        auth.throttle_fail(ip, body.username)
        auth.audit(db, body.username, "login.fail", "user", body.username, ip=ip)
        db.commit()
        raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다")
    auth.throttle_ok(ip, body.username)
    token = auth.create_session(db, user, request)
    auth.audit(db, user.username, "login", "user", user.username, ip=ip)
    db.commit()
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True,
                        samesite="strict", secure=auth.is_https(request), path="/")
    return {"user": user_out(user)}


@router.post("/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    tok = getattr(request.state, "session_token", None)
    if tok and (s := db.get(AuthSession, tok)):
        db.delete(s)
    auth.audit(db, user.username, "logout", "user", user.username, ip=auth.client_ip(request))
    db.commit()
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/session")
def session_info(request: Request, db: Session = Depends(get_db)) -> dict:
    """로그인 여부 확인 (미로그인이어도 200 — 브라우저 콘솔에 401 오류가 쌓이지 않게)."""
    try:
        return {"user": user_out(current_user(request, db))}
    except HTTPException:
        return {"user": None}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"user": user_out(user), "permissions": {
        "editAssets": auth.ROLE_RANK[user.role] >= auth.ROLE_RANK["manager"],
        "deleteAssets": user.role == "admin",
        "manageUsers": user.role == "admin",
        "viewAudit": auth.ROLE_RANK[user.role] >= auth.ROLE_RANK["manager"],
        "createIssues": auth.ROLE_RANK[user.role] >= auth.ROLE_RANK["inspector"],
    }}


class PwIn(BaseModel):
    current: str
    new: str = Field(max_length=200)


@router.post("/me/password")
def change_password(body: PwIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if not auth.verify_password(body.current, user.pw_salt, user.pw_hash):
        raise HTTPException(status_code=403, detail="현재 비밀번호가 올바르지 않습니다")
    if body.new == body.current:
        raise HTTPException(status_code=422, detail="새 비밀번호가 현재 비밀번호와 같습니다")
    auth.require_strong(body.new, user.username)
    user.pw_salt, user.pw_hash = auth.hash_password(body.new)
    user.must_change_password = False
    auth.revoke_sessions(db, user.id, except_token=getattr(request.state, "session_token", None))  # 다른 기기는 로그아웃
    auth.audit(db, user.username, "password.change", "user", user.username, ip=auth.client_ip(request))
    db.commit()
    return {"ok": True}


# ───────────────────────── 대시보드 ─────────────────────────

@router.get("/dashboard")
def dashboard(days: int = 14, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    days = max(3, min(days, 90))
    today = datetime.now().astimezone().date()
    start = today - timedelta(days=days - 1)

    assets = db.scalars(select(Asset)).all()
    obs_all = db.scalars(select(Observation).order_by(Observation.created_at.desc())).all()
    issues = db.scalars(select(Issue)).all()
    names = {a.asset_id: (a.name or a.asset_id) for a in assets}

    dec = Counter(o.decision or "UNKNOWN" for o in obs_all)
    judged = dec["PASS"] + dec["FAIL"] + dec["REVIEW"]
    series = {(start + timedelta(days=i)).isoformat(): {d: 0 for d in DECISIONS} for i in range(days)}
    today_n = 0
    for o in obs_all:
        d = local_date(o.created_at)
        if d == today:
            today_n += 1
        if d.isoformat() in series and (o.decision or "") in DECISIONS:
            series[d.isoformat()][o.decision] += 1

    open_issues = [i for i in issues if i.status in wf.OPEN_STATUSES]
    overdue = [i for i in open_issues if i.due_date and i.due_date < today.isoformat()]
    fails = Counter(o.asset_id for o in obs_all if o.decision == "FAIL")
    devices = {d for d in [a.device_id for a in assets] + [o.device_id for o in obs_all] if d}

    def recent_issue(i: Issue) -> dict:
        return {"id": i.id, "code": wf.code(i), "title": i.title, "severity": i.severity, "status": i.status,
                "statusLabel": wf.STATUS_LABEL[i.status], "assetId": i.asset_id, "assignee": i.assignee,
                "createdAt": iso(i.created_at)}

    out = {
        "kpi": {
            "assets": len(assets), "activeAssets": sum(1 for a in assets if a.status == "ACTIVE"),
            "spaces": db.scalar(select(func.count()).select_from(Space)) or 0, "devices": len(devices),
            "inspections": len(obs_all), "inspectionsToday": today_n,
            "passRate": round(100 * dec["PASS"] / judged, 1) if judged else None,
            "openIssues": len(open_issues), "overdueIssues": len(overdue),
            "criticalIssues": sum(1 for i in open_issues if i.severity in ("HIGH", "CRITICAL")),
        },
        "decisions": {d: dec.get(d, 0) for d in DECISIONS},
        "daily": [{"date": k, **v} for k, v in series.items()],
        "issuesByStatus": {s: sum(1 for i in issues if i.status == s) for s in wf.STATUS_LABEL},
        "issuesBySeverity": {s: sum(1 for i in open_issues if i.severity == s) for s in wf.SEVERITIES},
        "topFailAssets": [{"assetId": a, "name": names.get(a, a), "fails": n} for a, n in fails.most_common(5)],
        "recentInspections": [{
            "id": o.id, "assetId": o.asset_id, "assetName": names.get(o.asset_id, o.asset_id), "decision": o.decision,
            "deviceId": o.device_id, "createdAt": iso(o.created_at),
        } for o in obs_all[:8]],
        "recentIssues": [recent_issue(i) for i in sorted(issues, key=lambda x: x.updated_at, reverse=True)[:6]],
    }
    if auth.ROLE_RANK[user.role] >= auth.ROLE_RANK["manager"]:
        out["recentAudit"] = [{"at": iso(a.at), "actor": a.actor, "action": a.action, "target": f"{a.target_type}:{a.target_id}"}
                              for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(6)).all()]
    return out


# ───────────────────────── 자산 ─────────────────────────

class TolIn(BaseModel):
    positionMm: float = Field(gt=0, le=500)
    zToleranceMm: float | None = Field(default=None, gt=0, le=500)
    rotationDeg: float = Field(ge=0, le=45)


class AssetPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    family: str | None = Field(default=None, max_length=128)
    type: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    status: Literal["ACTIVE", "ARCHIVED"] | None = None
    tolerance: TolIn | None = None
    inspectView: dict[str, bool] | None = None


class InspectViewBulkIn(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=1000)  # 비우면 전체 자산
    inspectView: dict[str, bool]


class BulkIn(BaseModel):
    action: Literal["delete", "archive", "activate"]
    ids: list[str] = Field(min_length=1, max_length=500)


def _asset_stats(db: Session) -> tuple[dict, dict, dict]:
    obs_by: dict[str, list[Observation]] = defaultdict(list)
    for o in db.scalars(select(Observation).order_by(Observation.created_at)).all():
        obs_by[o.asset_id].append(o)
    open_by: Counter = Counter(i.asset_id for i in db.scalars(select(Issue).where(Issue.status.in_(wf.OPEN_STATUSES))).all() if i.asset_id)
    spaces = {s.id: s for s in db.scalars(select(Space)).all()}
    return obs_by, open_by, spaces


def _asset_row(a: Asset, obs: list[Observation], open_issues: int, spaces: dict, storage) -> dict:
    last = obs[-1] if obs else None
    sp = spaces.get(a.space_id)
    return {
        "assetId": a.asset_id, "name": a.name or a.asset_id, "family": a.family, "type": a.asset_type,
        "status": a.status or "ACTIVE", "spaceId": a.space_id, "spaceCode": sp.code if sp else None,
        "spaceName": sp.name if sp else None, "deviceId": a.device_id, "sizeCm": _size_cm(a.aabb),
        "tolerance": a.tolerance, "bimRevision": a.bim_revision, "notes": a.notes or "",
        "photos": len(list_photos(storage, a.asset_id)), "createdAt": iso(a.created_at), "updatedAt": iso(a.updated_at),
        "scannedAt": iso(a.scanned_at or a.created_at), "geo": a.geo, "inspectView": effective_inspect_view(a),
        "modelOk": model_ok(storage, a),
        "inspections": len(obs), "lastDecision": last.decision if last else None, "lastInspectedAt": iso(last.created_at) if last else None,
        "fails": sum(1 for o in obs if o.decision == "FAIL"), "openIssues": open_issues,
        "hasModel": a.cad_uri is not None,
    }


@router.get("/assets")
def list_assets(q: str = "", space: int | None = None, status: str = "", decision: str = "", sort: str = "-created",
                user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    obs_by, open_by, spaces = _asset_stats(db)
    rows = [_asset_row(a, obs_by.get(a.asset_id, []), open_by.get(a.asset_id, 0), spaces, storage)
            for a in db.scalars(select(Asset)).all()]
    ql = q.strip().lower()
    if ql:
        rows = [r for r in rows if ql in (r["assetId"] + " " + r["name"] + " " + (r["family"] or "") + " " + (r["deviceId"] or "")).lower()]
    if space is not None:
        rows = [r for r in rows if r["spaceId"] == space]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if decision:
        rows = [r for r in rows if r["lastDecision"] == decision]
    keymap = {"created": lambda r: r["createdAt"] or "", "name": lambda r: r["name"].lower(),
              "fails": lambda r: r["fails"], "inspected": lambda r: r["lastInspectedAt"] or ""}
    key = sort.lstrip("-")
    rows.sort(key=keymap.get(key, keymap["created"]), reverse=sort.startswith("-"))
    return {"total": len(rows), "assets": rows}


def _get_asset(db: Session, asset_id: str) -> Asset:
    a = db.get(Asset, asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return a


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    a = _get_asset(db, asset_id)
    obs_by, open_by, spaces = _asset_stats(db)
    row = _asset_row(a, obs_by.get(asset_id, []), open_by.get(asset_id, 0), spaces, storage)
    recent = [{"id": o.id, "decision": o.decision, "confidence": o.confidence, "deviceId": o.device_id,
               "createdAt": iso(o.created_at), "matchedBy": ((o.extra or {}).get("icp") or {}).get("matchedBy")}
              for o in reversed(obs_by.get(asset_id, [])[-15:])]
    issues = db.scalars(select(Issue).where(Issue.asset_id == asset_id).order_by(Issue.id.desc()).limit(15)).all()
    return {**row, "designPose": a.design_pose, "aabb": a.aabb, "photoCards": list_photos(storage, asset_id),
            "recentInspections": recent,
            "issues": [{"id": i.id, "code": wf.code(i), "title": i.title, "severity": i.severity, "status": i.status,
                        "statusLabel": wf.STATUS_LABEL[i.status], "createdAt": iso(i.created_at)} for i in issues]}


@router.patch("/assets/{asset_id}")
def patch_asset(asset_id: str, body: AssetPatch, request: Request, user: User = Depends(require_role("manager")),
                db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    a = _get_asset(db, asset_id)
    changes: dict = {}
    for field, attr in (("name", "name"), ("family", "family"), ("type", "asset_type"), ("notes", "notes"), ("status", "status")):
        v = getattr(body, field)
        if v is not None and v != (getattr(a, attr) or ""):
            changes[field] = [getattr(a, attr), v]
            setattr(a, attr, v)
    if body.tolerance is not None:
        new = body.tolerance.model_dump(exclude_none=True)
        if new != (a.tolerance or {}):
            changes["tolerance"] = [a.tolerance, new]
            a.tolerance = new
    if body.inspectView is not None:
        new_view = _clean_inspect_view(body.inspectView)
        if new_view != effective_inspect_view(a):
            changes["inspectView"] = [effective_inspect_view(a), new_view]
            a.inspect_view = new_view
    if changes:
        a.updated_at = datetime.now(timezone.utc)
        auth.audit(db, user.username, "asset.update", "asset", asset_id, {"changes": changes}, auth.client_ip(request))
        db.commit()
    return get_asset(asset_id, user, db, storage)


def _clean_inspect_view(v: dict[str, bool]) -> dict:
    unknown = [k for k in v if k not in INSPECT_VIEW_KEYS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"알 수 없는 표시 항목: {', '.join(unknown)} (허용: {', '.join(INSPECT_VIEW_KEYS)})")
    return {k: bool(v.get(k, DEFAULT_INSPECT_VIEW[k])) for k in INSPECT_VIEW_KEYS}


@router.post("/assets/inspect-view")
def bulk_inspect_view(body: InspectViewBulkIn, request: Request, user: User = Depends(require_role("manager")),
                      db: Session = Depends(get_db)) -> dict:
    """검수 표시 설정을 여러(또는 전체) 자산에 한 번에 적용한다."""
    view = _clean_inspect_view(body.inspectView)
    q = select(Asset) if body.ids is None else select(Asset).where(Asset.asset_id.in_(body.ids))
    assets = db.scalars(q).all()
    now = datetime.now(timezone.utc)
    for a in assets:
        a.inspect_view = dict(view)
        a.updated_at = now
    auth.audit(db, user.username, "asset.bulk_inspect_view", "asset", f"{len(assets)}개", {"inspectView": view, "all": body.ids is None},
               auth.client_ip(request))
    db.commit()
    return {"updated": len(assets), "inspectView": view}


@router.delete("/assets/{asset_id}")
def remove_asset(asset_id: str, request: Request, user: User = Depends(require_role("admin")),
                 db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    deleted, _ = delete_assets(db, storage, [asset_id])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    auth.audit(db, user.username, "asset.delete", "asset", asset_id, ip=auth.client_ip(request))
    db.commit()
    return {"deleted": deleted}


@router.post("/assets/bulk")
def bulk_assets(body: BulkIn, request: Request, user: User = Depends(require_role("manager")),
                db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    ip = auth.client_ip(request)
    if body.action == "delete":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="삭제는 통합관리자만 할 수 있습니다")
        deleted, missing = delete_assets(db, storage, body.ids)
        auth.audit(db, user.username, "asset.bulk_delete", "asset", ",".join(deleted)[:60], {"ids": deleted, "notFound": missing}, ip)
        db.commit()
        return {"done": deleted, "notFound": missing}
    status = "ARCHIVED" if body.action == "archive" else "ACTIVE"
    done, missing = [], []
    for aid in dict.fromkeys(body.ids):
        a = db.get(Asset, aid)
        if a is None:
            missing.append(aid)
            continue
        a.status, a.updated_at = status, datetime.now(timezone.utc)
        done.append(aid)
    auth.audit(db, user.username, f"asset.bulk_{body.action}", "asset", ",".join(done)[:60], {"ids": done}, ip)
    db.commit()
    return {"done": done, "notFound": missing}


@router.get("/assets/{asset_id}/model.glb")
def asset_model(asset_id: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)):
    a = _get_asset(db, asset_id)
    if a.cad_uri is None:
        raise HTTPException(status_code=404, detail="모델이 없습니다")
    return FileResponse(storage.open_path(a.cad_uri), media_type="model/gltf-binary", filename=f"{asset_id}.glb")


@router.get("/assets/{asset_id}/outline")
def asset_outline_api(asset_id: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    a = _get_asset(db, asset_id)
    try:
        return asset_outline(storage, a)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/assets/{asset_id}/photos/{side}.png")
def asset_photo(asset_id: str, side: str, user: User = Depends(current_user), storage=Depends(get_storage)):
    p = photo_path(storage, asset_id, side)
    if p is None:
        raise HTTPException(status_code=404, detail="photo not found")
    return FileResponse(p, media_type="image/png")


# ───────────────────────── 검수 이력 / 리뷰 ─────────────────────────

def _obs_row(o: Observation, names: dict, res: InspectionResult | None, issue_by_obs: dict) -> dict:
    icp = (o.extra or {}).get("icp") or {}
    dev = o.deviation or {}
    return {
        "id": o.id, "createdAt": iso(o.created_at), "assetId": o.asset_id, "assetName": names.get(o.asset_id, o.asset_id),
        "decision": o.decision, "confidence": o.confidence, "deviceId": o.device_id,
        "matchedBy": icp.get("matchedBy"), "inlier": icp.get("inlierRatio"),
        "rmseMm": round(icp["rmseM"] * 1000, 1) if icp.get("rmseM") is not None else None,
        "distMm": dev.get("distMm"), "rotDeg": dev.get("maxRotDeg"), "restored": icp.get("worldMapRestored"),
        "reasons": list(res.reasons or []) if res else [], "issueId": issue_by_obs.get(o.id),
    }


@router.get("/inspections")
def list_inspections(decision: str = "", asset: str = "", device: str = "", q: str = "", date_from: str = "", date_to: str = "",
                     page: int = 1, size: int = 25, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    names = {a.asset_id: (a.name or a.asset_id) for a in db.scalars(select(Asset)).all()}
    results = {r.observation_id: r for r in db.scalars(select(InspectionResult)).all()}
    issue_by_obs = {i.observation_id: i.id for i in db.scalars(select(Issue)).all() if i.observation_id}
    rows = []
    for o in db.scalars(select(Observation).order_by(Observation.created_at.desc())).all():
        if decision and o.decision != decision:
            continue
        if asset and o.asset_id != asset:
            continue
        if device and o.device_id != device:
            continue
        d = local_date(o.created_at).isoformat()
        if (date_from and d < date_from) or (date_to and d > date_to):
            continue
        if q and q.lower() not in f"{o.asset_id} {names.get(o.asset_id, '')} {o.device_id}".lower():
            continue
        rows.append(o)
    total = len(rows)
    size = max(5, min(size, 100))
    page = max(1, page)
    sl = rows[(page - 1) * size: page * size]
    return {"total": total, "page": page, "size": size,
            "inspections": [_obs_row(o, names, results.get(o.id), issue_by_obs) for o in sl]}


@router.get("/inspections.csv")
def export_inspections(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    names = {a.asset_id: (a.name or a.asset_id) for a in db.scalars(select(Asset)).all()}
    results = {r.observation_id: r for r in db.scalars(select(InspectionResult)).all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["검수ID", "일시(UTC)", "자산ID", "자산명", "판정", "신뢰도", "단말", "매칭방식", "일치율", "잔차(mm)", "위치편차(mm)", "회전편차(°)", "판정근거"])
    for o in db.scalars(select(Observation).order_by(Observation.created_at.desc())).all():
        r = _obs_row(o, names, results.get(o.id), {})
        w.writerow([o.id, r["createdAt"], o.asset_id, r["assetName"], o.decision, o.confidence, o.device_id, r["matchedBy"] or "",
                    r["inlier"] if r["inlier"] is not None else "", r["rmseMm"] if r["rmseMm"] is not None else "",
                    r["distMm"] if r["distMm"] is not None else "", r["rotDeg"] if r["rotDeg"] is not None else "", " / ".join(r["reasons"])])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="inspections-{date.today().isoformat()}.csv"'})


def _get_obs(db: Session, oid: str) -> Observation:
    o = db.get(Observation, oid)
    if o is None:
        raise HTTPException(status_code=404, detail=f"inspection not found: {oid}")
    return o


@router.get("/inspections/{oid}")
def inspection_detail(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    o = _get_obs(db, oid)
    a = db.get(Asset, o.asset_id)
    res = db.scalar(select(InspectionResult).where(InspectionResult.observation_id == oid).order_by(InspectionResult.created_at.desc()))
    names = {o.asset_id: (a.name or a.asset_id) if a else o.asset_id}
    issues = db.scalars(select(Issue).where(Issue.observation_id == oid)).all()
    extra = o.extra or {}
    review_file = storage.root / "reviews" / f"{oid}.json"
    return {
        **_obs_row(o, names, res, {i.observation_id: i.id for i in issues}),
        "designPose": o.design_pose, "actualPose": o.actual_pose, "deviation": o.deviation, "uncertainty": o.uncertainty,
        "bimRevision": o.bim_revision, "margins": (res.margins if res else {}),
        "icp": extra.get("icp"), "match": extra.get("match"), "hasReview": review_file.exists(),
        "tolerance": a.tolerance if a else None,
        "asset": {"assetId": a.asset_id, "name": a.name or a.asset_id, "sizeCm": _size_cm(a.aabb), "status": a.status} if a else None,
        "issues": [{"id": i.id, "code": wf.code(i), "title": i.title, "status": i.status, "statusLabel": wf.STATUS_LABEL[i.status],
                    "severity": i.severity} for i in issues],
    }


@router.get("/inspections/{oid}/review")
def inspection_review(oid: str, user: User = Depends(current_user), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    """3D 리뷰 데이터: 관측 사물 외곽선·점, 맞춘 포즈. (단말이 보낸 점은 분리된 사물만 저장한다)"""
    _get_obs(db, oid)
    f = storage.root / "reviews" / f"{oid}.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="이 검수에는 3D 리뷰 데이터가 없습니다 (리뷰 기능 이전에 수행된 검수)")
    return json.loads(f.read_text(encoding="utf-8"))


# ───────────────────────── 이슈 (조치 워크플로우) ─────────────────────────

def _issue_row(i: Issue, names: dict, today: str) -> dict:
    overdue = bool(i.due_date and i.due_date < today and i.status in wf.OPEN_STATUSES)
    return {"id": i.id, "code": wf.code(i), "title": i.title, "severity": i.severity, "severityLabel": wf.SEVERITY_LABEL[i.severity],
            "status": i.status, "statusLabel": wf.STATUS_LABEL[i.status], "assetId": i.asset_id,
            "assetName": names.get(i.asset_id or "", i.asset_id), "assignee": i.assignee, "dueDate": i.due_date, "overdue": overdue,
            "source": i.source, "reporter": i.reporter, "observationId": i.observation_id, "createdAt": iso(i.created_at),
            "updatedAt": iso(i.updated_at), "closedAt": iso(i.closed_at)}


@router.get("/issues")
def list_issues(status: str = "", severity: str = "", assignee: str = "", asset: str = "", q: str = "", mine: bool = False,
                overdue: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    names = {a.asset_id: (a.name or a.asset_id) for a in db.scalars(select(Asset)).all()}
    today = datetime.now().astimezone().date().isoformat()
    rows = [_issue_row(i, names, today) for i in db.scalars(select(Issue).order_by(Issue.updated_at.desc())).all()]
    if status:
        wanted = set(status.split(","))
        rows = [r for r in rows if r["status"] in wanted]
    if severity:
        rows = [r for r in rows if r["severity"] == severity]
    if assignee:
        rows = [r for r in rows if r["assignee"] == assignee]
    if mine:
        rows = [r for r in rows if r["assignee"] == user.username or r["reporter"] == user.username]
    if asset:
        rows = [r for r in rows if r["assetId"] == asset]
    if overdue:
        rows = [r for r in rows if r["overdue"]]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in f'{r["code"]} {r["title"]} {r["assetId"]} {r["assetName"]}'.lower()]
    counts = Counter(r["status"] for r in rows)
    return {"total": len(rows), "issues": rows, "counts": {s: counts.get(s, 0) for s in wf.STATUS_LABEL}}


class IssueIn(BaseModel):
    assetId: str | None = None
    observationId: str | None = None
    title: str = Field(min_length=2, max_length=300)
    description: str = Field(default="", max_length=4000)
    severity: str = "MEDIUM"
    assignee: str | None = None
    dueDate: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class TransitionIn(BaseModel):
    to: str
    comment: str = Field(default="", max_length=2000)
    assignee: str | None = None
    dueDate: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    resolution: str | None = Field(default=None, max_length=4000)
    force: bool = False


class CommentIn(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class IssuePatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    severity: str | None = None
    assignee: str | None = None
    dueDate: str | None = Field(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")


def _wf_guard(fn):
    try:
        return fn()
    except wf.WorkflowError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def _issue_detail(db: Session, i: Issue, user: User) -> dict:
    names = {a.asset_id: (a.name or a.asset_id) for a in db.scalars(select(Asset)).all()}
    today = datetime.now().astimezone().date().isoformat()
    events = db.scalars(select(IssueEvent).where(IssueEvent.issue_id == i.id).order_by(IssueEvent.id)).all()
    obs = db.get(Observation, i.observation_id) if i.observation_id else None
    return {
        **_issue_row(i, names, today), "description": i.description, "resolution": i.resolution,
        "allowedTransitions": [{"to": t, "label": wf.STATUS_LABEL[t]} for t in wf.allowed_transitions(i, user)],
        "reinspectPass": wf._has_reinspect_pass(db, i),
        "events": [{"id": e.id, "at": iso(e.at), "actor": e.actor, "type": e.type, "comment": e.comment,
                    "from": e.from_status, "to": e.to_status,
                    "fromLabel": wf.STATUS_LABEL.get(e.from_status or ""), "toLabel": wf.STATUS_LABEL.get(e.to_status or ""),
                    "data": e.data} for e in events],
        "inspection": ({"id": obs.id, "decision": obs.decision, "createdAt": iso(obs.created_at), "deviceId": obs.device_id} if obs else None),
    }


@router.post("/issues", status_code=201)
def create_issue(body: IssueIn, request: Request, user: User = Depends(require_role("inspector")), db: Session = Depends(get_db)) -> dict:
    def go():
        i = wf.create_issue(db, asset_id=body.assetId, observation_id=body.observationId, title=body.title,
                            description=body.description, severity=body.severity, reporter=user.username,
                            assignee=body.assignee, due_date=body.dueDate)
        auth.audit(db, user.username, "issue.create", "issue", str(i.id), {"title": i.title}, auth.client_ip(request))
        db.commit()
        return i
    return _issue_detail(db, _wf_guard(go), user)


@router.get("/issues/{iid}")
def get_issue(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    i = db.get(Issue, iid)
    if i is None:
        raise HTTPException(status_code=404, detail="issue not found")
    return _issue_detail(db, i, user)


@router.post("/issues/{iid}/transition")
def issue_transition(iid: int, body: TransitionIn, request: Request, user: User = Depends(require_role("inspector")),
                     db: Session = Depends(get_db)) -> dict:
    i = db.get(Issue, iid)
    if i is None:
        raise HTTPException(status_code=404, detail="issue not found")

    def go():
        wf.transition(db, i, user, body.to, comment=body.comment, assignee=body.assignee, due_date=body.dueDate,
                      resolution=body.resolution, force=body.force)
        auth.audit(db, user.username, "issue.transition", "issue", str(iid), {"to": body.to}, auth.client_ip(request))
        db.commit()
    _wf_guard(go)
    return _issue_detail(db, i, user)


@router.post("/issues/{iid}/comment")
def issue_comment(iid: int, body: CommentIn, user: User = Depends(require_role("inspector")), db: Session = Depends(get_db)) -> dict:
    i = db.get(Issue, iid)
    if i is None:
        raise HTTPException(status_code=404, detail="issue not found")
    wf.add_event(db, i, user.username, "comment", comment=body.comment)
    db.commit()
    return _issue_detail(db, i, user)


@router.patch("/issues/{iid}")
def issue_patch(iid: int, body: IssuePatch, request: Request, user: User = Depends(require_role("inspector")),
                db: Session = Depends(get_db)) -> dict:
    i = db.get(Issue, iid)
    if i is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if (body.assignee is not None) and auth.ROLE_RANK[user.role] < auth.ROLE_RANK["manager"]:
        raise HTTPException(status_code=403, detail="담당자 지정은 현장관리자 이상만 할 수 있습니다")

    def go():
        wf.assign_or_edit(db, i, user, title=body.title, description=body.description, severity=body.severity,
                          assignee=body.assignee, due_date=body.dueDate)
        db.commit()
    _wf_guard(go)
    return _issue_detail(db, i, user)


# ───────────────────────── 검수 이력 삭제 ─────────────────────────

class InspectionBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@router.post("/inspections/bulk-delete")
def bulk_delete_inspections(body: InspectionBulkDelete, request: Request, user: User = Depends(require_role("admin")),
                            db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    """선택한 검수 기록 삭제(통합관리자만). 판정 결과·3D 리뷰 데이터까지 지우고,
    연결된 이슈는 남기되 검수 연결만 끊는다. 증거 원본 파일(원칙 13)은 보존한다."""
    from ..models import InspectionResult

    deleted: list[str] = []
    missing: list[str] = []
    for oid in dict.fromkeys(body.ids):
        o = db.get(Observation, oid)
        if o is None:
            missing.append(oid)
            continue
        for res in db.scalars(select(InspectionResult).where(InspectionResult.observation_id == oid)).all():
            db.delete(res)
        for i in db.scalars(select(Issue).where(Issue.observation_id == oid)).all():
            i.observation_id = None
            wf.add_event(db, i, user.username, "edit", comment="연결된 검수 기록이 삭제되어 검수 연결을 해제했습니다")
        (storage.root / "reviews" / f"{oid}.json").unlink(missing_ok=True)
        db.delete(o)
        deleted.append(oid)
    auth.audit(db, user.username, "inspection.bulk_delete", "inspection", f"{len(deleted)}건",
               {"ids": deleted, "notFound": missing}, auth.client_ip(request))
    db.commit()
    return {"deleted": deleted, "notFound": missing}


# ───────────────────────── 실시간 변경 이벤트 ─────────────────────────

@router.get("/events")
async def admin_events(request: Request):
    """백오피스 화면용 변경 이벤트(SSE) — 모바일에서 등록·검수한 내용이나 다른 관리자의 저장이 그 시점에 화면에 반영되게 한다."""
    from fastapi.responses import StreamingResponse

    from ..events import SSE_HEADERS, sse_stream

    with request.app.state.session_factory() as db:  # 인증만 하고 DB 세션은 바로 닫는다 (스트림 동안 연결을 잡지 않게)
        current_user(request, db)

    async def payload(scopes: set[str]) -> dict:
        return {"scopes": sorted(scopes)}

    return StreamingResponse(sse_stream(request, payload), media_type="text/event-stream", headers=SSE_HEADERS)


# ───────────────────────── 공간 · 단말기 ─────────────────────────

class SpacePatch(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=200)
    level: str | None = Field(default=None, max_length=64)


class SpaceCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)
    level: str = Field(default="", max_length=64)
    projectId: str | None = Field(default=None, max_length=64)


@router.get("/spaces")
def list_spaces(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    assets = db.scalars(select(Asset)).all()
    issues = db.scalars(select(Issue).where(Issue.status.in_(wf.OPEN_STATUSES))).all()
    obs_count: Counter = Counter()
    asset_space = {a.asset_id: a.space_id for a in assets}
    for o in db.scalars(select(Observation)).all():
        obs_count[asset_space.get(o.asset_id)] += 1
    out = []
    for s in db.scalars(select(Space).order_by(Space.id)).all():
        out.append({"id": s.id, "projectId": s.project_code, "code": s.code, "name": s.name, "level": s.level,
                    "hasWorldMap": s.worldmap_uri is not None, "createdAt": iso(s.created_at),
                    "assets": sum(1 for a in assets if a.space_id == s.id), "inspections": obs_count.get(s.id, 0),
                    "openIssues": sum(1 for i in issues if i.space_id == s.id)})
    return {"spaces": out}


@router.post("/spaces", status_code=201)
def create_space_admin(body: SpaceCreate, request: Request, user: User = Depends(require_role("manager")),
                       db: Session = Depends(get_db)) -> dict:
    """공간 추가 — 프로젝트를 안 주면 기존 공간이 쓰는 프로젝트(없으면 DEFAULT)에 만든다."""
    from ..services.assets import upsert_project

    project = body.projectId or (db.scalar(select(Space.project_code).order_by(Space.id)) or "DEFAULT")
    code = body.code.strip()
    if not code:
        raise HTTPException(status_code=422, detail="공간 ID를 입력하세요")
    if db.scalar(select(Space).where(Space.project_code == project, Space.code == code)) is not None:
        raise HTTPException(status_code=409, detail=f"이미 있는 공간 ID입니다: {code}")
    upsert_project(db, project)
    s = Space(project_code=project, code=code, name=body.name.strip(), level=body.level.strip(), updated_at=datetime.now(timezone.utc))
    db.add(s)
    db.flush()
    auth.audit(db, user.username, "space.create", "space", str(s.id), {"code": code, "name": s.name, "level": s.level, "project": project},
               auth.client_ip(request))
    db.commit()
    return {"id": s.id, "code": s.code, "projectId": s.project_code}


@router.patch("/spaces/{sid}")
def patch_space(sid: int, body: SpacePatch, request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    s = db.get(Space, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="space not found")
    changes: dict = {}
    if body.code is not None and body.code.strip() and body.code.strip() != s.code:
        new = body.code.strip()
        if db.scalar(select(Space).where(Space.project_code == s.project_code, Space.code == new, Space.id != sid)) is not None:
            raise HTTPException(status_code=409, detail=f"이미 있는 공간 ID입니다: {new}")
        changes["code"] = [s.code, new]
        s.code = new
    if body.name is not None and body.name != s.name:
        changes["name"] = [s.name, body.name]
        s.name = body.name
    if body.level is not None and body.level != s.level:
        changes["level"] = [s.level, body.level]
        s.level = body.level
    if changes:
        s.updated_at = datetime.now(timezone.utc)  # 모바일이 변경을 감지하는 기준
        auth.audit(db, user.username, "space.update", "space", str(sid), {"changes": changes}, auth.client_ip(request))
        db.commit()
    return {"ok": True}


@router.delete("/spaces/{sid}")
def delete_space(sid: int, request: Request, cascade: bool = False, user: User = Depends(require_role("admin")),
                 db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    """공간 삭제. 자산이 있으면 기본은 막고(409), cascade=true면 자산·검수 기록·사진·월드맵까지 함께 지운다."""
    s = db.get(Space, sid)
    if s is None:
        raise HTTPException(status_code=404, detail="space not found")
    ids = [a.asset_id for a in db.scalars(select(Asset).where(Asset.space_id == sid)).all()]
    if ids and not cascade:
        raise HTTPException(status_code=409, detail=f"이 공간에 자산이 {len(ids)}개 있습니다 — 자산·검수 기록을 함께 삭제하려면 확인하세요")
    deleted: list[str] = []
    if ids:
        deleted, _ = delete_assets(db, storage, ids)
    for i in db.scalars(select(Issue).where(Issue.space_id == sid)).all():
        i.space_id = None  # 이슈는 보존하고 공간 연결만 끊는다
    if s.worldmap_uri:
        storage.delete(s.worldmap_uri)
    info = {"code": s.code, "name": s.name, "assetsDeleted": deleted}
    db.delete(s)
    auth.audit(db, user.username, "space.delete", "space", str(sid), info, auth.client_ip(request))
    db.commit()
    return {"deleted": sid, "assetsDeleted": len(deleted)}


@router.get("/devices")
def list_devices(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    agg: dict[str, dict] = {}

    def rec(d: str) -> dict:
        return agg.setdefault(d, {"deviceId": d, "assetsRegistered": 0, "inspections": 0, "lastSeen": None,
                                  "lastDecision": None, "fails": 0, "_t": None})

    for a in db.scalars(select(Asset)).all():
        if a.device_id:
            r = rec(a.device_id)
            r["assetsRegistered"] += 1
            t = _aware(a.created_at)
            if r["_t"] is None or t > r["_t"]:
                r["_t"], r["lastSeen"] = t, iso(a.created_at)
    for o in db.scalars(select(Observation).order_by(Observation.created_at)).all():
        if o.device_id and o.device_id != "unknown":
            r = rec(o.device_id)
            r["inspections"] += 1
            r["fails"] += 1 if o.decision == "FAIL" else 0
            t = _aware(o.created_at)
            if r["_t"] is None or t >= r["_t"]:
                r["_t"], r["lastSeen"], r["lastDecision"] = t, iso(o.created_at), o.decision
    out = sorted(agg.values(), key=lambda r: r["lastSeen"] or "", reverse=True)
    for r in out:
        r.pop("_t", None)
    return {"devices": out}


# ───────────────────────── 사용자 · 감사 · 시스템 ─────────────────────────

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


class UserIn(BaseModel):
    username: str
    displayName: str = Field(default="", max_length=100)
    role: Literal["admin", "manager", "inspector", "viewer"] = "inspector"
    password: str = Field(max_length=200)


class UserPatch(BaseModel):
    displayName: str | None = Field(default=None, max_length=100)
    role: Literal["admin", "manager", "inspector", "viewer"] | None = None
    active: bool | None = None


class PwReset(BaseModel):
    password: str = Field(max_length=200)


@router.get("/users")
def list_users(assignable: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """assignable=true: 담당자 선택용 (조치 담당 가능한 활성 계정, 로그인한 누구나). 전체 목록은 통합관리자만."""
    users = db.scalars(select(User).order_by(User.id)).all()
    if assignable:
        return {"users": [{"username": u.username, "displayName": u.display_name or u.username, "role": u.role,
                           "roleLabel": auth.ROLE_LABEL[u.role]} for u in users if u.active and u.role != "viewer"]}
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="통합관리자만 볼 수 있습니다")
    return {"users": [user_out(u) for u in users]}


@router.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict:
    if not USERNAME_RE.match(body.username):
        raise HTTPException(status_code=422, detail="아이디는 영문·숫자·._- 3~32자여야 합니다")
    if db.scalar(select(User).where(User.username == body.username)) is not None:
        raise HTTPException(status_code=409, detail="이미 있는 아이디입니다")
    auth.require_strong(body.password, body.username)
    salt, h = auth.hash_password(body.password)
    u = User(username=body.username, display_name=body.displayName or body.username, role=body.role, pw_salt=salt, pw_hash=h, must_change_password=True)  # 관리자가 정한 초기 암호 — 본인이 바꾸게 한다
    db.add(u)
    auth.audit(db, user.username, "user.create", "user", body.username, {"role": body.role}, auth.client_ip(request))
    db.commit()
    return user_out(u)


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict:
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    if u.id == user.id and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(status_code=409, detail="자기 자신의 권한·활성 상태는 바꿀 수 없습니다")
    if body.displayName is not None:
        u.display_name = body.displayName
    if body.role is not None:
        u.role = body.role
    if body.active is not None:
        u.active = body.active
        if not body.active:
            auth.revoke_sessions(db, u.id)  # 비활성화하면 즉시 로그아웃
    auth.audit(db, user.username, "user.update", "user", u.username, body.model_dump(exclude_none=True), auth.client_ip(request))
    db.commit()
    return user_out(u)


@router.post("/users/{uid}/password")
def reset_password(uid: int, body: PwReset, request: Request, user: User = Depends(require_role("admin")), db: Session = Depends(get_db)) -> dict:
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(status_code=404, detail="user not found")
    auth.require_strong(body.password, u.username)
    u.pw_salt, u.pw_hash = auth.hash_password(body.password)
    u.must_change_password = True  # 초기화된 임시 비밀번호 — 다음 로그인 때 바꾸게 한다
    auth.revoke_sessions(db, u.id)
    auth.audit(db, user.username, "user.password_reset", "user", u.username, ip=auth.client_ip(request))
    db.commit()
    return {"ok": True}


@router.get("/audit")
def audit_log(limit: int = 100, actor: str = "", user: User = Depends(require_role("manager")), db: Session = Depends(get_db)) -> dict:
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(10, min(limit, 500)))
    if actor:
        q = q.where(AuditLog.actor == actor)
    return {"logs": [{"id": a.id, "at": iso(a.at), "actor": a.actor, "action": a.action, "targetType": a.target_type,
                      "targetId": a.target_id, "detail": a.detail, "ip": a.ip} for a in db.scalars(q).all()]}


@router.get("/system")
def system_info(request: Request, user: User = Depends(require_role("manager")), db: Session = Depends(get_db), storage=Depends(get_storage)) -> dict:
    size = 0
    for root, _, files in os.walk(storage.root):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return {"version": "0.1.0", "python": sys.version.split()[0], "uptimeSec": int(time.time() - _STARTED),
            "storageMB": round(size / 1e6, 1), "externalUrl": f"{proto}://{host}/admin/",
            "counts": {"assets": db.scalar(select(func.count()).select_from(Asset)),
                       "inspections": db.scalar(select(func.count()).select_from(Observation)),
                       "issues": db.scalar(select(func.count()).select_from(Issue)),
                       "users": db.scalar(select(func.count()).select_from(User))},
            "debugLogging": os.environ.get("MARKERLESS_DEBUG") == "1"}


_DEVDOC = Path(__file__).resolve().parent.parent / "docs" / "DEVELOPMENT.md"


@router.get("/system/devdoc.md")
def download_devdoc(user: User = Depends(require_role("manager")), db: Session = Depends(get_db),
                    storage=Depends(get_storage)) -> Response:
    """개발 내역서(정적 본문) + 지금 시점의 운영 현황을 붙여 내려받게 한다."""
    body = _DEVDOC.read_text(encoding="utf-8") if _DEVDOC.exists() else "# 개발 내역서\n\n(원본 문서를 찾을 수 없습니다)\n"
    n = lambda m: db.scalar(select(func.count()).select_from(m)) or 0  # noqa: E731
    dec = Counter(o.decision or "UNKNOWN" for o in db.scalars(select(Observation)).all())
    st = Counter(i.status for i in db.scalars(select(Issue)).all())
    now = datetime.now().astimezone()
    live = [
        "", "---", "", "## 부록. 문서 생성 시점의 운영 현황", "",
        f"- 생성 시각: {now.strftime('%Y-%m-%d %H:%M %Z')} (요청자: {user.username})",
        f"- 서버: v0.1.0 · Python {sys.version.split()[0]} · 가동 {int((time.time() - _STARTED) // 3600)}시간",
        f"- 데이터: 공간 {n(Space)} · 자산 {n(Asset)} · 검수 {n(Observation)} · 이슈 {n(Issue)} · 사용자 {n(User)}",
        "- 검수 판정 분포: " + (", ".join(f"{k} {v}" for k, v in dec.most_common()) or "없음"),
        "- 이슈 상태 분포: " + (", ".join(f"{wf.STATUS_LABEL.get(k, k)} {v}" for k, v in st.most_common()) or "없음"), "",
    ]
    return Response("\ufeff" + body.rstrip() + "\n" + "\n".join(live), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="markerless-development-{date.today().isoformat()}.md"'})
