from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Asset, Observation
from ..schemas import ObservationCreateIn, ObservationOut
from ..serializers import observation_out

router = APIRouter(prefix="/api/v1", tags=["observations"])


@router.post("/observations", response_model=ObservationOut, status_code=201)
def create_observation(
    body: ObservationCreateIn, db: Session = Depends(get_db)
) -> ObservationOut:
    """현장 관측(캡처 세션/증거) 원본 기록. 판정은 /measurements에서 수행."""
    asset = db.get(Asset, body.assetId)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {body.assetId}")
    obs = Observation(
        task_id=body.taskId,
        asset_id=body.assetId,
        device_id=body.deviceId,
        bim_revision=asset.bim_revision,
        design_pose=asset.design_pose,
        evidence=body.evidenceUris,
        extra=body.captureMeta,
    )
    db.add(obs)
    db.commit()
    return observation_out(obs)


@router.get("/observations", response_model=list[ObservationOut])
def list_observations(
    assetId: str | None = None,
    spaceId: int | None = None,
    taskId: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[ObservationOut]:
    limit = max(1, min(limit, 500))
    q = select(Observation)
    if assetId is not None:
        q = q.where(Observation.asset_id == assetId)
    if taskId is not None:
        q = q.where(Observation.task_id == taskId)
    if spaceId is not None:
        q = q.join(Asset, Observation.asset_id == Asset.asset_id).where(
            Asset.space_id == spaceId
        )
    q = q.order_by(Observation.created_at.desc()).limit(limit)
    return [observation_out(o) for o in db.scalars(q).all()]


@router.get("/observations/{observation_id}", response_model=ObservationOut)
def get_observation(observation_id: str, db: Session = Depends(get_db)) -> ObservationOut:
    obs = db.get(Observation, observation_id)
    if obs is None:
        raise HTTPException(status_code=404, detail=f"observation not found: {observation_id}")
    return observation_out(obs)
