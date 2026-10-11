from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import InspectionResult

router = APIRouter(prefix="/api/v1", tags=["inspection-results"])


class ResultOutModel(BaseModel):
    resultId: str
    observationId: str
    assetId: str
    decision: str
    confidence: str
    deviation: dict | None = None
    reasons: list[str] = []
    margins: dict = {}
    createdAt: str | None = None


def _result_out(r: InspectionResult) -> dict:
    return {
        "resultId": r.id,
        "observationId": r.observation_id,
        "assetId": r.asset_id,
        "decision": r.decision,
        "confidence": r.confidence,
        "deviation": r.deviation,
        "reasons": list(r.reasons or []),
        "margins": r.margins or {},
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/inspection-results/{result_id}")
def get_result(result_id: str, db: Session = Depends(get_db)) -> dict:
    r = db.get(InspectionResult, result_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"result not found: {result_id}")
    return _result_out(r)


@router.get("/inspection-results")
def list_results(
    assetId: str | None = None,
    observationId: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[dict]:
    limit = max(1, min(limit, 500))
    q = select(InspectionResult)
    if assetId is not None:
        q = q.where(InspectionResult.asset_id == assetId)
    if observationId is not None:
        q = q.where(InspectionResult.observation_id == observationId)
    q = q.order_by(InspectionResult.created_at.desc()).limit(limit)
    return [_result_out(r) for r in db.scalars(q).all()]
