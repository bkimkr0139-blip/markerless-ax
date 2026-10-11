"""AI 의미 분할로 융합에서 제외할 픽셀(사람·거울·유리·창)을 찾는다 — SegFormer-B0(ADE20K, ONNX).

- 사람/이동물: 프레임마다 위치가 달라 벽에 '구워지는' 것을 막는다 (지시서 §5.2-5).
- 거울·유리·창: LiDAR/RGB가 가짜 깊이·반사 영상을 만든다 → 그 픽셀은 깊이·텍스처에 쓰지 않고 '미관측'으로 남긴다(창은 개구부 후보로 이어진다).
모델이 없으면 마스크가 없다(전부 사용). 실제 iPhone 실내 장면에서의 정확도는 아직 검증되지 않았다(docs/GROUND_TRUTH.md).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from scipy import ndimage

MODEL_ENV = "MARKERLESS_SEG_MODEL"
_DEFAULT = Path(__file__).resolve().parents[3] / "models" / "segformer_b0_ade.onnx"
# ADE20K(150) 클래스 id: 8 windowpane, 12 person, 27 mirror, 147 glass
MASK_CLASSES = {8: "창", 12: "사람", 27: "거울", 147: "유리"}
PEOPLE = (12,)
REFLECTIVE = (8, 27, 147)
# 클래스별 최소 덩어리 크기(이미지 면적 비율): 작은 오검출 조각은 무시. 반사면은 경계가 거칠어(번짐) 더 크게 요구한다
MIN_FRAC = {12: 0.003, 8: 0.015, 27: 0.015, 147: 0.015}


class SemanticMasker:
    def __init__(self, model_path: str | os.PathLike | None = None, classes: dict[int, str] | None = None, dilate_px: int = 6) -> None:
        self.session = None
        self.classes = classes or MASK_CLASSES
        self.dilate = dilate_px
        path = Path(model_path or os.environ.get(MODEL_ENV) or _DEFAULT)
        if path.exists():
            try:
                import onnxruntime as ort

                so = ort.SessionOptions()
                so.log_severity_level = 3
                prov = [p for p in ("CoreMLExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
                self.session = ort.InferenceSession(str(path), sess_options=so, providers=prov)
                self.input_name = self.session.get_inputs()[0].name
            except Exception:  # noqa: BLE001
                self.session = None

    @property
    def available(self) -> bool:
        return self.session is not None

    def labels(self, rgb: np.ndarray) -> np.ndarray:
        """RGB(H,W,3) → 클래스 맵(H,W) int16."""
        from PIL import Image

        H, W = rgb.shape[:2]
        im = np.asarray(Image.fromarray(rgb).resize((512, 512), Image.BILINEAR), np.float32) / 255.0
        im = (im - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        logits = self.session.run(None, {self.input_name: im.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]  # (150,h,w)
        lab = logits.argmax(0).astype(np.int16)
        return np.asarray(Image.fromarray(lab.astype(np.uint8)).resize((W, H), Image.NEAREST)).astype(np.int16)

    def mask(self, rgb: np.ndarray, people: bool = True, reflective: bool = False) -> np.ndarray:
        """제외할 픽셀 True. people=사람, reflective=거울·유리·창(경계가 거칠어 기본 꺼짐). 모델이 없으면 전부 False."""
        H, W = rgb.shape[:2]
        out = np.zeros((H, W), bool)
        want = (PEOPLE if people else ()) + (REFLECTIVE if reflective else ())
        if not self.available or not want:
            return out
        lab = self.labels(rgb)
        for k in want:
            m = lab == k
            if not m.any():
                continue
            cc, n = ndimage.label(m)
            if n:
                sizes = ndimage.sum(m, cc, range(1, n + 1))
                keep = np.nonzero(sizes >= MIN_FRAC[k] * H * W)[0] + 1
                if len(keep):
                    out |= np.isin(cc, keep)
        if out.any() and self.dilate > 0:
            out = ndimage.binary_dilation(out, iterations=self.dilate)
        return out

    def describe(self, rgb: np.ndarray) -> dict[str, float]:
        lab = self.labels(rgb)
        return {name: round(float((lab == k).mean()), 4) for k, name in self.classes.items() if (lab == k).any()}
