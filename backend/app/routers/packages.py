from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Asset
from ..schemas import PackageIn, SpaceIn
from ..services.assets import upsert_project, upsert_space

router = APIRouter(prefix="/api/v1", tags=["packages"])


@router.post("/packages", status_code=201)
def import_package(body: PackageIn, db: Session = Depends(get_db)) -> dict:
    """Revit 애드인의 모바일 패키지 임포트 (개발지시서 6.1).

    스키마 검증(pydantic) 실패는 422로 BIM 데이터 오류를 보고한다(12.2).
    """
    if not body.assets:
        raise HTTPException(status_code=422, detail="package has no assets")

    upsert_project(db, body.projectId)
    space = upsert_space(
        db,
        body.projectId,
        SpaceIn(spaceId=body.spaceId, name=body.spaceName, level=body.level),
    )

    asset_ids: list[str] = []
    for a in body.assets:
        pose_json = a.designPose.to_json()
        asset = db.get(Asset, a.assetId)
        if asset is None:
            asset = Asset(
                asset_id=a.assetId, project_code=body.projectId, space_id=space.id
            )
            db.add(asset)
        asset.bim_revision = body.revision
        asset.ifc_guid = a.ifcGuid
        asset.family = a.family
        asset.asset_type = a.type
        asset.design_pose = pose_json
        asset.tolerance = a.tolerance.model_dump()
        if a.cad:
            asset.cad_uri = None  # PoC: 패키지 cad 경로는 메타만 기록
        asset_ids.append(a.assetId)

    db.commit()
    return {
        "projectId": body.projectId,
        "revision": body.revision,
        "spaceId": space.id,
        "imported": len(asset_ids),
        "assetIds": asset_ids,
    }
