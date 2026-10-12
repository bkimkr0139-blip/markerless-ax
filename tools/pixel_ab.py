#!/usr/bin/env python3
"""픽셀 규약 A/B — 저장된 실기기 스캔 세션을 두 규약(MARKERLESS_PIXEL_OFFSET=0.5 / 0)으로 다시 융합해 어느 쪽이 맞는지 가린다.

맞는 규약일수록 같은 벽이 한 겹으로 얇게 나온다(벽 층 두께·평면 잔차가 작다). 차이가 뚜렷하면 권장값을 알려 준다.
사용: .venv/bin/python tools/pixel_ab.py <scanSessionId> [--db backend/markerless.db] [--storage backend/storage] [--frames 80]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spatial-core"))
sys.path.insert(0, str(ROOT / "backend"))


def measure(frames_meta, loader_factory, offset: str, n: int) -> dict:
    os.environ["MARKERLESS_PIXEL_OFFSET"] = offset
    from spatial_core.reconstruction.mesh import remove_small_components
    from spatial_core.reconstruction.structure import extract_structure
    from spatial_core.reconstruction.tsdf import SparseTSDF

    t = SparseTSDF(voxel=0.04)
    idx = np.linspace(0, len(frames_meta) - 1, min(n, len(frames_meta))).astype(int)
    for i in idx:
        fr = loader_factory(frames_meta[i])(640)
        fr.rgb = None  # 깊이만 필요
        t.integrate(fr, stride=2)
    mesh = remove_small_components(t.extract_mesh(), 0.1)
    if mesh.empty:
        return {"error": "메시가 비어 있음"}
    st = extract_structure(mesh, t.surface_points())
    c, nn = mesh.face_centers(), mesh.face_normals()
    spreads, rms = [], []
    for w in st.walls:
        nv = np.array([w.normal[0], 0.0, w.normal[1]])
        d = (mesh.v - np.array([w.p0[0], 0.0, w.p0[1]])) @ nv
        near = np.abs(d) < 0.06
        if near.sum() > 300:
            rms.append(float(np.sqrt(np.mean(d[near] ** 2))) * 1000)
        sel = (np.abs((c[:, [0, 2]] - np.array(w.p0)) @ nv[[0, 2]]) < 0.1) & (np.abs(nn @ nv) > 0.9)
        if sel.sum() > 100:
            dd = (c[sel][:, [0, 2]] - np.array(w.p0)) @ nv[[0, 2]]
            spreads.append(float(np.percentile(dd, 95) - np.percentile(dd, 5)) * 1000)
    return {"walls": len(st.walls), "wallLayerSpreadMm": round(float(np.mean(spreads)), 1) if spreads else None,
            "planeRmsMm": round(float(np.mean(rms)), 1) if rms else None, "coverage": round(st.overall_coverage, 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--db", default=str(ROOT / "backend" / "markerless.db"))
    ap.add_argument("--storage", default=str(ROOT / "backend" / "storage"))
    ap.add_argument("--frames", type=int, default=80)
    a = ap.parse_args()
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models_twin import ScanFrame
    from app.services.twin import _make_loader
    from app.storage import FileStorage

    eng = create_engine(f"sqlite:///{a.db}")
    storage = FileStorage(a.storage)
    with Session(eng) as db:
        frames = db.scalars(select(ScanFrame).where(ScanFrame.session_id == a.session, ScanFrame.state == "DONE").order_by(ScanFrame.frame_index)).all()
        if len(frames) < 12:
            sys.exit(f"프레임이 {len(frames)}개뿐입니다")
        db.expunge_all()
    fac = lambda fr: _make_loader(storage, a.session, fr)  # noqa: E731
    res = {off: measure(frames, fac, off, a.frames) for off in ("0.5", "0")}
    print(json.dumps(res, indent=1, ensure_ascii=False))
    s5, s0 = res["0.5"].get("wallLayerSpreadMm"), res["0"].get("wallLayerSpreadMm")
    if s5 is None or s0 is None:
        print("판정 불가: 벽이 충분히 잡히지 않았습니다 — 벽이 많이 보이는 스캔으로 다시")
        return
    ratio = max(s5, s0) / max(min(s5, s0), 0.1)
    best = "0.5" if s5 <= s0 else "0"
    if ratio < 1.15:
        print(f"차이가 작습니다(벽 번짐 {s5} vs {s0}mm, 비 {ratio:.2f}) — 기본값(0.5) 유지, 다른 방으로 한 번 더 확인")
    else:
        print(f"권장: MARKERLESS_PIXEL_OFFSET={best} (벽 층 번짐 0.5→{s5}mm, 0→{s0}mm, {ratio:.1f}배 차이)")
        if best == "0":
            print("→ 서버를 MARKERLESS_PIXEL_OFFSET=0 으로 재시작하세요 (Makefile api 타깃에 환경변수 추가)")


if __name__ == "__main__":
    main()
