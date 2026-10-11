// 3D 디지털 트윈 — 데모(MOCK) 데이터. 백엔드 없이 UI를 개발·점검하기 위한 절차적 건물.
// 계약은 docs/INTERFACES.md §6 그대로. 좌표: Y-up, m, 건물 좌표. 각 공간 메시는 "공간 로컬" 프레임(공간 모서리가 원점)에 만든다.
import { THREE } from './viewer3d.js';

const FLOORS = [{ id: 1, level: '1F', elevationM: 0, heightM: 3.2 }, { id: 2, level: '2F', elevationM: 3.4, heightM: 3.2 }];
const ROOMS = [  // 건물 좌표 모서리(x0,z0) + 크기(w,d)
  { spaceId: 'R101', numericId: 3, name: '기계실', floorId: 1, kind: 'ROOM', x0: 0, z0: 0, w: 6, d: 4, cov: 0.86, q: 'REVIEW' },
  { spaceId: 'R102', numericId: 4, name: '전기실', floorId: 1, kind: 'ROOM', x0: 6, z0: 0, w: 6, d: 4, cov: 0.62, q: 'REVIEW' },
  { spaceId: 'COR1', numericId: 5, name: '복도', floorId: 1, kind: 'CORRIDOR', x0: 0, z0: 4, w: 12, d: 2, cov: 0.93, q: 'GOOD' },
  { spaceId: 'R201', numericId: 6, name: '회의실 (2층)', floorId: 2, kind: 'ROOM', x0: 3, z0: 0, w: 6, d: 4, cov: 0.4, q: 'POOR' },
];
const DOORS = [  // 벽의 문 틈 (건물 좌표)
  { id: 'D-R101', fromSpace: 'COR1', toSpace: 'R101', pos: [3, 0, 4], yaw: 0 },   // 복도 → 기계실 (북쪽 -Z 방향으로 진입; yaw 0 = -Z를 바라봄)
  { id: 'D-R102', fromSpace: 'COR1', toSpace: 'R102', pos: [9, 0, 4], yaw: 0 },
];
const rad = (d) => (d * Math.PI) / 180;

export function mockBuildings() {
  const floors = FLOORS.map((f) => ({
    ...f,
    spaces: ROOMS.filter((r) => r.floorId === f.id).map((r) => ({ spaceId: r.spaceId, numericId: r.numericId, name: r.name, status: r.cov > 0.5 ? 'READY' : 'NEED_MORE_CAPTURE', hasMesh: true, coverage: r.cov, quality: r.q, reviewStatus: r.cov > 0.8 ? 'APPROVED' : 'REVIEW_REQUIRED' })),
  }));
  return [{ id: 'mock', name: '본관 (데모)', projectCode: 'DEMO-001', floors, hasEnvelope: true, entranceCount: 1 }];
}

function wallRec(id, spaceId, kind, thick, method, conf, rev, a, b, y0, y1) {
  // a,b: 벽 중심선 양 끝(x,z). 두께만큼 부풀린 AABB를 bboxBuilding으로 제공(충돌용; 계약 외 선택 필드)
  const min = [Math.min(a[0], b[0]) - thick / 2, y0, Math.min(a[1], b[1]) - thick / 2];
  const max = [Math.max(a[0], b[0]) + thick / 2, y1, Math.max(a[1], b[1]) + thick / 2];
  return { id, spaceId, kind, thicknessM: thick, thicknessMethod: method, confidence: conf, reviewStatus: rev, bboxBuilding: { min, max }, _a: a, _b: b };
}

