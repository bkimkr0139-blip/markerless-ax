"""ORM → API 응답 변환."""

from __future__ import annotations

from datetime import datetime, timezone

from .services.assets import effective_inspect_view
from .models import Asset, InspectionTask, Observation, Project
from .schemas import AssetOut, ObservationOut, ProjectOut, SpaceOut


def _project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        projectId=p.code,
        name=p.name,
        createdAt=p.created_at.isoformat() if p.created_at else None,
    )


def space_out(s) -> SpaceOut:
    return SpaceOut(
        id=s.id,
        projectId=s.project_code,
        code=s.code,
        name=s.name,
        level=s.level,
        hasWorldMap=s.worldmap_uri is not None,
        worldmapSha256=s.worldmap_sha256,
    )


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).isoformat()


def asset_out(a: Asset) -> AssetOut:
    return AssetOut(
        assetId=a.asset_id,
        projectId=a.project_code,
        spaceId=a.space_id,
        bimRevision=a.bim_revision,
        ifcGuid=a.ifc_guid,
        family=a.family,
        type=a.asset_type,
        designPose=a.design_pose,
        tolerance=a.tolerance,
        cadUri=a.cad_uri,
        aabb=a.aabb,
        scannedAt=_iso_utc(a.scanned_at or a.created_at),
        geo=a.geo,
        deviceId=a.device_id,
        inspectView=effective_inspect_view(a),
        name=a.name or a.asset_id,
        status=a.status or "ACTIVE",
    )


def observation_out(o: Observation) -> ObservationOut:
    return ObservationOut(
        observationId=o.id,
        taskId=o.task_id,
        assetId=o.asset_id,
        deviceId=o.device_id,
        bimRevision=o.bim_revision,
        designPose=o.design_pose,
        actualPose=o.actual_pose,
        deviation=o.deviation,
        uncertainty=o.uncertainty,
        confidence=o.confidence,
        decision=o.decision,
        evidence=list(o.evidence or []),
        createdAt=o.created_at.isoformat() if o.created_at else None,
    )


def task_out(t: InspectionTask) -> dict:
    return {
        "taskId": t.id,
        "projectId": t.project_code,
        "spaceId": t.space_id,
        "assetIds": list(t.asset_ids or []),
        "status": t.status,
        "assignee": t.assignee,
        "createdAt": t.created_at.isoformat() if t.created_at else None,
    }
