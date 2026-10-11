"""자산 서비스 — 스캔 기반 자가 에셋 생성 (Scan-to-BIM/CAD)과 샘플 캐시."""

from __future__ import annotations

import re

from datetime import datetime, timezone

from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from spatial_core.registration import (
    mesh_aabb,
    mesh_extent,
    read_glb_mesh,
    sample_mesh_points,
)

from ..config import settings
from ..models import Asset, Project, Space
from ..schemas import ScanAssetIn, SpaceIn
from ..storage import FileStorage

# 스캔 메시 유효성 기준 (사물 스캔용) — 지시서 2.2절 대상 선정조건에 대응
MIN_EXTENT_M = 0.05
MAX_EXTENT_M = 10.0
# ICP target 표면 샘플: 면적 밀도 기반, 300~24000 클램프 (간격 ~6mm 수준)
SAMPLE_DENSITY_PER_M2 = 60000.0
SAMPLE_MIN = 300
SAMPLE_MAX = 24000


def _sample_count(vertices: np.ndarray, triangles: np.ndarray) -> int:
    a = vertices[triangles[:, 0]]
    b = vertices[triangles[:, 1]]
    c = vertices[triangles[:, 2]]
    area = float(
        0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum()
    )
    n = int(area * SAMPLE_DENSITY_PER_M2)
    return max(SAMPLE_MIN, min(SAMPLE_MAX, n))


def upsert_project(db: Session, code: str, name: str = "") -> Project:
    project = db.get(Project, code)
    if project is None:
        project = Project(code=code, name=name)
        db.add(project)
    elif name and project.name != name:
        project.name = name
    return project


def upsert_space(db: Session, project_code: str, body: SpaceIn) -> Space:
    space = (
        db.query(Space)
        .filter(Space.project_code == project_code, Space.code == body.spaceId)
        .first()
    )
    if space is None:
        space = Space(
            project_code=project_code,
            code=body.spaceId,
            name=body.name,
            level=body.level,
        )
        db.add(space)
    else:
        space.name = body.name or space.name
        space.level = body.level or space.level
        space.updated_at = datetime.now(timezone.utc)
    db.flush()
    return space


def _sample_cache_path(storage: FileStorage, asset_id: str) -> Path:
    d = storage.root / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{asset_id}.npy"


# 방향 카드 이름: 신규 a00..a47 (a{k} = 사물 둘레 k번째 방향, 앞=a00에서 시계 방향) + 구버전 front/right/back/left
# 검수 표시 레이어 — 통합관리자가 자산별로 고르고, 앱은 검수 때 이 설정대로만 그린다.
# 기본: 외곽 박스·치수·사진 실루엣(스캔 표면 자체는 CAD/BIM처럼 쓰기에 거칠어서 필요할 때만 켠다)
INSPECT_VIEW_KEYS = ("surface", "points", "outline", "box", "dimensions", "silhouette")
DEFAULT_INSPECT_VIEW = {"surface": False, "points": False, "outline": True, "box": True, "dimensions": True, "silhouette": True}


_integrity_cache: dict[str, bool] = {}


def model_ok(storage: FileStorage, asset) -> bool | None:
    """등록 모델(GLB)이 온전한가. 구버전 앱이 만든 정점 버퍼가 깨진 GLB는 False (재등록 필요). 모델 없으면 None."""
    if asset.cad_uri is None:
        return None
    key = asset.cad_sha256 or asset.cad_uri
    if key not in _integrity_cache:
        try:
            read_glb_mesh(storage.open_bytes(asset.cad_uri))
            _integrity_cache[key] = True
        except Exception:
            _integrity_cache[key] = False
    return _integrity_cache[key]


def effective_inspect_view(asset) -> dict:
    saved = asset.inspect_view or {}
    return {k: bool(saved.get(k, DEFAULT_INSPECT_VIEW[k])) for k in INSPECT_VIEW_KEYS}


LEGACY_SIDES = {"front": 0.0, "right": 90.0, "back": 180.0, "left": 270.0}
_SIDE_RE = re.compile(r"^a(\d{2})$")
MAX_PHOTO_CARDS = 96


def valid_side(side: str) -> bool:
    m = _SIDE_RE.match(side)
    return side in LEGACY_SIDES or (m is not None and int(m.group(1)) < MAX_PHOTO_CARDS)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_PHOTO_BYTES = 3_000_000


