"""검수 서비스 — 정합(ICP) 실행 → Design-vs-Actual → 판정 (개발지시서 9/13/14절)."""

from __future__ import annotations

import numpy as np

from spatial_core.confidence import derive_confidence
from spatial_core.coordinate import Pose, matrix_to_quat, pose_delta
from spatial_core.inspection import judge, judge_shape_fit
from spatial_core.registration import align_object, icp_align, icp_plane_align
from spatial_core.segmentation import extract_outline, obb_of, outline_to_json, segment_object

from ..config import settings
from ..models import Asset
from ..schemas import MeasurementIn
from ..storage import FileStorage
from .assets import load_sample_points

# ICP 운영 파라미터 (PoC v1 — 현장 데이터로 보정 예정)
ICP_MAX_ITERATIONS = 40
ICP_TRIM_RATIO = 0.25
# 초깃값이 월드맵 복원 품질에 좌우되므로 대응 거리 상한은 관대하게:
# 사물 크기의 75%, 단 0.25~1.0m 클램프
CORR_DIST_MIN_M = 0.25
CORR_DIST_MAX_M = 1.0

# ICP 잔차로부터 측정 불확도를 추정하는 휴리스틱 (15절 — Ground Truth로 보정 예정):
# 위치 불확도 ≈ 2×RMSE(target 샘플 양자화 바닥 포함), 최소 3mm
# 회전 불확도는 PoC에서 고정값 — 공차(1°) 판정이 가능하도록 0.5°로 둔다
UNC_POSITION_FACTOR = 2.0
UNC_POSITION_MIN_MM = 3.0
UNC_ROTATION_DEFAULT_DEG = 0.5


class NoMatchError(ValueError):
    """자동 매칭 실패 — 닮은 등록 자산이 없다."""


OBJECT_CLOUD_CAP = 20_000  # 사물 모드는 분리를 위해 배경 점까지 받는다 (space 모드 상한 3000과 별개)
AUTO_TOP_K = 4  # 크기로 걸러낸 후보 중 정합까지 돌릴 최대 개수
AUTO_ACCEPT_INLIER = 0.50
AUTO_ACCEPT_SCORE = 0.50


def _session_up_axis() -> int:
    return 1  # ARKit/ARCore Y-up


def _segment_observation(cloud: np.ndarray, hint: np.ndarray | None, max_radius: float) -> tuple[np.ndarray, dict]:
    """검수 점에서도 등록과 같은 방식으로 사물만 분리한다 (배경·탁자가 섞이면 정합이 틀어진다)."""
    poi = hint if hint is not None else np.median(cloud, axis=0)
    seg = segment_object(cloud, poi, max_radius=max_radius, up_axis=_session_up_axis())
    info = {
        "segmented": bool(seg.n_in >= 40), "nIn": int(seg.n_in), "nRemoved": int(seg.n_removed),
        "quality": seg.quality, "warnings": seg.warnings, "supportHeightM": seg.support_h,
    }
    if seg.n_in >= 40:
        return cloud[seg.mask], info
    # 분리 실패 → 힌트 주변 점으로 대체 (품질 낮음을 알린다)
    near = cloud[np.linalg.norm(cloud - poi, axis=1) <= min(max_radius, 0.35)]
    if len(near) < 10:
        raise ValueError("사물 점이 부족합니다 — 사물을 중심에 두고 천천히 더 스캔하세요")
    info["fallback"] = True
    return near, info


def _model_dims(model: np.ndarray) -> np.ndarray:
    """(L, W, H): 수평 footprint 두 변(큰 순) + 높이 — 방위와 무관한 크기 서명."""
    size, _ = obb_of(model, _session_up_axis())
    return np.array([max(size[0], size[2]), min(size[0], size[2]), size[1]])


def _size_penalty(obs: np.ndarray, mdl: np.ndarray) -> float:
    """관측 크기 vs 모델 크기 (L, W, H)의 로그 비율 벌점.

    부분 관측(한쪽 면만)은 깊이 방향(작은 가로 W)이 작게 나오므로 W의 '작음'은 관대하게(0.4),
    큰 가로 L과 높이 H의 '작음'은 엄격하게(1.0) 본다. 어느 치수든 모델보다 '큼'은 엄격하게(1.0).
    """
    r = np.maximum(obs, 1e-3) / np.maximum(mdl, 1e-3)
    small_w = np.array([1.0, 0.4, 1.0])
    pen = np.where(r > 1.0, np.log(np.maximum(r, 1.0)), small_w * np.log(1.0 / np.minimum(r, 1.0)))
    return float(pen.max())


