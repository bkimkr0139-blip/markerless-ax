from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..deps import get_db, get_storage
from ..models import Asset, InspectionResult, Observation
from ..schemas import MatchIn, MeasurementIn, MeasurementOut
from ..serializers import observation_out
from ..services import issues as wf
from ..services.inspection import NoMatchError, evaluate_measurement, run_object_inspection, run_object_match

router = APIRouter(prefix="/api/v1", tags=["measurements"])


def _save_review(storage, obs, asset, body, evaluation: dict, match: dict | None) -> None:
    """백오피스 3D 리뷰용: 관측한 사물 점·외곽선, 맞춘 포즈, 매칭 상세 (object 모드에서만)."""
    pts = evaluation.get("observedPoints")
    if match is None or pts is None:
        return
    import base64

    import numpy as np

    pts = np.asarray(pts)
    if len(pts) > 4000:
        pts = pts[np.linspace(0, len(pts) - 1, 4000).astype(int)]
    review = {
        "observationId": obs.id, "assetId": asset.asset_id, "decision": evaluation["decision"],
        "matchedBy": match.get("matchedBy"), "worldMapRestored": bool(body.worldMapRestored),
        "actualPose": evaluation["actualPose"], "designPose": asset.design_pose,
        "match": {k: v for k, v in match.items() if k != "observedOutline"},
        "observedOutline": match.get("observedOutline"),
        "observedPoints": {"count": int(len(pts)), "points": base64.b64encode(pts.astype("<f4").tobytes()).decode()},
    }
    d = storage.root / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{obs.id}.json").write_text(json.dumps(review), encoding="utf-8")


@router.post("/match")
def match_assets(
    body: MatchIn, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> dict:
    """자산 찾기(자동 매칭만): 비춘 사물과 닮은 등록 자산 후보를 유사도 순으로 돌려준다.

    판정·저장은 하지 않는다 — 앱이 후보를 보여 주고, 사용자가 고른 자산으로 /measurements(assetId 지정)를 호출한다.
    """
    try:
        return run_object_match(db, storage, body)
    except NoMatchError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/measurements", response_model=MeasurementOut, status_code=201)
def submit_measurement(
    body: MeasurementIn,
    db: Session = Depends(get_db),
    storage=Depends(get_storage),
) -> MeasurementOut:
    """측정 제출 → 정합(ICP) → Design-vs-Actual → 판정.

    - cloud가 있으면 서버 정합엔진이 actualPose를 산출한다 (공통 엔진, iOS/Android 공용).
    - actualPose가 있으면(수동/기기 정합) 신뢰도는 기기 보고값 또는 UNKNOWN.
    - 판정/근거/불확도는 Observation + InspectionResult로 저장한다 (원칙 8/12/13).
    """
    try:
        if body.alignment == "object" and body.cloud is not None:
            # 사물 대 사물 검수 (분리 → 자동 매칭/지정 자산 정합 → 판정)
            asset, evaluation = run_object_inspection(db, storage, body)
        else:
            asset = db.get(Asset, body.assetId)
            if asset is None:
                raise LookupError(f"asset not found: {body.assetId}")
            evaluation = evaluate_measurement(storage, asset, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except NoMatchError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        # BIM Rule 누락, 클라우드 형식 오류 등 — 검사 중단 + 오류 보고 (12.2)
        raise HTTPException(status_code=422, detail=str(e)) from e

    # 관측 레코드: 기존 관측 부착 또는 신규 생성. 설계 Pose는 시점 스냅샷(원칙 12).
    if body.observationId is not None:
        obs = db.get(Observation, body.observationId)
        if obs is None:
            raise HTTPException(
                status_code=404, detail=f"observation not found: {body.observationId}"
            )
    else:
        obs = Observation(
            task_id=body.taskId,
            asset_id=asset.asset_id,
            device_id=body.deviceId,
        )
        db.add(obs)
    obs.bim_revision = body.bimRevision or asset.bim_revision
    obs.design_pose = asset.design_pose
    obs.actual_pose = evaluation["actualPose"]
    obs.deviation = evaluation["deviation"]
    obs.uncertainty = evaluation["uncertainty"]
    obs.confidence = evaluation["confidence"]
    obs.decision = evaluation["decision"]
    if evaluation["icp"] is not None:
        obs.extra = {**(obs.extra or {}), "icp": evaluation["icp"]}
    match = evaluation.get("match")
    if match is not None:  # 큰 외곽선은 저장하지 않는다 (요약만)
        obs.extra = {**(obs.extra or {}), "match": {k: v for k, v in match.items() if k != "observedOutline"}}
    db.flush()

    result = InspectionResult(
        observation_id=obs.id,
        asset_id=asset.asset_id,
        decision=evaluation["decision"],
        confidence=evaluation["confidence"],
        deviation=evaluation["deviation"],
        reasons=evaluation["reasons"],
        margins=evaluation["margins"],
    )
    db.add(result)
    db.flush()
    # 백오피스 연동: ① FAIL/REVIEW는 조치 워크플로우 이슈로 (열린 이슈가 있으면 재측정 이력), ② 3D 리뷰 데이터 저장
    wf.on_measurement(db, obs, evaluation["decision"], list(evaluation["reasons"] or []), asset, evaluation["deviation"])
    _save_review(storage, obs, asset, body, evaluation, match)
    db.commit()

    from ..schemas import ResultOut

    result_out = ResultOut(
        resultId=result.id,
        observationId=result.observation_id,
        assetId=result.asset_id,
        decision=result.decision,
        confidence=result.confidence,
        deviation=result.deviation,
        reasons=list(result.reasons or []),
        margins=result.margins or {},
        createdAt=result.created_at.isoformat() if result.created_at else None,
    )
    return MeasurementOut(observation=observation_out(obs), result=result_out, match=match)
