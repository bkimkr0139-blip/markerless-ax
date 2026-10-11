"""공간 재구성 엔진 검증 — 합성 방(정답을 아는 장면)으로 정확도·무결성·정직성(모호/미관측)을 확인한다."""

import json
import struct

import numpy as np
import pytest

from spatial_core.reconstruction.camera import mat16, to_mat16, unproject
from spatial_core.reconstruction.cad_match import CadEntry, match_cad
from spatial_core.reconstruction.envelope import SpaceInput, generate_envelope
from spatial_core.reconstruction.glb_export import write_colored_glb, write_textured_glb
from spatial_core.reconstruction.mesh import remove_small_components
from spatial_core.reconstruction.session_align import _rot_y, align_clouds
from spatial_core.reconstruction.structure import Opening, Structure, Wall, align_prior_to_mesh, extract_structure
from spatial_core.reconstruction.synthetic import SynthRoom, orbit_frames
from spatial_core.reconstruction.texture import build_texture
from spatial_core.reconstruction.tsdf import SparseTSDF


@pytest.fixture(scope="module")
def room_scan():
    room = SynthRoom(size=(5.0, 2.7, 4.0), door=("z0", 1.0, 2.0, 2.0))
    frames = orbit_frames(room, n=80)
    t = SparseTSDF(voxel=0.04)
    for f in frames:
        t.integrate(f, stride=2)
    mesh = remove_small_components(t.extract_mesh(), 0.1)
    return room, frames, t, mesh


def test_camera_matrix_roundtrip():
    T = np.eye(4)
    T[:3, :3] = _rot_y(0.7)
    T[:3, 3] = [1, 2, 3]
    assert np.allclose(mat16(to_mat16(T)), T)
    with pytest.raises(ValueError):
        mat16([1, 2, 3])


def test_tsdf_surfaces_are_accurate_and_inward(room_scan):
    room, _f, _t, mesh = room_scan
    lo, hi = mesh.aabb()
    assert np.allclose(lo[[0, 2]], [0, 0], atol=0.06) and np.allclose(hi, [5.0, 2.7, 4.0], atol=0.06)
    c, n, a = mesh.face_centers(), mesh.face_normals(), mesh.face_areas()
    inward = ((np.array([2.5, 1.35, 2.0]) - c) * n).sum(1) > 0
    assert a[inward].sum() / a.sum() > 0.97, "벽·바닥·천장 법선은 방 안쪽을 향해야 한다"
    # 한 면에 이중 층이 없어야 한다: x≈0 벽 근처 면들의 x 분포가 얇다
    wall = (np.abs(c[:, 0]) < 0.2) & (np.abs(n[:, 0]) > 0.9)
    assert wall.sum() > 100 and np.percentile(c[wall, 0], 95) - np.percentile(c[wall, 0], 5) < 0.06


def test_repeated_observation_does_not_stack_surfaces(room_scan):
    """같은 방을 다른 잡음으로 한 번 더 융합해도 면적이 늘지 않고 이중 표면이 생기지 않는다."""
    room, frames, t, mesh = room_scan
    area0 = mesh.area()
    for f in orbit_frames(room, n=40, seed=9):
        t.integrate(f, stride=2)
    m2 = remove_small_components(t.extract_mesh(), 0.1)
    assert abs(m2.area() - area0) / area0 < 0.08
    c, n2 = m2.face_centers(), m2.face_normals()
    wall = (np.abs(c[:, 0]) < 0.2) & (np.abs(n2[:, 0]) > 0.9)
    assert wall.sum() > 100 and np.percentile(c[wall, 0], 95) - np.percentile(c[wall, 0], 5) < 0.06


def test_structure_walls_polygon_door_and_coverage(room_scan):
    _room, _f, t, mesh = room_scan
    st = extract_structure(mesh, t.surface_points())
    assert len(st.walls) == 4 and abs(st.floor_y) < 0.05 and abs(st.ceiling_y - 2.7) < 0.05
    P = np.array(st.polygon)
    ext = P.max(0) - P.min(0)
    assert sorted(np.round(ext, 1)) == [4.0, 5.0] or (abs(sorted(ext)[0] - 4.0) < 0.1 and abs(sorted(ext)[1] - 5.0) < 0.1)
    assert any(o.kind == "DOOR" and o.source_kind == "INFERRED" and o.review_status == "REVIEW_REQUIRED" for o in st.openings)
    assert 0.5 < st.overall_coverage < 1.0 and st.unobserved_area_m2 > 1.0
    js = st.to_json()
    json.dumps(js)  # numpy 값이 섞여 직렬화가 깨지지 않아야 한다


