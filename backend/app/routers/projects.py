from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Project
from ..schemas import ProjectIn, ProjectOut
from ..serializers import _project_out

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[ProjectOut]:
    return [_project_out(p) for p in db.scalars(select(Project)).all()]


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db)) -> ProjectOut:
    if db.get(Project, body.projectId) is not None:
        raise HTTPException(status_code=409, detail=f"project already exists: {body.projectId}")
    project = Project(code=body.projectId, name=body.name)
    db.add(project)
    db.commit()
    return _project_out(project)


@router.get("/projects/{code}", response_model=ProjectOut)
def get_project(code: str, db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, code)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project not found: {code}")
    return _project_out(project)