SIZE_PENALTY_MAX = float(np.log(1.7))  # 큰 치수 기준 모델의 약 59% 이상은 보여야 후보


def _align_to_asset(
    storage: FileStorage, asset: Asset, observed: np.ndarray, hint: np.ndarray | None, restored: bool
):
    model, _ = load_sample_points(storage, asset)
    design = Pose.from_json(asset.design_pose)
    obs_size, obs_yaw = obb_of(observed, _session_up_axis())
    mdl_size, mdl_yaw = obb_of(model, _session_up_axis())
    # OBB 방위로 얻은 yaw 후보: 관측이 모델에 대해 돈 각 (+90° 배수) — 직사각형 사물의 90° 모호성 해소용 후보
    base = np.degrees(mdl_yaw - obs_yaw)
    extra = [float(base + k * 90.0) for k in range(4)]
    res = align_object(
        observed, model,
        hints=[hint] if hint is not None else None,
        prior=design.to_matrix() if restored else None,
        extra_yaws_deg=extra, up_axis=_session_up_axis(),
    )
    return res, model, mdl_size, obs_size


def _lite_outline(points: np.ndarray) -> dict | None:
    try:
        o = outline_to_json(extract_outline(points, up_axis=_session_up_axis()))
    except ValueError:
        return None
    return {"lines": o["lines"], "obb": o["obb"], "levels": o["levels"]}  # views는 용량상 제외


def _evaluate_object(
    asset: Asset, m: MeasurementIn, res, observed: np.ndarray, seg_info: dict,
    mdl_size: np.ndarray, obs_size: np.ndarray, ranking: list[dict] | None, matched_by: str,
) -> dict:
    design = Pose.from_json(asset.design_pose)
    actual = Pose.from_matrix(np.linalg.inv(res.transform))  # 사물(local→session)
    actual_json = actual.to_json()
    qx, qy, qz, qw = matrix_to_quat(actual.rotation)
    actual_json["quat"] = {"x": qx, "y": qy, "z": qz, "w": qw}  # 오버레이는 축 모호성 없는 quat 사용

    extent = float(np.linalg.norm(mdl_size))
    confidence = derive_confidence(
        icp_rmse_m=res.rmse, object_extent_m=extent, inlier_ratio=res.inlier_ratio,
        n_points=len(observed), tracking_normal=m.trackingNormal, depth_valid_ratio=m.depthValidRatio,
    )
    uncertainty = {
        "positionMm": max(UNC_POSITION_MIN_MM, res.rmse * 1000.0 * UNC_POSITION_FACTOR),
        "rotationDeg": UNC_ROTATION_DEFAULT_DEG,
    }
    if m.worldMapRestored:
        deviation = pose_delta(design, actual)
        judgment = judge(deviation, asset.tolerance, confidence, uncertainty)
    else:
        deviation = pose_delta(actual, actual)  # 전부 0 — 공간 프레임이 없어 위치 편차 없음(판정 제외)
        judgment = judge_shape_fit(
            rmse_m=res.rmse, inlier_ratio=res.inlier_ratio, confidence=confidence,
            rmse_pass_m=float(np.clip(res.tau_m * 0.9, 0.008, 0.025)),
        )
    size_ratio = (obs_size[[0, 2]].max() / max(mdl_size[[0, 2]].max(), 1e-6),
                  obs_size[1] / max(mdl_size[1], 1e-6))
    # 크기 불일치 가드: 형상 일치도만으로는 '큰 모델 표면 어딘가에 놓인 작은 물체'가 PASS가 된다 — 크기가 맞지 않으면 합격 불가
    obs_dims = np.array([max(obs_size[0], obs_size[2]), min(obs_size[0], obs_size[2]), obs_size[1]])
    mdl_dims = np.array([max(mdl_size[0], mdl_size[2]), min(mdl_size[0], mdl_size[2]), mdl_size[1]])
    pen = _size_penalty(obs_dims, mdl_dims)
    reasons = list(judgment.reasons)
    decision = judgment.decision
    if pen >= SIZE_PENALTY_MAX and decision in ("PASS", "REVIEW"):
        decision = "FAIL" if pen >= np.log(2.5) else "REVIEW"
        reasons.append(f"크기 불일치: 관측 사물이 등록 모델의 {np.exp(-pen) if size_ratio[0] < 1 else np.exp(pen):.2f}배 "
                       f"(길이 {size_ratio[0]:.2f} · 높이 {size_ratio[1]:.2f}) — 다른 사물이거나 일부만 스캔됨")
    align_info = {
        "mode": "object", "matchedBy": matched_by,
        "rmseM": res.rmse, "inlierRatio": res.inlier_ratio, "score": res.score,
        "modelCoverage": res.model_coverage, "yawDeg": res.yaw_deg, "tauM": res.tau_m,
        "candidates": res.candidates, "bestInit": res.best_init,
        "points": int(len(observed)), "worldMapRestored": m.worldMapRestored,
        "segmented": seg_info.get("segmented"),
    }
    match = {
        "matchedBy": matched_by, "assetId": asset.asset_id, "ranking": ranking or [],
        "segmentation": seg_info, "alignment": align_info,
        "modelSizeM": [float(x) for x in mdl_size], "observedSizeM": [float(x) for x in obs_size],
        "sizeRatio": {"footprint": float(size_ratio[0]), "height": float(size_ratio[1])},
        "observedOutline": _lite_outline(observed),
    }
    return {
        "actualPose": actual_json, "deviation": deviation, "confidence": confidence,
        "decision": decision, "reasons": reasons, "margins": judgment.margins,
        "uncertainty": uncertainty, "icp": align_info, "match": match,
        "observedPoints": observed,  # 3D 리뷰용 (라우터가 저장하고 응답에서는 쓰지 않는다)
    }


