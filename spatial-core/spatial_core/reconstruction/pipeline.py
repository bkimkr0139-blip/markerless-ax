"""공간 재구성 파이프라인: 프레임 → (구간 정합) → TSDF 융합 → 메시 → 구조·커버리지 → 사진 텍스처 → LOD·파일.

모든 산출물에 출처(sourceKind)를 붙인다: 융합 메시/텍스처 = OBSERVED, 미관측 평면 패치 = INFERRED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .camera import Frame, unproject
from .glb_export import write_colored_glb, write_textured_glb
from .layers import object_candidates
from .mesh import TriMesh, decimate_clustering, remove_small_components
from .pose_graph import optimize as pose_graph_optimize
from .pose_track import PoseTracker
from .refine_mesh import regularize
from .semantic_mask import SemanticMasker
from .session_align import align_clouds
from .structure import CELL, Structure, align_prior_to_mesh, extract_structure, from_roomplan
from .texture import build_texture
from .tsdf import SparseTSDF


@dataclass
class FrameRef:
    index: int
    segment: int
    tracking: str  # normal | limited | notAvailable
    blur: float
    excluded: bool
    loader: Callable[[int], Frame]  # (rgb_max_px) → Frame

    def load(self, rgb_max_px: int = 960) -> Frame:
        return self.loader(rgb_max_px)


@dataclass
class ReconParams:
    voxel_m: float = 0.03
    texture_px: int = 2048
    web_cell_m: float = 0.08
    mobile_cell_m: float = 0.16
    rgb_max_px: int = 960
    max_texture_frames: int = 160
    min_frames: int = 12
    good_coverage: float = 0.85
    min_coverage: float = 0.6
    stride: int = 2
    max_blur: float = 0.75
    # 프레임-모델 자세 보정(실험). 합성 벤치(tools/twin_bench.py)에서 기준선 대비 이득이 입증되지 않아 기본 꺼짐 — docs/REPORT.md 참고
    track_poses: bool = False
    pose_graph: bool = True  # 키프레임 포즈 그래프(루프 클로저)로 VIO 드리프트 보정 — 합성 드리프트 벤치에서 층 번짐 86→5mm
    pgo_key_step: int = 4
    regularize: bool = True  # 벽·바닥·천장 정점을 정밀 피팅한 평면에 스냅(텍스처·LOD 메시에만 적용, observed 원본은 보존)
    mask_people: bool = True  # AI 분할로 사람(이동물)을 융합·텍스처에서 제외 — 모델이 없으면 무시
    mask_reflective: bool = False  # 거울·유리·창 제외: 경계가 거칠어 실기기 검증 전까지 기본 꺼짐


@dataclass
class ReconResult:
    files: dict[str, bytes] = field(default_factory=dict)  # kind → glb 바이트
    structure: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    status: str = "READY"  # READY | NEED_MORE_CAPTURE | FAILED
    quality: str = "REVIEW"  # GOOD | REVIEW | POOR
    coverage: float = 0.0
    bbox: tuple | None = None
    axes_yaw: float = 0.0
    objects: list = field(default_factory=list)


Progress = Callable[[str, float, str], None]


def _noop(stage: str, p: float, msg: str = "") -> None:
    return None


def _heat_and_inferred(st: Structure) -> tuple[TriMesh, TriMesh]:
    """구조 평면을 CELL 격자로 쪼개 관측(초록)/미관측(빨강) 히트맵과, 미관측 패치만 모은 INFERRED 메시를 만든다."""
    hv, hf, hc = [], [], []
    iv, if_ = [], []
    ho = 0
    io = 0

    def quad(P, cols, obs, V, F, C, off):
        a, b, c, d = P
        V += [a, b, c, d]
        F += [(off, off + 1, off + 2), (off, off + 2, off + 3)]
        C += [cols] * 4
        return off + 4

    GREEN, RED = (70, 190, 100), (230, 70, 60)
    ivc = []
    for w in st.walls:
        if w.cells is None:
            continue
        p0 = np.array([w.p0[0], 0, w.p0[1]])
        L = w.length
        tang = (np.array([w.p1[0], 0, w.p1[1]]) - p0) / max(L, 1e-9)
        n3 = np.array([w.normal[0], 0, w.normal[1]])
        nt, ny = w.cells.shape
        for i in range(nt):
            for j in range(ny):
                t0, t1 = i * CELL, min((i + 1) * CELL, L)
                y0, y1 = w.y_min + j * CELL, min(w.y_min + (j + 1) * CELL, w.y_max)
                eps = n3 * 0.01
                P = [p0 + tang * t0 + np.array([0, y0, 0]) + eps, p0 + tang * t1 + np.array([0, y0, 0]) + eps,
                     p0 + tang * t1 + np.array([0, y1, 0]) + eps, p0 + tang * t0 + np.array([0, y1, 0]) + eps]
                ob = bool(w.cells[i, j])
                ho = quad(P, GREEN if ob else RED, ob, hv, hf, hc, ho)
                if not ob:
                    io = quad(P, (255, 190, 60), ob, iv, if_, ivc, io)
    ox, oz = st.grid_origin
    for cells, yv in ((st.floor_cells, st.floor_y), (st.ceiling_cells, st.ceiling_y)):
        if cells is None or yv is None:
            continue
        nx, nz = cells.shape
        for i in range(nx):
            for j in range(nz):
                P = [np.array([ox + i * CELL, yv + (0.01 if yv == st.floor_y else -0.01), oz + j * CELL]),
                     np.array([ox + (i + 1) * CELL, yv, oz + j * CELL]), np.array([ox + (i + 1) * CELL, yv, oz + (j + 1) * CELL]),
                     np.array([ox + i * CELL, yv, oz + (j + 1) * CELL])]
                ob = bool(cells[i, j])
                ho = quad(P, GREEN if ob else RED, ob, hv, hf, hc, ho)
                if not ob:
                    io = quad(P, (255, 190, 60), ob, iv, if_, ivc, io)
    # 방 다각형 밖 칸은 히트맵에서 제외하기 위해 floor/ceiling cells가 이미 inside 마스크를 거쳤음 → 미관측 판단은 inside 기준이 아니므로 아래서 걸러냄
    heat = TriMesh(np.array(hv) if hv else np.zeros((0, 3)), np.array(hf) if hf else np.zeros((0, 3), np.int64), np.array(hc, np.uint8) if hc else None)
    inf = TriMesh(np.array(iv) if iv else np.zeros((0, 3)), np.array(if_) if if_ else np.zeros((0, 3), np.int64), np.array(ivc, np.uint8) if ivc else None)
    return heat, inf


def reconstruct(refs: list[FrameRef], params: ReconParams | None = None, structure_prior: dict | None = None,
                progress: Progress = _noop, assets: list[dict] | None = None) -> ReconResult:
    P = params or ReconParams()
    res = ReconResult()
    usable = [r for r in refs if not r.excluded and r.tracking == "normal" and r.blur <= P.max_blur]
    notes: list[str] = []
    dropped = len(refs) - len(usable)
    if len(usable) < P.min_frames:
        res.status, res.quality = "NEED_MORE_CAPTURE", "POOR"
        res.report = {"reason": f"사용 가능한 프레임 {len(usable)}개 < 최소 {P.min_frames}개", "framesTotal": len(refs), "framesDropped": dropped}
        return res

    # 1) 구간(segment)별 정합 — 가장 큰 구간을 기준으로 다른 구간을 맞춘 뒤 전체를 다시 융합한다
    progress("align", 0.02, "구간 정합")
    segs: dict[int, list[FrameRef]] = {}
    for r in usable:
        segs.setdefault(r.segment, []).append(r)
    seg_T: dict[int, np.ndarray] = {s: np.eye(4) for s in segs}
    seg_report = {}
    excluded_segments: list[int] = []
    if len(segs) > 1:
        ref_seg = max(segs, key=lambda s: len(segs[s]))
        clouds = {}
        for s, lst in segs.items():
            pts = []
            for r in lst[:: max(1, len(lst) // 25)]:
                pts.append(unproject(r.load(320), stride=3)[0])
            clouds[s] = np.vstack(pts)
        for s in segs:
            if s == ref_seg:
                seg_report[s] = {"role": "reference", "frames": len(segs[s])}
                continue
            a = align_clouds(clouds[s], clouds[ref_seg])
            seg_report[s] = {"fitness": round(a.fitness, 3), "rmse": round(a.rmse, 4), "ambiguous": a.ambiguous, "accepted": a.accepted,
                             "reason": a.reason, "frames": len(segs[s])}
            if a.accepted:
                seg_T[s] = a.transform
            else:
                excluded_segments.append(s)
                notes.append(f"구간 {s}: 기준 구간과 정합 불확실({a.reason}) — 병합하지 않고 제외(REVIEW_REQUIRED)")
    keep = [r for r in usable if r.segment not in excluded_segments]

    # 1.5) 포즈 그래프(루프 클로저): 깊이·포즈만 가볍게 읽어 드리프트를 푼 뒤 프레임별 보정 변환을 얻는다
    pgo_info = None
    corr: list[np.ndarray | None] = [None] * len(keep)
    if P.pose_graph and len(keep) >= 24:
        progress("pgo", 0.04, "포즈 그래프")
        try:
            light = []
            for r in keep:
                f0 = r.load(0)  # RGB 없이 깊이·포즈만
                Tseg = seg_T[r.segment]
                if not np.allclose(Tseg, np.eye(4)):
                    f0.T_world_cam = Tseg @ f0.T_world_cam
                light.append(f0)
            pg = pose_graph_optimize(light, key_step=P.pgo_key_step)
            pgo_info = {"accepted": pg.accepted, "loops": pg.loops, **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in pg.info.items()}}
            if pg.accepted:
                keys = list(range(0, len(light), P.pgo_key_step))
                G = [pg.poses[k] @ np.linalg.inv(light[keys[k]].T_world_cam) for k in range(len(keys))]
                for idx in range(len(light)):
                    corr[idx] = G[min(len(keys) - 1, int(round(idx / P.pgo_key_step)))]
            del light
        except Exception as e:  # noqa: BLE001
            notes.append(f"포즈 그래프 실패({e}) — 보정 없이 진행")

    # 2) TSDF 융합
    tsdf = SparseTSDF(voxel=P.voxel_m, trunc_voxels=3.0)
    n = len(keep)
    frames_for_tex: list[Frame] = []
    tex_every = max(1, n // P.max_texture_frames)
    tracker = PoseTracker(tsdf) if P.track_poses else None
    masker = SemanticMasker() if (P.mask_people or P.mask_reflective) else None
    masked_frames = 0
    masked_frac_sum = 0.0
    for i, r in enumerate(keep):
        fr = r.load(P.rgb_max_px)
        T = seg_T[r.segment]
        if not np.allclose(T, np.eye(4)):
            fr.T_world_cam = T @ fr.T_world_cam
        if corr[i] is not None:
            fr.T_world_cam = corr[i] @ fr.T_world_cam
        if masker is not None and masker.available and fr.rgb is not None:
            m = masker.mask(fr.rgb, people=P.mask_people, reflective=P.mask_reflective)
            if m.any():
                fr.mask = m
                dh, dw = fr.depth.shape
                md = np.asarray(__import__("PIL.Image", fromlist=["Image"]).fromarray(m.astype(np.uint8) * 255).resize((dw, dh))) > 127
                fr.depth = np.where(md, 0.0, fr.depth).astype(np.float32)  # 마스크 픽셀은 깊이를 쓰지 않는다(자유공간 갱신도 안 함)
                masked_frames += 1
                masked_frac_sum += float(m.mean())
        if tracker is not None:
            fr, _ti = tracker.correct(fr, i)
        tsdf.integrate(fr, stride=P.stride)
        if i % tex_every == 0:
            frames_for_tex.append(fr)
        progress("fuse", 0.05 + 0.5 * (i + 1) / n, f"융합 {i + 1}/{n}")
    progress("mesh", 0.6, "메시 추출")
    mesh = remove_small_components(tsdf.extract_mesh(), min_area_m2=0.1)
    if mesh.empty:
        res.status, res.quality = "FAILED", "POOR"
        res.report = {"reason": "융합 결과가 비어 있음 (깊이 신뢰도 낮음 또는 범위 부족)", "framesUsed": n}
        return res

    # 3) 구조 + 커버리지
    progress("structure", 0.68, "구조·커버리지")
    obs_pts = tsdf.surface_points()
    st = extract_structure(mesh, obs_pts)  # 메시 기준 구조(정렬 기준이자 RoomPlan이 없을 때의 결과)
    if structure_prior and structure_prior.get("walls"):
        try:
            aligned, info = align_prior_to_mesh(structure_prior, st)
            if aligned is not None:
                st = from_roomplan(aligned, mesh, obs_pts)
                notes.append(f"RoomPlan 구조를 메시에 정렬해 사용 (yaw {info['yawDeg']:.0f}°, 잔차 {info['rms'] * 100:.1f}cm)")
            else:
                notes.append(f"RoomPlan 구조 정렬 실패({info.get('reason')}) — 메시에서 추정한 구조 사용")
        except Exception as e:  # noqa: BLE001
            notes.append(f"RoomPlan 구조 변환 실패({e}) — 메시 추정 사용")
    notes += st.notes
    res.coverage = float(st.overall_coverage)

    # 4) 텍스처 (맨해튼 축 정렬: 가장 긴 벽 방향)
    progress("texture", 0.75, "사진 텍스처")
    axes = np.eye(3)
    if st.walls:
        w = max(st.walls, key=lambda x: x.length)
        d = np.array([w.p1[0] - w.p0[0], w.p1[1] - w.p0[1]])
        yaw = float(np.arctan2(d[1], d[0]))
        c, s_ = np.cos(yaw), np.sin(yaw)
        axes = np.array([[c, 0, s_], [0, 1, 0], [-s_, 0, c]])  # 행 = 새 축들(월드에서 본)
        res.axes_yaw = yaw
    tex_mesh, refine_stats = mesh, None
    if P.regularize and st.walls:
        try:
            tex_mesh, rs = regularize(mesh, st)
            refine_stats = rs.to_json()
        except Exception as e:  # noqa: BLE001
            notes.append(f"메시 정규화 실패({e}) — 원본 메시로 텍스처")
    tm = build_texture(tex_mesh, frames_for_tex, atlas_px=P.texture_px, axes=axes)
    extras = {"sourceKind": "OBSERVED", "unobservedFaces": tm.stats["unobservedFaces"], "textureCoverage": tm.stats["textureCoverage"]}
    res.files["textured"] = write_textured_glb(tm, extras)
    res.files["observed"] = write_colored_glb(mesh, {"sourceKind": "OBSERVED"}, name="observed")
    # LOD
    for key, cell, px in (("lodweb", P.web_cell_m, min(P.texture_px, 2048)), ("lodmobile", P.mobile_cell_m, 1024)):
        lm = decimate_clustering(tex_mesh, cell)
        if lm.empty:
            continue
        try:
            lt = build_texture(lm, frames_for_tex[:: max(1, len(frames_for_tex) // 60)], atlas_px=px, axes=axes, refine=False)  # LOD는 속도 우선(2패스 보정 생략)
            res.files[key] = write_textured_glb(lt, {"sourceKind": "OBSERVED", "lod": key})
        except Exception as e:  # noqa: BLE001
            notes.append(f"{key} 생성 실패: {e}")
    progress("export", 0.92, "내보내기")
    heat, inf = _heat_and_inferred(st)
    if not heat.empty:
        res.files["coverage"] = write_colored_glb(heat, {"kind": "coverageHeat"}, name="coverage", alpha=0.55)
    if not inf.empty:
        res.files["inferred"] = write_colored_glb(inf, {"sourceKind": "INFERRED", "reviewStatus": "REVIEW_REQUIRED"}, name="inferred", alpha=0.45)

    try:
        res.objects = [o.to_json() for o in object_candidates(mesh, st, assets)]
    except Exception as e:  # noqa: BLE001
        notes.append(f"사물 후보 분리 실패: {e}")
    lo, hi = mesh.aabb()
    res.bbox = (lo.tolist(), hi.tolist())
    res.structure = st.to_json()
    if res.coverage >= P.good_coverage and len(st.walls) >= 3 and tm.stats["textureCoverage"] >= P.good_coverage:
        res.quality, res.status = "GOOD", "READY"
    elif res.coverage >= P.min_coverage and len(st.walls) >= 3:
        res.quality, res.status = "REVIEW", "READY"
    else:
        res.quality, res.status = "POOR", "NEED_MORE_CAPTURE"
        notes.append("커버리지/구조가 부족합니다 — 미관측 영역(커버리지 히트맵의 빨간 부분)을 추가 촬영하세요")
    res.report = {"framesTotal": len(refs), "framesUsed": n, "framesDropped": dropped, "segments": seg_report,
                  "excludedSegments": excluded_segments, "voxelM": P.voxel_m, "blocks": tsdf.block_count,
                  "meshFaces": int(len(mesh.f)), "refine": refine_stats, "objectCandidates": len(res.objects), "meshAreaM2": round(mesh.area(), 2), "texture": tm.stats, "coverage": res.structure["coverage"],
                  "notes": notes, "quality": res.quality, "status": res.status,
                  "poseTracking": tracker.summary() if tracker else None, "poseGraph": pgo_info,
                  "semanticMask": {"model": bool(masker and masker.available), "people": P.mask_people, "reflective": P.mask_reflective,
                                   "maskedFrames": masked_frames, "meanMaskedFraction": round(masked_frac_sum / max(masked_frames, 1), 4)}}
    progress("done", 1.0, "완료")
    return res