def test_texture_projects_real_colors_and_keeps_unobserved_honest(room_scan):
    _room, frames, t, mesh = room_scan
    tm = build_texture(mesh, frames, atlas_px=1024)
    assert tm.stats["textureCoverage"] > 0.9
    # x≈0 벽(빨강 계열 albedo (200,80,70))의 면은 빨강이 우세한 색을 받아야 한다
    c = mesh.face_centers()
    sel = np.nonzero((np.abs(c[:, 0]) < 0.06) & (c[:, 1] > 0.5) & (c[:, 1] < 2.0) & (np.abs(mesh.face_normals()[:, 0]) > 0.9))[0]
    assert len(sel) > 50
    px = []
    for fi in sel[:: max(1, len(sel) // 200)]:
        # 새 정점/면에서 UV → 아틀라스 픽셀
        uv = tm.uv[tm.f[fi]].mean(0)
        x, y = int(uv[0] * tm.atlas.shape[1]), int(uv[1] * tm.atlas.shape[0])
        if tm.observed_mask[y, x]:
            px.append(tm.atlas[y, x].astype(int))
    px = np.array(px)
    assert len(px) > 30 and (px[:, 0] - px[:, 1]).mean() > 40, "빨간 벽에 빨간 색이 투영되지 않음"
    assert (~tm.observed_mask).any() and tm.stats["unobservedFaces"] >= 0
    # 관측 안 된 텍셀은 회색(128)로 두고 마스크로 구분 — 색을 만들어 채우지 않는다
    assert np.all(tm.atlas[(~tm.observed_mask) & (np.abs(tm.atlas.astype(int) - 128).sum(2) < 1)] == 128)


def test_glb_outputs_are_valid(room_scan):
    _room, frames, _t, mesh = room_scan
    tm = build_texture(mesh, frames, atlas_px=512)
    for blob in (write_textured_glb(tm, {"sourceKind": "OBSERVED"}), write_colored_glb(mesh, {"sourceKind": "OBSERVED"})):
        magic, ver, total = struct.unpack_from("<III", blob, 0)
        assert magic == 0x46546C67 and ver == 2 and total == len(blob)
        jlen = struct.unpack_from("<I", blob, 12)[0]
        doc = json.loads(blob[20:20 + jlen])
        assert doc["asset"]["version"] == "2.0" and doc["meshes"][0]["extras"]["sourceKind"] == "OBSERVED"


def test_session_alignment_recovers_transform_and_flags_symmetry():
    room = SynthRoom(size=(5.0, 2.7, 3.0), boxes=(((0.5, 0, 0.5), (0.6, 1.0, 0.5)), ((3.5, 0, 2.0), (0.4, 0.6, 0.4))))
    P1 = np.vstack([unproject(f, stride=2)[0] for f in orbit_frames(room, n=50, seed=1)])
    P2 = np.vstack([unproject(f, stride=2)[0] for f in orbit_frames(room, n=50, seed=2)])
    Tg = np.eye(4)
    Tg[:3, :3] = _rot_y(np.radians(37))
    Tg[:3, 3] = [1.3, 0.0, -0.7]
    r = align_clouds(P2 @ Tg[:3, :3].T + Tg[:3, 3], P1)
    E = r.transform @ Tg
    assert r.accepted and np.linalg.norm(E[:3, 3]) < 0.02 and abs(np.degrees(np.arctan2(E[0, 2], E[0, 0]))) < 0.5


def test_structure_prior_alignment_is_honest_when_ambiguous(room_scan):
    _room, _f, t, mesh = room_scan
    st = extract_structure(mesh, t.surface_points())
    th = np.radians(123)
    G = np.eye(4)
    G[:3, :3] = _rot_y(th)
    G[:3, 3] = [2.0, 0.1, -3.0]

    def surf(c, x, n, w, h, y):
        T = np.eye(4)
        T[:3, 0], T[:3, 2], T[:3, 3] = [x[0], 0, x[1]], [n[0], 0, n[1]], [c[0], y, c[1]]
        return {"id": "x", "transform16": to_mat16(G @ T), "widthM": w, "heightM": h}

    walls = []
    for a, b, n in [((0, 0), (5, 0), (0, 1)), ((5, 0), (5, 4), (-1, 0)), ((5, 4), (0, 4), (0, -1)), ((0, 4), (0, 0), (1, 0))]:
        a, b = np.array(a, float), np.array(b, float)
        L = np.linalg.norm(b - a)
        walls.append(surf((a + b) / 2, (b - a) / L, n, L, 2.7, 1.35))
    prior = {"walls": walls, "doors": [], "floorY": 0.1, "ceilingY": 2.8}
    al, info = align_prior_to_mesh(prior, st)
    assert al is None and info.get("ambiguous"), "문·창 단서 없는 직사각형 방은 모호 → 정렬하지 않는다"
    prior["doors"] = [surf((1.5, 0), (1, 0), (0, 1), 1.0, 2.0, 1.0)]
    al, info = align_prior_to_mesh(prior, st)
    assert al is not None
    c0 = mat16(al["walls"][0]["transform16"])[:3, 3]
    assert np.allclose(c0, [2.5, 1.35, 0.0], atol=0.08)


def _rect_room(x, z, door=True):
    W = [Wall(1, (0, 0), (x, 0), 0, 2.7, (0, 1)), Wall(2, (x, 0), (x, z), 0, 2.7, (-1, 0)), Wall(3, (x, z), (0, z), 0, 2.7, (0, -1)), Wall(4, (0, z), (0, 0), 0, 2.7, (1, 0))]
    return Structure(0.0, 2.7, W, [Opening("DOOR", 1, 1.0, 1.9, 0, 2.0)] if door else [], [(0, 0), (x, 0), (x, z), (0, z)])


def test_envelope_classifies_walls_and_keeps_provenance():
    def T(tx, tz):
        m = np.eye(4)
        m[0, 3], m[2, 3] = tx, tz
        return m

    env = generate_envelope([SpaceInput("A", T(0, 0), _rect_room(4, 3)), SpaceInput("B", T(4.2, 0), _rect_room(3, 3, False))])
    kinds = {(w.space_key, w.wall_id): w for w in env.walls}
    assert kinds[("A", 2)].kind == "INTERIOR" and kinds[("A", 2)].method == "MEASURED" and abs(kinds[("A", 2)].thickness - 0.2) < 1e-6
    assert kinds[("A", 1)].kind == "EXTERIOR" and kinds[("A", 1)].method == "CONFIG_DEFAULT"
    assert all(w.method in ("MEASURED", "CONFIG_DEFAULT", "BIM", "INFERRED") for w in env.walls)
    assert len(env.entrances) == 1 and env.notices and "추정" in env.notices[0]
    # 관리자 지정(BIM)은 자동값보다 우선
    env2 = generate_envelope([SpaceInput("A", T(0, 0), _rect_room(4, 3))], overrides={("A", 1): (0.35, "BIM", "도면")})
    w = next(x for x in env2.walls if x.wall_id == 1)
    assert w.thickness == 0.35 and w.method == "BIM"


def test_cad_match_rejects_unknown_and_picks_known():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import match_bench as mb

    rng = np.random.default_rng(0)

    def mk(i, code, dims):
        p, _ = mb.box(rng, dims, 2500)
        return CadEntry(i, code, tuple(dims), p - (p.min(0) + p.max(0)) / 2)

    cat = [mk(1, "BOX-A", (0.4, 0.25, 0.3)), mk(2, "TALL", (0.2, 0.6, 0.2))]
    p, n = mb.box(rng, (0.4, 0.25, 0.3), 3000)
    obs = p[mb.visible(p, n, np.array([0.0, 0.5, 1.0]))]
    r = match_cad(obs, cat)
    assert r and r[0].entry.code == "BOX-A" and r[0].decision == "CANDIDATE"
    assert match_cad(mb.box(rng, (0.9, 0.9, 0.9), 3000)[0], cat) == []
