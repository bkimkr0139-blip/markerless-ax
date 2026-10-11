from __future__ import annotations

import spatial_core
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["health"])


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin/")


@router.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@router.get("/api/v1/version")
def version() -> dict:
    return {"api": "0.1.0", "spatialCore": spatial_core.__version__}