def run_object_inspection(db, storage: FileStorage, m: MeasurementIn) -> tuple[Asset, dict]:
    """사물 대 사물 검수: 검수 점에서 사물을 분리 → (자동 매칭 또는 지정 자산에) 정합 → 판정.

    autoMatch: 공간의 등록 자산 중 크기가 비슷한 후보를 골라 정합까지 돌리고 가장 잘 맞는 것을 택한다.
    닮은 자산이 없으면 NoMatchError.
    """
    from sqlalchemy import select

    cloud = m.cloud.decode(OBJECT_CLOUD_CAP)  # type: ignore[union-attr]
    hint = (
        np.array([m.hintCenter.x, m.hintCenter.y, m.hintCenter.z]) if m.hintCenter is not None else None
    )

    if not m.autoMatch:
        asset = db.get(Asset, m.assetId)
        if asset is None:
            raise LookupError(f"asset not found: {m.assetId}")
        if asset.tolerance is None:
            raise ValueError(f"asset tolerance missing: {asset.asset_id} (BIM rule error)")
        ext = _asset_extent(asset)
        observed, seg_info = _segment_observation(cloud, hint, float(np.clip(ext * 0.5 + 0.12, 0.25, 1.2)))
        res, _model, mdl_size, obs_size = _align_to_asset(storage, asset, observed, hint, m.worldMapRestored)
        return asset, _evaluate_object(asset, m, res, observed, seg_info, mdl_size, obs_size, None, "pinned")

    # ---- 자동 매칭 ----
    observed, seg_info, entries, ranking = _rank_candidates(db, storage, m.spaceId, cloud, hint, m.worldMapRestored)
    best = entries[0]
    asset, res, mdl_size, obs_size = best["asset"], best["res"], best["mdl_size"], best["obs_size"]
    if not best["accepted"]:
        raise NoMatchError(
            f"등록된 자산 중 닮은 사물이 없습니다 (최선 {asset.asset_id}: 일치 {res.inlier_ratio:.0%}, 점수 {res.score:.2f}, "
            f"크기 차이 {np.exp(best['pen']):.2f}배). 사물이 중심에 오도록 조준하고 4방향에서 더 스캔하거나, 자산을 직접 선택하세요"
        )
    if asset.tolerance is None:
        raise ValueError(f"asset tolerance missing: {asset.asset_id} (BIM rule error)")
    return asset, _evaluate_object(asset, m, res, observed, seg_info, mdl_size, obs_size, ranking, "auto")


