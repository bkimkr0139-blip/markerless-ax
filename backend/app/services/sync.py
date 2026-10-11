"""모바일 동기화 상태 — 공간·자산의 이름·상태·공차·표시 설정·모델·시각이 하나라도 바뀌면 revision이 달라진다."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, Space
from .assets import effective_inspect_view


def compute_state(db: Session) -> dict:
    parts: list = []
    spaces = db.scalars(select(Space).order_by(Space.id)).all()
    for s in spaces:
        parts.append(["S", s.id, s.code, s.name, s.level, s.worldmap_sha256, s.updated_at.isoformat() if s.updated_at else None])
    n_assets = 0
    for a in db.scalars(select(Asset).order_by(Asset.asset_id)).all():
        n_assets += 1
        parts.append(["A", a.asset_id, a.space_id, a.name, a.status, a.family, a.asset_type, a.tolerance, a.cad_sha256,
                      effective_inspect_view(a), a.updated_at.isoformat() if a.updated_at else None])
    rev = hashlib.sha1(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:12]
    return {"revision": rev, "spaces": len(spaces), "assets": n_assets}
