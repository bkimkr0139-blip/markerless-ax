import pytest

from spatial_core.inspection import judge


def _dev(dx, dy, dz, roll=0.0, pitch=0.0, yaw=0.0):
    import numpy as np

    dist = float(np.linalg.norm([dx, dy, dz]))
    max_rot = max(abs(roll), abs(pitch), abs(yaw))
    return {
        "dxMm": dx,
        "dyMm": dy,
        "dzMm": dz,
        "distMm": dist,
        "rollDeg": roll,
        "pitchDeg": pitch,
        "yawDeg": yaw,
        "maxRotDeg": max_rot,
    }


TOL = {"positionMm": 15.0, "zToleranceMm": 10.0, "rotationDeg": 1.0}


def test_demo1_pass():
    """지시서 24절 Demo 1: +8/-4/+3mm, 신뢰도 HIGH → PASS."""
    j = judge(_dev(8, -4, 3), TOL, confidence="HIGH", uncertainty={"positionMm": 2, "rotationDeg": 0.1})
    assert j.decision == "PASS"


def test_demo2_fail():
    """지시서 24절 Demo 2: X +27mm > 공차 ±15mm → FAIL."""
    j = judge(_dev(27, 0, 0), TOL, confidence="HIGH", uncertainty={"positionMm": 2, "rotationDeg": 0.1})
    assert j.decision == "FAIL"
    assert any("position" in r for r in j.reasons)


def test_low_confidence_is_retake():
    j = judge(_dev(1, 0, 0), TOL, confidence="LOW")
    assert j.decision == "RETAKE"


def test_unknown_confidence_is_retake():
    j = judge(_dev(1, 0, 0), TOL, confidence="UNKNOWN")
    assert j.decision == "RETAKE"


def test_medium_confidence_within_tolerance_is_review():
    """공차 내여도 신뢰도가 HIGH가 아니면 자동 PASS 금지 (원칙 11)."""
    j = judge(_dev(3, 0, 0), TOL, confidence="MEDIUM", uncertainty={"positionMm": 1})
    assert j.decision == "REVIEW"


def test_straddle_is_review():
    """편차+불확도가 공차를 걸치면 확정 불가 → REVIEW."""
    j = judge(_dev(13, 0, 0), TOL, confidence="HIGH", uncertainty={"positionMm": 5})
    assert j.decision == "REVIEW"


def test_z_tolerance_independent():
    """Z 공차(10mm)가 더 타이트한 경우 — 수평 5mm여도 Z 12mm면 FAIL."""
    j = judge(_dev(0, 0, 12), TOL, confidence="HIGH", uncertainty={"positionMm": 1})
    assert j.decision == "FAIL"


def test_rotation_fail():
    j = judge(_dev(0, 0, 0, yaw=2.0), TOL, confidence="HIGH", uncertainty={"rotationDeg": 0.2})
    assert j.decision == "FAIL"


def test_rotation_within_tolerance_pass():
    j = judge(_dev(2, -1, 1, yaw=0.4), TOL, confidence="HIGH", uncertainty={"positionMm": 1, "rotationDeg": 0.1})
    assert j.decision == "PASS"


def test_fail_even_with_medium_confidence_when_certainly_out():
    j = judge(_dev(50, 0, 0), TOL, confidence="MEDIUM", uncertainty={"positionMm": 2})
    assert j.decision == "FAIL"


def test_missing_tolerance_raises_bim_data_error():
    """공차 누락은 BIM 데이터 오류 — 검사 중단 (12.2절)."""
    with pytest.raises(ValueError, match="tolerance"):
        judge(_dev(1, 0, 0), {"rotationDeg": 1.0}, confidence="HIGH")
    with pytest.raises(ValueError, match="tolerance"):
        judge(_dev(1, 0, 0), {}, confidence="HIGH")


def test_z_tolerance_defaults_to_position_tolerance():
    j = judge(_dev(0, 0, 12), {"positionMm": 15.0, "rotationDeg": 1.0}, confidence="HIGH")
    assert j.decision == "PASS"  # zToleranceMm 없음 → 15mm 적용
