"""개발용 로그 수집 엔드포인트 (MARKERLESS_DEBUG=1 에서만 동작)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import devlog

router = APIRouter(prefix="/api/v1/debug", tags=["debug"])


class ClientLog(BaseModel):
    level: str = "INFO"
    tag: str = ""
    message: str = ""
    device: str | None = None
    session: str | None = None
    clientTs: str | None = None
    extra: dict[str, str] | None = None


@router.post("/log", status_code=204)
def client_log(body: ClientLog) -> None:
    if not devlog.enabled():
        raise HTTPException(status_code=404, detail="debug logging disabled")
    devlog.write(
        "app", body.level, body.tag, body.message,
        device=body.device, session=body.session, cts=body.clientTs, extra=body.extra,
    )
