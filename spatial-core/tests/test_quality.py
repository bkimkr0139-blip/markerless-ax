"""정밀도·품질 기법 검증(합성 정답): 포즈 그래프, 메시 정규화, 텍스처 보정, 사람 마스킹, 깊이 정합. 효과가 입증된 기법만 기본값으로 켠다."""

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from spatial_core.reconstruction.camera import Frame
from spatial_core.reconstruction.depth_prior import DepthPrior, fit_affine_inverse_depth, fit_local_affine, guided_filter
from spatial_core.reconstruction.mesh import remove_small_components
from spatial_core.reconstruction.pose_graph import apply_to_frames, optimize, so3_exp, so3_log
from spatial_core.reconstruction.refine_mesh import regularize
from spatial_core.reconstruction.semantic_mask import SemanticMasker
from spatial_core.reconstruction.structure import extract_structure
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames
from spatial_core.reconstruction.texture import build_texture
from spatial_core.reconstruction.tsdf import SparseTSDF


def _spread_x0(mesh):
    c, n = mesh.face_centers(), mesh.face_normals()
    w = (np.abs(c[:, 0]) < 0.3) & (np.abs(n[:, 0]) > 0.9)
    assert w.sum() > 50
    return float(np.percentile(c[w, 0], 95) - np.percentile(c[w, 0], 5))


def _fuse(frames, voxel=0.04):
    t = SparseTSDF(voxel=voxel)
    for f in frames:
        t.integrate(f, stride=2)
    return t, remove_small_components(t.extract_mesh(), 0.1)


def _drift(frames, sig_t, sig_r_deg, seed):
    rng = np.random.default_rng(seed)
    G = np.eye(4)
    out = []
    for f in frames:
        d = np.eye(4)
        d[:3, :3] = so3_exp(np.radians(rng.normal(0, sig_r_deg, 3)))
        d[:3, 3] = rng.normal(0, sig_t, 3)
        G = d @ G
        out.append(Frame(f.index, G @ f.T_world_cam, f.K_rgb, f.rgb_size, f.depth, f.conf, f.rgb))
    return out


def test_so3_roundtrip():
    w = np.array([0.3, -0.2, 0.5])
    assert np.allclose(so3_log(so3_exp(w)), w, atol=1e-9)


def test_pose_graph_removes_accumulated_drift_and_does_no_harm_without_it():
    room = SynthRoom(size=(5, 2.7, 4))
    base = orbit_frames(room, n=120, rgb_size=(160, 120), depth_size=(80, 60))
    dr = _drift(base, 0.002, 0.05, 1)  # 프레임당 2mm/0.05° 누적 랜덤워크
    _t, m_bad = _fuse(dr)
    res = optimize(dr, key_step=4)
    assert res.accepted and res.loops >= 10
    _t, m_fix = _fuse(apply_to_frames(dr, res, 4))
    assert _spread_x0(m_fix) < 0.25 * _spread_x0(m_bad), "층 번짐이 4배 이상 줄어야 한다"
    # 드리프트가 없으면 보정을 채택하지 않는다 (정확한 데이터를 건드리지 않음)
    assert optimize(base, key_step=4).accepted is False


def test_mesh_regularization_improves_flatness_and_keeps_objects():
    box = ((1.2, 0.0, 1.0), (0.5, 0.8, 0.5))
    room = SynthRoom(size=(5, 2.7, 4), boxes=(box,))
    rng = np.random.default_rng(1)
    fr = [Frame(f.index, f.T_world_cam, f.K_rgb, f.rgb_size, (f.depth + rng.normal(0, 0.008, f.depth.shape) * (f.depth > 0)).astype(np.float32), f.conf, f.rgb)
          for f in orbit_frames(room, n=90)]
    t, m = _fuse(fr, 0.03)
    st = extract_structure(m, t.surface_points())
    r, stats = regularize(m, st)

    def flat(mm):
        X, Y, Z = room.size
        e = []
        for ax, val in ((0, 0.0), (0, X), (1, 0.0), (1, Y), (2, 0.0), (2, Z)):
            w = np.abs(mm.v[:, ax] - val) < 0.04
            if w.sum() > 200:
                e.append(np.abs(mm.v[w, ax] - val).mean())
        return float(np.mean(e))

    assert flat(r) < 0.6 * flat(m)
    c, n = r.face_centers(), r.face_normals()
    top = (n[:, 1] > 0.9) & (c[:, 0] > 1.25) & (c[:, 0] < 1.65) & (c[:, 2] > 1.05) & (c[:, 2] < 1.45) & (c[:, 1] > 0.5)
    assert top.sum() > 5 and abs(float(np.median(c[top, 1])) - 0.8) < 0.01, "사물(박스) 높이가 보존되어야 한다"
    assert stats.corners > 0 and stats.max_move_cm < 6


