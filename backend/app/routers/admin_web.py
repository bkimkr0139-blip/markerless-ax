"""백오피스 웹(SPA) 정적 서빙 — /admin/ (같은 서버·같은 외부 URL)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "admin"
router = APIRouter()


@router.get("/admin", include_in_schema=False)
def admin_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/")


@router.get("/admin/", include_in_schema=False)
def admin_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


def mount_static(app: FastAPI) -> None:
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="admin-static")