def _rank_candidates(db, storage: FileStorage, space_id: int, cloud: np.ndarray, hint, restored: bool):
    """공간의 등록 자산 중 크기가 비슷한 후보를 정합해 순위를 매긴다.

    반환: (분리된 관측 점, 분리 정보, 정합한 후보 entries(좋은 순), 전체 순위표(크기 탈락 포함))
    """
    from sqlalchemy import select

    assets = [a for a in db.scalars(select(Asset).where(Asset.space_id == space_id)).all()
              if a.cad_uri and (a.status or "ACTIVE") != "ARCHIVED"]
    if not assets:
        raise NoMatchError("이 공간에 등록된 자산이 없습니다 — ② 스캔·등록에서 먼저 등록하세요")
    observed, seg_info = _segment_observation(cloud, hint, 0.9)
    obs_dims = _model_dims_from_obb(observed)
    cands = []
    for a in assets:
        model, _ = load_sample_points(storage, a)
        cands.append((_size_penalty(obs_dims, _model_dims(model)), a))
    cands.sort(key=lambda c: c[0])
    shortlist = [c for c in cands if c[0] < SIZE_PENALTY_MAX][:AUTO_TOP_K] or cands[:2]  # 비면 순위 안내용 2개

    entries: list[dict] = []
    for pen, a in shortlist:
        res, _model, mdl_size, obs_size = _align_to_asset(storage, a, observed, hint, restored)
        combined = res.score + 0.5 * pen
        accepted = pen < SIZE_PENALTY_MAX and res.inlier_ratio >= AUTO_ACCEPT_INLIER and res.score <= AUTO_ACCEPT_SCORE
        # 유사도(0~100): 일치율 × 크기 일치 × 정합 점수 — 화면에 보여 줄 한 숫자
        sim = 100.0 * float(np.clip(res.inlier_ratio, 0, 1)) * float(np.exp(-1.5 * pen)) * (1.0 - 0.5 * min(1.0, res.score / 0.6))
        entries.append({"asset": a, "res": res, "pen": pen, "mdl_size": mdl_size, "obs_size": obs_size,
                        "combined": combined, "accepted": accepted, "similarity": sim})
    entries.sort(key=lambda e: e["combined"])

    ranking: list[dict] = [{
        "assetId": e["asset"].asset_id, "score": round(float(e["res"].score), 3), "inlier": round(float(e["res"].inlier_ratio), 3),
        "sizePenalty": round(float(e["pen"]), 3), "combined": round(float(e["combined"]), 3),
        "rmseMm": round(float(e["res"].rmse) * 1000, 1), "similarity": round(e["similarity"], 1), "accepted": e["accepted"],
    } for e in entries]
    for pen, a in cands:  # 크기로 탈락한 후보도 순위표에 남긴다
        if all(r["assetId"] != a.asset_id for r in ranking):
            ranking.append({"assetId": a.asset_id, "score": None, "inlier": None, "sizePenalty": round(float(pen), 3),
                            "combined": None, "rmseMm": None, "similarity": 0.0, "accepted": False})
    return observed, seg_info, entries, ranking


def run_object_match(db, storage: FileStorage, m) -> dict:
    """자산 찾기: 판정·저장 없이 후보 순위만 돌려준다 (사용자가 후보를 고른 뒤 검수로 넘어간다)."""
    cloud = m.cloud.decode(OBJECT_CLOUD_CAP)
    hint = np.array([m.hintCenter.x, m.hintCenter.y, m.hintCenter.z]) if m.hintCenter is not None else None
    observed, seg_info, entries, ranking = _rank_candidates(db, storage, m.spaceId, cloud, hint, m.worldMapRestored)
    obs_size, _ = obb_of(observed, _session_up_axis())
    cands = []
    for e in entries:
        mdl = e["mdl_size"]
        cands.append({
            "assetId": e["asset"].asset_id, "similarity": round(e["similarity"], 1), "accepted": e["accepted"],
            "inlier": round(float(e["res"].inlier_ratio), 3), "rmseMm": round(float(e["res"].rmse) * 1000, 1),
            "score": round(float(e["res"].score), 3), "yawDeg": round(float(e["res"].yaw_deg), 1),
            "modelSizeM": [round(float(x), 3) for x in mdl],
            "sizeRatio": {"footprint": round(float(obs_size[[0, 2]].max() / max(mdl[[0, 2]].max(), 1e-6)), 3),
                          "height": round(float(obs_size[1] / max(mdl[1], 1e-6)), 3)},
        })
    best = cands[0] if cands and cands[0]["accepted"] else None
    return {
        "best": best["assetId"] if best else None, "accepted": best is not None, "candidates": cands,
        "ranking": ranking, "segmentation": seg_info, "observedSizeM": [round(float(x), 3) for x in obs_size],
        "observedOutline": _lite_outline(observed),
    }


def _model_dims_from_obb(points: np.ndarray) -> np.ndarray:
    return _model_dims(points)


