"""디지털 트윈 서비스 — 스캔 세션/프레임 업로드, 비동기 재구성 잡, 구조·벽 저장, 외피, 투어 매니페스트, CAD 치환.

원칙: OBSERVED 원본(프레임·메시)은 덮어쓰거나 삭제하지 않는다. 새 스캔 = 새 MeshRevision. 추정·치환은 별도 레코드/파일.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from spatial_core.reconstruction.camera import Frame, intrinsics3, mat16, to_mat16
from spatial_core.reconstruction.envelope import SpaceInput, generate_envelope
from spatial_core.reconstruction.glb_export import write_colored_glb
from spatial_core.reconstruction.pipeline import FrameRef, ReconParams, reconstruct
from spatial_core.reconstruction.structure import Opening as StOpening
from spatial_core.reconstruction.structure import Structure
from spatial_core.reconstruction.structure import Wall as StWall

from .. import devlog
from ..models import Asset, Space
from ..models_twin import (
    AssetGeometryLink, Building, CadCatalog, CadReplacement, Floor, MeshRevision, ModelApproval, Opening, RegistrationRun, RoomTopology,
    ScanCoverage, ScanFrame, ScanQualityReport, ScanSegment, ScanSession, SpaceConnector, SpaceGeometry, SpatialTransformEdge,
    TextureAtlas, TourPortal, TourScene, TourViewpoint, Wall, WallThicknessAssumption,
)
from ..storage import FileStorage

MAX_FRAMES_PER_SESSION = 4000
MAX_PART_BYTES = {"rgb": 12_000_000, "depth": 4_000_000, "conf": 1_000_000}
PARTS = ("rgb", "depth", "conf")
MIN_FRAMES = 12
SESSION_MODES = ("SPACE", "FACILITY", "OBJECT")
OPEN_STATES = ("SCANNING", "PAUSED", "RECOVERY")


class TwinError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ───────────── 공통 조회 ─────────────

def resolve_space(db: Session, ref: str | int) -> Space:
    """정수 PK 또는 공간 코드(문자열)로 공간을 찾는다 (앱은 PK, 사람은 코드)."""
    s = None
    if str(ref).isdigit():
        s = db.get(Space, int(ref))
    if s is None:
        s = db.scalar(select(Space).where(Space.code == str(ref)))
    if s is None:
        raise TwinError(f"space not found: {ref}", 404)
    return s


def get_session(db: Session, sid: str) -> ScanSession:
    s = db.get(ScanSession, sid)
    if s is None:
        raise TwinError(f"scan session not found: {sid}", 404)
    return s


def _scan_dir(storage: FileStorage, sid: str) -> Path:
    d = storage.root / "scans" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ───────────── 건물·층·배치 ─────────────

def create_building(db: Session, project_code: str, name: str, gps: dict | None, building_type: str, default_thickness_m: float | None,
                    exterior_points: list | None) -> Building:
    from ..models import Project

    if db.get(Project, project_code) is None:
        db.add(Project(code=project_code, name=project_code))
    b = Building(project_code=project_code, name=name, gps=gps, building_type=building_type, default_thickness_m=default_thickness_m,
                 exterior_points=exterior_points or [], notice="외관/벽두께 일부 추정")
    db.add(b)
    db.flush()
    db.add(TourScene(building_id=b.id, name="기본 투어", entry={}))
    db.flush()
    return b


def create_portal(db: Session, b: Building, from_ref, to_ref, position: list[float], yaw_deg: float, width: float, height: float,
                  kind: str = "DOOR", name: str = "", tour: TourScene | None = None) -> SpaceConnector:
    if len(position) != 3:
        raise TwinError("positionBuilding은 [x,y,z]")
    fs = resolve_space(db, from_ref) if from_ref not in (None, "") else None
    ts = resolve_space(db, to_ref) if to_ref not in (None, "") else None
    for s_ in (fs, ts):
        if s_ is not None and s_.building_id != b.id:
            raise TwinError(f"공간 {s_.code}은(는) 이 건물에 배치되지 않았습니다", 409)
    c = SpaceConnector(building_id=b.id, from_space_id=fs.id if fs else None, to_space_id=ts.id if ts else None,
                       kind="ENTRANCE" if (ts is None and kind == "DOOR") else kind, position=[float(x) for x in position], yaw_deg=float(yaw_deg),
                       width_m=float(width), height_m=float(height), name=name, source_kind="DESIGN", review_status="REVIEW_REQUIRED",
                       note="사람이 지정")
    db.add(c)
    db.flush()
    if tour is not None:
        n = len(db.scalars(select(TourPortal.id).where(TourPortal.tour_id == tour.id)).all())
        db.add(TourPortal(tour_id=tour.id, connector_id=c.id, label=name, order_no=n))
    return c


def place_space(db: Session, space: Space, building_id: int, floor_id: int | None, t16: list[float] | None, kind: str) -> dict:
    b = db.get(Building, building_id)
    if b is None:
        raise TwinError("building not found", 404)
    fl = db.get(Floor, floor_id) if floor_id else None
    if floor_id and (fl is None or fl.building_id != building_id):
        raise TwinError("floor not found in building", 404)
    review = "APPROVED"
    if t16 is None:
        # 자동 배치: 같은 층의 기존 방들 오른쪽에 한 줄로 (건물 내 상대 위치는 모르므로 REVIEW_REQUIRED)
        others = db.scalars(select(Space).where(Space.building_id == building_id, Space.floor_id == floor_id, Space.id != space.id)).all()
        x = 0.0
        for o in others:
            rev = current_revision(db, o.id)
            if rev and rev.bbox and o.t_building_space:
                T = mat16(o.t_building_space)
                hi = np.array(rev.bbox[1]) @ T[:3, :3].T + T[:3, 3]
                x = max(x, float(hi[0]) + 0.5)
        T = np.eye(4)
        T[0, 3] = x
        T[1, 3] = fl.elevation_m if fl else 0.0
        t16 = to_mat16(T)
        review = "REVIEW_REQUIRED"
    elif len(t16) != 16:
        raise TwinError("tBuildingSpace는 16개 값(열 우선)이어야 합니다")
    space.building_id, space.floor_id, space.t_building_space, space.kind = building_id, floor_id, [float(v) for v in t16], kind
    space.updated_at = _now()
    db.add(SpatialTransformEdge(building_id=building_id, from_frame=f"space:{space.code}", to_frame=f"building:{building_id}", matrix=space.t_building_space,
                                kind="PLACEMENT", review_status=review))
    db.flush()
    return {"spaceId": space.code, "numericId": space.id, "buildingId": building_id, "floorId": floor_id, "tBuildingSpace": space.t_building_space,
            "kind": kind, "reviewStatus": review}


# ───────────── 스캔 세션·프레임 업로드 ─────────────

def create_session(db: Session, space: Space, mode: str, device_id: str, device_profile: dict, capabilities: dict,
                   world_map_revision: str | None, relocalized: bool) -> ScanSession:
    if mode not in SESSION_MODES:
        raise TwinError(f"mode는 {SESSION_MODES} 중 하나")
    registered = True
    msg = ""
    if space.worldmap_uri and not relocalized:
        registered = False  # 월드맵이 있는데 재위치 인식에 실패 — 기존 공간에 자동 병합 금지
        msg = "UNREGISTERED: 월드맵 재위치 인식 실패 — 기존 공간 지오메트리에 자동 병합하지 않습니다"
    s = ScanSession(space_id=space.id, mode=mode, device_id=device_id, device_profile=device_profile, capabilities=capabilities,
                    world_map_revision=world_map_revision, relocalized=relocalized, status="SCANNING", registered=registered, message=msg)
    db.add(s)
    db.flush()
    return s


def _validate_meta(meta: dict) -> None:
    if len(meta.get("cameraTransform", [])) != 16 or len(meta.get("intrinsics", [])) != 9:
        raise TwinError("cameraTransform은 16개, intrinsics는 9개 값(열 우선)이어야 합니다")
    for k in ("imageWidth", "imageHeight", "depthWidth", "depthHeight"):
        if int(meta.get(k, 0)) <= 0:
            raise TwinError(f"{k}가 필요합니다")
    if int(meta["depthWidth"]) * int(meta["depthHeight"]) > 4_000_000:
        raise TwinError("depth 해상도가 너무 큽니다")
    arr = np.asarray(meta["cameraTransform"], float)
    if not np.isfinite(arr).all():
        raise TwinError("cameraTransform에 유한하지 않은 값")


def init_upload(db: Session, sess: ScanSession, meta: dict, parts: dict, idem: str | None) -> dict:
    if sess.status not in OPEN_STATES + ("PROCESSING", "NEED_MORE_CAPTURE"):
        raise TwinError(f"세션 상태가 {sess.status} 이라 프레임을 받을 수 없습니다", 409)
    _validate_meta(meta)
    idx = int(meta.get("frameIndex", -1))
    if idx < 0:
        raise TwinError("frameIndex 필요")
    dw, dh = int(meta["depthWidth"]), int(meta["depthHeight"])
    expect = {"depth": dw * dh * 4, "conf": dw * dh}
    for p in PARTS:
        if p not in parts or int(parts[p].get("bytes", 0)) <= 0:
            raise TwinError(f"parts.{p}.bytes 필요")
        if int(parts[p]["bytes"]) > MAX_PART_BYTES[p]:
            raise TwinError(f"{p} 파트가 상한({MAX_PART_BYTES[p]}B)을 넘습니다", 413)
        if p in expect and int(parts[p]["bytes"]) != expect[p]:
            raise TwinError(f"{p} 크기 {parts[p]['bytes']} ≠ 기대 {expect[p]}")
        if len(str(parts[p].get("sha256", ""))) != 64:
            raise TwinError(f"{p} sha256(64자 hex) 필요")
    fr = db.scalar(select(ScanFrame).where(ScanFrame.session_id == sess.id, ScanFrame.frame_index == idx))
    if fr is not None and fr.state == "DONE":
        return {"state": "DONE", "frameIndex": idx}
    if fr is None:
        if db.scalar(select(ScanFrame.id).where(ScanFrame.session_id == sess.id).offset(MAX_FRAMES_PER_SESSION)) is not None:
            raise TwinError("세션 프레임 상한 초과", 413)
        fr = ScanFrame(session_id=sess.id, frame_index=idx, segment_index=int(meta.get("segmentIndex", 0)), meta=meta, idempotency_key=idem,
                       parts={p: {"bytes": int(parts[p]["bytes"]), "sha256": parts[p]["sha256"].lower(), "received": 0} for p in PARTS})
        db.add(fr)
    else:  # 같은 프레임 재선언 — 해시/크기가 바뀌었으면 받은 조각을 버리고 다시 시작
        old = fr.parts or {}
        newp = {}
        for p in PARTS:
            same = old.get(p, {}).get("sha256") == parts[p]["sha256"].lower() and old.get(p, {}).get("bytes") == int(parts[p]["bytes"])
            newp[p] = {"bytes": int(parts[p]["bytes"]), "sha256": parts[p]["sha256"].lower(), "received": old[p]["received"] if same else 0}
        fr.parts, fr.meta = newp, meta
        fr.segment_index = int(meta.get("segmentIndex", fr.segment_index))
    db.flush()
    return {"state": "PENDING", "frameIndex": idx, "received": {p: fr.parts[p]["received"] for p in PARTS}}


def _part_path(storage: FileStorage, sid: str, idx: int, part: str) -> Path:
    ext = {"rgb": "jpg", "depth": "depth", "conf": "conf"}[part]
    return _scan_dir(storage, sid) / f"{idx:06d}.{ext}"


def put_part(db: Session, storage: FileStorage, sess: ScanSession, idx: int, part: str, offset: int, data: bytes, sha_header: str | None) -> dict:
    if part not in PARTS:
        raise TwinError("part는 rgb|depth|conf", 404)
    fr = db.scalar(select(ScanFrame).where(ScanFrame.session_id == sess.id, ScanFrame.frame_index == idx))
    if fr is None:
        raise TwinError("init-upload를 먼저 호출하세요", 409)
    if fr.state == "DONE":
        return {"received": fr.parts[part]["bytes"], "complete": True}
    info = dict(fr.parts[part])
    if offset != info["received"]:
        raise TwinError(f"offset 불일치: 서버가 받은 바이트 {info['received']}", 409)
    if offset + len(data) > info["bytes"]:
        raise TwinError("선언한 크기를 넘는 데이터", 413)
    path = _part_path(storage, sess.id, idx, part)
    tmp = path.with_suffix(path.suffix + ".part")
    mode = "r+b" if tmp.exists() and offset > 0 else "wb"
    with open(tmp, mode) as f:
        f.seek(offset)
        f.write(data)
    info["received"] = offset + len(data)
    complete = info["received"] == info["bytes"]
    if complete:
        raw = tmp.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expect = (sha_header or info["sha256"]).lower()
        if digest != info["sha256"] or (sha_header and digest != expect):
            tmp.unlink(missing_ok=True)
            info["received"] = 0
            fr.parts = {**fr.parts, part: info}
            db.flush()
            raise TwinError("sha256 불일치 — 파트를 처음부터 다시 보내세요", 422)
        os.replace(tmp, path)
        info["uri"] = f"scans/{sess.id}/{path.name}"
    fr.parts = {**fr.parts, part: info}
    db.flush()
    return {"received": info["received"], "complete": complete}


def complete_upload(db: Session, sess: ScanSession, idx: int) -> dict:
    fr = db.scalar(select(ScanFrame).where(ScanFrame.session_id == sess.id, ScanFrame.frame_index == idx))
    if fr is None:
        raise TwinError("frame not found", 404)
    if fr.state != "DONE":
        missing = [p for p in PARTS if fr.parts[p].get("received") != fr.parts[p]["bytes"] or not fr.parts[p].get("uri")]
        if missing:
            raise TwinError(f"아직 덜 올라온 파트: {', '.join(missing)}", 409)
        fr.state = "DONE"
    sess.frame_count = len(db.scalars(select(ScanFrame.id).where(ScanFrame.session_id == sess.id, ScanFrame.state == "DONE")).all())
    db.flush()
    return {"state": "DONE", "frameIndex": idx, "frames": sess.frame_count}


def save_structure(db: Session, storage: FileStorage, sess: ScanSession, js: dict) -> None:
    if not isinstance(js.get("walls"), list):
        raise TwinError("space-structure.json에는 walls 배열이 필요합니다")
    for w in js["walls"]:
        if len(w.get("transform16", [])) != 16 or float(w.get("widthM", 0)) <= 0 or float(w.get("heightM", 0)) <= 0:
            raise TwinError("wall에는 transform16(16), widthM, heightM이 필요합니다")
    data = json.dumps(js, ensure_ascii=False).encode()
    if len(data) > 4_000_000:
        raise TwinError("structure가 너무 큽니다", 413)
    path = _scan_dir(storage, sess.id) / "structure.json"
    path.write_bytes(data)
    sess.structure_uri = f"scans/{sess.id}/structure.json"
    db.flush()


def finalize_session(db: Session, sess: ScanSession, reason: str) -> dict:
    if sess.status not in OPEN_STATES + ("PROCESSING", "NEED_MORE_CAPTURE"):
        raise TwinError(f"세션 상태가 {sess.status}", 409)
    done = db.scalars(select(ScanFrame).where(ScanFrame.session_id == sess.id, ScanFrame.state == "DONE", ScanFrame.excluded.is_(False))).all()
    warnings: list[str] = []
    sectors = set()
    heights = []
    for f in done:
        T = mat16(f.meta["cameraTransform"])
        fwd = -T[:3, 2]
        sectors.add(int(((np.degrees(np.arctan2(fwd[2], fwd[0])) + 360) % 360) // 30))
        heights.append(float(T[1, 3]))
    if len(done) < MIN_FRAMES:
        warnings.append(f"프레임 {len(done)}개 — 최소 {MIN_FRAMES}개 필요")
    if sess.mode == "SPACE" and len(sectors) < 8:
        warnings.append(f"촬영 방향이 {len(sectors)}/12 구간뿐 — 방 전체를 둘러 촬영하세요")
    bad = [f for f in done if f.meta.get("trackingState") not in (None, "normal")]
    if done and len(bad) / len(done) > 0.3:
        warnings.append("추적 불량 프레임 비율이 높음 — 저조도/가속 움직임 확인")
    sess.ended_reason, sess.ended_at = reason, _now()
    sess.status = "NEED_MORE_CAPTURE" if len(done) < MIN_FRAMES else "PROCESSING"
    sess.message = " · ".join(warnings)
    db.add(ScanQualityReport(session_id=sess.id, quality="POOR" if len(done) < MIN_FRAMES else ("REVIEW" if warnings else "GOOD"),
                             report={"frames": len(done), "azimuthSectors": len(sectors), "warnings": warnings, "excludedFrames": 0}))
    db.flush()
    return {"status": sess.status, "frames": len(done), "warnings": warnings}


# ───────────── 재구성 잡 ─────────────

class TwinWorker:
    """재구성은 CPU/메모리가 크다 — 동시 1개(환경변수로 조정), 상태는 DB에 남겨 재시작 후에도 조회 가능."""

    def __init__(self, session_factory, storage: FileStorage) -> None:
        self.sf, self.storage = session_factory, storage
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("MARKERLESS_RECON_WORKERS", "1")), thread_name_prefix="recon")
        self.running: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, key: str, fn, *a) -> bool:
        with self._lock:
            if key in self.running:
                return False
            self.running.add(key)

        def run():
            try:
                fn(*a)
            finally:
                with self._lock:
                    self.running.discard(key)

        self.pool.submit(run)
        return True


def _make_loader(storage: FileStorage, sid: str, fr: ScanFrame):
    meta = fr.meta

    def load(max_px: int) -> Frame:
        from PIL import Image

        w0, h0 = int(meta["imageWidth"]), int(meta["imageHeight"])
        rgb = None
        if max_px > 0:  # max_px=0 → RGB 없이 깊이·포즈만 (포즈 그래프 사전 패스)
            with Image.open(storage.root / fr.parts["rgb"]["uri"]) as im:
                im.draft("RGB", (max_px, max_px))
                im = im.convert("RGB")
                scale = min(1.0, max_px / max(im.size))
                if scale < 1.0:
                    im = im.resize((max(1, int(im.size[0] * scale)), max(1, int(im.size[1] * scale))))
                rgb = np.asarray(im)
        dw, dh = int(meta["depthWidth"]), int(meta["depthHeight"])
        depth = np.frombuffer((storage.root / fr.parts["depth"]["uri"]).read_bytes(), dtype="<f4").reshape(dh, dw).copy()
        depth[~np.isfinite(depth)] = 0
        conf = np.frombuffer((storage.root / fr.parts["conf"]["uri"]).read_bytes(), dtype=np.uint8).reshape(dh, dw).copy()
        K = intrinsics3(meta["intrinsics"])  # 원본 RGB 해상도 기준
        # 리사이즈된 rgb는 같은 시야를 덮으므로 K는 원본 기준 유지하고 rgb_size에 원본을 둔다 (Frame.K_depth가 비율로 환산)
        return Frame(index=fr.frame_index, T_world_cam=mat16(meta["cameraTransform"]), K_rgb=K, rgb_size=(w0, h0), depth=depth, conf=conf, rgb=rgb,
                     blur=float(meta.get("motionBlurScore", 0.0)))

    return load


def _asset_boxes(sf, space_id: int) -> list[dict]:
    """공간 안 활성 자산의 AABB(공간 프레임) — 사물 후보와의 겹침(IoU) 계산용."""
    out = []
    with sf() as db:
        for a in db.scalars(select(Asset).where(Asset.space_id == space_id, Asset.status == "ACTIVE")).all():
            if not a.aabb:
                continue
            pos = (a.design_pose or {}).get("positionM", {})
            p = np.array([pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)], float)
            out.append({"assetId": a.asset_id, "min": (np.array(a.aabb["minM"]) + p).tolist(), "max": (np.array(a.aabb["maxM"]) + p).tolist()})
    return out


def _set_progress(sf, sid: str, status: str | None = None, stage: str | None = None, progress: float | None = None, message: str | None = None) -> None:
    with sf() as db:
        s = db.get(ScanSession, sid)
        if s is None:
            return
        if status:
            s.status = status
        if stage is not None:
            s.stage = stage
        if progress is not None:
            s.progress = progress
        if message is not None:
            s.message = message[:500]
        db.commit()


def run_reconstruction(sf, storage: FileStorage, space_id: int, session_ids: list[str], params: dict, label_session: str) -> None:
    """프레임 → 재구성 → 리비전/벽/개구부/커버리지 저장. 예외는 FAILED 상태와 메시지로 남긴다."""
    try:
        _set_progress(sf, label_session, status="PROCESSING", stage="QUEUED", progress=0.0, message="재구성 시작")
        refs: list[FrameRef] = []
        prior: dict | None = None
        seg_offset = 0
        with sf() as db:
            for k, sid in enumerate(session_ids):
                sess = db.get(ScanSession, sid)
                frames = db.scalars(select(ScanFrame).where(ScanFrame.session_id == sid, ScanFrame.state == "DONE").order_by(ScanFrame.frame_index)).all()
                for fr in frames:
                    m = fr.meta
                    refs.append(FrameRef(fr.frame_index + k * 1_000_000, seg_offset + fr.segment_index, m.get("trackingState", "normal"),
                                         float(m.get("motionBlurScore", 0.0)), bool(fr.excluded), _make_loader(storage, sid, fr)))
                seg_offset += 1000
                if prior is None and sess.structure_uri:
                    try:
                        prior = json.loads(storage.open_bytes(sess.structure_uri))
                    except Exception:  # noqa: BLE001
                        prior = None
        p = ReconParams(**{k: v for k, v in params.items() if k in ReconParams.__dataclass_fields__})
        asset_boxes = _asset_boxes(sf, space_id)

        def prog(stage: str, frac: float, msg: str) -> None:
            _set_progress(sf, label_session, stage="RUNNING:" + stage, progress=round(frac, 3), message=msg)

        result = reconstruct(refs, p, prior, prog, asset_boxes)
        with sf() as db:
            _persist_result(db, storage, space_id, session_ids, label_session, result)
            db.commit()
    except Exception as e:  # noqa: BLE001
        devlog.write("server", "ERROR", "recon", f"재구성 실패 {label_session}: {e}", trace=traceback.format_exc()[-2500:])
        _set_progress(sf, label_session, status="FAILED", stage="FAILED", message=f"재구성 실패: {e}")


def _save_file(storage: FileStorage, space_id: int, rev_id: str, kind: str, data: bytes, ext: str = "glb") -> str:
    d = storage.root / "twin" / str(space_id) / rev_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{kind}.{ext}").write_bytes(data)
    return f"twin/{space_id}/{rev_id}/{kind}.{ext}"


def current_revision(db: Session, space_id: int) -> MeshRevision | None:
    return db.scalar(select(MeshRevision).where(MeshRevision.space_id == space_id, MeshRevision.is_current.is_(True)).order_by(MeshRevision.created_at.desc()))


def _persist_result(db: Session, storage: FileStorage, space_id: int, session_ids: list[str], label_session: str, res) -> None:
    sess = db.get(ScanSession, label_session)
    if res.status == "FAILED" or not res.files:
        sess.status, sess.stage, sess.message = res.status if res.status != "READY" else "FAILED", "DONE", res.report.get("reason", "재구성 실패")
        sess.quality = res.quality
        db.add(ScanQualityReport(session_id=sess.id, quality=res.quality, report=res.report))
        return
    old = current_revision(db, space_id)
    rev = MeshRevision(space_id=space_id, session_ids=session_ids, status=res.status, quality=res.quality, coverage=res.coverage, bbox=list(res.bbox) if res.bbox else None,
                       stats=res.report, axes_yaw=res.axes_yaw, review_status="AUTO" if res.quality == "GOOD" else "REVIEW_REQUIRED", is_current=True)
    db.add(rev)
    db.flush()
    files = {k: _save_file(storage, space_id, rev.id, k, v) for k, v in res.files.items()}
    files["structure"] = _save_file(storage, space_id, rev.id, "structure", json.dumps(res.structure, ensure_ascii=False).encode(), "json")
    rev.files = files
    if old is not None:
        old.is_current = False  # 이전 리비전은 보존 (복원 가능)
    tex = res.report.get("texture", {})
    db.add(TextureAtlas(revision_id=rev.id, px=tex.get("atlasPx", 0), texel_cm=tex.get("texelCm", 0.0), texture_coverage=tex.get("textureCoverage", 0.0),
                        unobserved_faces=tex.get("unobservedFaces", 0), frames_used=tex.get("framesUsed", 0)))
    cov = res.structure.get("coverage", {})
    db.add(ScanCoverage(revision_id=rev.id, space_id=space_id, overall=cov.get("overall", 0.0), floor=cov.get("floor", 0.0), ceiling=cov.get("ceiling", 0.0),
                        unobserved_area_m2=cov.get("unobservedAreaM2", 0.0), detail=cov))
    kinds = {"observed": ("MESH", "OBSERVED"), "textured": ("TEXTURED", "OBSERVED"), "inferred": ("INFERRED", "INFERRED"), "coverage": ("COVERAGE", "OBSERVED"),
             "structure": ("STRUCTURE", "OBSERVED")}
    for k, (kind, sk) in kinds.items():
        if k in files:
            db.add(SpaceGeometry(space_id=space_id, revision_id=rev.id, source_kind=sk, kind=kind, uri=files[k], confidence=0.7 if sk == "OBSERVED" else 0.4,
                                 uncertainty_m=0.03 if sk == "OBSERVED" else None, session_ids=session_ids,
                                 review_status="AUTO" if sk == "OBSERVED" and res.quality == "GOOD" else "REVIEW_REQUIRED"))
    _store_walls(db, space_id, rev, res.structure, old)
    rev.stats = {**rev.stats, "objects": res.objects}
    for box in _asset_boxes_db(db, space_id):  # 자산↔공간 메시 연결(이중 표면 방지 영역). 마스크 방식은 기본 space-background-only
        link = db.scalar(select(AssetGeometryLink).where(AssetGeometryLink.asset_id == box["assetId"], AssetGeometryLink.space_id == space_id))
        if link is None:
            link = AssetGeometryLink(asset_id=box["assetId"], space_id=space_id)
            db.add(link)
        link.revision_id, link.bbox_space = rev.id, {"min": box["min"], "max": box["max"]}
    for sid in session_ids:
        s = db.get(ScanSession, sid)
        if s:
            s.revision_id = rev.id
    sess.status, sess.stage, sess.progress = res.status, "DONE", 1.0
    sess.quality, sess.coverage, sess.revision_id = res.quality, res.coverage, rev.id
    sess.message = " · ".join(res.report.get("notes", []))[:500]
    db.add(ScanQualityReport(session_id=sess.id, quality=res.quality, report=res.report))
    for seg, info in (res.report.get("segments") or {}).items():
        db.add(ScanSegment(session_id=sess.id, segment_index=int(seg), frame_count=info.get("frames", 0), aligned=bool(info.get("accepted", True)),
                           fitness=info.get("fitness"), reason=info.get("reason", "")))
        if "fitness" in info:
            db.add(RegistrationRun(space_id=space_id, src_session_id=sess.id, dst_ref=rev.id, fitness=info["fitness"], rmse_m=info.get("rmse", 0.0),
                                   accepted=bool(info.get("accepted")), ambiguous=bool(info.get("ambiguous")), reason=info.get("reason", "")))


def _asset_boxes_db(db: Session, space_id: int) -> list[dict]:
    out = []
    for a in db.scalars(select(Asset).where(Asset.space_id == space_id, Asset.status == "ACTIVE")).all():
        if not a.aabb:
            continue
        pos = (a.design_pose or {}).get("positionM", {})
        p = np.array([pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)], float)
        out.append({"assetId": a.asset_id, "min": (np.array(a.aabb["minM"]) + p).tolist(), "max": (np.array(a.aabb["maxM"]) + p).tolist()})
    return out


def _store_walls(db: Session, space_id: int, rev: MeshRevision, structure: dict, old: MeshRevision | None) -> None:
    prev = db.scalars(select(Wall).where(Wall.space_id == space_id, Wall.revision_id == old.id)).all() if old else []
    for w in structure.get("walls", []):
        wall = Wall(space_id=space_id, revision_id=rev.id, wall_no=int(w["id"]), geom={k: w[k] for k in ("p0", "p1", "yMin", "yMax", "normal", "lengthM", "heightM")},
                    coverage=float(w.get("coverage", 0)), source_kind=w.get("sourceKind", "OBSERVED"), confidence=float(w.get("confidence", 0.5)))
        # 사람이 정한 두께/승인은 새 리비전에도 이어받는다 (같은 위치·평행한 벽)
        mid = (np.array(w["p0"]) + np.array(w["p1"])) / 2
        for pw in prev:
            if pw.thickness_method in ("BIM", "MEASURED") or pw.review_status in ("APPROVED", "REJECTED"):
                pm = (np.array(pw.geom["p0"]) + np.array(pw.geom["p1"])) / 2
                if np.linalg.norm(pm - mid) < 0.3 and abs(float(np.dot(w["normal"], pw.geom["normal"]))) > 0.97:
                    wall.thickness_m, wall.thickness_method, wall.review_status, wall.note, wall.confidence = pw.thickness_m, pw.thickness_method, pw.review_status, pw.note, pw.confidence
                    break
        db.add(wall)
        db.flush()
        for o in structure.get("openings", []):
            if o["wallId"] == w["id"]:
                db.add(Opening(wall_id=wall.id, kind=o["kind"], t0=o["t0"], t1=o["t1"], y0=o["y0"], y1=o["y1"], source_kind=o.get("sourceKind", "INFERRED"),
                               review_status=o.get("reviewStatus", "REVIEW_REQUIRED"), confidence=o.get("confidence", 0.4)))


# ───────────── 커버리지·리비전 조회 ─────────────

def coverage_info(db: Session, space: Space, url_for) -> dict:
    rev = current_revision(db, space.id)
    if rev is None:
        raise TwinError("아직 재구성된 지오메트리가 없습니다", 404)
    c = db.scalar(select(ScanCoverage).where(ScanCoverage.revision_id == rev.id))
    d = c.detail if c else {}
    return {"revisionId": rev.id, "overall": d.get("overall", rev.coverage), "floor": d.get("floor", 0.0), "ceiling": d.get("ceiling", 0.0),
            "walls": d.get("walls", []), "unobservedAreaM2": d.get("unobservedAreaM2", 0.0), "quality": rev.quality,
            "heatmapUrl": url_for("coverage", rev.id) if "coverage" in rev.files else None}


def revisions_info(db: Session, space: Space, url_for) -> list[dict]:
    out = []
    for r in db.scalars(select(MeshRevision).where(MeshRevision.space_id == space.id).order_by(MeshRevision.created_at.desc())).all():
        out.append({"revisionId": r.id, "createdAt": iso(r.created_at), "sessionIds": r.session_ids, "quality": r.quality, "coverage": r.coverage,
                    "current": r.is_current, "reviewStatus": r.review_status, "approved": r.review_status == "APPROVED",
                    "sourceKinds": ["OBSERVED"] + (["INFERRED"] if "inferred" in r.files else []),
                    "files": {k: url_for(k, r.id) for k in r.files if k != "structure"}})
    return out


# ───────────── 외피 ─────────────

def structure_from_json(js: dict) -> Structure:
    walls = [StWall(int(w["id"]), tuple(w["p0"]), tuple(w["p1"]), float(w["yMin"]), float(w["yMax"]), tuple(w["normal"]),
                    coverage=float(w.get("coverage", 0)), source_kind=w.get("sourceKind", "OBSERVED"), confidence=float(w.get("confidence", 0.5))) for w in js.get("walls", [])]
    ops = [StOpening(o["kind"], int(o["wallId"]), float(o["t0"]), float(o["t1"]), float(o["y0"]), float(o["y1"]), o.get("sourceKind", "INFERRED"),
                     o.get("reviewStatus", "REVIEW_REQUIRED"), float(o.get("confidence", 0.4))) for o in js.get("openings", [])]
    return Structure(js.get("floorY"), js.get("ceilingY"), walls, ops, [tuple(p) for p in js.get("polygon", [])])


def generate_building_envelope(db: Session, storage: FileStorage, building: Building, default_thickness: float | None, building_type: str | None,
                               actor: str = "system") -> dict:
    spaces = db.scalars(select(Space).where(Space.building_id == building.id)).all()
    inputs: list[SpaceInput] = []
    walls_db: dict[tuple[str, int], Wall] = {}
    overrides: dict = {}
    for sp in spaces:
        rev = current_revision(db, sp.id)
        if rev is None or "structure" not in rev.files or not sp.t_building_space:
            continue
        st = structure_from_json(json.loads(storage.open_bytes(rev.files["structure"])))
        inputs.append(SpaceInput(sp.code, mat16(sp.t_building_space), st))
        for w in db.scalars(select(Wall).where(Wall.space_id == sp.id, Wall.revision_id == rev.id)).all():
            walls_db[(sp.code, w.wall_no)] = w
            if w.thickness_m is not None and (w.thickness_method in ("BIM", "MEASURED") or w.review_status == "APPROVED"):
                overrides[(sp.code, w.wall_no)] = (w.thickness_m, w.thickness_method, w.note or "관리자 지정")
    if not inputs:
        raise TwinError("건물에 재구성된 공간이 없습니다 — 공간을 배치(placement)하고 스캔을 재구성하세요", 409)
    if building_type:
        building.building_type = building_type
    if default_thickness is not None:
        building.default_thickness_m = default_thickness
    env = generate_envelope(inputs, building.building_type or "default", building.default_thickness_m, overrides=overrides)
    by_code = {sp.code: sp for sp in spaces}
    for rec in env.walls:
        w = walls_db.get((rec.space_key, rec.wall_id))
        if w is None:
            continue
        w.kind = rec.kind
        w.neighbor_space_id = by_code[rec.neighbor].id if rec.neighbor else None
        if (rec.space_key, rec.wall_id) not in overrides:
            w.thickness_m, w.thickness_method, w.confidence, w.note = rec.thickness, rec.method, rec.confidence, rec.note
            w.review_status = "REVIEW_REQUIRED"
            db.add(WallThicknessAssumption(wall_id=w.id, thickness_m=rec.thickness, method=rec.method, note=rec.note, actor=actor))
        w.updated_at = _now()
    building.envelope_revision += 1
    if not env.mesh.empty:
        data = write_colored_glb(env.mesh, {"sourceKind": "INFERRED", "reviewStatus": "REVIEW_REQUIRED", "notices": env.notices}, name="envelope", alpha=1.0)
        d = storage.root / "twin" / "envelope"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{building.id}.glb").write_bytes(data)
        building.envelope_uri = f"twin/envelope/{building.id}.glb"
    building.envelope_review = "REVIEW_REQUIRED"
    # 출입구 포털: 이미 있는 가까운 포털이 없으면 추가
    existing = db.scalars(select(SpaceConnector).where(SpaceConnector.building_id == building.id)).all()
    for e in env.entrances:
        if any(c.to_space_id is None and c.position and np.linalg.norm(np.array(c.position) - np.array(e.position)) < 0.6 for c in existing):
            continue
        db.add(SpaceConnector(building_id=building.id, from_space_id=by_code[e.space_key].id, to_space_id=None, kind="ENTRANCE", position=list(e.position),
                              yaw_deg=e.yaw_deg, width_m=e.width, height_m=e.height, exterior_viewpoint=list(e.exterior_viewpoint), name=f"출입구 {e.id}",
                              note="외벽 문 개구부에서 자동 추정", source_kind="INFERRED", review_status="REVIEW_REQUIRED"))
    # 인접 관계
    for old in db.scalars(select(RoomTopology).where(RoomTopology.building_id == building.id)).all():
        db.delete(old)
    seen = set()
    for rec in env.walls:
        if rec.neighbor:
            key = tuple(sorted([rec.space_key, rec.neighbor]))
            if key in seen:
                continue
            seen.add(key)
            db.add(RoomTopology(building_id=building.id, space_a=by_code[key[0]].id, space_b=by_code[key[1]].id, relation="ADJACENT", shared_wall_gap_m=rec.thickness))
    db.flush()
    return {"buildingId": building.id, "envelopeRevision": building.envelope_revision, "walls": len(env.walls), "entrances": len(env.entrances),
            "notices": env.notices, "exterior": sum(1 for w in env.walls if w.kind == "EXTERIOR"), "interior": sum(1 for w in env.walls if w.kind == "INTERIOR")}


def set_wall_thickness(db: Session, storage: FileStorage, wall: Wall, thickness: float, method: str, note: str, actor: str) -> Wall:
    if method not in ("MEASURED", "BIM", "CONFIG_DEFAULT", "INFERRED"):
        raise TwinError("method는 MEASURED|BIM|CONFIG_DEFAULT|INFERRED")
    if not (0.03 <= thickness <= 2.0):
        raise TwinError("두께는 3cm~2m 범위")
    wall.thickness_m, wall.thickness_method, wall.note = float(thickness), method, note or "관리자 지정"
    wall.confidence = 0.9 if method in ("BIM", "MEASURED") else 0.4
    wall.review_status = "APPROVED" if method in ("BIM", "MEASURED") else "REVIEW_REQUIRED"
    wall.updated_at = _now()
    db.add(WallThicknessAssumption(wall_id=wall.id, thickness_m=float(thickness), method=method, note=note, actor=actor))
    db.add(ModelApproval(target_type="wall", target_id=str(wall.id), action="EDIT", actor=actor, comment=f"{thickness:.3f}m {method} {note}"[:300]))
    db.flush()
    sp = db.get(Space, wall.space_id)
    if sp and sp.building_id:
        b = db.get(Building, sp.building_id)
        generate_building_envelope(db, storage, b, None, None, actor)  # 두께가 바뀌면 외피를 즉시 다시 만든다
    return wall


def set_review(db: Session, target_type: str, obj, status: str, actor: str, comment: str = "") -> None:
    if status not in ("AUTO", "REVIEW_REQUIRED", "APPROVED", "REJECTED"):
        raise TwinError("reviewStatus가 올바르지 않습니다")
    obj.review_status = status
    db.add(ModelApproval(target_type=target_type, target_id=str(getattr(obj, "id", "")), action={"APPROVED": "APPROVE", "REJECTED": "REJECT"}.get(status, "EDIT"),
                         actor=actor, comment=comment[:300]))
    if hasattr(obj, "approved_by"):
        obj.approved_by = actor if status == "APPROVED" else None
    db.flush()


# ───────────── 투어 매니페스트 ─────────────

def connector_json(c: SpaceConnector, code_of: dict[int, str]) -> dict:
    return {"id": c.id, "kind": c.kind, "name": c.name, "fromSpace": code_of.get(c.from_space_id), "toSpace": code_of.get(c.to_space_id) if c.to_space_id else None,
            "positionBuilding": c.position, "yawDeg": c.yaw_deg, "widthM": c.width_m, "heightM": c.height_m, "state": c.state, "sourceKind": c.source_kind,
            "reviewStatus": c.review_status, "exteriorViewpoint": c.exterior_viewpoint}


def tour_manifest(db: Session, storage: FileStorage, b: Building, url_for) -> dict:
    floors = db.scalars(select(Floor).where(Floor.building_id == b.id).order_by(Floor.elevation_m)).all()
    spaces = db.scalars(select(Space).where(Space.building_id == b.id)).all()
    code_of = {s.id: s.code for s in db.scalars(select(Space)).all()}
    walls_rows = []
    spaces_out, assets_out = [], []
    for sp in spaces:
        rev = current_revision(db, sp.id)
        T = mat16(sp.t_building_space) if sp.t_building_space else np.eye(4)
        item = {"spaceId": sp.code, "numericId": sp.id, "name": sp.name or sp.code, "floorId": sp.floor_id, "kind": sp.kind, "tBuildingSpace": to_mat16(T),
                "revisionId": None, "glbUrl": None, "texturedGlbUrl": None, "lod": {"web": None, "mobile": None}, "bboxSpace": None, "coverage": 0.0,
                "quality": None, "sourceKinds": [], "unobservedMask": False,
                "layers": {"observed": None, "inferred": None, "design": None, "replacement": None, "coverageHeat": None}, "placementReview": None}
        if rev is not None:
            f = rev.files
            item.update({"revisionId": rev.id, "glbUrl": url_for("observed", rev.id) if "observed" in f else None,
                         "texturedGlbUrl": url_for("textured", rev.id) if "textured" in f else None,
                         "lod": {"web": url_for("lodweb", rev.id) if "lodweb" in f else None, "mobile": url_for("lodmobile", rev.id) if "lodmobile" in f else None},
                         "bboxSpace": {"min": rev.bbox[0], "max": rev.bbox[1]} if rev.bbox else None, "coverage": rev.coverage, "quality": rev.quality,
                         "sourceKinds": ["OBSERVED"] + (["INFERRED"] if "inferred" in f else []), "unobservedMask": "textured" in f,
                         "layers": {"observed": url_for("observed", rev.id) if "observed" in f else None, "inferred": url_for("inferred", rev.id) if "inferred" in f else None,
                                    "design": None, "replacement": None, "coverageHeat": url_for("coverage", rev.id) if "coverage" in f else None}})
        item["objectCandidates"] = (rev.stats or {}).get("objects", []) if rev is not None else []
        dg = db.scalar(select(SpaceGeometry).where(SpaceGeometry.space_id == sp.id, SpaceGeometry.kind == "DESIGN").order_by(SpaceGeometry.id.desc()))
        if dg is not None:
            item["layers"]["design"] = url_for("design", str(dg.id))
            item["designReview"] = dg.review_status
            item["designRevision"] = dg.design_revision
            item["sourceKinds"] = item["sourceKinds"] + ["DESIGN"]
        spaces_out.append(item)
        for w in db.scalars(select(Wall).where(Wall.space_id == sp.id, Wall.revision_id == (rev.id if rev else "-"))).all():
            walls_rows.append({"id": w.id, "spaceId": sp.code, "kind": w.kind, "thicknessM": w.thickness_m, "thicknessMethod": w.thickness_method,
                               "confidence": w.confidence, "reviewStatus": w.review_status, "coverage": w.coverage, "geom": w.geom, "sourceKind": w.source_kind,
                               "openings": [{"id": o.id, "kind": o.kind, "t0": o.t0, "t1": o.t1, "y0": o.y0, "y1": o.y1, "sourceKind": o.source_kind, "reviewStatus": o.review_status}
                                            for o in db.scalars(select(Opening).where(Opening.wall_id == w.id)).all()]})
        # 자산: 공간 안 자산의 건물 좌표 위치와 치환 정보
        for a in db.scalars(select(Asset).where(Asset.space_id == sp.id, Asset.status == "ACTIVE")).all():
            pos = (a.design_pose or {}).get("positionM", {})
            p = np.array([pos.get("x", 0), pos.get("y", 0), pos.get("z", 0)], float)
            pb = T[:3, :3] @ p + T[:3, 3]
            bb = {"min": None, "max": None}
            if a.aabb:
                lo, hi = np.array(a.aabb["minM"]) + p, np.array(a.aabb["maxM"]) + p
                corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
                cb = corners @ T[:3, :3].T + T[:3, 3]
                bb = {"min": cb.min(0).tolist(), "max": cb.max(0).tolist()}
            rep = db.scalar(select(CadReplacement).where(CadReplacement.asset_id == a.asset_id, CadReplacement.status == "APPROVED"))
            link = db.scalar(select(AssetGeometryLink).where(AssetGeometryLink.asset_id == a.asset_id))
            assets_out.append({"assetId": a.asset_id, "name": a.name or a.asset_id, "spaceId": sp.code, "positionBuilding": pb.tolist(), "bboxBuilding": bb,
                               "replaced": rep is not None, "cadGlbUrl": url_for("replacement", str(rep.id)) if rep else None,
                               "extractedMask": bool(link and link.mask_mode == "asset-extracted") or bool(rev and a.aabb is not None),
                               "family": a.family, "status": a.status, "sourceKind": "REPLACEMENT" if rep else "OBSERVED"})
    portals = [connector_json(c, code_of) for c in db.scalars(select(SpaceConnector).where(SpaceConnector.building_id == b.id)).all()]
    tour = db.scalar(select(TourScene).where(TourScene.building_id == b.id))
    vps = []
    if tour:
        vps = [{"id": v.id, "name": v.name, "position": v.position, "target": v.target, "spaceId": code_of.get(v.space_id)}
               for v in db.scalars(select(TourViewpoint).where(TourViewpoint.tour_id == tour.id)).all()]
    ent = [{"id": p["id"], "name": p["name"], "positionBuilding": p["positionBuilding"], "yawDeg": p["yawDeg"], "spaceId": p["fromSpace"],
            "exteriorViewpoint": p["exteriorViewpoint"], "reviewStatus": p["reviewStatus"]} for p in portals if p["kind"] == "ENTRANCE"]
    space_T = {s_["spaceId"]: mat16(s_["tBuildingSpace"]) for s_ in spaces_out}
    env_walls = []
    for w in walls_rows:
        g = w["geom"]
        T_ = space_T[w["spaceId"]]
        t_ = float(w["thicknessM"] or 0.1)
        n_ = np.array([g["normal"][0], 0.0, g["normal"][1]])
        corners = []
        for p_ in (g["p0"], g["p1"]):
            for y_ in (g["yMin"], g["yMax"]):
                for d_ in (0.0, -t_):  # 바깥쪽(−normal)으로 두께
                    q_ = np.array([p_[0], y_, p_[1]]) + n_ * d_
                    corners.append(T_[:3, :3] @ q_ + T_[:3, 3])
        cb = np.array(corners)
        env_walls.append({"id": w["id"], "spaceId": w["spaceId"], "kind": w["kind"], "thicknessM": w["thicknessM"], "thicknessMethod": w["thicknessMethod"],
                          "confidence": w["confidence"], "reviewStatus": w["reviewStatus"],
                          "bboxBuilding": {"min": cb.min(0).tolist(), "max": cb.max(0).tolist()}})
    inferred_visible = bool(b.envelope_uri) or any("INFERRED" in s["sourceKinds"] for s in spaces_out)
    return {"building": {"id": b.id, "name": b.name, "projectCode": b.project_code, "gps": b.gps,
                         "notice": b.notice if inferred_visible else "", "buildingType": b.building_type, "envelopeRevision": b.envelope_revision},
            "floors": [{"id": f.id, "level": f.level, "elevationM": f.elevation_m, "heightM": f.height_m} for f in floors],
            "envelope": {"glbUrl": url_for("envelope", str(b.id)) if b.envelope_uri else None, "sourceKind": "INFERRED", "reviewStatus": b.envelope_review, "walls": env_walls},
            "entrances": ent, "spaces": spaces_out, "portals": portals, "assets": assets_out, "viewpoints": vps, "wallDetails": walls_rows,
            "tourId": tour.id if tour else None}


# ───────────── CAD 치환 ─────────────

def register_catalog(db: Session, storage: FileStorage, code: str, name: str, family: str, glb: bytes, ifc_guid: str | None, symmetry: list[float] | None,
                     revision: str = "R1") -> CadCatalog:
    from spatial_core.registration import mesh_extent, read_glb_mesh

    try:
        v, t = read_glb_mesh(glb)
    except Exception as e:  # noqa: BLE001
        raise TwinError(f"GLB를 읽을 수 없습니다: {e}") from e
    if len(v) == 0 or not np.isfinite(v).all():
        raise TwinError("GLB에 유효한 정점이 없습니다")
    ext = (v.max(0) - v.min(0))
    if not (0.02 <= float(ext.max()) <= 20):
        raise TwinError("CAD 크기가 비정상(2cm~20m)")
    uri, _sha, _n = storage.save("cad", glb, ".glb")
    row = db.scalar(select(CadCatalog).where(CadCatalog.code == code)) or CadCatalog(code=code)
    row.name, row.family, row.ifc_guid, row.glb_uri, row.revision = name or code, family, ifc_guid, uri, revision
    row.dims_m = [float(x) for x in ext]
    row.symmetry = symmetry or []
    db.add(row)
    db.flush()
    return row


def _catalog_entries(db: Session, storage: FileStorage):
    from spatial_core.reconstruction.cad_match import CadEntry
    from spatial_core.registration import read_glb_mesh, sample_mesh_points

    out = []
    for c in db.scalars(select(CadCatalog)).all():
        if not c.glb_uri:
            continue
        v, t = read_glb_mesh(storage.open_bytes(c.glb_uri))
        center = (v.max(0) + v.min(0)) / 2
        pts = sample_mesh_points(v - center, t, 2500) if len(t) else (v - center)[:: max(1, len(v) // 2500)]
        out.append(CadEntry(c.id, c.code, tuple(float(x) for x in (v.max(0) - v.min(0))), pts, list(c.symmetry or [])))
    return out


def cad_candidates(db: Session, storage: FileStorage, asset: Asset, actor: str) -> list[dict]:
    from spatial_core.reconstruction.cad_match import match_cad
    from spatial_core.registration import read_glb_mesh, sample_mesh_points

    if not asset.cad_uri:
        raise TwinError("자산에 스캔 모델이 없습니다", 409)
    v, t = read_glb_mesh(storage.open_bytes(asset.cad_uri))
    obs = sample_mesh_points(v, t, 3000) if len(t) else v[:: max(1, len(v) // 3000)]
    cat = _catalog_entries(db, storage)
    if not cat:
        raise TwinError("CAD 카탈로그가 비어 있습니다 — 먼저 /cad-catalog 로 CAD를 등록하세요", 409)
    cands = match_cad(obs, cat)
    # 이전 미승인 후보는 정리 (승인·되돌림 이력은 보존)
    for old in db.scalars(select(CadReplacement).where(CadReplacement.asset_id == asset.asset_id, CadReplacement.status.in_(("CANDIDATE", "UNDETERMINED")))).all():
        db.delete(old)
    out = []
    center_obs = (v.max(0) + v.min(0)) / 2
    for c in cands:
        T_inv = np.linalg.inv(c.transform_obs_to_model)  # model → asset local
        row = CadReplacement(asset_id=asset.asset_id, catalog_id=c.entry.id, transform=to_mat16(T_inv), fitness=c.inlier_ratio, rmse_m=c.rmse, dim_error_m=c.dim_error,
                             symmetry_ambiguous=c.symmetry_ambiguous, status=c.decision, review_status="REVIEW_REQUIRED", note=c.reason,
                             design_revision=None, model_revision=None)
        db.add(row)
        db.flush()
        out.append({"id": row.id, "catalogCode": c.entry.code, "decision": c.decision, "confidence": round(c.confidence, 3), "dimErrorPct": round(c.dim_error * 100, 1),
                    "inlierRatio": round(c.inlier_ratio, 3), "rmseMm": round(c.rmse * 1000, 1), "modelCoverage": round(c.model_coverage, 3),
                    "symmetryAmbiguous": c.symmetry_ambiguous, "reason": c.reason, "reviewStatus": row.review_status})
    return out


def approve_replacement(db: Session, storage: FileStorage, asset: Asset, rid: int, actor: str, comment: str = "", acknowledge: bool = False) -> dict:
    from spatial_core.registration import read_glb_mesh, write_glb

    row = db.get(CadReplacement, rid)
    if row is None or row.asset_id != asset.asset_id:
        raise TwinError("replacement not found", 404)
    if row.status == "REJECTED":
        raise TwinError("반려된 후보입니다", 409)
    if row.status == "UNDETERMINED":
        # 형상만으로 확정되지 않은 후보(대칭·근소한 차이 등) — 사람이 근거를 적고 명시적으로 확인한 경우에만 승인
        if not acknowledge or not comment.strip():
            raise TwinError(f"미확정(UNDETERMINED) 후보입니다 [{row.note}] — 근거 코멘트와 acknowledge=true 로 사람이 확인해야 승인됩니다", 409)
        if (row.dim_error_m or 0) > 0.35:
            raise TwinError("치수 오차가 35%를 넘어 승인할 수 없습니다", 409)
        comment = f"[사람 확인] {comment}"
    cat = db.get(CadCatalog, row.catalog_id)
    v, t = read_glb_mesh(storage.open_bytes(cat.glb_uri))
    center = (v.max(0) + v.min(0)) / 2
    T = mat16(row.transform)
    pv = (v - center) @ T[:3, :3].T + T[:3, 3]
    glb = write_glb(pv.astype("float32"), t.astype("uint32"), name=f"replacement-{asset.asset_id}")
    d = storage.root / "twin" / "replacement"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{row.id}.glb").write_bytes(glb)  # 새 파일 — 자산의 스캔 GLB(OBSERVED)는 그대로 둔다
    for other in db.scalars(select(CadReplacement).where(CadReplacement.asset_id == asset.asset_id, CadReplacement.status == "APPROVED")).all():
        other.status = "REVERTED"
    row.status, row.review_status, row.approved_by, row.approved_at = "APPROVED", "APPROVED", actor, _now()
    row.model_revision = cat.revision
    row.note = (comment or row.note)[:300]
    space = db.get(Space, asset.space_id)
    db.add(SpaceGeometry(space_id=asset.space_id, source_kind="REPLACEMENT", kind="REPLACEMENT", uri=f"twin/replacement/{row.id}.glb", confidence=0.9,
                         review_status="APPROVED", approved_by=actor, model_revision=cat.revision))
    db.add(ModelApproval(target_type="cad_replacement", target_id=str(row.id), action="APPROVE", actor=actor, comment=comment[:300]))
    db.flush()
    return {"id": row.id, "status": row.status, "assetId": asset.asset_id, "glbUrl": f"/twin/files/replacement/{row.id}.glb", "observedPreserved": True}


def revert_replacement(db: Session, asset: Asset, rid: int, actor: str) -> dict:
    row = db.get(CadReplacement, rid)
    if row is None or row.asset_id != asset.asset_id:
        raise TwinError("replacement not found", 404)
    row.status, row.review_status = "REVERTED", "REVIEW_REQUIRED"
    db.add(ModelApproval(target_type="cad_replacement", target_id=str(rid), action="REVERT", actor=actor))
    db.flush()
    return {"id": rid, "status": row.status, "observedRestored": True}


# ───────────── 설계(DESIGN) 지오메트리 ─────────────

def add_design_geometry(db: Session, storage: FileStorage, space: Space, glb: bytes, design_revision: str, actor: str, ifc_guid: str | None = None) -> SpaceGeometry:
    """Revit/IFC/CAD에서 뽑은 설계 모델(GLB, space 프레임)을 공간에 붙인다. 관측 모델은 그대로 두고 별도 레이어로만 보관한다."""
    from spatial_core.registration import read_glb_mesh

    try:
        v, _t = read_glb_mesh(glb)
    except Exception as e:  # noqa: BLE001
        raise TwinError(f"GLB를 읽을 수 없습니다: {e}") from e
    if len(v) == 0 or not np.isfinite(v).all():
        raise TwinError("GLB에 유효한 정점이 없습니다")
    if float((v.max(0) - v.min(0)).max()) > 500:
        raise TwinError("설계 모델 크기가 비정상(500m 초과) — 단위(m)와 좌표를 확인하세요")
    g = SpaceGeometry(space_id=space.id, source_kind="DESIGN", kind="DESIGN", confidence=0.95, design_revision=design_revision, review_status="REVIEW_REQUIRED")
    db.add(g)
    db.flush()
    d = storage.root / "twin" / "design"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{g.id}.glb").write_bytes(glb)
    g.uri = f"twin/design/{g.id}.glb"
    db.add(ModelApproval(target_type="design_geometry", target_id=str(g.id), action="EDIT", actor=actor, comment=f"업로드 {design_revision}"))
    db.flush()
    return g
