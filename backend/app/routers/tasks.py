from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Asset, InspectionTask, Space
from ..serializers import task_out

router = APIRouter(prefix="/api/v1", tags=["inspection-tasks"])


class TaskIn(BaseModel):
    projectId: str
    spaceId: int
    assetIds: list[str] = Field(min_length=1)
    assignee: str | None = None


class TaskStatusIn(BaseModel):
    status: str = Field(pattern="^(PENDING|IN_PROGRESS|COMPLETED|CANCELLED)$")


@router.post("/inspection-tasks", status_code=201)
def create_task(body: TaskIn, db: Session = Depends(get_db)) -> dict:
    space = db.get(Space, body.spaceId)
    if space is None or space.project_code != body.projectId:
        raise HTTPException(status_code=404, detail=f"space not found: {body.spaceId}")
    for aid in body.assetIds:
        asset = db.get(Asset, aid)
        if asset is None or asset.space_id != space.id:
            raise HTTPException(
                status_code=422,
                detail=f"asset {aid} not in space {space.code} — BIM/task data error",
            )
    task = InspectionTask(
        project_code=body.projectId,
        space_id=body.spaceId,
        asset_ids=body.assetIds,
        assignee=body.assignee,
    )
    db.add(task)
    db.commit()
    return task_out(task)


@router.get("/inspection-tasks")
def list_tasks(spaceId: int | None = None, db: Session = Depends(get_db)) -> list[dict]:
    q = select(InspectionTask)
    if spaceId is not None:
        q = q.where(InspectionTask.space_id == spaceId)
    return [task_out(t) for t in db.scalars(q).all()]


@router.get("/inspection-tasks/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    task = db.get(InspectionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return task_out(task)


@router.patch("/inspection-tasks/{task_id}")
def update_task_status(
    task_id: str, body: TaskStatusIn, db: Session = Depends(get_db)
) -> dict:
    task = db.get(InspectionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    task.status = body.status
    db.commit()
    return task_out(task)