def _asset_extent(asset: Asset) -> float:
    """저장 모델 AABB 대각선(m). 없으면 0.6."""
    box = asset.aabb or {}
    try:
        mn, mx = np.array(box["minM"], float), np.array(box["maxM"], float)
        return float(np.linalg.norm(mx - mn))
    except (KeyError, TypeError, ValueError):
        return 0.6


def evaluate_measurement(
    storage: FileStorage, asset: Asset, m: MeasurementIn
) -> dict:
    """측정 요청 → actualPose 산출 → 편차/판정. 사전조건: asset.tolerance 존재."""
    if asset.tolerance is None:
        # BIM 데이터 오류 — 검사 중단 + 보고 (개발지시서 12.2)
        raise ValueError(f"asset tolerance missing: {asset.asset_id} (BIM rule error)")

    design = Pose.from_json(asset.design_pose)
    icp_info: dict | None = None
    uncertainty = m.uncertainty.model_dump() if m.uncertainty is not None else None

    if m.cloud is not None:
        cloud = m.cloud.decode(settings.max_cloud_points)
        target_local, normals_local = load_sample_points(storage, asset)
        extent = float(np.linalg.norm(target_local.max(axis=0) - target_local.min(axis=0)))
        target_space = design.apply(target_local)
        # 법선은 회전 성분만 적용 (평행이동은 법선 방향을 바꾸지 않는다)
        normals_space = normals_local @ design.rotation.T
        corr = float(
            np.clip(extent * 0.75, CORR_DIST_MIN_M, CORR_DIST_MAX_M)
        )
        # 클라우드는 이미 공간 프레임(frame="space")이므로 초깃값은 항등이다.
        # (designPose를 초깃값으로 주면 이중 변환된다 — COORDINATE.md 3절)
        # 잔여 오차(월드맵 복원+VIO 드리프트, 수 cm)가 ICP가 흡수할 대상이다.
        # 2단 정합: point-to-point로 coarse 포획 → point-to-plane으로 접선 미끄럼 제거
        # (개발지시서 9.3 권장 — 대칭/평면 사물에서 p2p는 접선방향으로 미끄러진다)
        coarse = icp_align(
            cloud,
            target_space,
            init_transform=None,
            max_iterations=ICP_MAX_ITERATIONS,
            max_correspondence_dist_m=corr,
            trim_ratio=ICP_TRIM_RATIO,
            tolerance=1e-7,
        )
        icp_res = icp_plane_align(
            cloud,
            target_space,
            normals_space,
            init_transform=coarse.transform,
            max_iterations=ICP_MAX_ITERATIONS,
            max_correspondence_dist_m=corr,
            trim_ratio=ICP_TRIM_RATIO,
            tolerance=1e-7,
        )
        # icp_res.transform = "실측 클라우드 → 설계 위치" 보정행렬 T.
        # 따라서 실제 포즈는 actualPose = T⁻¹ ∘ designPose (COORDINATE.md 3절)
        actual = Pose.from_matrix(
            np.linalg.inv(icp_res.transform) @ design.to_matrix()
        )
        # 기기 보고 신뢰도보다 서버 정합 지표를 우선한다 (HIGH 판정 근거 확보)
        confidence = derive_confidence(
            icp_rmse_m=icp_res.rmse,
            object_extent_m=extent,
            inlier_ratio=icp_res.inlier_ratio,
            n_points=len(cloud),
            tracking_normal=m.trackingNormal,
            depth_valid_ratio=m.depthValidRatio,
        )
        if uncertainty is None:
            uncertainty = {
                "positionMm": max(
                    UNC_POSITION_MIN_MM, icp_res.rmse * 1000.0 * UNC_POSITION_FACTOR
                ),
                "rotationDeg": UNC_ROTATION_DEFAULT_DEG,
            }
        icp_info = {
            "rmseM": icp_res.rmse,
            "inlierRatio": icp_res.inlier_ratio,
            "iterations": icp_res.iterations,
            "converged": icp_res.converged,
            "points": int(len(cloud)),
            "maxCorrespondenceDistM": corr,
        }
    else:
        actual = Pose.from_json(m.actualPose.to_json())  # type: ignore[union-attr]
        confidence = m.confidence or "UNKNOWN"

    deviation = pose_delta(design, actual)
    judgment = judge(deviation, asset.tolerance, confidence, uncertainty)

    return {
        "actualPose": actual.to_json(),
        "deviation": deviation,
        "confidence": confidence,
        "decision": judgment.decision,
        "reasons": judgment.reasons,
        "margins": judgment.margins,
        "uncertainty": uncertainty,
        "icp": icp_info,
    }