export function mockScene() {
  const walls = [
    wallRec(10, 'COR1', 'EXTERIOR', 0.2, 'CONFIG_DEFAULT', 0.4, 'REVIEW_REQUIRED', [-0.1, 0], [12.1, 0], 0, 3.2),   // 북
    wallRec(11, 'COR1', 'EXTERIOR', 0.2, 'MEASURED', 0.8, 'APPROVED', [0, 0], [0, 6], 0, 3.2),                      // 서
    wallRec(12, 'COR1', 'EXTERIOR', 0.2, 'INFERRED', 0.35, 'REVIEW_REQUIRED', [12, 0], [12, 6], 0, 3.2),            // 동
    wallRec(13, 'COR1', 'EXTERIOR', 0.2, 'BIM', 0.95, 'APPROVED', [0, 6], [5.4, 6], 0, 3.2),                        // 남(왼쪽)
    wallRec(14, 'COR1', 'EXTERIOR', 0.2, 'BIM', 0.95, 'APPROVED', [6.6, 6], [12, 6], 0, 3.2),                       // 남(오른쪽) — 정문 틈 5.4~6.6
    wallRec(15, 'R101', 'INTERIOR', 0.12, 'CONFIG_DEFAULT', 0.4, 'AUTO', [0, 4], [2.55, 4], 0, 3.2),                // 복도 북벽 3조각(문 틈 2)
    wallRec(16, 'COR1', 'INTERIOR', 0.12, 'CONFIG_DEFAULT', 0.4, 'AUTO', [3.45, 4], [8.55, 4], 0, 3.2),
    wallRec(17, 'R102', 'INTERIOR', 0.12, 'CONFIG_DEFAULT', 0.4, 'AUTO', [9.45, 4], [12, 4], 0, 3.2),
    wallRec(18, 'R101', 'INTERIOR', 0.12, 'INFERRED', 0.3, 'REVIEW_REQUIRED', [6, 0], [6, 4], 0, 3.2),              // 방 사이 칸막이
    wallRec(20, 'R201', 'EXTERIOR', 0.2, 'CONFIG_DEFAULT', 0.4, 'REVIEW_REQUIRED', [3, 0], [9, 0], 3.4, 6.6),
    wallRec(21, 'R201', 'EXTERIOR', 0.2, 'CONFIG_DEFAULT', 0.4, 'REVIEW_REQUIRED', [3, 4], [9, 4], 3.4, 6.6),
    wallRec(22, 'R201', 'EXTERIOR', 0.2, 'CONFIG_DEFAULT', 0.4, 'REVIEW_REQUIRED', [3, 0], [3, 4], 3.4, 6.6),
    wallRec(23, 'R201', 'EXTERIOR', 0.2, 'CONFIG_DEFAULT', 0.4, 'REVIEW_REQUIRED', [9, 0], [9, 4], 3.4, 6.6),
  ];
  const spaces = ROOMS.map((r) => {
    const el = FLOORS.find((f) => f.id === r.floorId).elevationM;
    const m = new THREE.Matrix4().makeTranslation(r.x0, el, r.z0);
    return {
      spaceId: r.spaceId, numericId: r.numericId, name: r.name, floorId: r.floorId, kind: r.kind, tBuildingSpace: m.toArray(),
      revisionId: `rev-${r.spaceId}-2`, glbUrl: `mock:observed:${r.spaceId}`, texturedGlbUrl: `mock:textured:${r.spaceId}`,
      lod: { web: `mock:textured:${r.spaceId}`, mobile: `mock:textured:${r.spaceId}` },
      bboxSpace: { min: [0, 0, 0], max: [r.w, 3.2, r.d] }, coverage: r.cov, quality: r.q,
      sourceKinds: r.spaceId === 'R102' ? ['OBSERVED', 'INFERRED'] : ['OBSERVED'],
      layers: { observed: `mock:observed:${r.spaceId}`, inferred: r.spaceId === 'R102' ? `mock:inferred:${r.spaceId}` : null, design: r.spaceId === 'R101' ? `mock:design:${r.spaceId}` : null,
        replacement: r.spaceId === 'R101' ? `mock:replacement:${r.spaceId}` : null, coverageHeat: `mock:heat:${r.spaceId}` },
      unobservedMask: r.spaceId === 'R102',
    };
  });
  const assets = [
    { assetId: 'PUMP-301', name: '순환 펌프 #301', spaceId: 'R101', positionBuilding: [2, 0.4, 1.5], bboxBuilding: { min: [1.5, 0, 1], max: [2.5, 0.8, 2] }, replaced: true, cadGlbUrl: 'mock:cad:PUMP-301', extractedMask: true },
    { assetId: 'PANEL-12', name: '배전반 #12', spaceId: 'R102', positionBuilding: [9, 1, 0.4], bboxBuilding: { min: [8.4, 0, 0.1], max: [9.6, 2, 0.7] }, replaced: false, cadGlbUrl: null, extractedMask: true },
    { assetId: 'VALVE-7', name: '제어 밸브 #7', spaceId: 'COR1', positionBuilding: [1, 0.6, 5], bboxBuilding: { min: [0.8, 0.3, 4.8], max: [1.2, 0.9, 5.2] }, replaced: false, cadGlbUrl: null, extractedMask: false },
  ];
  return {
    building: { id: 'mock', name: '본관 (데모)', projectCode: 'DEMO-001', gps: { lat: 37.5, lon: 127.0 }, notice: '외관/벽두께 일부 추정 (데모 데이터)' },
    floors: FLOORS,
    envelope: { glbUrl: 'mock:envelope', sourceKind: 'INFERRED', reviewStatus: 'REVIEW_REQUIRED', walls },
    entrances: [{ id: 'E1', name: '정문', positionBuilding: [6, 0, 6], yawDeg: 0, spaceId: 'COR1', exteriorViewpoint: [6, 1.7, 14] }],
    spaces,
    portals: [
      ...DOORS.map((d) => ({ id: d.id, fromSpace: d.fromSpace, toSpace: d.toSpace, positionBuilding: d.pos, yawDeg: d.yaw, widthM: 0.9, heightM: 2.1, state: 'OPEN', reviewStatus: 'APPROVED' })),
      { id: 'P-ENT', fromSpace: null, toSpace: 'COR1', positionBuilding: [6, 0, 6], yawDeg: 0, widthM: 1.2, heightM: 2.2, state: 'OPEN', reviewStatus: 'AUTO' },
      { id: 'P-STAIR', fromSpace: 'COR1', toSpace: 'R201', positionBuilding: [11, 0, 5], yawDeg: 270, widthM: 1.0, heightM: 2.1, state: 'UNKNOWN', reviewStatus: 'REVIEW_REQUIRED' },
    ],
    assets,
    viewpoints: [{ id: 'V1', name: '정면 전경', position: [6, 4, 17], target: [6, 1.5, 3], spaceId: null }, { id: 'V2', name: '복도', position: [1, 1.6, 5], target: [11, 1.4, 5], spaceId: 'COR1' }],
  };
}

