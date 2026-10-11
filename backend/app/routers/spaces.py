from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db, get_storage
from ..models import Space
from ..schemas import SpaceIn, SpaceOut
from ..serializers import space_out
from ..services.assets import upsert_project, upsert_space
from ..storage import sha256_hex

router = APIRouter(prefix="/api/v1", tags=["spaces"])


@router.get("/sync/state")
def sync_state(db: Session = Depends(get_db)) -> dict:
    """현재 동기화 상태(revision). 앱이 이벤트 연결을 (재)시작할 때 한 번 따라잡기용으로 쓴다."""
    from ..services.sync import compute_state

    return compute_state(db)


@router.get("/sync/events")
async def sync_events(request: Request):
    """변경 이벤트 스트림(SSE) — 관리자가 공간·자산을 저장하거나 다른 단말이 등록·검수하면 그 시점에 한 줄이 온다.
    앱은 연결을 유지하고 있다가 이벤트가 올 때만 새로 읽는다 (주기 확인 없음). 연결 중 DB 세션은 잡고 있지 않는다."""
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import StreamingResponse

    from ..events import SSE_HEADERS, sse_stream
    from ..services.sync import compute_state

    sf = request.app.state.session_factory

    def state() -> dict:
        with sf() as db:
            return compute_state(db)

    async def payload(scopes: set[str]) -> dict:
        return {**await run_in_threadpool(state), "scopes": sorted(scopes)}

    return StreamingResponse(sse_stream(request, payload), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/projects/{project_code}/spaces", response_model=SpaceOut, status_code=201)
def create_space(
    project_code: str, body: SpaceIn, db: Session = Depends(get_db)
) -> SpaceOut:
    # PoC 편의: 프로젝트가 없으면 자동 생성 (모바일 앱 최초 설정 부담 제거)
    upsert_project(db, project_code)
    space = upsert_space(db, project_code, body)
    db.commit()
    return space_out(space)


@router.get("/projects/{project_code}/spaces", response_model=list[SpaceOut])
def list_spaces(project_code: str, db: Session = Depends(get_db)) -> list[SpaceOut]:
    return [
        space_out(s)
        for s in db.scalars(
            select(Space).where(Space.project_code == project_code)
        ).all()
    ]


@router.get("/spaces/{space_id}", response_model=SpaceOut)
def get_space(space_id: int, db: Session = Depends(get_db)) -> SpaceOut:
    space = db.get(Space, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail=f"space not found: {space_id}")
    return space_out(space)


@router.put("/spaces/{space_id}/worldmap", response_model=SpaceOut)
async def upload_worldmap(
    space_id: int,
    request: Request,
    db: Session = Depends(get_db),
    storage=Depends(get_storage),
) -> SpaceOut:
    """ARWorldMap 저장 — 공간 좌표계 복원 기준 (docs/COORDINATE.md T_space_session).

    body: 바이너리 (ARKit worldMap archivedData, bplist 시그니처 검사)
    """
    space = db.get(Space, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail=f"space not found: {space_id}")
    data = await request.body()
    if not data.startswith(b"bplist00"):
        raise HTTPException(
            status_code=422,
            detail="worldmap must be a binary plist (ARKit worldMap archivedData)",
        )
    uri, sha, _size = storage.save("worldmaps", data, suffix=".worldmap")
    space.worldmap_uri = uri
    space.worldmap_sha256 = sha
    db.commit()
    return space_out(space)


@router.get("/spaces/{space_id}/worldmap")
def download_worldmap(
    space_id: int, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> Response:
    space = db.get(Space, space_id)
    if space is None or space.worldmap_uri is None:
        raise HTTPException(
            status_code=404, detail=f"worldmap not found for space: {space_id}"
        )
    data = storage.open_bytes(space.worldmap_uri)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Sha256": space.worldmap_sha256 or sha256_hex(data)},
    )
