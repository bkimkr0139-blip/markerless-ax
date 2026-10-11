from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db, get_storage
from ..models import Asset, Space
from ..schemas import AssetOut, BulkDeleteIn, BulkDeleteOut, ScanAssetIn, TolerancePut
from ..serializers import asset_out
from ..services.assets import (
    asset_outline,
    create_asset_from_scan,
    delete_assets,
    list_photos,
    photo_path,
    save_photo,
)

router = APIRouter(prefix="/api/v1", tags=["assets"])


@router.post("/assets/scan", response_model=AssetOut, status_code=201)
async def create_asset_from_scan_api(
    meta: str = Form(..., description="ScanAssetIn JSON"),
    model: UploadFile = File(..., description="자산 로컬 프레임 GLB"),
    db: Session = Depends(get_db),
    storage=Depends(get_storage),
) -> AssetOut:
    """앱 스캔 → 자산 생성 (Scan-to-BIM/CAD).

    - GLB는 자산 로컬 프레임(중심=design origin), designPose는 공간 프레임.
    - 동일 assetId 재업로드 시 교체(갱신)한다.
    """
    try:
        body = ScanAssetIn.model_validate(json.loads(meta))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"meta is not valid JSON: {e}") from e
    glb = await model.read()
    if not glb:
        raise HTTPException(status_code=422, detail="model file is empty")
    try:
        asset = create_asset_from_scan(db, storage, body, glb)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    db.commit()
    return asset_out(asset)


@router.get("/spaces/{space_id}/assets", response_model=list[AssetOut])
def list_space_assets(space_id: int, db: Session = Depends(get_db)) -> list[AssetOut]:
    if db.get(Space, space_id) is None:
        raise HTTPException(status_code=404, detail=f"space not found: {space_id}")
    return [
        asset_out(a)
        for a in db.scalars(select(Asset).where(Asset.space_id == space_id)).all()
        if (a.status or "ACTIVE") != "ARCHIVED"  # 관리자가 보관 처리한 자산은 현장에 내보이지 않는다
    ]


@router.get("/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, db: Session = Depends(get_db)) -> AssetOut:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return asset_out(asset)


@router.get("/assets/{asset_id}/model.glb")
def download_asset_model(
    asset_id: str, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> FileResponse:
    asset = db.get(Asset, asset_id)
    if asset is None or asset.cad_uri is None:
        raise HTTPException(status_code=404, detail=f"asset model not found: {asset_id}")
    return FileResponse(
        storage.open_path(asset.cad_uri),
        media_type="model/gltf-binary",
        filename=f"{asset_id}.glb",
    )


@router.get("/assets/{asset_id}/outline")
def get_asset_outline(
    asset_id: str, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> dict:
    """자산 외곽선(와이어프레임, 자산 로컬 프레임). 검수 화면이 사물 위에 선으로 겹쳐 그린다."""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.cad_uri is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    try:
        return asset_outline(storage, asset)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.put("/assets/{asset_id}/photos/{side}", status_code=204)
async def put_asset_photo(
    asset_id: str,
    side: str,
    meta: str = Form(..., description="카드 배치 JSON: center/right/up/forward [x,y,z], widthM, heightM (자산 로컬 프레임)"),
    image: UploadFile = File(..., description="흑백 실루엣 PNG (알파=사물 윤곽)"),
    db: Session = Depends(get_db),
    storage=Depends(get_storage),
) -> None:
    """방향(a00..a47 둘레 순서, 또는 구버전 front|right|back|left)별 사진 실루엣 카드 업로드 — 검수 때 사물 위에 겹쳐 보여 준다."""
    if db.get(Asset, asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    try:
        save_photo(storage, asset_id, side, await image.read(), json.loads(meta))
        from ..events import broker
        broker.publish({"assets"})  # 사진은 DB가 아니라 파일이라 훅이 못 본다 — 연속 업로드는 한 번으로 합쳐진다
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"meta is not valid JSON: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/assets/{asset_id}/photos")
def get_asset_photos(
    asset_id: str, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> dict:
    if db.get(Asset, asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return {"photos": list_photos(storage, asset_id)}


@router.get("/assets/{asset_id}/photos/{side}.png")
def get_asset_photo_png(
    asset_id: str, side: str, storage=Depends(get_storage)
) -> FileResponse:
    p = photo_path(storage, asset_id, side)
    if p is None:
        raise HTTPException(status_code=404, detail=f"photo not found: {asset_id}/{side}")
    return FileResponse(p, media_type="image/png")


@router.put("/assets/{asset_id}/tolerance", response_model=AssetOut)
def set_tolerance(
    asset_id: str, body: TolerancePut, db: Session = Depends(get_db)
) -> AssetOut:
    """현장 Rule 설정 (개발지시서 13.2)."""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    asset.tolerance = body.model_dump()
    db.commit()
    return asset_out(asset)


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(
    asset_id: str, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> None:
    """자산 1개 삭제 (관측·판정 결과·모델 파일 포함)."""
    deleted, _ = delete_assets(db, storage, [asset_id])
    if not deleted:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    db.commit()


@router.post("/assets/bulk-delete", response_model=BulkDeleteOut)
def bulk_delete_assets(
    body: BulkDeleteIn, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> BulkDeleteOut:
    """자산 일괄 삭제. 없는 ID는 notFound로 알리고 나머지는 삭제한다."""
    deleted, missing = delete_assets(db, storage, body.assetIds)
    db.commit()
    return BulkDeleteOut(deleted=deleted, notFound=missing)
