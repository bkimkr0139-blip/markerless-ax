from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from .storage import FileStorage


def get_db(request: Request):
    db: Session = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()


def get_storage(request: Request) -> FileStorage:
    return request.app.state.storage
