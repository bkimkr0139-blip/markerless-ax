"""합성 방 렌더러 — 실기기 없이 재구성 파이프라인을 검증하기 위한 RGB-D 프레임 생성.

직육면체 방(바닥·천장·벽 4 + 문 개구부 선택) 과 박스 사물을 광선 투사로 렌더링하고,
벽마다 다른 체크 패턴을 입혀 색 투영 결과를 눈/수치로 확인할 수 있게 한다.
"""

from __future__ import annotations

import numpy as np

from .camera import Frame, to_mat16

_FLIP = np.diag([1.0, -1.0, -1.0, 1.0])


class SynthRoom:
    def __init__(self, size=(5.0, 2.7, 4.0), boxes=(((1.2, 0.0, 1.0), (0.5, 0.8, 0.5)),), door: tuple | None = None, checker: bool = True):
        self.checker = checker
        self.size = np.array(size, float)  # x폭, 높이, z깊이 — 바닥 y=0, x∈[0,X], z∈[0,Z]
        self.boxes = [(np.array(c, float), np.array(s, float)) for c, s in boxes]  # (min코너, 크기)
        self.door = door  # (wall 'z0', x0, x1, h)

    def _albedo(self, p: np.ndarray, label: np.ndarray) -> np.ndarray:
        base = np.array([[200, 80, 70], [70, 160, 90], [70, 90, 200], [220, 200, 80], [160, 160, 160], [120, 100, 80], [250, 140, 40]], float)
        c = base[label]
        if self.checker:
            chk = ((np.floor(p[:, 0] * 5) + np.floor(p[:, 1] * 5) + np.floor(p[:, 2] * 5)) % 2) * 0.35 + 0.65
        else:  # 면마다 균일한 색 — 노출·이음매 평가용
            chk = np.ones(len(p))
        return (c * chk[:, None]).clip(0, 255)

    def raycast(self, o: np.ndarray, dirs: np.ndarray):
        """광선(원점 o, 방향 N×3) → (거리 t, 라벨). 라벨 0..3 벽, 4 바닥, 5 천장, 6 박스."""
        X, Y, Z = self.size
        N = len(dirs)
        best = np.full(N, np.inf)
        lab = np.full(N, -1)
        with np.errstate(divide="ignore", invalid="ignore"):
            for L, (ax, val) in enumerate([(0, 0.0), (0, X), (2, 0.0), (2, Z), (1, 0.0), (1, Y)]):
                t = (val - o[ax]) / dirs[:, ax]
                ok = t > 0.05
                p = o + dirs * t[:, None]
                others = [a for a in range(3) if a != ax]
                lim = self.size
                ok &= (p[:, others[0]] >= -1e-6) & (p[:, others[0]] <= lim[others[0]] + 1e-6)
                ok &= (p[:, others[1]] >= -1e-6) & (p[:, others[1]] <= lim[others[1]] + 1e-6)
                if self.door and L == 2:  # z=0 벽에 문 개구부 → 그 뒤는 비어 있음(광선 통과 → 무효)
                    _w, x0, x1, h = self.door
                    hole = (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] <= h)
                    ok &= ~hole
                better = ok & (t < best)
                best[better] = t[better]
                lab[better] = L
            for c, s in self.boxes:
                lo, hi = c, c + s
                t1 = (lo - o) / dirs
                t2 = (hi - o) / dirs
                tn = np.minimum(t1, t2).max(axis=1)
                tf = np.maximum(t1, t2).min(axis=1)
                hit = (tn <= tf) & (tn > 0.05)
                better = hit & (tn < best)
                best[better] = tn[better]
                lab[better] = 6
        return best, lab

    def render(self, T_world_cam: np.ndarray, K_rgb: np.ndarray, rgb_size=(320, 240), depth_size=(80, 60),
               index=0, noise=0.003, rng=None) -> Frame:
        rng = rng or np.random.default_rng(index)
        T_cv = T_world_cam @ _FLIP
        o = T_cv[:3, 3]

        def cast(size):
            W, H = size
            Ks = K_rgb.copy()
            Ks[0, :] *= W / rgb_size[0]
            Ks[1, :] *= H / rgb_size[1]
            v, u = np.mgrid[0:H, 0:W]
            d_cam = np.stack([(u + 0.5 - Ks[0, 2]) / Ks[0, 0], (v + 0.5 - Ks[1, 2]) / Ks[1, 1], np.ones((H, W))], -1).reshape(-1, 3)
            dw = d_cam @ T_cv[:3, :3].T
            t, lab = self.raycast(o, dw / np.linalg.norm(dw, axis=1, keepdims=True))
            zc = t * (1.0 / np.linalg.norm(d_cam, axis=1))  # 광선 길이 → 깊이(z)
            return zc.reshape(H, W), lab.reshape(H, W), (o + (dw / np.linalg.norm(dw, axis=1, keepdims=True)) * t[:, None]).reshape(H, W, 3)

        zc, lab, _ = cast(depth_size)
        depth = np.where(np.isfinite(zc), zc + rng.normal(0, noise, zc.shape), 0).astype(np.float32)
        conf = np.where(depth > 0, 2, 0).astype(np.uint8)
        zr, labr, pr = cast(rgb_size)
        rgb = np.zeros(zr.shape + (3,), np.uint8)
        ok = np.isfinite(zr)
        rgb[ok] = self._albedo(pr[ok], labr[ok]).astype(np.uint8)
        return Frame(index=index, T_world_cam=T_world_cam, K_rgb=K_rgb, rgb_size=rgb_size, depth=depth, conf=conf, rgb=rgb)


def look_at_cam(eye, target, up=(0, 1, 0)) -> np.ndarray:
    """ARKit 카메라(−Z 전방) 포즈 4×4 (camera→world)."""
    eye = np.asarray(eye, float)
    f = np.asarray(target, float) - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2], T[:3, 3] = r, u, -f, eye
    return T


def orbit_frames(room: SynthRoom, n=60, K=None, rgb_size=(320, 240), depth_size=(80, 60), seed=0) -> list[Frame]:
    """방 안을 천천히 돌며(여러 높이·방향) 촬영한 프레임 열."""
    rng = np.random.default_rng(seed)
    X, Y, Z = room.size
    if K is None:
        f = 0.9 * rgb_size[0]
        K = np.array([[f, 0, rgb_size[0] / 2], [0, f, rgb_size[1] / 2], [0, 0, 1.0]])
    frames = []
    for i in range(n):
        ang = 2 * np.pi * i / n * 2.0
        eye = np.array([X / 2 + 0.6 * np.cos(ang * 0.5), 1.4 + 0.15 * np.sin(ang), Z / 2 + 0.6 * np.sin(ang * 0.5)])
        pitch = 0.35 * np.sin(ang * 1.7)
        tgt = eye + np.array([np.cos(ang), pitch, np.sin(ang)]) * 2.0
        frames.append(room.render(look_at_cam(eye, tgt), K, rgb_size, depth_size, index=i, rng=rng))
    return frames
