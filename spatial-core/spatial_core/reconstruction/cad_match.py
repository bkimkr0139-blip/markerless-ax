"""CAD/BIM 치환 후보 선정 — 스캔 자산 점군을 카탈로그 CAD 모델과 비교한다.

원칙(지시서 §10·§13.1): 미확정은 억지로 고르지 않는다. 치수 불일치·낮은 일치도·1·2위 근소 차이·대칭 모호이면 `UNDETERMINED`.
CAD가 설계대로라는 사실이 시공 상태 PASS를 뜻하지 않는다 — 여기서는 '어느 CAD 형상이 이 스캔과 같은 부품인가'만 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..registration.global_align import _rot_about, _truncated_score, align_object
from ..registration.icp import _nearest


@dataclass
class CadEntry:
    id: int
    code: str
    dims_m: tuple[float, float, float]  # (x, y, z) 로컬 AABB 크기, y = 위
    points: np.ndarray  # 모델 로컬 표면 샘플 (AABB 중심 원점)
    symmetry_deg: list[float] = field(default_factory=list)


@dataclass
class CadCandidate:
    entry: CadEntry
    dim_error: float
    inlier_ratio: float
    rmse: float
    model_coverage: float
    yaw_deg: float
    transform_obs_to_model: np.ndarray
    symmetry_ambiguous: bool
    confidence: float
    decision: str = "UNDETERMINED"  # CANDIDATE | UNDETERMINED
    reason: str = ""


def dim_error(obs_extent: np.ndarray, model_dims: tuple[float, float, float]) -> float:
    """높이(y)는 그대로, 수평 두 치수는 정렬해 비교 — 상대 오차의 최댓값."""
    oh = obs_extent[1]
    mh = model_dims[1]
    ow = sorted([obs_extent[0], obs_extent[2]])
    mw = sorted([model_dims[0], model_dims[2]])
    errs = [abs(oh - mh) / max(mh, 1e-6), abs(ow[0] - mw[0]) / max(mw[0], 1e-6), abs(ow[1] - mw[1]) / max(mw[1], 1e-6)]
    return float(max(errs))


def model_symmetries(points: np.ndarray, tau_frac: float = 0.012) -> list[float]:
    """모델 자신이 수직축 둘레 회전에 겹치는 각도(90/180/270)를 추정한다."""
    diag = float(np.linalg.norm(points.max(0) - points.min(0)))
    sub = points[:: max(1, len(points) // 1200)]
    out = []
    for deg in (90, 180, 270):
        R = _rot_about(1, np.radians(deg))
        d, _ = _nearest(sub @ R.T, sub)
        if float(np.mean(d)) < tau_frac * diag:
            out.append(float(deg))
    return out


def match_cad(obs_points: np.ndarray, catalog: list[CadEntry], *, max_dim_error: float = 0.35, top: int = 5,
              min_inlier: float = 0.6, accept_dim_error: float = 0.12, min_margin: float = 0.08) -> list[CadCandidate]:
    obs = np.asarray(obs_points, float)
    ext = obs.max(0) - obs.min(0)
    cands: list[CadCandidate] = []
    for e in catalog:
        de = dim_error(ext, e.dims_m)
        if de > max_dim_error:
            continue
        a = align_object(obs, e.points, up_axis=1)
        T = a.transform
        syms = e.symmetry_deg or model_symmetries(e.points)
        amb = False
        base = a.score
        for deg in syms:
            if abs(deg) % 360 < 1e-6:
                continue
            R = np.eye(4)
            R[:3, :3] = _rot_about(1, np.radians(deg))
            sc = _truncated_score(obs, e.points, R @ T, a.tau_m)
            if sc <= base * 1.2 + 1e-4:
                amb = True
                break
        conf = a.inlier_ratio * float(np.exp(-3.0 * de))
        cands.append(CadCandidate(e, de, a.inlier_ratio, a.rmse, a.model_coverage, a.yaw_deg, T, amb, conf))
    cands.sort(key=lambda c: -c.confidence)
    cands = cands[:top]
    for i, c in enumerate(cands):
        reasons = []
        if c.dim_error > accept_dim_error:
            reasons.append(f"치수 오차 {c.dim_error * 100:.0f}%")
        if c.inlier_ratio < min_inlier:
            reasons.append(f"일치율 {c.inlier_ratio * 100:.0f}%")
        if c.symmetry_ambiguous:
            reasons.append("대칭 모호(방향 불확정)")
        if i == 0 and len(cands) > 1 and c.confidence - cands[1].confidence < min_margin:
            reasons.append("2위와 근소한 차이")
        if i > 0:
            reasons.append("1위 아님")
        c.reason = ", ".join(reasons)
        c.decision = "CANDIDATE" if not reasons else "UNDETERMINED"
    return cands
