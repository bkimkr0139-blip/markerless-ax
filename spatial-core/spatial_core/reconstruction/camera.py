"""ARKit 프레임 ↔ 컴퓨터 비전 규약 변환과 역투영.

ARKit 카메라: +X 오른쪽, +Y 위, −Z 전방. 이미지 픽셀은 좌상단 원점, v가 아래로 증가(OpenCV와 동일).
픽셀 규약(이 패키지 전체에서 동일): 정수 인덱스 i인 픽셀의 중심은 연속 좌표 i+0.5 이다(intrinsics의 cx,cy는 이미지 좌상단 모서리 기준).
실기기 ARKit의 half-pixel 규약은 실측으로 확인해야 한다(docs/GROUND_TRUTH.md). 틀리면 3m 거리에서 약 1cm 오차가 생긴다.
OpenCV 카메라(x 오른쪽, y 아래, z 전방) = ARKit 카메라의 (X, −Y, −Z).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_FLIP = np.diag([1.0, -1.0, -1.0, 1.0])


def mat16(vals) -> np.ndarray:
    """열 우선 16개 float → 4×4 (INTERFACES.md §1)."""
    a = np.asarray(vals, dtype=np.float64)
    if a.size != 16:
        raise ValueError("4x4 변환은 16개 값이어야 합니다")
    return a.reshape(4, 4).T


def to_mat16(T: np.ndarray) -> list[float]:
    return [float(x) for x in np.asarray(T, dtype=np.float64).T.reshape(-1)]


def intrinsics3(vals) -> np.ndarray:
    """열 우선 3×3 [fx,0,0, 0,fy,0, cx,cy,1] → K (행렬)."""
    a = np.asarray(vals, dtype=np.float64)
    if a.size != 9:
        raise ValueError("intrinsics는 9개 값이어야 합니다")
    return a.reshape(3, 3).T


@dataclass
class Frame:
    """융합에 쓰는 프레임 1개. depth는 m(무효 0), conf는 0/1/2, rgb는 HxWx3 uint8(없으면 None)."""

    index: int
    T_world_cam: np.ndarray  # ARKit 카메라 → 세션/공간 (4x4)
    K_rgb: np.ndarray  # RGB 이미지 픽셀 기준 3x3
    rgb_size: tuple[int, int]  # (W, H)
    depth: np.ndarray  # (dh, dw) float32
    conf: np.ndarray | None  # (dh, dw) uint8
    rgb: np.ndarray | None = None
    exposure: float = 1.0
    blur: float = 0.0
    mask: np.ndarray | None = None  # RGB 해상도, True = 융합·텍스처에서 제외(사람·반사면 등)

    @property
    def T_world_cv(self) -> np.ndarray:
        return self.T_world_cam @ _FLIP

    @property
    def K_depth(self) -> np.ndarray:
        """RGB 기준 intrinsics를 depth 해상도로 환산."""
        dh, dw = self.depth.shape
        W, H = self.rgb_size
        K = self.K_rgb.copy()
        K[0, :] *= dw / W
        K[1, :] *= dh / H
        return K

    @property
    def center(self) -> np.ndarray:
        return self.T_world_cam[:3, 3]


def depth_normals_cos(depth: np.ndarray, K: np.ndarray) -> np.ndarray:
    """깊이 맵에서 시선-표면 입사각의 |cos|를 추정 (정면=1, 비스듬히 볼수록 0). 무효 픽셀은 0."""
    dh, dw = depth.shape
    v, u = np.mgrid[0:dh, 0:dw].astype(np.float32) + 0.5
    z = depth.astype(np.float32)
    x = (u - K[0, 2]) / K[0, 0] * z
    y = (v - K[1, 2]) / K[1, 1] * z
    P = np.stack([x, y, z], -1)
    dx = np.zeros_like(P)
    dy = np.zeros_like(P)
    dx[:, 1:-1] = P[:, 2:] - P[:, :-2]
    dy[1:-1] = P[2:] - P[:-2]
    n = np.cross(dx, dy)
    nn = np.linalg.norm(n, axis=-1)
    ray = P / np.maximum(np.linalg.norm(P, axis=-1, keepdims=True), 1e-6)
    cos = np.abs((n * ray).sum(-1)) / np.maximum(nn, 1e-9)
    # 이웃과 깊이가 크게 다르면(가장자리) 신뢰 낮음
    jump = np.zeros_like(z, dtype=bool)
    jump[:, 1:-1] |= np.abs(z[:, 2:] - z[:, :-2]) > 0.25 * np.maximum(z[:, 1:-1], 0.1)
    jump[1:-1] |= np.abs(z[2:] - z[:-2]) > 0.25 * np.maximum(z[1:-1], 0.1)
    cos[(z <= 0) | jump | (nn < 1e-12)] = 0.0
    return cos.astype(np.float32)


def unproject(frame: Frame, stride: int = 1, min_conf: int = 1, max_depth: float = 6.0):
    """유효 깊이를 세션 좌표 점으로. 반환 (points Nx3, colors Nx3 uint8 | None)."""
    d = frame.depth
    ok = np.isfinite(d) & (d > 0.1) & (d < max_depth)
    if frame.conf is not None:
        ok &= frame.conf >= min_conf
    sel = np.zeros_like(ok)
    sel[::stride, ::stride] = True
    ok &= sel
    v, u = np.nonzero(ok)
    z = d[v, u].astype(np.float64)
    K = frame.K_depth
    pc = np.stack([(u + 0.5 - K[0, 2]) / K[0, 0] * z, (v + 0.5 - K[1, 2]) / K[1, 1] * z, z], 1)
    T = frame.T_world_cv
    pw = pc @ T[:3, :3].T + T[:3, 3]
    col = None
    if frame.rgb is not None:
        H, W = frame.rgb.shape[:2]
        uu = np.clip((u + 0.5) * W / d.shape[1], 0, W - 1).astype(int)
        vv = np.clip((v + 0.5) * H / d.shape[0], 0, H - 1).astype(int)
        col = frame.rgb[vv, uu]
    return pw, col