export function mockRevisions(spaceId) {
  return [
    { revisionId: `rev-${spaceId}-2`, createdAt: new Date(Date.now() - 86400e3).toISOString(), sessionIds: ['s2'], sourceKinds: ['OBSERVED'], quality: 'REVIEW', coverage: 0.86, approved: false, files: {} },
    { revisionId: `rev-${spaceId}-1`, createdAt: new Date(Date.now() - 9 * 86400e3).toISOString(), sessionIds: ['s1'], sourceKinds: ['OBSERVED'], quality: 'POOR', coverage: 0.55, approved: true, files: {} },
  ];
}

// ───────── 절차적 텍스처 ─────────
function canvasTex(w, h, draw, repeat) {
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace; t.wrapS = t.wrapT = THREE.RepeatWrapping;
  if (repeat) t.repeat.set(...repeat);
  t.anisotropy = 4;
  return t;
}
function noise(g, w, h, n, a) { for (let i = 0; i < n; i++) { g.fillStyle = `rgba(${Math.random() < 0.5 ? '0,0,0' : '255,255,255'},${Math.random() * a})`; g.fillRect(Math.random() * w, Math.random() * h, 1 + Math.random() * 3, 1 + Math.random() * 3); } }
const texCache = {};
function texFloor() { return texCache.f ||= canvasTex(256, 256, (g, w, h) => { g.fillStyle = '#8c8f93'; g.fillRect(0, 0, w, h); g.strokeStyle = 'rgba(0,0,0,.35)'; g.lineWidth = 2; for (let i = 0; i <= 4; i++) { g.beginPath(); g.moveTo(i * w / 4, 0); g.lineTo(i * w / 4, h); g.moveTo(0, i * h / 4); g.lineTo(w, i * h / 4); g.stroke(); } noise(g, w, h, 1500, .12); }, [3, 3]); }
function texWall() { return texCache.w ||= canvasTex(256, 256, (g, w, h) => { const gr = g.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, '#d9d4c7'); gr.addColorStop(1, '#bdb6a6'); g.fillStyle = gr; g.fillRect(0, 0, w, h); noise(g, w, h, 2500, .08); g.fillStyle = 'rgba(60,70,80,.5)'; g.fillRect(0, h - 28, w, 28); }, [2, 1]); }
function texCeil() { return texCache.c ||= canvasTex(128, 128, (g, w, h) => { g.fillStyle = '#e4e6e8'; g.fillRect(0, 0, w, h); g.strokeStyle = 'rgba(0,0,0,.25)'; g.strokeRect(1, 1, w - 2, h - 2); noise(g, w, h, 400, .06); }, [4, 3]); }
function texScan() { return texCache.s ||= canvasTex(64, 64, (g, w, h) => { g.fillStyle = '#6d7a52'; g.fillRect(0, 0, w, h); noise(g, w, h, 300, .2); }); }
function texHeat(cov) {
  return canvasTex(64, 64, (g, w, h) => { const gr = g.createLinearGradient(0, 0, w, h); gr.addColorStop(0, '#e5484d'); gr.addColorStop(Math.min(1, cov * 0.6), '#f5a623'); gr.addColorStop(Math.min(1, 0.4 + cov * 0.6), '#2ecc71'); gr.addColorStop(1, '#1f9d55'); g.fillStyle = gr; g.fillRect(0, 0, w, h); });
}

