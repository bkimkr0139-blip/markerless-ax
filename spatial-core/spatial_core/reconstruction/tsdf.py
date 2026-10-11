"""희소 블록 TSDF 융합 (numpy) — Open3D 비의존.

다수 RGB-D 관측을 한 표면에 가중 평균으로 합친다. 같은 표면의 반복 depth가 폴리곤을 쌓지 않고 확률적 관측으로
융합되며(INTERFACES §5.3), 한 프레임에서 사라진 물체(사람)는 free-space 갱신으로 서서히 지워진다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .camera import Frame, depth_normals_cos
from .mesh import TriMesh, weld

B = 16  # 블록 한 변 복셀 수
_CONF_W = np.array([0.15, 0.6, 1.0], dtype=np.float32)

_LOCAL = np.stack(np.meshgrid(np.arange(B), np.arange(B), np.arange(B), indexing="ij"), -1).reshape(-1, 3).astype(np.float64)


@dataclass
class TSDFStats:
    frames: int = 0
    blocks: int = 0
    skipped_frames: int = 0


class SparseTSDF:
    def __init__(self, voxel: float = 0.02, trunc_voxels: float = 3.0, max_weight: float = 48.0, max_depth: float = 6.0):
        self.vs = float(voxel)
        self.trunc = float(voxel) * trunc_voxels
        self.max_weight = float(max_weight)
        self.max_depth = float(max_depth)
        self._slot: dict[tuple[int, int, int], int] = {}
        self._cap = 0
        self.tsdf = np.zeros((0, B, B, B), np.float32)
        self.wt = np.zeros((0, B, B, B), np.float32)
        self.col = np.zeros((0, B, B, B, 3), np.uint8)
        self.stats = TSDFStats()

    # ---------- 저장소 ----------
    def _ensure(self, keys: list[tuple[int, int, int]]) -> np.ndarray:
        new = [k for k in keys if k not in self._slot]
        need = len(self._slot) + len(new)
        if need > self._cap:
            cap = max(need, int(self._cap * 1.6) + 64)
            for name, shape, dt, fill in (("tsdf", (B, B, B), np.float32, 1.0), ("wt", (B, B, B), np.float32, 0.0),
                                          ("col", (B, B, B, 3), np.uint8, 0)):
                old = getattr(self, name)
                arr = np.full((cap,) + shape, fill, dt)
                arr[: len(old)] = old
                setattr(self, name, arr)
            self._cap = cap
        for k in new:
            self._slot[k] = len(self._slot)
        return np.array([self._slot[k] for k in keys], dtype=np.int64)

    @property
    def block_count(self) -> int:
        return len(self._slot)

    # ---------- 융합 ----------
    def integrate(self, fr: Frame, stride: int = 4, min_conf: int = 1) -> bool:
        d = fr.depth.astype(np.float32)
        ok = np.isfinite(d) & (d > 0.1) & (d < self.max_depth)
        if fr.conf is not None:
            ok &= fr.conf >= min_conf
        if ok.sum() < 200:
            self.stats.skipped_frames += 1
            return False
        K = fr.K_depth
        T = fr.T_world_cv
        Tinv = np.linalg.inv(T)
        dh, dw = d.shape

        # 1) 표면 근방 블록 후보: 서브샘플 픽셀을 광선 방향 d−trunc, d, d+trunc 에서 샘플
        vv, uu = np.nonzero(ok[::stride, ::stride])
        vv *= stride
        uu *= stride
        z = d[vv, uu].astype(np.float64)
        ray = np.stack([(uu + 0.5 - K[0, 2]) / K[0, 0], (vv + 0.5 - K[1, 2]) / K[1, 1], np.ones_like(z)], 1)
        bs = self.vs * B
        keys = set()
        for dz in (-self.trunc, 0.0, self.trunc):
            pc = ray * (z + dz)[:, None]
            pw = pc @ T[:3, :3].T + T[:3, 3]
            kk = np.floor(pw / bs).astype(np.int64)
            keys.update(map(tuple, np.unique(kk, axis=0)))
        keys = sorted(keys)
        slots = self._ensure(keys)

        cosn = depth_normals_cos(d, K)
        blur_w = 1.0 - 0.7 * float(np.clip(fr.blur, 0, 1))
        conf = fr.conf if fr.conf is not None else np.full(d.shape, 2, np.uint8)
        confw = _CONF_W[np.clip(conf, 0, 2)]
        rgb = fr.rgb
        if rgb is not None:
            H, W = rgb.shape[:2]

        # 2) 블록별 복셀 갱신 (청크)
        CH = 192
        for s0 in range(0, len(keys), CH):
            kb = np.array(keys[s0 : s0 + CH], dtype=np.float64)
            sl = slots[s0 : s0 + CH]
            nb = len(kb)
            pw = ((kb[:, None, :] * B + _LOCAL[None] + 0.5) * self.vs).reshape(-1, 3)
            pc = pw @ Tinv[:3, :3].T + Tinv[:3, 3]
            zc = pc[:, 2]
            front = zc > 0.05
            zs = np.where(front, zc, 1.0)
            u = pc[:, 0] / zs * K[0, 0] + K[0, 2]
            v = pc[:, 1] / zs * K[1, 1] + K[1, 2]
            ui = np.floor(u).astype(np.int64)
            vi = np.floor(v).astype(np.int64)
            inside = front & (ui >= 0) & (ui < dw) & (vi >= 0) & (vi < dh)
            uc = np.clip(ui, 0, dw - 1)
            vc = np.clip(vi, 0, dh - 1)
            dpix = d[vc, uc]
            valid = inside & ok[vc, uc]
            sdf = dpix - zc
            upd = valid & (sdf > -self.trunc)
            if not upd.any():
                continue
            idx = np.nonzero(upd)[0]
            tval = np.clip(sdf[idx] / self.trunc, -1.0, 1.0).astype(np.float32)
            w = (confw[vc[idx], uc[idx]] * (0.25 + 0.75 * cosn[vc[idx], uc[idx]])
                 / (1.0 + (zc[idx] / 3.0) ** 2) * blur_w).astype(np.float32)
            bi = idx // (B * B * B)
            li = idx % (B * B * B)
            slot = sl[bi]
            flat_t = self.tsdf.reshape(len(self.tsdf), -1)
            flat_w = self.wt.reshape(len(self.wt), -1)
            W0 = flat_w[slot, li]
            T0 = flat_t[slot, li]
            W1 = W0 + w
            flat_t[slot, li] = (T0 * W0 + tval * w) / np.maximum(W1, 1e-6)
            flat_w[slot, li] = np.minimum(W1, self.max_weight)
            if rgb is not None:
                near = np.abs(sdf[idx]) < self.trunc * 0.6
                if near.any():
                    ii = idx[near]
                    cu = np.clip((u[ii] * W / dw).astype(np.int64), 0, W - 1)
                    cv_ = np.clip((v[ii] * H / dh).astype(np.int64), 0, H - 1)
                    newc = rgb[cv_, cu].astype(np.float32)
                    flat_c = self.col.reshape(len(self.col), -1, 3)
                    sl2 = slot[near]
                    li2 = li[near]
                    a = (w[near] / np.maximum(W1[near], 1e-6))[:, None]
                    flat_c[sl2, li2] = (flat_c[sl2, li2].astype(np.float32) * (1 - a) + newc * a).clip(0, 255).astype(np.uint8)
        self.stats.frames += 1
        self.stats.blocks = self.block_count
        return True

    # ---------- 추출 ----------
    def extract_mesh(self, min_weight: float = 0.4, with_color: bool = True) -> TriMesh:
        from skimage.measure import marching_cubes

        V, F, C = [], [], []
        nv = 0
        keys = list(self._slot.keys())
        for key in keys:
            s = self._slot[key]
            wt = self.wt[s]
            if wt.max() < min_weight:
                continue
            vol = np.ones((B + 1, B + 1, B + 1), np.float32)
            obs = np.zeros((B + 1, B + 1, B + 1), bool)
            colv = np.zeros((B + 1, B + 1, B + 1, 3), np.uint8)
            for dx in (0, 1):
                for dy in (0, 1):
                    for dz in (0, 1):
                        nk = (key[0] + dx, key[1] + dy, key[2] + dz)
                        ns = self._slot.get(nk)
                        if ns is None:
                            continue
                        sx = slice(0, B) if dx == 0 else slice(B, B + 1)
                        sy = slice(0, B) if dy == 0 else slice(B, B + 1)
                        sz = slice(0, B) if dz == 0 else slice(B, B + 1)
                        gx = slice(0, B) if dx == 0 else slice(0, 1)
                        gy = slice(0, B) if dy == 0 else slice(0, 1)
                        gz = slice(0, B) if dz == 0 else slice(0, 1)
                        vol[sx, sy, sz] = self.tsdf[ns][gx, gy, gz]
                        obs[sx, sy, sz] = self.wt[ns][gx, gy, gz] >= min_weight
                        colv[sx, sy, sz] = self.col[ns][gx, gy, gz]
            if not (vol[obs].min(initial=1) < 0 < vol[obs].max(initial=-1)):
                continue
            try:
                verts, faces, _n, _val = marching_cubes(vol, level=0.0, mask=obs, allow_degenerate=False)
            except (ValueError, RuntimeError):
                continue
            if len(faces) == 0:
                continue
            # 관측되지 않은 복셀이 모서리에 낀 큐브의 삼각형은 가짜 교차(관측↔기본값)이므로 버린다
            cen = np.floor(verts[faces].mean(axis=1)).astype(int).clip(0, B - 1)
            allobs = np.ones(len(faces), bool)
            for ox in (0, 1):
                for oy in (0, 1):
                    for oz in (0, 1):
                        allobs &= obs[cen[:, 0] + ox, cen[:, 1] + oy, cen[:, 2] + oz]
            faces = faces[allobs]
            if len(faces) == 0:
                continue
            origin = np.array(key, dtype=np.float64) * B * self.vs
            wv = origin + (verts.astype(np.float64) + 0.5) * self.vs
            if with_color:
                ij = np.clip(np.round(verts).astype(int), 0, B)
                C.append(colv[ij[:, 0], ij[:, 1], ij[:, 2]])
            V.append(wv)
            F.append(faces + nv)
            nv += len(wv)
        if not V:
            return TriMesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64))
        mesh = TriMesh(np.vstack(V), np.vstack(F), np.vstack(C) if with_color else None)
        return weld(mesh, tol=self.vs * 0.05)

    def surface_points(self, min_weight: float = 0.4, band: float = 0.5) -> np.ndarray:
        """표면 근처(|tsdf|<band) 관측 복셀 중심 — 커버리지 계산용."""
        pts = []
        for key, s in self._slot.items():
            m = (self.wt[s] >= min_weight) & (np.abs(self.tsdf[s]) < band)
            if m.any():
                ij = np.argwhere(m)
                pts.append((np.array(key) * B + ij + 0.5) * self.vs)
        return np.vstack(pts) if pts else np.zeros((0, 3))

    def observed_bounds(self, min_weight: float = 0.4) -> tuple[np.ndarray, np.ndarray] | None:
        p = self.surface_points(min_weight)
        return (p.min(0), p.max(0)) if len(p) else None