def _photo_dir(storage: FileStorage, asset_id: str) -> Path:
    d = storage.root / "photos" / asset_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_photo(storage: FileStorage, asset_id: str, side: str, png: bytes, meta: dict) -> None:
    """방향별 흑백 실루엣 카드(PNG, 알파=사물 윤곽)와 배치 정보(자산 로컬 프레임)를 저장한다."""
    import json

    if not valid_side(side):
        raise ValueError(f"invalid side: {side} (a00..a95 또는 front|right|back|left)")
    if not png.startswith(PNG_MAGIC):
        raise ValueError("image must be PNG")
    if len(png) > MAX_PHOTO_BYTES:
        raise ValueError(f"image too large: {len(png)} bytes")
    for key in ("center", "right", "up", "forward"):
        v = meta.get(key)
        if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)):
            raise ValueError(f"meta.{key} must be [x,y,z]")
    for key in ("widthM", "heightM"):
        if not isinstance(meta.get(key), (int, float)) or not (0.01 <= meta[key] <= 5.0):
            raise ValueError(f"meta.{key} must be within 0.01..5.0 m")
    d = _photo_dir(storage, asset_id)
    (d / f"{side}.png").write_bytes(png)
    mp = d / "meta.json"
    allmeta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    allmeta[side] = {k: meta[k] for k in ("center", "right", "up", "forward", "widthM", "heightM")}
    if isinstance(meta.get("azimuthDeg"), (int, float)):
        allmeta[side]["azimuthDeg"] = float(meta["azimuthDeg"])  # 촬영 방향(앞=0°, 시계 방향) — 정렬·표시용
    mp.write_text(json.dumps(allmeta), encoding="utf-8")


def list_photos(storage: FileStorage, asset_id: str) -> list[dict]:
    import json

    mp = storage.root / "photos" / asset_id / "meta.json"
    if not mp.exists():
        return []
    allmeta = json.loads(mp.read_text(encoding="utf-8"))
    def az(s: str) -> float:
        return allmeta[s].get("azimuthDeg", LEGACY_SIDES.get(s, 0.0) if s in LEGACY_SIDES else int(s[1:]) * 15.0)

    sides = [s for s in allmeta if valid_side(s) and (mp.parent / f"{s}.png").exists()]
    return [{"side": s, **allmeta[s]} for s in sorted(sides, key=az)]


def photo_path(storage: FileStorage, asset_id: str, side: str) -> Path | None:
    if not valid_side(side):
        return None
    p = storage.root / "photos" / asset_id / f"{side}.png"
    if p.exists():
        return p
    if side == "front":  # 대표 썸네일: 신규 카드에는 front가 없으므로 앞쪽(a00) 또는 첫 카드
        cards = list_photos(storage, asset_id)
        if cards:
            return storage.root / "photos" / asset_id / f"{cards[0]['side']}.png"
    return None


def clear_photos(storage: FileStorage, asset_id: str) -> None:
    import shutil

    d = storage.root / "photos" / asset_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _outline_cache_path(storage: FileStorage, asset_id: str) -> Path:
    d = storage.root / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{asset_id}.outline.v2.json"  # v2: 수직 단면 포함 — 옛 캐시(v1, 가로 단면만)는 쓰지 않는다


def asset_outline(storage: FileStorage, asset: Asset) -> dict:
    """자산 로컬 프레임의 외곽선(와이어프레임). 모델 표면 샘플에서 계산해 캐시한다."""
    import json

    from spatial_core.segmentation import extract_outline, outline_to_json

    cache = _outline_cache_path(storage, asset.asset_id)
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    pts, _ = load_sample_points(storage, asset)
    out = outline_to_json(extract_outline(pts, up_axis=1))
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def create_asset_from_scan(
    db: Session,
    storage: FileStorage,
    meta: ScanAssetIn,
    glb_bytes: bytes,
) -> Asset:
    """앱에서 스캔한 GLB(자산 로컬 프레임)로 자산을 생성/갱신한다."""
    project = db.get(Project, meta.projectId)
    if project is None:
        raise LookupError(f"project not found: {meta.projectId}")
    space = (
        db.query(Space)
        .filter(Space.project_code == meta.projectId, Space.code == meta.spaceId)
        .first()
    )
    if space is None:
        raise LookupError(f"space not found: {meta.spaceId}")

    try:
        vertices, triangles = read_glb_mesh(glb_bytes)
    except ValueError as e:
        raise ValueError(f"GLB parse failed: {e}") from e

    extent = mesh_extent(vertices)
    if extent < MIN_EXTENT_M:
        raise ValueError(
            f"scanned mesh too small ({extent:.3f} m diagonal) — 스캔 범위를 확인하세요"
        )
    if extent > MAX_EXTENT_M:
        raise ValueError(
            f"scanned mesh too large ({extent:.2f} m diagonal) — 사물 단위로 스캔하세요"
        )
    if len(vertices) < 3 or len(triangles) < 1:
        raise ValueError("scanned mesh has no triangles")

    # 자산 레코드 (갱신 허용 — 재스캔)
    asset = db.get(Asset, meta.assetId)
    if asset is None:
        asset = Asset(asset_id=meta.assetId, project_code=meta.projectId, space_id=space.id)
        db.add(asset)
    asset.bim_revision = meta.bimRevision
    asset.family = meta.family
    asset.asset_type = meta.type
    asset.design_pose = meta.designPose.to_json()
    asset.tolerance = (
        meta.tolerance.model_dump()
        if meta.tolerance is not None
        else {
            "positionMm": settings.default_position_tolerance_mm,
            "zToleranceMm": settings.default_z_tolerance_mm,
            "rotationDeg": settings.default_rotation_tolerance_deg,
        }
    )
    if meta.notes and not asset.notes:  # 백오피스에서 관리자가 고친 메모는 재등록으로 덮어쓰지 않는다
        asset.notes = meta.notes
    if meta.deviceId:
        asset.device_id = meta.deviceId
    if meta.name and not asset.name:
        asset.name = meta.name
    now = datetime.now(timezone.utc)
    asset.updated_at = now
    asset.scanned_at = _utc(meta.scannedAt) or now
    asset.geo = meta.geo.model_dump(exclude_none=True) if meta.geo else None

    uri, sha, _size = storage.save("assets", glb_bytes, suffix=".glb")
    asset.cad_uri = uri
    asset.cad_sha256 = sha
    asset.aabb = mesh_aabb(vertices)

    # ICP target 샘플 캐시 (자산 로컬 프레임, N×6 = xyz+법선) — 표면적 기반 밀도
    pts, nrm = sample_mesh_points(
        vertices, triangles, _sample_count(vertices, triangles), seed=hash(meta.assetId) % (2**32),
        return_normals=True,
    )
    np.save(_sample_cache_path(storage, meta.assetId), np.hstack([pts, nrm]).astype(np.float32))
    _outline_cache_path(storage, meta.assetId).unlink(missing_ok=True)  # 재등록이면 외곽선 다시 계산
    clear_photos(storage, meta.assetId)  # 재등록이면 이전 사진 실루엣은 낡았다 — 앱이 새로 올린다

    db.flush()
    return asset