// 벽 한 줄을 문 틈(gaps: [{c,w}] 길이 방향 중심·폭)을 비워 평면 조각들로 만든다. 평면은 방 안쪽을 향한다.
function wallPlanes(len, hgt, gaps, mat, place) {
  const g = new THREE.Group();
  const cuts = [[0, len]];
  const out = [];
  let cur = 0;
  for (const gp of [...gaps].sort((a, b) => a.c - b.c)) { const s = gp.c - gp.w / 2, e = gp.c + gp.w / 2; if (s > cur) out.push([cur, s]); cur = e; }
  if (cur < len) out.push([cur, len]);
  for (const [s, e] of out.length ? out : cuts) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(e - s, hgt), mat);
    place(m, (s + e) / 2, hgt / 2);
    g.add(m);
  }
  return g;
}

function info(mesh, o) { mesh.userData.info = o; return mesh; }

// 공간 레이어 GLB 대용 Group (space 로컬 프레임)
export function buildMockSpace(space, layer) {
  const r = ROOMS.find((x) => x.spaceId === space.spaceId);
  const g = new THREE.Group();
  const H = 3.2, { w, d } = r;
  const conf = r.cov;
  const unobsPatch = r.spaceId === 'R102';
  const skind = layer === 'inferred' ? 'INFERRED' : layer === 'design' ? 'DESIGN' : layer === 'replacement' ? 'REPLACEMENT' : 'OBSERVED';
  const base = { sourceKind: skind, confidence: conf, reviewStatus: r.cov > 0.8 ? 'APPROVED' : 'REVIEW_REQUIRED', label: `${r.name} ${layer}` };
  const mk = (geo, mat, extra) => { const m = new THREE.Mesh(geo, mat); m.userData.info = { ...base, ...extra }; return m; };
  const doors = DOORS.filter((dd) => dd.fromSpace === r.spaceId || dd.toSpace === r.spaceId);

  if (layer === 'textured' || layer === 'observed') {
    const flat = layer === 'observed';
    const M = (map, color) => new THREE.MeshStandardMaterial(flat ? { color, roughness: 1, flatShading: true, side: THREE.FrontSide } : { map, roughness: 0.9, side: THREE.FrontSide });
    // 바닥
    const floor = mk(new THREE.PlaneGeometry(w, d), M(texFloor(), 0x8d9aa8)); floor.rotation.x = -Math.PI / 2; floor.position.set(w / 2, 0, d / 2); g.add(floor);
    const ceil = mk(new THREE.PlaneGeometry(w, d), M(texCeil(), 0xb7c2cf)); ceil.rotation.x = Math.PI / 2; ceil.position.set(w / 2, H, d / 2); ceil.userData.part = 'ceiling'; g.add(ceil);
    // 벽 4면 (안쪽이 앞면). 문 틈은 건물 좌표를 공간 로컬로 바꿔 계산
    const gapsOn = (axis, fixed) => doors.map((dd) => ({ x: dd.pos[0] - r.x0, z: dd.pos[2] - r.z0 })).filter((p) => (axis === 'x' ? Math.abs(p.z - fixed) < 0.01 : Math.abs(p.x - fixed) < 0.01)).map((p) => ({ c: axis === 'x' ? p.x : p.z, w: 0.9 }));
    const entr = r.spaceId === 'COR1' ? [{ c: 6 - r.x0, w: 1.2 }] : [];
    const faces = [  // [길이, 위치, 회전Y, 틈]
      { len: w, pos: (s) => [s, 0, 0], ry: 0, gaps: gapsOn('x', 0) },                    // 북벽(z=0) 안쪽 +Z를 향함
      { len: w, pos: (s) => [s, 0, d], ry: Math.PI, gaps: [...gapsOn('x', d), ...entr].map((x) => ({ ...x, c: x.c })), flip: true },
      { len: d, pos: (s) => [0, 0, s], ry: Math.PI / 2, gaps: gapsOn('z', 0) },
      { len: d, pos: (s) => [w, 0, s], ry: -Math.PI / 2, gaps: gapsOn('z', w) },
    ];
    const wmat = M(texWall(), 0xa9b4c0);
    faces.forEach((f, i) => {
      // 남쪽(+Z) 벽은 -x 방향으로 진행하므로 틈 위치를 뒤집는다
      const gaps = f.flip ? f.gaps : f.gaps;
      const grp = wallPlanes(f.len, H, gaps, wmat, (m, c, y) => { const p = f.pos(c); m.position.set(p[0], y, p[2]); m.rotation.y = f.ry; });
      grp.traverse((m) => {
        if (!m.isMesh) return;
        const gray = unobsPatch && i === 0;  // R102 북쪽 벽: 미관측 → 회색 패치 (UV 고정 하단 회색 패치 규약의 모사)
        const mat = gray ? new THREE.MeshStandardMaterial({ color: 0x70757c, roughness: 1, side: THREE.FrontSide }) : wmat;
        m.material = mat;
        m.userData.info = { ...base, sourceKind: gray ? 'INFERRED' : 'OBSERVED', confidence: gray ? 0.2 : conf, unobserved: gray, label: gray ? `${r.name} 북벽 (미관측)` : `${r.name} 벽` };
        if (gray) m.userData.unobserved = true;
      });
      g.add(grp);
    });
    // 자산 위치에 "스캔된 자산 표면"이 공간 메시에 섞여 있다 → 이중표면 방지 마스크가 숨겨야 한다
    for (const a of mockScene().assets.filter((x) => x.spaceId === r.spaceId)) {
      const el = FLOORS.find((f) => f.id === r.floorId).elevationM;
      const c = a.bboxBuilding;
      const sz = [c.max[0] - c.min[0] - 0.1, c.max[1] - c.min[1] - 0.1, c.max[2] - c.min[2] - 0.1];
      const blob = mk(new THREE.BoxGeometry(...sz), flat ? M(null, 0xc77d3a) : M(texScan()), { label: `${a.assetId} 스캔 표면` });
      blob.position.set((c.min[0] + c.max[0]) / 2 - r.x0, (c.min[1] + c.max[1]) / 2 - el, (c.min[2] + c.max[2]) / 2 - r.z0);
      blob.userData.scanOfAsset = a.assetId;
      g.add(blob);
    }
    g.userData.extras = { assetRegions: [] };  // 데모는 매니페스트 자산 bbox로 마스크 (extras.assetRegions는 실제 GLB에서 읽는다)
  } else if (layer === 'inferred') {
    // R102 북벽(미관측)을 추정으로 채움
    const p = mk(new THREE.PlaneGeometry(w, H), new THREE.MeshBasicMaterial({ color: 0xffa94d, side: THREE.DoubleSide }), { label: `${r.name} 북벽 추정면` });
    p.position.set(w / 2, H / 2, 0.01); g.add(p);
  } else if (layer === 'design') {
    const duct = mk(new THREE.BoxGeometry(w - 0.6, 0.35, 0.4), new THREE.MeshBasicMaterial({ color: 0x4fd1ff, side: THREE.DoubleSide }), { label: '덕트 (BIM)' });
    duct.position.set(w / 2, H - 0.45, 0.5); g.add(duct);
    const pipe = mk(new THREE.CylinderGeometry(0.1, 0.1, d - 0.4, 12), new THREE.MeshBasicMaterial({ color: 0x4fd1ff }), { label: '배관 (BIM)' });
    pipe.rotation.x = Math.PI / 2; pipe.position.set(0.5, 2.6, d / 2); g.add(pipe);
  } else if (layer === 'replacement') {
    // 치환 CAD 모델 자리표시 (자산 PUMP-301 외부 형상)
    g.add(mk(new THREE.BoxGeometry(0.3, 0.3, 0.3), new THREE.MeshStandardMaterial({ color: 0x2ecc71 }), { label: '치환 모델' }));
    g.children[0].position.set(0.4, 0.2, 0.4);
  } else if (layer === 'coverageHeat') {
    const f = mk(new THREE.PlaneGeometry(w, d), new THREE.MeshBasicMaterial({ map: texHeat(r.cov), side: THREE.DoubleSide }), { sourceKind: 'OBSERVED', label: `스캔 커버리지 ${(r.cov * 100).toFixed(0)}%` });
    f.rotation.x = -Math.PI / 2; f.position.set(w / 2, 0.02, d / 2); g.add(f);
  }
  return g;
}

