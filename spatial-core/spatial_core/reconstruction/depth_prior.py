"""깊이 품질 향상 — RGB 가이드 업샘플링 + 단안(monocular) 깊이 AI 추정(Depth Anything V2)을 LiDAR에 정렬해 빈 곳을 채운다.

원칙(지시서 §4·§13.1):
- **LiDAR 관측이 있는 픽셀은 LiDAR가 진실**이다. AI 추정은 그것을 덮어쓰지 않는다.
- AI가 채운 깊이는 `inferred`로 분리해 따로 다룬다(OBSERVED로 위장 금지). 신뢰도·불확실성을 같이 낸다.
- 단안 모델은 상대(affine-invariant) 역깊이만 안다 → 프레임마다 LiDAR와의 강건한 아핀 정합(1/depth = a·rel + b)으로 미터 단위를 맞춘다.
  정합 품질(잔차·인라이어·결정계수)이 나쁘면 그 프레임의 AI 추정은 쓰지 않는다.
- 모델 파일이 없으면 AI 단계는 조용히 꺼지고 고전적 가이드 업샘플링만 동작한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage

MODEL_ENV = "MARKERLESS_DEPTH_MODEL"
_DEFAULT_MODEL = Path(__file__).resolve().parents[3] / "models" / "depth_anything_v2_small.onnx"


# ───────────── 고전: 가이디드 필터 업샘플링 ─────────────

def _box(a: np.ndarray, r: int) -> np.ndarray:
    return ndimage.uniform_filter(a, size=2 * r + 1, mode="nearest")


def guided_filter(guide: np.ndarray, src: np.ndarray, r: int = 4, eps: float = 1e-3, weight: np.ndarray | None = None) -> np.ndarray:
    """He et al. 가이드 필터(회색 가이드). weight로 유효 픽셀만 통계에 반영한다."""
    w = np.ones_like(src) if weight is None else weight.astype(np.float64)
    mw = _box(w, r)
    mw = np.maximum(mw, 1e-6)
    mg = _box(guide * w, r) / mw
    ms = _box(src * w, r) / mw
    cgs = _box(guide * src * w, r) / mw - mg * ms
    vg = _box(guide * guide * w, r) / mw - mg * mg
    a = cgs / (vg + eps)
    b = ms - a * mg
    return _box(a, r) * guide + _box(b, r)


def fill_holes_nearest(depth: np.ndarray, valid: np.ndarray) -> np.ndarray:
    if valid.all() or not valid.any():
        return depth
    _, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    return depth[iy, ix]


def guided_upsample(depth_lr: np.ndarray, valid_lr: np.ndarray, rgb_hr: np.ndarray, r: int = 3, eps: float = 2e-4) -> tuple[np.ndarray, np.ndarray]:
    """저해상도 LiDAR 깊이를 RGB 해상도로 올린다. 경계는 RGB 경계를 따른다. 반환 (depth_hr, valid_hr)
    — valid_hr는 원래 LiDAR 유효 픽셀 주변(관측이 뒷받침하는 곳)만 True이고, 빈 곳은 False로 남긴다(만들어 채우지 않는다)."""
    H, W = rgb_hr.shape[:2]
    h, w = depth_lr.shape
    zoom = (H / h, W / w)
    d0 = np.where(valid_lr, depth_lr, 0.0)
    # 유효 가중 보간 (빈 곳이 0으로 끌어내리지 않게 분자/분모를 따로 보간)
    num = ndimage.zoom(d0, zoom, order=1)
    den = ndimage.zoom(valid_lr.astype(np.float64), zoom, order=1)
    ok = den > 0.5
    base = np.where(ok, num / np.maximum(den, 1e-6), 0.0)
    gray = rgb_hr.astype(np.float64).mean(-1) / 255.0 if rgb_hr.ndim == 3 else rgb_hr.astype(np.float64)
    filled = fill_holes_nearest(base, ok)
    out = guided_filter(gray, filled, r=r, eps=eps, weight=None)
    # 가이드 필터가 깊이 범위를 벗어나지 않게
    lo, hi = float(depth_lr[valid_lr].min()), float(depth_lr[valid_lr].max())
    out = np.clip(out, lo * 0.98, hi * 1.02)
    support = ndimage.binary_dilation(ok, iterations=1)
    return out.astype(np.float32), support


# ───────────── AI: 단안 깊이 → LiDAR 정렬 ─────────────

@dataclass
class AlignFit:
    a: float
    b: float
    inlier: float
    rmse_inv: float
    r2: float
    n: int
    ok: bool
    reason: str = ""


def fit_affine_inverse_depth(rel: np.ndarray, lidar: np.ndarray, valid: np.ndarray, iters: int = 6, min_n: int = 400,
                             min_r2: float = 0.55, min_inlier: float = 0.5) -> AlignFit:
    """1/depth ≈ a·rel + b 를 강건하게(트림 최소제곱) 맞춘다. rel/lidar/valid는 같은 격자."""
    m = valid & np.isfinite(lidar) & (lidar > 0.2) & np.isfinite(rel)
    if m.sum() < min_n:
        return AlignFit(0, 0, 0, 9, 0, int(m.sum()), False, "유효 LiDAR 점이 부족")
    x = rel[m].astype(np.float64)
    y = 1.0 / lidar[m].astype(np.float64)
    keep = np.ones(len(x), bool)
    a, b = 1.0, 0.0
    for _ in range(iters):
        A = np.stack([x[keep], np.ones(keep.sum())], 1)
        sol, *_ = np.linalg.lstsq(A, y[keep], rcond=None)
        a, b = float(sol[0]), float(sol[1])
        res = np.abs(a * x + b - y)
        thr = max(np.quantile(res, 0.7), 0.01)
        keep = res <= thr * 1.5
    res = a * x + b - y
    inl = float((np.abs(res) < 0.05 + 0.05 * y).mean())  # 역깊이 5% 이내
    ss = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((res[keep] ** 2).sum()) / max(float(((y[keep] - y[keep].mean()) ** 2).sum()), 1e-9) if keep.sum() > 10 else 0.0
    rmse = float(np.sqrt(np.mean(res[keep] ** 2)))
    ok = a > 0 and r2 >= min_r2 and inl >= min_inlier
    return AlignFit(a, b, inl, rmse, r2, int(m.sum()), ok, "" if ok else f"정합 품질 낮음(R²={r2:.2f}, 인라이어={inl:.2f})")


def fit_local_affine(rel: np.ndarray, lidar: np.ndarray, valid: np.ndarray, glob: AlignFit, sigma_px: float = 18.0, ridge: float = 0.08,
                     s0: float = 25.0) -> tuple[np.ndarray, np.ndarray]:
    """공간적으로 변하는 아핀(a(x), b(x)): 각 위치에서 주변 LiDAR 점들(가우시안 가중)로 1/depth = a·rel + b 를 푼다.
    LiDAR가 없는 큰 구멍은 가장자리 관측으로 앵커링되고, 관측에서 멀면 전역 해로 서서히 돌아간다(정규화·블렌딩)."""
    m = valid & np.isfinite(lidar) & (lidar > 0.2)
    y = np.where(m, 1.0 / np.maximum(lidar, 0.2), 0.0)
    x = np.where(m, rel, 0.0)
    mf = m.astype(np.float64)

    def G(a):
        return ndimage.gaussian_filter(a, sigma_px, mode="nearest")

    S1, Sx, Sy = G(mf), G(x), G(y)
    Sxx, Sxy = G(x * x), G(x * y)
    var_x = max(float(np.var(rel[m])), 1e-6)
    den = (Sxx - Sx * Sx / np.maximum(S1, 1e-9)) + ridge * var_x * np.maximum(S1, 1e-9)
    a_l = (Sxy - Sx * Sy / np.maximum(S1, 1e-9)) / np.maximum(den, 1e-12)
    b_l = (Sy - a_l * Sx) / np.maximum(S1, 1e-9)
    w = S1 * (sigma_px ** 2 * 2 * np.pi) / (S1 * (sigma_px ** 2 * 2 * np.pi) + s0)  # 주변 유효 점 수에 비례한 신뢰
    bad = (a_l <= 0) | ~np.isfinite(a_l)
    a = np.where(bad, glob.a, w * a_l + (1 - w) * glob.a)
    b = np.where(bad, glob.b, w * b_l + (1 - w) * glob.b)
    return a, b


class DepthPrior:
    """Depth Anything V2(ONNX) 래퍼. 없으면 available=False."""

    def __init__(self, model_path: str | os.PathLike | None = None, input_hw: tuple[int, int] = (294, 392)) -> None:
        self.session = None
        self.input_hw = input_hw
        path = Path(model_path or os.environ.get(MODEL_ENV) or _DEFAULT_MODEL)
        if path.exists():
            try:
                import onnxruntime as ort

                prov = [p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
                so = ort.SessionOptions()
                so.log_severity_level = 3
                self.session = ort.InferenceSession(str(path), sess_options=so, providers=prov)
                self.input_name = self.session.get_inputs()[0].name
            except Exception:  # noqa: BLE001
                self.session = None

    @property
    def available(self) -> bool:
        return self.session is not None

    def relative(self, rgb: np.ndarray) -> np.ndarray:
        """RGB(H,W,3 uint8) → 상대 역깊이(입력 해상도 격자 → 원래 해상도로 쌍선형 복원), 값이 클수록 가깝다."""
        from PIL import Image

        H, W = rgb.shape[:2]
        ih, iw = self.input_hw
        im = np.asarray(Image.fromarray(rgb).resize((iw, ih), Image.BICUBIC), dtype=np.float32) / 255.0
        im = (im - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        out = self.session.run(None, {self.input_name: im.transpose(2, 0, 1)[None].astype(np.float32)})[0]
        rel = np.asarray(out).reshape(out.shape[-2], out.shape[-1]).astype(np.float64)
        return ndimage.zoom(rel, (H / rel.shape[0], W / rel.shape[1]), order=1)


@dataclass
class Densified:
    depth_obs: np.ndarray  # RGB 해상도, LiDAR 기반 (빈 곳 0)
    obs_valid: np.ndarray
    depth_inferred: np.ndarray  # AI가 채운 깊이 (obs가 없는 곳만, 나머지 0)
    inferred_valid: np.ndarray
    inferred_sigma: np.ndarray  # 추정 불확실성(m) — 거리에 비례
    fit: AlignFit | None


def densify(depth_lr: np.ndarray, conf_lr: np.ndarray | None, rgb: np.ndarray, prior: DepthPrior | None, max_depth: float = 6.0,
            fill_ai: bool = True, local_affine: bool = True, guided: bool = False) -> Densified:
    """LiDAR(저해상도) + RGB → (관측 깊이 업샘플, AI 추정으로 채운 빈 곳). 관측 픽셀은 AI가 건드리지 않는다."""
    valid_lr = np.isfinite(depth_lr) & (depth_lr > 0.1) & (depth_lr < max_depth)
    if conf_lr is not None:
        valid_lr &= conf_lr >= 1
    H, W = rgb.shape[:2]
    if guided:  # 실제 RGB-D 벤치(tools/depth_prior_bench.py)에서 쌍선형 대비 이득이 없어 기본 꺼짐
        d_obs, support = guided_upsample(depth_lr, valid_lr, rgb)
    else:
        h, w = depth_lr.shape
        zoom = (H / h, W / w)
        num = ndimage.zoom(np.where(valid_lr, depth_lr, 0.0), zoom, order=1)
        den = ndimage.zoom(valid_lr.astype(np.float64), zoom, order=1)
        support = den > 0.5
        d_obs = np.where(support, num / np.maximum(den, 1e-6), 0.0)
    d_obs = np.where(support, d_obs, 0.0)
    inferred = np.zeros((H, W), np.float32)
    inf_valid = np.zeros((H, W), bool)
    sigma = np.zeros((H, W), np.float32)
    fit = None
    if fill_ai and prior is not None and prior.available and valid_lr.sum() > 400:
        rel = prior.relative(rgb)
        # 정합은 저해상도 격자에서 (LiDAR 픽셀과 1:1)
        rel_lr = ndimage.zoom(rel, (depth_lr.shape[0] / H, depth_lr.shape[1] / W), order=1)
        fit = fit_affine_inverse_depth(rel_lr, depth_lr, valid_lr)
        if fit.ok:
            if local_affine:
                a_l, b_l = fit_local_affine(rel_lr, depth_lr, valid_lr, fit)
                a_f = ndimage.zoom(a_l, (H / a_l.shape[0], W / a_l.shape[1]), order=1)
                b_f = ndimage.zoom(b_l, (H / b_l.shape[0], W / b_l.shape[1]), order=1)
                inv = a_f * rel + b_f
            else:
                inv = fit.a * rel + fit.b
            with np.errstate(divide="ignore", invalid="ignore"):
                dd = np.where(inv > 1.0 / max_depth, 1.0 / inv, 0.0)
            hole = ~support & (dd > 0.2) & (dd < max_depth)
            inferred = np.where(hole, dd, 0.0).astype(np.float32)
            inf_valid = hole
            # 불확실성: 정합 잔차(역깊이)에서 유도 — σ_depth ≈ σ_inv · depth²  (+ 모델 한계 바닥값 3%)
            sigma = np.where(hole, np.sqrt((fit.rmse_inv * dd ** 2) ** 2 + (0.03 * dd) ** 2), 0.0).astype(np.float32)
    return Densified(d_obs.astype(np.float32), support, inferred, inf_valid, sigma, fit)