def load_sample_points(storage: FileStorage, asset: Asset) -> tuple[np.ndarray, np.ndarray]:
    """자산 로컬 프레임의 표면 샘플 (points, normals) 각 (N,3) 미터.

    캐시는 N×6 (xyz+법선). 구버전 N×3 캐시는 법선을 재계산한다.
    """
    cache = _sample_cache_path(storage, asset.asset_id)
    if cache.exists():
        data = np.load(cache).astype(np.float64)
        if data.ndim == 2 and data.shape[1] >= 6:
            return data[:, :3], data[:, 3:6]
        return data, np.zeros_like(data)
    if asset.cad_uri is None:
        raise ValueError(f"asset has no CAD model: {asset.asset_id}")
    vertices, triangles = read_glb_mesh(storage.open_bytes(asset.cad_uri))
    pts, nrm = sample_mesh_points(
        vertices, triangles, _sample_count(vertices, triangles), seed=hash(asset.asset_id) % (2**32),
        return_normals=True,
    )
    np.save(cache, np.hstack([pts, nrm]).astype(np.float32))
    return pts, nrm


def delete_assets(db, storage: FileStorage, asset_ids: list[str]) -> tuple[list[str], list[str]]:
    """자산 삭제 — 관측/판정 결과·모델 파일·샘플 캐시·작업 목록의 참조까지 정리. (deleted, not_found) 반환.

    증거(Evidence) 원본 파일은 보존한다(원칙 13) — 관측 레코드의 참조만 사라진다.
    """
    from sqlalchemy import select

    from ..models import Asset, InspectionResult, InspectionTask, Observation

    deleted: list[str] = []
    missing: list[str] = []
    for aid in dict.fromkeys(asset_ids):  # 중복 제거, 순서 유지
        asset = db.get(Asset, aid)
        if asset is None:
            missing.append(aid)
            continue
        for obs in db.scalars(select(Observation).where(Observation.asset_id == aid)).all():
            for res in db.scalars(
                select(InspectionResult).where(InspectionResult.observation_id == obs.id)
            ).all():
                db.delete(res)
            db.delete(obs)
        for task in db.scalars(select(InspectionTask)).all():
            if aid in (task.asset_ids or []):
                task.asset_ids = [x for x in task.asset_ids if x != aid]
        if asset.cad_uri:
            # 같은 GLB(sha256)를 다른 자산이 쓰고 있으면 파일은 남긴다
            shared = db.scalar(
                select(Asset.asset_id).where(Asset.cad_uri == asset.cad_uri, Asset.asset_id != aid)
            )
            if shared is None:
                storage.delete(asset.cad_uri)
        cache = _sample_cache_path(storage, aid)
        if cache.exists():
            cache.unlink()
        _outline_cache_path(storage, aid).unlink(missing_ok=True)
        clear_photos(storage, aid)
        db.delete(asset)
        deleted.append(aid)
    db.flush()
    return deleted, missing
