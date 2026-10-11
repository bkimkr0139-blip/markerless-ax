"""프레임-모델 자세 보정 — 융합된 표면(모델)에 새 프레임의 깊이 점을 point-to-plane ICP로 맞춰 VIO 오차를 줄인다.

ARKit 포즈는 국소적으로는 정확하지만 cm 단위로 흔들리고 흘러간다(드리프트). 보정 없이 융합하면 같은 벽이 여러 겹으로 번진다.
KinectFusion식으로 이미 융합된 모델에 맞춘 뒤 적분하면 층이 얇아진다. 보정은 작은 값만(이동·회전 상한) 받아들이고,
잔차가 실제로 줄지 않으면 원래 포즈를 쓴다 — 틀린 보정이 오히려 망치는 것을 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .camera import Frame, unproject
from .tsdf import SparseTSDF


@dataclass
class TrackInfo:
    applied: bool
    trans_cm: float = 0.0
    rot_deg: float = 0.0
    resid_before_mm: float = 0.0
    resid_after_mm: float = 0.0
    reason: str = ""


def _small_rot(w: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(w)
    if th < 1e-12:
        return np.eye(3)
    k = w / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


def damped_plane_icp(src: np.ndarray, tree: cKDTree, pts: np.ndarray, nrm: np.ndarray, iters: int = 8, gate: float = 0.08, trim: float = 0.25,
                     sigma_res: float = 0.006, sigma_t: float = 0.03, sigma_r_deg: float = 1.5):
    """사전분포로 감쇠한 점-대-평면 가우스-뉴턴(MAP). 벽·바닥뿐인 방처럼 평면 위로 미끄러지는 방향은 사전분포가 0으로 붙잡아
    제약이 잘 되는 성분(법선 방향 이동, 평면을 가로지르는 회전)만 보정된다. 반환 (T 4x4, 사용 점 수)."""
    R = np.eye(3)
    t = np.zeros(3)
    prior = np.diag([1 / np.radians(sigma_r_deg) ** 2] * 3 + [1 / sigma_t ** 2] * 3)
    used = 0
    for _ in range(iters):
        cur = src @ R.T + t
        d, idx = tree.query(cur, k=1)
        ok = d < gate
        if ok.sum() < 100:
            break
        p, q, n = cur[ok], pts[idx[ok]], nrm[idx[ok]]
        r = np.einsum("ij,ij->i", p - q, n)
        keep = np.abs(r) <= np.quantile(np.abs(r), 1 - trim)
        p, n, r = p[keep], n[keep], r[keep]
        J = np.hstack([np.cross(p, n), n])
        # 현재 보정(R,t)에 대한 증분 [ω,δt]를 풀고 합성 — 사전분포는 누적 보정량(R,t) 기준
        x_now = np.hstack([_rot_vec(R), t])
        A = J.T @ J / sigma_res ** 2 + prior
        b = -(J.T @ r) / sigma_res ** 2 - prior @ x_now
        dx = np.linalg.solve(A, b)
        dR = _small_rot(dx[:3])
        R = dR @ R
        t = dR @ t + dx[3:]
        used = len(r)
        if np.linalg.norm(dx[3:]) < 1e-5 and np.linalg.norm(dx[:3]) < 1e-6:
            break
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, t
    return T, used


def _rot_vec(R: np.ndarray) -> np.ndarray:
    th = np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1))
    if th < 1e-9:
        return np.zeros(3)
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2 * np.sin(th))
    return w * th


class PoseTracker:
    def __init__(self, tsdf: SparseTSDF, refresh_every: int = 10, warmup: int = 6, max_trans_m: float = 0.10, max_rot_deg: float = 6.0,
                 sample_stride: int = 5, max_model_pts: int = 70000):
        self.tsdf, self.refresh_every, self.warmup = tsdf, refresh_every, warmup
        self.max_t, self.max_r, self.stride, self.cap = max_trans_m, max_rot_deg, sample_stride, max_model_pts
        self._pts = np.zeros((0, 3))
        self._nrm = np.zeros((0, 3))
        self._tree: cKDTree | None = None
        self._since = 10**9
        self.applied = 0
        self.total = 0
        self.sum_t = 0.0
        self.sum_r = 0.0

    def _refresh(self) -> None:
        # 복셀 중심은 격자에 양자화돼(±반 복셀) 모델로 쓰기엔 거칠다 — 서브복셀 정밀도의 메시 정점과 정점 법선을 쓴다
        m = self.tsdf.extract_mesh(min_weight=1.5, with_color=False)
        if m.empty or len(m.v) < 500:
            self._tree = None
            return
        p, n = m.v, m.vertex_normals()
        if len(p) > self.cap:
            sel = np.random.default_rng(0).choice(len(p), self.cap, replace=False)
            p, n = p[sel], n[sel]
        self._pts, self._nrm = p, n
        self._tree = cKDTree(p)
        self._since = 0

    def correct(self, fr: Frame, n_integrated: int) -> tuple[Frame, TrackInfo]:
        self.total += 1
        if n_integrated < self.warmup:
            return fr, TrackInfo(False, reason="warmup")
        if self._tree is None or self._since >= self.refresh_every:
            self._refresh()
        self._since += 1
        if self._tree is None:
            return fr, TrackInfo(False, reason="모델 점 부족")
        dh, dw = fr.depth.shape
        stride = max(1, int(round(np.sqrt(dh * dw / 1800.0))))  # 해상도와 무관하게 약 1,800점 샘플
        src, _ = unproject(fr, stride=stride, min_conf=1, max_depth=self.tsdf.max_depth)
        if len(src) < 200:
            return fr, TrackInfo(False, reason="프레임 점 부족")
        d0, i0 = self._tree.query(src, k=1)
        keep = d0 < 0.15
        if keep.sum() < 200:
            return fr, TrackInfo(False, reason="모델과 겹침 부족")
        s = src[keep]
        n0 = self._nrm[i0[keep]]
        before = float(np.sqrt(np.mean(np.einsum("ij,ij->i", s - self._pts[i0[keep]], n0) ** 2)))
        T, n_used = damped_plane_icp(s, self._tree, self._pts, self._nrm)
        ang = float(np.degrees(np.arccos(np.clip((np.trace(T[:3, :3]) - 1) / 2, -1, 1))))
        tr = float(np.linalg.norm(T[:3, 3]))
        s2 = s @ T[:3, :3].T + T[:3, 3]
        d2, j2 = self._tree.query(s2, k=1)
        ok = d2 < 0.08
        after = float(np.sqrt(np.mean(np.einsum("ij,ij->i", s2[ok] - self._pts[j2[ok]], self._nrm[j2[ok]]) ** 2))) if ok.any() else 9.0
        info = TrackInfo(False, tr * 100, ang, before * 1000, after * 1000)
        if tr > self.max_t or ang > self.max_r:
            info.reason = "보정량이 상한을 넘음"
            return fr, info
        if n_used < 150 or after > 0.9 * before:
            info.reason = "잔차가 충분히 줄지 않음"
            return fr, info
        new = Frame(fr.index, T @ fr.T_world_cam, fr.K_rgb, fr.rgb_size, fr.depth, fr.conf, fr.rgb, fr.exposure, fr.blur)
        info.applied = True
        self.applied += 1
        self.sum_t += tr
        self.sum_r += ang
        return new, info

    def summary(self) -> dict:
        n = max(self.applied, 1)
        return {"frames": self.total, "applied": self.applied, "meanCorrectionCm": round(100 * self.sum_t / n, 2), "meanCorrectionDeg": round(self.sum_r / n, 2)}
