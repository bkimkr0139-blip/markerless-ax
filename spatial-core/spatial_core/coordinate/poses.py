"""강체 포즈와 좌표 규약 (docs/COORDINATE.md의 구현).

규약:
- 오른손 좌표계, 내부 단위는 미터(m)와 라디안(rad).
- roll/pitch/yaw는 intrinsic Z-Y-X 순서(먼저 Z축 yaw, 그 다음 Y축 pitch, 마지막 X축 roll):
  R = Rz(yaw) @ Ry(pitch) @ Rx(roll)
- T_A_B: B프레임에서 표현된 점을 A프레임으로 옮기는 변환. p_A = T_A_B @ p_B.
- API(JSON)의 quaternion은 {x, y, z, w} (Hamilton, w가 마지막).
- BIM/Revit은 Z-up, glTF/ARKit/ARCore는 Y-up (둘 다 오른손).
  Z_UP_TO_Y_UP: BIM (x,y,z)up -> glTF (x,z,-y)up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

DEG2RAD = float(np.pi / 180.0)
RAD2DEG = float(180.0 / np.pi)

# BIM/Revit(Z-up) -> glTF/ARKit/ARCore(Y-up). BIM의 +Y(북쪽)은 glTF의 -Z(전방)이 된다.
Z_UP_TO_Y_UP = np.array(
    [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.0, -1.0, 0.0],
    ]
)


def rpy_deg_to_matrix(roll_deg: float, pitch_deg: float, yaw_deg: float) -> np.ndarray:
    """intrinsic Z-Y-X 오일러각(도) -> 3x3 회전행렬."""
    r = roll_deg * DEG2RAD
    p = pitch_deg * DEG2RAD
    y = yaw_deg * DEG2RAD
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    Rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    Ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    return Rz @ Ry @ Rx


def matrix_to_rpy_deg(R: np.ndarray) -> tuple[float, float, float]:
    """3x3 회전행렬 -> intrinsic Z-Y-X 오일러각(도). 반환: (roll, pitch, yaw)."""
    sy = -float(R[2, 0])
    sy = max(-1.0, min(1.0, sy))
    pitch = float(np.arcsin(sy))
    if abs(sy) < 1.0 - 1e-9:
        roll = float(np.arctan2(R[2, 1], R[2, 2]))
        yaw = float(np.arctan2(R[1, 0], R[0, 0]))
    else:  # gimbal lock: pitch = ±90°, roll/yaw 중 하나만 결정 가능
        roll = float(np.arctan2(-R[1, 2], R[1, 1]))
        yaw = 0.0
    return (roll * RAD2DEG, pitch * RAD2DEG, yaw * RAD2DEG)


def quat_to_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    """{x,y,z,w} quaternion -> 3x3 회전행렬."""
    n = np.sqrt(x * x + y * y + z * z + w * w)
    if n == 0.0:
        raise ValueError("quaternion norm is zero")
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def matrix_to_quat(R: np.ndarray) -> tuple[float, float, float, float]:
    """3x3 회전행렬 -> Hamilton 쿼터니언 (x, y, z, w), w >= 0."""
    R = np.asarray(R, dtype=float)
    tr = float(np.trace(R))
    if tr > 0.0:
        s = np.sqrt(tr + 1.0) * 2.0
        w, x, y, z = 0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = np.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
        w, x, y, z = (R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = np.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
        w, x, y, z = (R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s
    else:
        s = np.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
        w, x, y, z = (R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s
    if w < 0:
        w, x, y, z = -w, -x, -y, -z
    n = float(np.sqrt(w * w + x * x + y * y + z * z))
    return (float(x / n), float(y / n), float(z / n), float(w / n))


@dataclass
class Pose:
    """SE(3) 포즈. position은 미터.

    Pose는 프레임 매핑 T_A_B를 나타낸다 (A = 부모, B = 자식 프레임):
      p_A = rotation @ p_B + position
    """

    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))

    def __post_init__(self) -> None:
        self.position = np.asarray(self.position, dtype=float).reshape(3)
        self.rotation = np.asarray(self.rotation, dtype=float).reshape(3, 3)

    # ---- 생성 ----
    @classmethod
    def from_rpy_deg(
        self, position_m: dict[str, float], rpy_deg: dict[str, float]
    ) -> "Pose":
        return Pose(
            position=np.array(
                [position_m["x"], position_m["y"], position_m["z"]], dtype=float
            ),
            rotation=rpy_deg_to_matrix(rpy_deg["roll"], rpy_deg["pitch"], rpy_deg["yaw"]),
        )

    @classmethod
    def from_json(self, data: dict[str, Any]) -> "Pose":
        """API canonical JSON에서 복원.

        허용 형식:
          {"positionM": {x,y,z}, "rpyDeg": {roll,pitch,yaw}}          (canonical)
          {"positionM": {x,y,z}, "quat": {x,y,z,w}}                    (입력 대안)
          {"position": {x,y,z}, "rpy": {...}}                          (관대한 별칭)
        """
        pos = data.get("positionM", data.get("position"))
        if pos is None:
            raise ValueError("pose JSON requires positionM")
        rpy = data.get("rpyDeg", data.get("rpy"))
        quat = data.get("quat", data.get("quaternion"))
        if rpy is not None:
            return Pose.from_rpy_deg(pos, rpy)
        if quat is not None:
            return Pose(
                position=np.array([pos["x"], pos["y"], pos["z"]], dtype=float),
                rotation=quat_to_matrix(quat["x"], quat["y"], quat["z"], quat["w"]),
            )
        raise ValueError("pose JSON requires rpyDeg or quat")

    # ---- 직렬화 ----
    def to_json(self) -> dict[str, Any]:
        roll, pitch, yaw = matrix_to_rpy_deg(self.rotation)
        return {
            "positionM": {
                "x": float(self.position[0]),
                "y": float(self.position[1]),
                "z": float(self.position[2]),
            },
            "rpyDeg": {"roll": roll, "pitch": pitch, "yaw": yaw},
        }

    # ---- 연산 ----
    def apply(self, points: np.ndarray) -> np.ndarray:
        """(N,3) 점들을 이 포즈가 정의한 프레임으로 변환."""
        pts = np.asarray(points, dtype=float).reshape(-1, 3)
        return pts @ self.rotation.T + self.position

    def compose(self, other: "Pose") -> "Pose":
        """T_A_C = T_A_B.compose(T_B_C)."""
        return Pose(
            position=self.rotation @ other.position + self.position,
            rotation=self.rotation @ other.rotation,
        )

    def inverse(self) -> "Pose":
        """T_B_A = T_A_B.inverse()."""
        R_T = self.rotation.T
        return Pose(position=-R_T @ self.position, rotation=R_T)

    def __matmul__(self, other: "Pose") -> "Pose":
        return self.compose(other)

    # ---- 4x4 동차행렬 상호변환 (ICP 등 행렬 기반 코드 연계용) ----
    def to_matrix(self) -> np.ndarray:
        T = np.eye(4)
        T[:3, :3] = self.rotation
        T[:3, 3] = self.position
        return T

    @classmethod
    def from_matrix(self, T: np.ndarray) -> "Pose":
        T = np.asarray(T, dtype=float)
        return Pose(position=T[:3, 3].copy(), rotation=T[:3, :3].copy())


def pose_delta(design: Pose, actual: Pose) -> dict[str, float]:
    """Design-vs-Actual 편차 (개발지시서 13.1절).

    - 위치 편차: 공간 프레임 기준 성분差 dx/dy/dz (mm)와 유클리드 거리 distMm.
      보고 축은 공간(BIM-로컬) 좌표계 X/Y/Z — 자산 로컬 축 보고는 Week 5 옵션.
    - 회전 편차: R_rel = R_design^T @ R_actual 을 ZYX 오일러각(도)으로 분해.
    """
    dp = actual.position - design.position  # 미터
    R_rel = design.rotation.T @ actual.rotation
    roll, pitch, yaw = matrix_to_rpy_deg(R_rel)
    return {
        "dxMm": float(dp[0] * 1000.0),
        "dyMm": float(dp[1] * 1000.0),
        "dzMm": float(dp[2] * 1000.0),
        "distMm": float(np.linalg.norm(dp) * 1000.0),
        "rollDeg": roll,
        "pitchDeg": pitch,
        "yawDeg": yaw,
        "maxRotDeg": float(max(abs(roll), abs(pitch), abs(yaw))),
    }
