"""사진 질감 생성 — 융합 메시에 UV 아틀라스를 만들고 관측 프레임을 가시성 검사 후 투영·가중 혼합한다.

단계(지시서 §6.2): ① 표면 차트 분할 + 평면 UV 전개 + 아틀라스 패킹 ② 텍셀마다 후보 프레임 선택
③ 깊이 버퍼로 가림(occlusion) 검사 ④ 정면성·거리·선명도·가장자리 가중 + 노출 정규화 혼합
⑤ 투영되지 않은 텍셀은 UNOBSERVED(회색 + 마스크)로 표시하고 만들어 채우지 않는다 ⑥ 경계 확장(seam 완화).
멀티밴드 블렌딩은 구현하지 않았다(가중 평균 + 경계 확장까지). 한계는 docs/REPORT.md 참고.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .camera import Frame
from .mesh import TriMesh


@dataclass
class TexturedMesh:
    v: np.ndarray  # (N,3)
    n: np.ndarray  # (N,3)
    uv: np.ndarray  # (N,2) glTF 규약 (v는 아래로 증가)
    f: np.ndarray  # (M,3)
    atlas: np.ndarray  # (H,W,3) uint8
    observed_mask: np.ndarray  # (H,W) bool — 프레임에서 실제 색을 얻은 텍셀
    texel_m: float
    stats: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)


def _charts(mesh: TriMesh, axes: np.ndarray):
    """면 법선의 지배 축(6방향)별로 나누고 같은 방향끼리 연결된 성분 = 차트."""
    fn = mesh.face_normals()
    local = fn @ axes.T  # 기준 프레임 기준 법선
    dom = np.argmax(np.abs(local), axis=1)
    sign = np.sign(local[np.arange(len(fn)), dom])
    label6 = dom * 2 + (sign < 0)
    f = mesh.f
    n = len(mesh.v)
    # 같은 label6 인 면이 정점을 공유하면 연결
    rows, cols = [], []
    for k in range(6):
        sel = np.nonzero(label6 == k)[0]
        if not len(sel):
            continue
        ff = f[sel]
        rows += [ff[:, 0], ff[:, 1], ff[:, 2]]
        cols += [ff[:, 1], ff[:, 2], ff[:, 0]]
        # 정점 노드에 label6 오프셋을 주어 다른 방향 면과 섞이지 않게 한다
    # 노드 = (정점, label6) 쌍
    key = (f.astype(np.int64) * 6 + label6[:, None]).reshape(-1)
    uniq, inv = np.unique(key, return_inverse=True)
    inv = inv.reshape(-1, 3)
    m = len(uniq)
    g = coo_matrix((np.ones(len(inv) * 3, np.int8), (np.r_[inv[:, 0], inv[:, 1], inv[:, 2]], np.r_[inv[:, 1], inv[:, 2], inv[:, 0]])), shape=(m, m))
    _, lab = connected_components(g, directed=False)
    return lab[inv[:, 0]], dom


def _pack(sizes: list[tuple[int, int]], atlas: int):
    """선반(shelf) 패킹. 반환 (좌표 리스트 | None)."""
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    pos = [None] * len(sizes)
    x = y = shelf_h = 0
    for i in order:
        w, h = sizes[i]
        if w > atlas or h > atlas:
            return None
        if x + w > atlas:
            x, y, shelf_h = 0, y + shelf_h, 0
        if y + h > atlas:
            return None
        pos[i] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return pos


def build_atlas_layout(mesh: TriMesh, axes: np.ndarray, atlas_px: int, texel_m: float | None, pad: int = 3):
    chart_of_face, dom = _charts(mesh, axes)
    nch = chart_of_face.max() + 1
    f = mesh.f
    # 차트별 2D 투영 좌표 (지배 축 제외 두 축)
    P = mesh.v @ axes.T
    ax_u = (dom + 1) % 3
    ax_v = (dom + 2) % 3
    corner_u = P[f, :][np.arange(len(f))[:, None], np.arange(3)[None, :], ax_u[:, None]]
    corner_v = P[f, :][np.arange(len(f))[:, None], np.arange(3)[None, :], ax_v[:, None]]
    cmin_u = np.full(nch, np.inf)
    cmax_u = np.full(nch, -np.inf)
    cmin_v = np.full(nch, np.inf)
    cmax_v = np.full(nch, -np.inf)
    for k in range(3):
        np.minimum.at(cmin_u, chart_of_face, corner_u[:, k])
        np.maximum.at(cmax_u, chart_of_face, corner_u[:, k])
        np.minimum.at(cmin_v, chart_of_face, corner_v[:, k])
        np.maximum.at(cmax_v, chart_of_face, corner_v[:, k])
    ext_u = cmax_u - cmin_u
    ext_v = cmax_v - cmin_v
    rect_area = float((ext_u * ext_v).sum())
    tm = texel_m or max(0.005, float(np.sqrt(max(rect_area, 1e-6) / (atlas_px * atlas_px * 0.55))))
    for _ in range(40):
        sizes = [(int(np.ceil(ext_u[c] / tm)) + 2 * pad + 1, int(np.ceil(ext_v[c] / tm)) + 2 * pad + 1) for c in range(nch)]
        pos = _pack(sizes, atlas_px)
        if pos is not None:
            break
        tm *= 1.12
    else:
        raise RuntimeError("텍스처 아틀라스 패킹 실패")
    return dict(chart_of_face=chart_of_face, dom=dom, ax_u=ax_u, ax_v=ax_v, corner_u=corner_u, corner_v=corner_v,
                cmin_u=cmin_u, cmin_v=cmin_v, sizes=sizes, pos=pos, texel_m=tm, pad=pad)


def _rasterize(lay, atlas_px: int):
    """모든 면을 UV 공간에 래스터라이즈 → 텍셀별 (면 번호, 무게중심). 반환 tx, ty, face, bary(N,3)."""
    ch = lay["chart_of_face"]
    pos = np.array(lay["pos"])
    tm = lay["texel_m"]
    pad = lay["pad"]
    # 각 면 꼭짓점의 아틀라스 픽셀 좌표
    px = (lay["corner_u"] - lay["cmin_u"][ch][:, None]) / tm + pos[ch, 0][:, None] + pad
    py = (lay["corner_v"] - lay["cmin_v"][ch][:, None]) / tm + pos[ch, 1][:, None] + pad
    x0 = np.floor(px.min(1)).astype(np.int64)
    x1 = np.ceil(px.max(1)).astype(np.int64)
    y0 = np.floor(py.min(1)).astype(np.int64)
    y1 = np.ceil(py.max(1)).astype(np.int64)
    bw = np.maximum(x1 - x0, 1)
    bh = np.maximum(y1 - y0, 1)
    cnt = bw * bh
    tot = int(cnt.sum())
    face = np.repeat(np.arange(len(ch)), cnt)
    off = np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    tx = x0[face] + off % bw[face]
    ty = y0[face] + off // bw[face]
    cx = tx + 0.5
    cy = ty + 0.5
    ax, ay = px[face, 0], py[face, 0]
    bx, by = px[face, 1], py[face, 1]
    qx, qy = px[face, 2], py[face, 2]
    den = (by - qy) * (ax - qx) + (qx - bx) * (ay - qy)
    ok = np.abs(den) > 1e-9
    l1 = ((by - qy) * (cx - qx) + (qx - bx) * (cy - qy)) / np.where(ok, den, 1)
    l2 = ((qy - ay) * (cx - qx) + (ax - qx) * (cy - qy)) / np.where(ok, den, 1)
    l3 = 1 - l1 - l2
    eps = -0.02
    inside = ok & (l1 >= eps) & (l2 >= eps) & (l3 >= eps) & (tx >= 0) & (tx < atlas_px) & (ty >= 0) & (ty < atlas_px)
    bary = np.stack([l1, l2, l3], 1)[inside]
    return tx[inside], ty[inside], face[inside], np.clip(bary, 0, 1)


def _sharpness(rgb: np.ndarray) -> float:
    """라플라시안 분산(≤320px 회색조) — 클수록 선명."""
    g = rgb[:: max(1, rgb.shape[0] // 240), :: max(1, rgb.shape[1] // 320)].astype(np.float32).mean(-1)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def _lowfreq_offset(avg: np.ndarray, best: np.ndarray, ok: np.ndarray, tx: np.ndarray, ty: np.ndarray, atlas_px: int, sigma: float = 5.0) -> np.ndarray:
    """저주파 보정량 = blur(평균 색) − blur(최선 뷰 색): 관측된 텍셀만으로 정규화한 가우시안."""
    m = np.zeros((atlas_px, atlas_px), np.float32)
    A = np.zeros((atlas_px, atlas_px, 3), np.float32)
    B = np.zeros_like(A)
    m[ty[ok], tx[ok]] = 1.0
    A[ty[ok], tx[ok]] = avg[ok]
    B[ty[ok], tx[ok]] = best[ok]
    g = lambda a: ndimage.gaussian_filter(a, sigma, mode="constant")  # noqa: E731
    gm = np.maximum(g(m), 1e-6)
    off = np.zeros((len(tx), 3), np.float32)
    for c in range(3):
        d = g(A[..., c]) / gm - g(B[..., c]) / gm
        off[:, c] = d[ty, tx]
    return np.where(ok[:, None], off, 0.0).astype(np.float32)


def _contrib(fr: Frame, P: np.ndarray, N: np.ndarray, min_cos: float):
    """프레임 하나가 텍셀에 주는 (텍셀 인덱스, 색, 가중치). 가림·마스크·정면성 검사를 통과한 것만."""
    T = fr.T_world_cv
    Ti = np.linalg.inv(T)
    pc = P @ Ti[:3, :3].T + Ti[:3, 3]
    cand = pc[:, 2] > 0.15
    if not cand.any():
        return None
    idx = np.nonzero(cand)[0]
    pcz = pc[idx]
    Kd = fr.K_depth
    dh, dw = fr.depth.shape
    u = pcz[:, 0] / pcz[:, 2] * Kd[0, 0] + Kd[0, 2]
    v = pcz[:, 1] / pcz[:, 2] * Kd[1, 1] + Kd[1, 2]
    inside = (u >= 1) & (u < dw - 1) & (v >= 1) & (v < dh - 1)
    idx, pcz, u, v = idx[inside], pcz[inside], u[inside], v[inside]
    if not len(idx):
        return None
    to_cam = T[:3, 3] - P[idx]
    dist = np.linalg.norm(to_cam, axis=1)
    cosv = (N[idx] * to_cam).sum(1) / np.maximum(dist, 1e-9)
    ok = cosv > min_cos
    ui, vi = u.astype(int), v.astype(int)
    offs = np.array([-1, 0, 1])
    yy = np.clip(vi[:, None, None] + offs[None, :, None], 0, dh - 1)
    xx = np.clip(ui[:, None, None] + offs[None, None, :], 0, dw - 1)
    dpatch = fr.depth[yy, xx].reshape(len(idx), -1)
    dpatch = np.where(dpatch > 0.05, dpatch, np.inf)
    dmin = dpatch.min(1)
    zt = pcz[:, 2]
    tol = 0.05 + 0.03 * zt
    ok &= np.isfinite(dmin) & (np.abs(dmin - zt) < tol) & (dmin > zt - tol)
    if fr.mask is not None and fr.mask.any():  # 사람·반사면 등은 색을 가져오지 않는다 (미관측으로 남김)
        mh, mw = fr.mask.shape
        mi = np.clip((u * mw / dw).astype(int), 0, mw - 1)
        mj = np.clip((v * mh / dh).astype(int), 0, mh - 1)
        ok &= ~fr.mask[mj, mi]
    if not ok.any():
        return None
    sel, uu, vv = idx[ok], u[ok], v[ok]
    H, W = fr.rgb.shape[:2]
    su = np.clip(uu * W / dw - 0.5, 0, W - 1)  # 연속 좌표(픽셀 중심=i+0.5) → 보간용 인덱스 좌표
    sv = np.clip(vv * H / dh - 0.5, 0, H - 1)
    x0 = np.clip(np.floor(su).astype(int), 0, W - 2)
    y0 = np.clip(np.floor(sv).astype(int), 0, H - 2)
    ax, ay = (su - x0)[:, None], (sv - y0)[:, None]
    rgb = fr.rgb.astype(np.float32)
    col = (rgb[y0, x0] * (1 - ax) * (1 - ay) + rgb[y0, x0 + 1] * ax * (1 - ay) + rgb[y0 + 1, x0] * (1 - ax) * ay + rgb[y0 + 1, x0 + 1] * ax * ay)
    edge = np.minimum.reduce([uu / dw, (dw - uu) / dw, vv / dh, (dh - vv) / dh])
    w = (cosv[ok] ** 2) / np.maximum(dist[ok], 0.3) ** 2 * (1.0 - 0.7 * float(np.clip(fr.blur, 0, 1))) * np.clip(edge * 6, 0.05, 1.0)
    return sel, col, w.astype(np.float32)


def build_texture(mesh: TriMesh, frames: list[Frame], atlas_px: int = 2048, texel_m: float | None = None,
                  axes: np.ndarray | None = None, max_frames: int | None = None, min_cos: float = 0.2, refine: bool = True) -> TexturedMesh:
    axes = np.eye(3) if axes is None else np.asarray(axes, float)
    if mesh.empty:
        raise ValueError("빈 메시")
    lay = build_atlas_layout(mesh, axes, atlas_px, texel_m)
    tx, ty, face, bary = _rasterize(lay, atlas_px)
    f = mesh.f
    V = mesh.v
    # 텍셀의 3D 위치·법선
    P = np.einsum("nk,nkc->nc", bary, V[f[face]])
    fn = mesh.face_normals()
    N = fn[face]

    n_tex = len(tx)
    use = frames if max_frames is None or len(frames) <= max_frames else [frames[i] for i in np.linspace(0, len(frames) - 1, max_frames).astype(int)]
    use = [fr for fr in use if fr.rgb is not None]
    lumas = np.array([float(fr.rgb[::8, ::8].astype(np.float32).mean()) for fr in use]) if use else np.array([])
    ref_luma = float(np.median(lumas)) if len(lumas) else 128.0
    sharp = np.array([_sharpness(fr.rgb) for fr in use]) if use else np.array([])
    sharp_ref = float(np.median(sharp)) if len(sharp) and np.median(sharp) > 0 else 1.0

    # 패스 A: 단순 가중 평균(휘도 정규화만) → 텍셀별 기준 색
    acc = np.zeros((n_tex, 3), np.float32)
    wsum = np.zeros(n_tex, np.float32)
    for fr, lu in zip(use, lumas):
        c = _contrib(fr, P, N, min_cos)
        if c is None or lu < 1:
            continue
        sel, col, w = c
        np.add.at(acc, sel, col * float(np.clip(ref_luma / lu, 0.8, 1.25)) * w[:, None])
        np.add.at(wsum, sel, w)
    if refine and len(use) >= 3:
        base = np.zeros_like(acc)
        okb = wsum > 1e-9
        base[okb] = acc[okb] / wsum[okb, None]
        # 패스 B: ① 프레임·채널별 이득(노출·화이트밸런스)을 기준 색에 맞춰 중앙값으로 추정 ② 선명도 가중
        # ③ 2대역 블렌딩: 저주파 = 모든 뷰의 평균, 고주파 = 가장 좋은 뷰(가중치^6) — 뷰 전환 이음매를 저주파 보정으로 메운다
        acc_avg = np.zeros_like(acc)
        w_avg = np.zeros(n_tex, np.float32)
        acc_best = np.zeros_like(acc)
        w_best = np.zeros(n_tex, np.float32)
        gains = []
        for fr, sh in zip(use, sharp):
            c = _contrib(fr, P, N, min_cos)
            if c is None:
                continue
            sel, col, w = c
            hi = (w >= np.quantile(w, 0.4)) & okb[sel]
            if hi.sum() >= 50:
                ratio = base[sel[hi]] / np.maximum(col[hi], 1.0)
                g3 = np.clip(np.median(ratio, axis=0), 0.6, 1.6).astype(np.float32)
            else:
                g3 = np.ones(3, np.float32)
            gains.append(g3)
            col = col * g3
            w = w * float(np.clip(sh / sharp_ref, 0.3, 2.0)) if sharp_ref > 0 else w
            np.add.at(acc_avg, sel, col * w[:, None])
            np.add.at(w_avg, sel, w)
            wb = w ** 6
            np.add.at(acc_best, sel, col * wb[:, None])
            np.add.at(w_best, sel, wb)
        ok2 = w_avg > 1e-12
        avg = np.zeros_like(acc)
        best = np.zeros_like(acc)
        avg[ok2] = acc_avg[ok2] / w_avg[ok2, None]
        best[ok2] = acc_best[ok2] / np.maximum(w_best[ok2, None], 1e-30)
        off = _lowfreq_offset(avg, best, ok2, tx, ty, atlas_px)
        final = np.clip(best + off, 0, 255)
        acc = final * np.where(ok2, 1.0, 0.0)[:, None].astype(np.float32)
        wsum = ok2.astype(np.float32)
        gain_spread = float(np.mean([np.abs(g - 1).max() for g in gains])) if gains else 0.0
    else:
        gain_spread = 0.0

    atlas = np.full((atlas_px, atlas_px, 3), 128, np.uint8)
    obs = np.zeros((atlas_px, atlas_px), bool)
    got = wsum > 1e-9
    colr = np.zeros_like(acc)
    colr[got] = acc[got] / wsum[got, None]
    atlas[ty[got], tx[got]] = np.clip(colr[got], 0, 255).astype(np.uint8)
    obs[ty[got], tx[got]] = True
    # 경계 확장: 관측 텍셀 가장자리를 3px 번지게 해서 seam/보간 번짐 완화 (관측 마스크는 그대로 둔다)
    covered = np.zeros_like(obs)
    covered[ty, tx] = True
    dil_src = obs
    if dil_src.any():
        dist, (iy, ix) = ndimage.distance_transform_edt(~dil_src, return_indices=True)
        fill = (dist <= 3) & ~obs
        atlas[fill] = atlas[iy[fill], ix[fill]]

    # 면 단위 관측 비율
    f_total = np.bincount(face, minlength=len(f)).astype(float)
    f_seen = np.bincount(face[got], minlength=len(f)).astype(float)
    frac = np.where(f_total > 0, f_seen / np.maximum(f_total, 1), 0)
    unobs_faces = int((frac < 0.3).sum())
    area = mesh.face_areas()
    tex_cov = float((area * frac).sum() / max(area.sum(), 1e-9))

    # 최종 정점: (면 코너) 단위로 UV가 달라서 코너마다 정점 복제
    ch = lay["chart_of_face"]
    pos = np.array(lay["pos"])
    tm = lay["texel_m"]
    pad = lay["pad"]
    cu = (lay["corner_u"] - lay["cmin_u"][ch][:, None]) / tm + pos[ch, 0][:, None] + pad
    cv = (lay["corner_v"] - lay["cmin_v"][ch][:, None]) / tm + pos[ch, 1][:, None] + pad
    uv_c = np.stack([cu / atlas_px, cv / atlas_px], -1)  # (M,3,2)
    vn = mesh.vertex_normals()
    # 정점 = (원 정점, 차트) 쌍 — 같은 차트 안에서는 공유해 GLB 크기를 줄인다
    nch_all = int(ch.max() + 1)
    key = (f.astype(np.int64) * nch_all + ch[:, None]).reshape(-1)
    uniq, first, inv = np.unique(key, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    newv = V[f.reshape(-1)[first]]
    newn = vn[f.reshape(-1)[first]]
    uv = uv_c.reshape(-1, 2)[first]
    newf = inv.reshape(-1, 3)
    stats = {"atlasPx": atlas_px, "texelCm": round(tm * 100, 2), "charts": int(ch.max() + 1), "faces": int(len(f)),
             "textureCoverage": round(tex_cov, 4), "unobservedFaces": unobs_faces, "framesUsed": len(use),
             "observedTexelFraction": round(float(got.mean()), 4), "refine": bool(refine), "gainSpread": round(gain_spread, 3)}
    return TexturedMesh(newv, newn, uv, newf, atlas, obs, tm, stats, {"unobservedFaces": unobs_faces, "textureCoverage": tex_cov})