function wallBox(rec) {
  const a = rec._a, b = rec._b, t = rec.thicknessM, bb = rec.bboxBuilding;
  const m = new THREE.Mesh(new THREE.BoxGeometry(bb.max[0] - bb.min[0], bb.max[1] - bb.min[1], bb.max[2] - bb.min[2]), new THREE.MeshStandardMaterial({ color: 0xc9b79c, roughness: 0.95 }));
  m.position.set((bb.min[0] + bb.max[0]) / 2, (bb.min[1] + bb.max[1]) / 2, (bb.min[2] + bb.max[2]) / 2);
  m.userData.wallId = rec.id; m.name = `wall_${rec.id}`; void a; void b; void t;
  return m;
}
export function buildMockEnvelope(scene) {
  const g = new THREE.Group();
  for (const w of scene.envelope.walls) g.add(wallBox(w));
  // 바닥 슬래브(1층)
  const slab = new THREE.Mesh(new THREE.BoxGeometry(12.6, 0.2, 6.6), new THREE.MeshStandardMaterial({ color: 0x9aa3ad }));
  slab.position.set(6, -0.11, 3); slab.name = 'slab'; g.add(slab);
  return g;
}
export function buildMockCad(asset) {
  const g = new THREE.Group();
  const c = asset.bboxBuilding;
  const mat = new THREE.MeshStandardMaterial({ color: 0x35c98a, metalness: 0.4, roughness: 0.4 });
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.28, 0.6, 24), mat);
  body.position.set((c.min[0] + c.max[0]) / 2, 0.4, (c.min[2] + c.max[2]) / 2);
  const base = new THREE.Mesh(new THREE.BoxGeometry(0.95, 0.12, 0.95), mat); base.position.set(body.position.x, 0.06, body.position.z);
  g.add(body, base);
  return g;
}
export function buildMockModel(asset) {  // 비치환 자산의 스캔 모델 대용
  const c = asset.bboxBuilding;
  const m = new THREE.Mesh(new THREE.BoxGeometry(c.max[0] - c.min[0], c.max[1] - c.min[1], c.max[2] - c.min[2]), new THREE.MeshStandardMaterial({ color: 0x8fa6bf, flatShading: true }));
  m.position.set(...c.min.map((v, i) => (v + c.max[i]) / 2));
  const g = new THREE.Group(); g.add(m); return g;
}

export const MOCK_INSPECTIONS = {
  'PUMP-301': [{ id: 1, createdAt: new Date(Date.now() - 3 * 86400e3).toISOString(), decision: 'PASS' }, { id: 2, createdAt: new Date(Date.now() - 20 * 86400e3).toISOString(), decision: 'REVIEW' }],
  'PANEL-12': [{ id: 3, createdAt: new Date(Date.now() - 5 * 86400e3).toISOString(), decision: 'FAIL' }],
};