def test_texture_exposure_solve_reduces_patchiness():
    room = SynthRoom(size=(5, 2.7, 4), boxes=(), checker=False)
    base = orbit_frames(room, n=90, rgb_size=(480, 360), depth_size=(120, 90))
    rng = np.random.default_rng(5)
    frs = [Frame(f.index, f.T_world_cam, f.K_rgb, f.rgb_size, f.depth, f.conf, np.clip(f.rgb.astype(float) * rng.uniform(0.7, 1.35, 3), 0, 255).astype(np.uint8)) for f in base]
    _t, m = _fuse(base)

    def patchiness(tm):
        nn = m.face_normals()
        ax = np.argmax(np.abs(nn), 1)
        key = ax * 2 + (nn[np.arange(len(nn)), ax] > 0)
        res = []
        for k in np.unique(key):
            fs = np.nonzero(key == k)[0]
            px = []
            for fi in fs[:: max(1, len(fs) // 1500)]:
                uv = tm.uv[tm.f[fi]].mean(0)
                x, y = int(uv[0] * tm.atlas.shape[1]), int(uv[1] * tm.atlas.shape[0])
                if tm.observed_mask[y, x]:
                    px.append(tm.atlas[y, x].astype(float))
            if len(px) > 50:
                px = np.array(px)
                res.append(np.abs(px - np.median(px, 0)).mean())
        return float(np.mean(res))

    plain = patchiness(build_texture(m, frs, atlas_px=1024, refine=False))
    refined = patchiness(build_texture(m, frs, atlas_px=1024, refine=True))
    assert refined < 0.9 * plain


def test_person_masking_removes_ghost_surfaces():
    room = SynthRoom(size=(5, 2.7, 4), boxes=())
    base = orbit_frames(room, n=70)
    rng = np.random.default_rng(3)
    frs = []
    for f in base:
        h, w = f.depth.shape
        H, W = f.rgb.shape[:2]
        x0, y0, x1, y1 = int(rng.uniform(0.1, 0.6) * w), int(0.15 * h), 0, int(0.95 * h)
        x1 = x0 + int(0.22 * w)
        d = f.depth.copy()
        d[y0:y1, x0:x1] = rng.uniform(1.4, 2.0)
        mask = np.zeros((H, W), bool)
        mask[int(y0 * H / h): int(y1 * H / h), int(x0 * W / w): int(x1 * W / w)] = True
        frs.append(Frame(f.index, f.T_world_cam, f.K_rgb, f.rgb_size, d, f.conf, f.rgb, mask=mask))

    def ghost(use_mask):
        t = SparseTSDF(voxel=0.04)
        for f in frs:
            d = f.depth
            if use_mask:
                md = np.asarray(Image.fromarray(f.mask.astype(np.uint8) * 255).resize((d.shape[1], d.shape[0]))) > 127
                d = np.where(md, 0, d).astype(np.float32)
            t.integrate(Frame(f.index, f.T_world_cam, f.K_rgb, f.rgb_size, d, f.conf, f.rgb), stride=2)
        m = remove_small_components(t.extract_mesh(), 0.1)
        c, a = m.face_centers(), m.face_areas()
        inner = (c[:, 0] > 0.15) & (c[:, 0] < 4.85) & (c[:, 2] > 0.15) & (c[:, 2] < 3.85) & (c[:, 1] > 0.15) & (c[:, 1] < 2.55)
        return float(a[inner].sum())

    assert ghost(False) > 1.0, "마스크 없으면 사람이 방 안에 유령 표면을 남긴다"
    assert ghost(True) < 0.1


def test_masker_without_model_masks_nothing(tmp_path):
    sm = SemanticMasker(model_path=tmp_path / "missing.onnx")
    assert not sm.available and not sm.mask(np.zeros((40, 60, 3), np.uint8)).any()


def test_depth_affine_alignment_recovers_metric_scale():
    rng = np.random.default_rng(0)
    true = rng.uniform(0.8, 4.0, (60, 80))
    rel = 0.7 * (1.0 / true) + 0.12 + rng.normal(0, 0.003, true.shape)  # 상대 역깊이 (아핀 모호)
    valid = rng.random(true.shape) > 0.5
    fit = fit_affine_inverse_depth(rel, true, valid)
    assert fit.ok and abs(fit.a - 1 / 0.7) < 0.05  # 1/depth = a·rel + b
    rel_bad = rng.random(true.shape)  # 무관한 노이즈면 정합을 거부해야 한다
    assert not fit_affine_inverse_depth(rel_bad, true, valid).ok
    a, b = fit_local_affine(rel, true, valid, fit)
    assert a.shape == true.shape and np.isfinite(a).all()


def test_guided_filter_preserves_shape():
    g = np.random.default_rng(0).random((30, 40))
    assert guided_filter(g, g.copy(), r=3).shape == g.shape


@pytest.mark.skipif(not DepthPrior().available, reason="AI 깊이 모델 파일 없음")
def test_depth_prior_model_runs():
    rgb = (np.random.default_rng(0).random((120, 160, 3)) * 255).astype(np.uint8)
    rel = DepthPrior().relative(rgb)
    assert rel.shape == (120, 160) and np.isfinite(rel).all()
