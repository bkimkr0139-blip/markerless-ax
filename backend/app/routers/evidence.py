from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_storage
from ..models import EvidenceRecord
from ..schemas import EvidenceOut

router = APIRouter(prefix="/api/v1", tags=["evidence"])

_ALLOWED_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".heic", ".dng",
    ".bin", ".ply", ".npy", ".glb", ".worldmap", ".json", ".csv",
}


@router.post("/evidence", response_model=EvidenceOut, status_code=201)
async def upload_evidence(
    file: UploadFile = File(...),
    kind: str = Form("image"),
    db: Session = Depends(get_db),
    storage=Depends(get_storage),
) -> EvidenceOut:
    """증거 원본 업로드 — 재인코딩 없이 원본 보존 (원칙 13)."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="empty file")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported evidence type: {suffix or '(none)'} — allowed: {sorted(_ALLOWED_SUFFIXES)}",
        )
    uri, sha, size = storage.save("evidence", data, suffix=suffix)
    rec = EvidenceRecord(
        kind=kind,
        uri=uri,
        original_filename=file.filename or "",
        sha256=sha,
        size_bytes=size,
    )
    db.add(rec)
    db.commit()
    return EvidenceOut(evidenceId=rec.id, kind=rec.kind, uri=uri, sha256=sha, sizeBytes=size)


@router.get("/evidence/{evidence_id}", response_model=EvidenceOut)
def evidence_meta(evidence_id: str, db: Session = Depends(get_db)) -> EvidenceOut:
    rec = db.get(EvidenceRecord, evidence_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"evidence not found: {evidence_id}")
    return EvidenceOut(
        evidenceId=rec.id,
        kind=rec.kind,
        uri=rec.uri,
        sha256=rec.sha256,
        sizeBytes=rec.size_bytes,
    )


@router.get("/evidence/{evidence_id}/download")
def download_evidence(
    evidence_id: str, db: Session = Depends(get_db), storage=Depends(get_storage)
) -> FileResponse:
    rec = db.get(EvidenceRecord, evidence_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"evidence not found: {evidence_id}")
    path = storage.open_path(rec.uri)
    return FileResponse(path, filename=rec.original_filename or Path(rec.uri).name)
