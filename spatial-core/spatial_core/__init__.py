"""spatial-core: 좌표·정합·판정 엔진 (개발지시서 9/13/14절).

- coordinate : SE(3) 포즈, 좌표변환 체인, Design-vs-Actual delta
- registration: GLB 파싱/샘플링, numpy 기반 ICP (의존성 최소화)
- inspection : 공차 판정 (PASS/REVIEW/FAIL/RETAKE/INVALID)
- confidence : 측정 신뢰도 산출 (HIGH/MEDIUM/LOW)
"""

from spatial_core.coordinate import Pose, pose_delta, Z_UP_TO_Y_UP
from spatial_core.inspection import Judgment, judge
from spatial_core.registration import ICPResult, icp_align
from spatial_core.confidence import derive_confidence

__version__ = "0.1.0"

__all__ = [
    "Pose",
    "pose_delta",
    "Z_UP_TO_Y_UP",
    "Judgment",
    "judge",
    "ICPResult",
    "icp_align",
    "derive_confidence",
]
