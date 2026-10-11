"""Design-vs-Actual 공차 판정 규칙 (개발지시서 13.2/13.3/14절).

판정 규칙 (우선순위 순):

1. INVALID  : 측정 자체가 무효 (호출자가 사전 걸러냄 — 여기서는 tolerance 누락 시 예외)
2. RETAKE   : Confidence가 LOW 또는 UNKNOWN → 재촬영 요구 (14절)
3. FAIL     : 불확도를 반영한 "하한"이라도 공차를 벗어남
              (dist - unc > posTol, |dz| - unc > zTol, maxRot - uncRot > rotTol 중 하나)
4. PASS     : 모든 항목이 불확도를 반영해도 공차 내부이고 Confidence가 HIGH
              (자동 PASS는 HIGH만 허용 — 개발지시서 원칙 11)
5. REVIEW   : 그 외 (MEDIUM 신뢰도의 공차 내, 또는 공차 경계에 걸친 경우) → 사람 확인

모든 비교는 불확도(uncertainty)를 반영한다 (14절: Deviation + Uncertainty vs Tolerance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNKNOWN = "UNKNOWN"

DECISION_PASS = "PASS"
DECISION_REVIEW = "REVIEW"
DECISION_FAIL = "FAIL"
DECISION_RETAKE = "RETAKE"
DECISION_INVALID = "INVALID"


@dataclass
class Judgment:
    decision: str
    reasons: list[str] = field(default_factory=list)
    margins: dict[str, Any] = field(default_factory=dict)


def _missing(value: Any) -> bool:
    return value is None


def judge(
    deviation: dict[str, float],
    tolerance: dict[str, float],
    confidence: str = CONFIDENCE_UNKNOWN,
    uncertainty: dict[str, float] | None = None,
) -> Judgment:
    """편차/공차/신뢰도로 최종 판정.

    deviation  : pose_delta() 출력 (dxMm..distMm, rollDeg..maxRotDeg)
    tolerance  : {positionMm, zToleranceMm?, rotationDeg} — zToleranceMm이 없으면
                 positionMm을 Z축에도 사용
    confidence : HIGH/MEDIUM/LOW/UNKNOWN
    uncertainty: {positionMm=0, rotationDeg=0}
    """
    tol_pos = tolerance.get("positionMm")
    tol_rot = tolerance.get("rotationDeg")
    if _missing(tol_pos) or _missing(tol_rot):
        # BIM 데이터 오류 — 검사 중단 + 보고 (개발지시서 12.2)
        raise ValueError(
            "tolerance missing required field (positionMm, rotationDeg): BIM rule error"
        )
    tol_pos = float(tol_pos)
    tol_rot = float(tol_rot)
    tol_z = float(tolerance.get("zToleranceMm", tol_pos))

    unc_pos = float((uncertainty or {}).get("positionMm", 0.0) or 0.0)
    unc_rot = float((uncertainty or {}).get("rotationDeg", 0.0) or 0.0)

    d = float(deviation["distMm"])
    dz = abs(float(deviation["dzMm"]))
    rot = float(deviation["maxRotDeg"])

    margins = {
        "position": {
            "valueMm": d,
            "toleranceMm": tol_pos,
            "uncertaintyMm": unc_pos,
            "certainlyIn": d + unc_pos <= tol_pos,
            "certainlyOut": d - unc_pos > tol_pos,
        },
        "z": {
            "valueMm": dz,
            "toleranceMm": tol_z,
            "uncertaintyMm": unc_pos,
            "certainlyIn": dz + unc_pos <= tol_z,
            "certainlyOut": dz - unc_pos > tol_z,
        },
        "rotation": {
            "valueDeg": rot,
            "toleranceDeg": tol_rot,
            "uncertaintyDeg": unc_rot,
            "certainlyIn": rot + unc_rot <= tol_rot,
            "certainlyOut": rot - unc_rot > tol_rot,
        },
    }

    reasons: list[str] = []

    # 2. 신뢰도 게이트 — LOW/UNKNOWN은 재촬영
    if confidence in (CONFIDENCE_LOW, CONFIDENCE_UNKNOWN):
        reasons.append(
            f"측정 신뢰도가 {confidence}이므로 자동 판정 불가 — 재촬영 필요"
        )
        return Judgment(decision=DECISION_RETAKE, reasons=reasons, margins=margins)

    # 3. 확정 초과 → FAIL
    certainly_out = [
        name
        for name in ("position", "z", "rotation")
        if margins[name]["certainlyOut"]
    ]
    if certainly_out:
        for name in certainly_out:
            m = margins[name]
            value = m.get("valueMm", m.get("valueDeg"))
            tol = m.get("toleranceMm", m.get("toleranceDeg"))
            unit = "mm" if "valueMm" in m else "°"
            reasons.append(
                f"{name} 편차 {value:.1f}{unit} > 공차 {tol:.1f}{unit} (불확도 반영)"
            )
        return Judgment(decision=DECISION_FAIL, reasons=reasons, margins=margins)

    # 4. 확정 내부 + HIGH → 자동 PASS
    all_in = all(margins[name]["certainlyIn"] for name in ("position", "z", "rotation"))
    if all_in:
        if confidence == CONFIDENCE_HIGH:
            reasons.append("모든 항목이 공차 내(불확도 반영), 신뢰도 HIGH → 자동 PASS")
            return Judgment(decision=DECISION_PASS, reasons=reasons, margins=margins)
        reasons.append(
            "공차 내로 측정되었으나 신뢰도가 HIGH가 아니어서 자동 PASS 불가 — 사람 확인"
        )
        return Judgment(decision=DECISION_REVIEW, reasons=reasons, margins=margins)

    # 5. 공차 경계에 걸침 (불확도 때문에 확정 불가)
    reasons.append("편차와 공차 경계가 불확도 범위 안에서 겹쳐 확정 불가 — 사람 확인")
    return Judgment(decision=DECISION_REVIEW, reasons=reasons, margins=margins)


# 형상 일치도 판정 기준 (PoC v1 — 현장 데이터로 보정 예정)
SHAPE_INLIER_PASS = 0.60
SHAPE_INLIER_FAIL = 0.25
SHAPE_RMSE_PASS_M = 0.02


def judge_shape_fit(
    *,
    rmse_m: float,
    inlier_ratio: float,
    confidence: str = CONFIDENCE_UNKNOWN,
    rmse_pass_m: float = SHAPE_RMSE_PASS_M,
) -> Judgment:
    """공간 프레임이 없을 때(월드맵 미복원)의 판정 — 저장 모델과 현재 사물의 형상 일치도.

    위치/회전 편차는 의미가 없으므로 판정에 쓰지 않는다. 사물에 모델이 붙는지(정합 품질)만 본다:
    - RETAKE: 신뢰도 LOW/UNKNOWN (점이 너무 적거나 정합이 불안정)
    - FAIL  : 일치 점이 너무 적음 → 다른 사물이거나 형상이 크게 변함
    - PASS  : 일치 비율·잔차가 모두 양호하고 신뢰도 MEDIUM 이상
              (위치 정밀도를 판정하지 않으므로 HIGH까지는 요구하지 않는다 — VIO는 깊이 없어 MEDIUM이 상한)
    - REVIEW: 그 외 → 사람 확인
    """
    margins = {
        "shapeFit": {
            "inlierRatio": inlier_ratio,
            "rmseMm": rmse_m * 1000.0,
            "passInlier": SHAPE_INLIER_PASS,
            "failInlier": SHAPE_INLIER_FAIL,
            "passRmseMm": rmse_pass_m * 1000.0,
        }
    }
    note = "공간 프레임(월드맵)이 복원되지 않아 위치 편차는 판정하지 않고 형상 일치도만 평가"
    if confidence in (CONFIDENCE_LOW, CONFIDENCE_UNKNOWN):
        return Judgment(
            DECISION_RETAKE,
            [note, f"측정 신뢰도 {confidence} — 사물 주위를 더 촬영한 뒤 재시도"],
            margins,
        )
    if inlier_ratio < SHAPE_INLIER_FAIL:
        return Judgment(
            DECISION_FAIL,
            [note, f"일치 점 {inlier_ratio:.0%} — 다른 사물이거나 형상이 크게 달라졌습니다"],
            margins,
        )
    good = inlier_ratio >= SHAPE_INLIER_PASS and rmse_m <= rmse_pass_m
    if good and confidence in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM):
        return Judgment(
            DECISION_PASS,
            [note, f"형상 일치 {inlier_ratio:.0%}, 잔차 {rmse_m * 1000:.1f}mm → 일치"],
            margins,
        )
    return Judgment(
        DECISION_REVIEW,
        [note, f"형상 일치 {inlier_ratio:.0%}, 잔차 {rmse_m * 1000:.1f}mm — 사람 확인"],
        margins,
    )
