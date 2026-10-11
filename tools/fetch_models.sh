#!/usr/bin/env bash
# AI 모델 내려받기(선택) — 없으면 해당 AI 단계가 조용히 꺼지고 나머지 파이프라인은 그대로 동작한다.
#   segformer_b0_ade.onnx        사람/거울/유리/창 의미 분할 (SegFormer-B0 ADE20K, ~15MB)  → 융합·텍스처에서 사람 제외
#   depth_anything_v2_small.onnx 단안 깊이 (Depth Anything V2 Small, ~99MB) → 실험용(기본 꺼짐, docs/REPORT.md 참고)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models && cd models
[ -f segformer_b0_ade.onnx ] || curl -fL -o segformer_b0_ade.onnx https://huggingface.co/Xenova/segformer-b0-finetuned-ade-512-512/resolve/main/onnx/model.onnx
[ "${1:-}" = "--depth" ] && { [ -f depth_anything_v2_small.onnx ] || curl -fL -o depth_anything_v2_small.onnx https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx; }
ls -la
