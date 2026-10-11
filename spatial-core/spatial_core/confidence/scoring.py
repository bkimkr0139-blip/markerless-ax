"""측정 신뢰도 산출 — 결정적(deterministic) 규칙 기반 (PoC v1).

입력 지표를 감점 방식으로 합산해 HIGH/MEDIUM/LOW를 내린다.
기준값은 PoC 첫 현장 데이터로 보정할 예정이므로 코드 상단에 상수로 모아둔다.

자동 PASS는 HIGH에서만 허용되므로(개발지시서 원칙 11), HIGH는 보수적으로 준다:
데이터가 없는 항목은 감점 없이 통과시키지 않고 "보수값"으로 처리한다.
"""

from __future__ import annotations

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

# ICP 잔차(RMSE) 기준 — 대상 사물 크기(대각선)에 대한 상대값
RMSE_REL_GOOD = 0.01  # ≤ 1% of extent → 양호
RMSE_REL_BAD = 0.05  # > 5% of extent → 불량

INLIER_RATIO_GOOD = 0.85
INLIER_RATIO_BAD = 0.60

MIN_POINTS_OK = 300  # 이보다 적으면 신뢰도 상한이 MEDIUM
MIN_POINTS_VERY_LOW = 80  # 이보다 적으면 LOW

DEPTH_VALID_RATIO_GOOD = 0.70
DEPTH_VALID_RATIO_BAD = 0.40

SCORE_HIGH = 8  # 총점 ≥ 8 → HIGH
SCORE_LOW = 3  # 총점 < 3 → LOW


def derive_confidence(
    *,
    icp_rmse_m: float | None = None,
    object_extent_m: float | None = None,
    inlier_ratio: float | None = None,
    n_points: int | None = None,
    tracking_normal: bool = True,
    depth_valid_ratio: float | None = None,
) -> str:
    """정합 지표에서 HIGH/MEDIUM/LOW 산출.

    각 항목: 좋으면 +2, 모르면 +0(보수), 나쁘면 -2.
    tracking 실패는 즉시 LOW (VIO 풀림 상태에서의 포즈는 무의미).
    """
    if not tracking_normal:
        return CONFIDENCE_LOW

    score = 6.0  # 기본 시작점
    caps = [CONFIDENCE_HIGH]  # 도달 가능한 최대 등급

    if icp_rmse_m is not None and object_extent_m and object_extent_m > 0:
        rel = icp_rmse_m / object_extent_m
        if rel <= RMSE_REL_GOOD:
            score += 2
        elif rel > RMSE_REL_BAD:
            score -= 2
    else:
        score -= 1  # 핵심 지표 누락은 감점
        caps.append(CONFIDENCE_MEDIUM)  # RMSE 없이 HIGH 불가

    if inlier_ratio is not None:
        if inlier_ratio >= INLIER_RATIO_GOOD:
            score += 2
        elif inlier_ratio < INLIER_RATIO_BAD:
            score -= 2
    else:
        score -= 1
        caps.append(CONFIDENCE_MEDIUM)

    if n_points is not None:
        if n_points < MIN_POINTS_VERY_LOW:
            score -= 4
            caps.append(CONFIDENCE_LOW)  # 점이 극소면 LOW 고정
        elif n_points < MIN_POINTS_OK:
            score -= 1
            caps.append(CONFIDENCE_MEDIUM)
    else:
        score -= 1
        caps.append(CONFIDENCE_MEDIUM)

    if depth_valid_ratio is not None:
        if depth_valid_ratio >= DEPTH_VALID_RATIO_GOOD:
            score += 2
        elif depth_valid_ratio < DEPTH_VALID_RATIO_BAD:
            score -= 2
    else:
        score -= 1
        caps.append(CONFIDENCE_MEDIUM)

    if score < SCORE_LOW:
        return CONFIDENCE_LOW

    rank = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}
    cap = min(caps, key=lambda c: rank[c])
    best = CONFIDENCE_HIGH if score >= SCORE_HIGH else CONFIDENCE_MEDIUM
    return best if rank[best] <= rank[cap] else cap
