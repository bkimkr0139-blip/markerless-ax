// 3D 디지털 트윈 엔진 — 건물 장면(외피·공간 레이어·자산·포털) + 외부 조망/실내 워크/자유 투어 + 도구(측정·핀·포털 배치).
// 좌표: Y-up 오른손, m, 장면 루트가 곧 "건물 좌표"(루트 변환 없음). 공간 GLB는 space 프레임 → 노드 행렬로 T_building_space 적용(§1, §8).
//
// 이중 표면 방지(§7): 자산이 extractedMask=true면 공간 메시(실사/관측 레이어)에서 자산 bbox(+2cm) 안쪽 조각을 그리지 않는다.
//  구현 = "조각 단위 geometry 분할" 대신 머티리얼 onBeforeCompile 로 프래그먼트 셰이더에서 월드 좌표가 어느 영역(최대 24개) 안이면 discard.
//  영역 = 매니페스트 assets[].bboxBuilding(+0.02) ∪ 공간 GLB 노드 extras.assetRegions(space 프레임 → T 적용 AABB). 유니폼은 모든 공간 머티리얼이 공유하므로
//  토글/자산 변경이 즉시 반영되고, 지오메트리를 복제·분할하지 않아 메모리/로드 비용이 없다. 자산 자신의 GLB·치환 GLB·추정/설계 레이어는 가리지 않는다.
//
// 가정(계약에 없는 부분): yawDeg = 카메라 rotation.y(도). 바라보는 방향 = (-sin yaw, 0, -cos yaw). 출입구/포털은 이 방향으로 "들어가는" 쪽을 가리킨다.
//  positionBuilding = 문 바닥 중심. envelope.walls[].bboxBuilding({min,max}) 가 있으면 충돌 AABB로 쓰고, 없으면 외피 GLB의 wall 노드(extras.wallId 또는 이름 wall_<id>) 바운딩박스를 쓴다.
import { THREE, labelSprite } from './viewer3d.js';
import { OrbitControls } from '../vendor/OrbitControls.js';
import { GLTFLoader } from '../vendor/GLTFLoader.js';

export const LAYERS = [
  { key: 'textured', label: '실사 텍스처', color: '#e8eef5', sk: 'OBSERVED' },
  { key: 'observed', label: '관측 메시', color: '#8fa6bf', sk: 'OBSERVED' },
  { key: 'inferred', label: '추정 (INFERRED)', color: '#ffa94d', sk: 'INFERRED' },
  { key: 'design', label: '설계 (DESIGN)', color: '#4fd1ff', sk: 'DESIGN' },
  { key: 'replacement', label: '치환 CAD (REPLACEMENT)', color: '#2ecc71', sk: 'REPLACEMENT' },
  { key: 'coverageHeat', label: '스캔 커버리지 히트맵', color: '#f5a623', sk: 'OBSERVED' },
  { key: 'envelope', label: '외피', color: '#c9b79c', sk: 'INFERRED' },
];
const MASKED = new Set(['textured', 'observed']);
// 화질 프리셋: pr=픽셀비 상한, tex=텍스처 최대 변(px), maxTier=LOD 상한(1=mobile,2=web,3=full textured)
export const QUALITIES = [['auto', '자동'], ['high', '높음'], ['normal', '보통'], ['low', '낮음']];
const QSET = { high: { pr: 2, tex: 4096, maxTier: 3, aa: true }, normal: { pr: 1.5, tex: 2048, maxTier: 2, aa: true }, low: { pr: 1, tex: 1024, maxTier: 1, aa: false } };
const STEP = 0.05, EYE = 1.6, FADE_MS = 250;   // 자동 오르내림 문턱(5 cm) · 눈높이 · LOD 교차 페이드
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, String(v)); } catch { /* 저장 불가 */ } };
const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
const angDiff = (a, b) => { let d = (b - a) % (Math.PI * 2); if (d > Math.PI) d -= Math.PI * 2; if (d < -Math.PI) d += Math.PI * 2; return d; };
const smoother = (t) => t * t * t * (t * (t * 6 - 15) + 10);
const MAXR = 24;
const rad = (d) => (d * Math.PI) / 180;
export const isMobileDevice = () => matchMedia('(pointer: coarse)').matches || innerWidth < 700;

export class TwinEngine {
  constructor(container, cb = {}) {
    this.container = container; this.cb = cb; this.ok = true;
    this.mobile = isMobileDevice();
    this.qualityPref = ['auto', 'high', 'normal', 'low'].includes(lsGet('twin.quality', 'auto')) ? lsGet('twin.quality', 'auto') : 'auto';
    const q0 = QSET[this._resolveQuality(this.qualityPref)];
    try { this.renderer = new THREE.WebGLRenderer({ antialias: q0.aa, alpha: false, powerPreference: 'high-performance' }); }   // MSAA 는 생성 시점에만 결정된다
    catch { this.ok = false; return; }
    const r = this.renderer;
    r.outputColorSpace = THREE.SRGBColorSpace; r.toneMapping = THREE.NoToneMapping; r.shadowMap.enabled = false;   // 베이크된 사진: 색관리 sRGB, 톤매핑·그림자 없음
    this.maxAniso = r.capabilities.getMaxAnisotropy(); this.maxTexSize = r.capabilities.maxTextureSize;
    this.prScale = 1; this.tierCap = 3; this.guardOn = true; this.guardSteps = 0; this._lowT = 0; this._graceUntil = performance.now() + 6000;
    this.texBytes = 0; this.texBudgetMB = this.mobile ? 200 : 400;
    this.lookSens = Number(lsGet('twin.sens', 1)) || 1; this.headBob = lsGet('twin.bob', '0') === '1';
    this.vel = new THREE.Vector2(); this.baseY = 0; this.stepY = 0; this._bobPh = 0; this._eyeY = null;
    this.cyaw = 0; this.cpitch = 0; this._tour = null; this.photo = false; this.review = false;
    this.unobsU = { uUnobs: { value: 0 } };
    this._q = []; this._qActive = 0; this._fades = []; this._lastInfo = 0;
    r.setClearColor(0x0a1018, 1);
    this.renderer.domElement.style.touchAction = 'none';
    container.append(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(Math.max(60, Math.min(90, Number(lsGet('twin.fov', 65)) || 65)), 1, 0.3, 800);   // 눈높이 1.6 m 투어에는 60~90° 시야
    this.scene.fog = null;
    this.camera.rotation.order = 'YXZ';
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.09; this.controls.maxPolarAngle = Math.PI * 0.495;
    this.scene.add(new THREE.HemisphereLight(0xdbe8ff, 0x2a303a, 1.5));
    const dir = new THREE.DirectionalLight(0xffffff, 1.2); dir.position.set(8, 14, 6); this.scene.add(dir);
    this.grid = new THREE.GridHelper(60, 60, 0x2f4259, 0x182331); this.grid.position.set(6, -0.25, 3); this.scene.add(this.grid);
    this.gEnv = new THREE.Group(); this.gSpaces = new THREE.Group(); this.gAssets = new THREE.Group(); this.gPortals = new THREE.Group();
    this.gEntr = new THREE.Group(); this.gTools = new THREE.Group(); this.gHold = new THREE.Group(); this.gSel = new THREE.Group();
    this.scene.add(this.gEnv, this.gSpaces, this.gAssets, this.gPortals, this.gEntr, this.gTools, this.gHold, this.gSel);
    this.layers = Object.fromEntries(LAYERS.map((l) => [l.key, { on: false, opacity: 1 }]));
    this.mats = Object.fromEntries(LAYERS.map((l) => [l.key, new Set()]));
    this.maskOn = true;
    this.maskU = { uMaskN: { value: 0 }, uMaskMin: { value: Array.from({ length: MAXR }, () => new THREE.Vector3()) }, uMaskMax: { value: Array.from({ length: MAXR }, () => new THREE.Vector3()) } };
    this.spaces = new Map(); this.assets = new Map(); this.portalObjs = new Map(); this.colliders = [];
    this.loadRange = 14; this.loading = 0; this.selected = { spaceId: null };
    this.mode = 'exterior'; this.fly = false;
    this.pos = new THREE.Vector3(); this.yaw = 0; this.pitch = 0; this.keys = new Set(); this.joy = { x: 0, y: 0 };
    this.floorId = null; this.tool = null; this.measure = []; this.pins = []; this._tween = null;
    this.ray = new THREE.Raycaster(); this.clip = new THREE.Plane(new THREE.Vector3(0, -1, 0), 100); this.clipOn = false;
    this.loader = new GLTFLoader();
    this._applyQuality(false);
    this._bind();
    this._ro = new ResizeObserver(() => this.resize()); this._ro.observe(container); this.resize();
    this._last = performance.now(); this._frames = 0; this._fpsT = this._last; this._lazyT = 0;
    this._loop = this._loop.bind(this); this._raf = requestAnimationFrame(this._loop);
  }


  // ───────── 화질 · 성능 보호 ─────────
  _resolveQuality(pref) { return pref === 'auto' ? (this.mobile ? 'normal' : 'high') : pref; }
  get quality() { return this._resolveQuality(this.qualityPref); }
  get qualityLabel() { return QUALITIES.find((q) => q[0] === this.qualityPref)[1] + (this.qualityPref === 'auto' ? `(${QUALITIES.find((q) => q[0] === this.quality)[1]})` : ''); }
  /** 사용자가 고른 화질 — 성능 보호로 낮춘 단계는 이때만 초기화된다(자동으로 올라가지는 않는다) */
  setQuality(pref) {
    if (!['auto', 'high', 'normal', 'low'].includes(pref)) return;
    this.qualityPref = pref; lsSet('twin.quality', pref);
    this.prScale = 1; this._guardTierCap = undefined; this.guardSteps = 0; this._lowT = 0; this._graceUntil = performance.now() + 5000;
    this._applyQuality(true);
  }
  _applyQuality(requal) {
    const q = QSET[this.quality], prevTex = this.texMax, prevCap = this.tierCap;
    this.texMax = Math.min(q.tex, this.maxTexSize);
    this.tierCap = Math.min(q.maxTier, this._guardTierCap ?? 3);
    this.renderer.setPixelRatio(Math.max(0.5, Math.min(devicePixelRatio || 1, this.mobile ? Math.min(q.pr, 1.5) : q.pr) * this.prScale));
    if (this.container) this.resize();
    if (requal && this.m && (prevTex !== this.texMax || this.tierCap < prevCap)) this._requalify();
  }
  /** 텍스처 상한/LOD 상한이 바뀌면 적재된 공간의 실사 레이어를 현재 정책으로 다시 불러온다(페이드 교체) */
  _requalify() {
    for (const s of this.spaces.values()) {
      if (!s.loaded || !s.tiers?.length || !s.texTier) continue;
      const av = s.tiers, cur = av.find((t) => t.t === s.texTier) || av[0];
      let pick = av[0]; for (const t of av) if (t.t <= Math.min(s.texTier, this.tierCap)) pick = t;
      if (pick.t !== cur.t || s.texMax !== this.texMax) this._queueTier(s, pick, true);
    }
  }
  /** 평균 FPS < 20 이 3초 지속되면 한 단계씩 낮춘다: 해상도 → LOD. 자동으로 올리지 않는다. */
  _guard(fps) {
    const now = performance.now();
    if (!this.guardOn || document.hidden || now < this._graceUntil || this.loading > 0 || this._qActive > 0) { this._lowT = 0; return; }
    if (fps >= 20) { this._lowT = 0; return; }
    this._lowT += 500;
    if (this._lowT < 3000) return;
    this._lowT = 0; this._graceUntil = now + 4000;
    let msg = null;
    if (this.prScale > 0.55) { this.prScale = this.prScale > 0.8 ? 0.75 : 0.5; msg = `성능 보호: 해상도를 낮췄습니다 (×${this.prScale})`; }
    else if (this.tierCap > 1) { this._guardTierCap = this.tierCap - 1; msg = `성능 보호: 텍스처 LOD를 낮췄습니다 (${this._guardTierCap === 2 ? 'web' : 'mobile'})`; }
    if (!msg) return;
    this.guardSteps++; this._applyQuality(true);
    this.cb.onStatus?.(msg, 'warn'); this.cb.onGuard?.(msg);
  }

  // ───────── 카메라 설정 ─────────
  setFov(v) { v = Math.max(60, Math.min(90, Number(v) || 65)); this.camera.fov = v; this.camera.updateProjectionMatrix(); lsSet('twin.fov', v); }
  setLookSens(v) { this.lookSens = Math.max(0.3, Math.min(3, Number(v) || 1)); lsSet('twin.sens', this.lookSens); }
  setHeadBob(on) { this.headBob = !!on; lsSet('twin.bob', on ? '1' : '0'); }
  /** 미관측(회색 128) 텍셀 강조. 투어 기본 OFF, 검수 기본 ON */
  setUnobserved(on) { this.unobsU.uUnobs.value = on ? 1 : 0; }
  get unobservedOn() { return this.unobsU.uUnobs.value > 0; }
  setPurpose(p) { this.review = p === 'review'; this.setUnobserved(this.review); }
  setPhoto(on) { this.photo = !!on; this.grid.visible = !on && (this.mode === 'exterior' || this.mode === 'entrance'); this.gHold.visible = !on; this.gSel.visible = !on; }

  // ───────── 장면 구성 ─────────
  async setManifest(m, mock) {
    this.m = m; this.mock = mock;
    this.floors = new Map(m.floors.map((f) => [f.id, f]));
    this.gEnv.clear(); this.gAssets.clear(); this.gPortals.clear(); this.gEntr.clear(); this.gHold.clear();
    for (const s of [...this.spaces.values()]) this._unloadSpace(s);
    this.spaces.clear(); this.assets.clear();
    for (const sp of m.spaces) {
      const T = new THREE.Matrix4().fromArray(sp.tBuildingSpace);
      const bb = sp.bboxSpace ? new THREE.Box3(new THREE.Vector3(...sp.bboxSpace.min), new THREE.Vector3(...sp.bboxSpace.max)).applyMatrix4(T) : new THREE.Box3();
      const group = new THREE.Group(); group.matrixAutoUpdate = false; group.matrix.copy(T); group.visible = true;
      this.gSpaces.add(group);
      const hold = new THREE.Box3Helper(bb, 0x4f6a88); hold.visible = false; this.gHold.add(hold);   // 미적재 공간 자리표시 상자
      this.spaces.set(sp.spaceId, { def: sp, T, bbox: bb, group, layerObjs: {}, loaded: false, loading: new Set(), hold, revision: null, epoch: 0, texTier: 0, texPending: 0 });
    }
    this._buildEntrances(); this._buildAssets(); this._buildPortals();
    await this._buildEnvelope();
    this._rebuildMask(); this._rebuildColliders();
    this.refreshHolders();
    this.applyLayerState();
  }

  _glb(url) {
    if (url && url.startsWith('mock:')) return Promise.resolve(null);
    return this.loader.loadAsync(url).then((g) => g.scene);
  }

  async _buildEnvelope() {
    const env = this.m.envelope; this.envObj = null;
    if (!env || !env.glbUrl) return;
    try {
      const obj = this.mock ? this.cb.mock.buildMockEnvelope(this.m) : await this._glb(env.glbUrl);
      this.envObj = obj; this._styleTree(obj, 'envelope', (mesh) => {
        let n = mesh, wid = null;
        while (n && wid == null) { wid = n.userData?.wallId ?? (/wall[_\- ]?(\d+)/i.exec(n.name || '')?.[1]); n = n.parent; }
        const w = wid != null ? env.walls?.find((x) => String(x.id) === String(wid)) : null;
        mesh.userData.wallId = w ? w.id : undefined;
        mesh.userData.info = { kind: w ? 'wall' : 'envelope', wallId: w?.id, sourceKind: env.sourceKind || 'INFERRED', confidence: w?.confidence ?? 0.4, reviewStatus: w?.reviewStatus || env.reviewStatus, label: w ? `벽 #${w.id} (${w.kind === 'EXTERIOR' ? '외벽' : '내벽'}, ${w.thicknessM}m)` : '외피' };
      });
      this.gEnv.add(obj);
    } catch (e) { this.cb.onStatus?.(`외피 GLB 로드 실패: ${e.message}`, 'warn'); }
  }

  _buildEntrances() {
    for (const e of this.m.entrances || []) {
      const g = new THREE.Group(); g.position.set(...e.positionBuilding);
      const cone = new THREE.Mesh(new THREE.ConeGeometry(0.28, 0.7, 4), new THREE.MeshBasicMaterial({ color: 0x38a3ff, transparent: true, opacity: 0.9 }));
      cone.rotation.x = Math.PI; cone.position.y = 2.7; cone.userData.info = { kind: 'entrance', id: e.id, label: `출입구 ${e.name}`, sourceKind: 'OBSERVED' };
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.4, 0.55, 32), new THREE.MeshBasicMaterial({ color: 0x38a3ff, side: THREE.DoubleSide }));
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.03; ring.userData.info = cone.userData.info;
      const lb = labelSprite(e.name, '#9bd0ff', 0.3); lb.position.y = 3.3;
      g.add(cone, ring, lb); g.userData.entrance = e; this.gEntr.add(g);
    }
  }

  _buildAssets() {
    for (const a of this.m.assets || []) {
      const bb = new THREE.Box3(new THREE.Vector3(...a.bboxBuilding.min), new THREE.Vector3(...a.bboxBuilding.max));
      const g = new THREE.Group();
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(...bb.getSize(new THREE.Vector3()).toArray())), new THREE.LineBasicMaterial({ color: 0xc77dff }));
      edges.position.copy(bb.getCenter(new THREE.Vector3()));
      const info = { kind: 'asset', assetId: a.assetId, label: `${a.name} (${a.assetId})`, sourceKind: a.replaced ? 'REPLACEMENT' : 'OBSERVED', reviewStatus: a.replaced ? 'APPROVED' : 'AUTO' };
      const hit = new THREE.Mesh(new THREE.BoxGeometry(...bb.getSize(new THREE.Vector3()).toArray()), new THREE.MeshBasicMaterial({ color: 0xc77dff, transparent: true, opacity: 0.12, depthWrite: false }));
      hit.position.copy(edges.position); hit.userData.info = info;
      const lb = labelSprite(a.assetId, '#d9b3ff', 0.22); lb.position.set(edges.position.x, bb.max.y + 0.3, edges.position.z);
      g.add(hit, edges, lb);
      const rec = { def: a, bb, group: g, hit, edges, model: null, cad: null, showCad: !!a.replaced };
      this.assets.set(a.assetId, rec); this.gAssets.add(g);
      this._assetModel(rec);
    }
  }
  async _assetModel(rec) {
    const a = rec.def;
    try {
      if (a.replaced || a.cadGlbUrl) {
        const obj = this.mock ? this.cb.mock.buildMockCad(a) : await this._glb(a.cadGlbUrl);
        if (obj) { this._styleTree(obj, 'replacement', (m) => { m.userData.info = { kind: 'asset', assetId: a.assetId, sourceKind: 'REPLACEMENT', confidence: 1, reviewStatus: 'APPROVED', label: `${a.name} CAD 치환` }; }); rec.cad = obj; rec.group.add(obj); }
      }
    } catch (e) { /* CAD 파일 없음: bbox만 표시 */ }
    this.applyAssetState(rec);
  }
  /** 비치환 자산의 스캔 모델(model.glb)을 선택될 때 지연 로드 */
  async ensureAssetModel(assetId) {
    const rec = this.assets.get(assetId); if (!rec || rec.model !== null && rec.model !== undefined) return;
    rec.model = false;
    try {
      const obj = this.mock ? this.cb.mock.buildMockModel(rec.def) : await this.loader.loadAsync(`/admin/api/assets/${encodeURIComponent(assetId)}/model.glb`).then((g) => g.scene);
      obj.traverse((o) => { if (o.isMesh) { o.material = new THREE.MeshStandardMaterial({ color: 0x8fa6bf, flatShading: true, roughness: 0.8 }); o.userData.info = { kind: 'asset', assetId, sourceKind: 'OBSERVED', confidence: 0.8, label: `${rec.def.name} 스캔 모델` }; } });
      rec.model = obj; rec.group.add(obj); this.applyAssetState(rec);
    } catch { /* 모델 없음 */ }
  }
  setAssetCad(assetId, on) { const r = this.assets.get(assetId); if (r) { r.showCad = on; this.applyAssetState(r); } }
  applyAssetState(rec) {
    const cadOn = rec.showCad && rec.cad && this.layers.replacement.on;
    if (rec.cad) rec.cad.visible = !!cadOn;
    if (rec.model) rec.model.visible = !cadOn;
    rec.hit.visible = true;
  }

  _buildPortals() {
    for (const o of this.portalObjs.values()) o.removeFromParent();
    this.portalObjs.clear();
    for (const p of this.m.portals || []) this._addPortalObj(p);
  }
  _addPortalObj(p) {
    const col = p.reviewStatus === 'APPROVED' ? 0x2ecc71 : p.reviewStatus === 'REJECTED' ? 0xff5a5f : 0xf5a623;
    const g = new THREE.Group(); g.position.set(...p.positionBuilding); g.rotation.y = rad(p.yawDeg || 0);
    const info = { kind: 'portal', portalId: p.id, label: `포털 ${p.fromSpace || '외부'} → ${p.toSpace || '?'}`, sourceKind: 'OBSERVED', reviewStatus: p.reviewStatus };
    const pl = new THREE.Mesh(new THREE.PlaneGeometry(p.widthM, p.heightM), new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.22, side: THREE.DoubleSide, depthWrite: false }));
    pl.position.y = p.heightM / 2; pl.userData.info = info;
    const fr = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(p.widthM, p.heightM)), new THREE.LineBasicMaterial({ color: col }));
    fr.position.y = p.heightM / 2;
    const ar = new THREE.Mesh(new THREE.ConeGeometry(0.1, 0.3, 8), new THREE.MeshBasicMaterial({ color: col }));
    ar.rotation.x = -Math.PI / 2; ar.position.set(0, 0.15, -0.3); ar.userData.info = info;   // 진행 방향(-Z 로컬)
    g.add(pl, fr, ar); g.userData.portal = p;
    this.gPortals.add(g); this.portalObjs.set(p.id, g);
  }
  refreshPortals() { this._buildPortals(); this._rebuildColliders(); }

  // ───────── 머티리얼/레이어 ─────────
  _maskify(mat, unobs = false) {
    const u = this.maskU, uo = this.unobsU;
    mat.onBeforeCompile = (sh) => {
      Object.assign(sh.uniforms, u);
      if (unobs) {
        // 미관측 텍셀 = 아틀라스에서 회색(128) (INTERFACES §8). 샘플 색이 (128±2)면 주황 빗금으로 섞는다. 샘플값은 선형이므로 sRGB 로 되돌려 비교한다.
        Object.assign(sh.uniforms, uo);
        sh.fragmentShader = sh.fragmentShader.replace('void main() {', 'uniform float uUnobs;\nvoid main() {')
          .replace('#include <map_fragment>', `#include <map_fragment>
#ifdef USE_MAP
  if (uUnobs > 0.5) {
    vec3 tw8 = sRGBTransferOETF(vec4(sampledDiffuseColor.rgb, 1.0)).rgb * 255.0;
    float twD = max(abs(tw8.r - 128.0), max(abs(tw8.g - 128.0), abs(tw8.b - 128.0)));
    float twW = 1.0 - smoothstep(2.0, 5.5, twD);
    float twH = step(0.5, fract((gl_FragCoord.x + gl_FragCoord.y) / 12.0));
    vec3 twA = mix(vec3(1.0, 0.52, 0.06), vec3(0.62, 0.22, 0.0), twH);
    diffuseColor.rgb = mix(diffuseColor.rgb, twA, twW * 0.72);
  }
#endif`);
      }
      sh.vertexShader = sh.vertexShader.replace('void main() {', 'varying vec3 vTwW;\nvoid main() {').replace('#include <begin_vertex>', '#include <begin_vertex>\nvTwW = (modelMatrix * vec4(transformed, 1.0)).xyz;');
      sh.fragmentShader = sh.fragmentShader.replace('void main() {', `varying vec3 vTwW;\nuniform int uMaskN;\nuniform vec3 uMaskMin[${MAXR}];\nuniform vec3 uMaskMax[${MAXR}];\nvoid main() {`)
        .replace('#include <clipping_planes_fragment>', `#include <clipping_planes_fragment>\nfor (int i = 0; i < ${MAXR}; i++) { if (i >= uMaskN) break; if (all(greaterThan(vTwW, uMaskMin[i])) && all(lessThan(vTwW, uMaskMax[i]))) discard; }`);
    };
    mat.customProgramCacheKey = () => (unobs ? 'twinmask-u' : 'twinmask');
  }
  _rebuildMask() {
    const regs = [];
    for (const a of this.m?.assets || []) if (a.extractedMask) regs.push([a.bboxBuilding.min, a.bboxBuilding.max]);
    for (const s of this.spaces.values()) for (const r of s.regions || []) regs.push(r);
    const n = Math.min(regs.length, MAXR);
    for (let i = 0; i < n; i++) { this.maskU.uMaskMin.value[i].set(...regs[i][0]).subScalar(0.02); this.maskU.uMaskMax.value[i].set(...regs[i][1]).addScalar(0.02); }
    this.maskU.uMaskN.value = this.maskOn ? n : 0; this.maskCount = n;
  }
  setMask(on) { this.maskOn = on; this._rebuildMask(); }

  /** 트리의 모든 Mesh에 레이어 머티리얼을 입히고(원본 머티리얼은 폐기) 정보 태그를 단다 */
  _styleTree(root, layer, tag) {
    root.traverse((o) => {
      if (!o.isMesh) return;
      const orig = o.material;
      let m;
      switch (layer) {
        case 'textured': m = this._bakedMaterial(o, orig); break;
        case 'observed': m = new THREE.MeshStandardMaterial({ color: 0x8fa6bf, roughness: 1, flatShading: true, vertexColors: !!o.geometry.getAttribute('color') }); break;
        case 'inferred': m = new THREE.MeshStandardMaterial({ color: 0xffa94d, emissive: 0x663300, roughness: 1, transparent: true, depthWrite: false }); break;
        case 'design': m = new THREE.MeshBasicMaterial({ color: 0x4fd1ff, transparent: true, wireframe: true }); break;
        case 'replacement': m = new THREE.MeshStandardMaterial({ color: 0x35c98a, metalness: 0.4, roughness: 0.4 }); break;
        case 'coverageHeat': m = new THREE.MeshBasicMaterial({ map: orig?.map || null, color: orig?.map ? 0xffffff : 0xf5a623, vertexColors: !!o.geometry.getAttribute('color'), transparent: true }); break;
        default: m = new THREE.MeshStandardMaterial({ color: 0xc9b79c, roughness: 0.95, transparent: true });
      }
      m.side = THREE.DoubleSide; m.clipShadows = true;
      if (layer === 'textured' || layer === 'observed') m.side = orig?.side === THREE.FrontSide && this.mock ? THREE.FrontSide : THREE.DoubleSide;
      if (MASKED.has(layer)) this._maskify(m, !!m.userData.baked);
      // 동일 평면 겹침(z-fighting) 방지: 방 메시는 살짝 뒤로, 오버레이(추정·히트맵·설계)는 앞으로, 외피 벽은 방 벽보다 뒤로
      const po = { textured: 1, observed: 1, inferred: -2, coverageHeat: -2, design: -2, envelope: 2 }[layer];
      if (po) { m.polygonOffset = true; m.polygonOffsetFactor = po; m.polygonOffsetUnits = po; }
      if (layer === 'envelope' && o.userData.wallId == null) { /* 비벽 외피 */ }
      o.material = m; this.mats[layer].add(m); if (orig && orig !== m) orig.dispose?.();
      if (layer === 'inferred' || layer === 'design' || layer === 'coverageHeat' || layer === 'envelope') o.renderOrder = 2;
      if (tag) tag(o);
    });
    this._applyOpacity(layer);
  }
  _applyOpacity(layer) {
    const L = this.layers[layer];
    for (const m of this.mats[layer]) {
      const f = m.userData.fade ?? 1;   // LOD 교차 페이드 계수
      const t = L.opacity < 0.999 || f < 1 || layer === 'inferred' || layer === 'design' || layer === 'coverageHeat';
      if (m.transparent !== t) { m.transparent = t; m.needsUpdate = true; }
      m.opacity = L.opacity * f; m.depthWrite = L.opacity > 0.97 && f >= 1 && layer !== 'inferred';
    }
  }

  // ───────── 베이크 사진 텍스처 ─────────
  /** 텍스처 설정: sRGB, flipY=false(glTF), 밉맵+삼선형, 최대 이방성, 화질 상한으로 다운스케일 */
  _prepTexture(tex) {
    if (!tex || tex.userData.twinPrep === this.texMax) return tex;
    const img = tex.image; let w = img?.width || 0, h = img?.height || 0;
    if (tex.userData.twinBytes) this.texBytes -= tex.userData.twinBytes;
    if (w && h && Math.max(w, h) > this.texMax) {
      const k = this.texMax / Math.max(w, h); w = Math.max(1, Math.round(w * k)); h = Math.max(1, Math.round(h * k));
      try {
        const c = document.createElement('canvas'); c.width = w; c.height = h;
        const g = c.getContext('2d'); g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high'; g.drawImage(img, 0, 0, w, h);
        img.close?.(); tex.image = c;
      } catch { w = img.width; h = img.height; }
    }
    tex.colorSpace = THREE.SRGBColorSpace;   // flipY 는 GLTFLoader 가 false 로 둔다(목업 CanvasTexture 는 그대로)
   
    tex.generateMipmaps = true; tex.minFilter = THREE.LinearMipmapLinearFilter; tex.magFilter = THREE.LinearFilter;
    tex.anisotropy = this.maxAniso; tex.needsUpdate = true;
    tex.userData.twinPrep = this.texMax; tex.userData.twinBytes = Math.round(w * h * 4 * 1.33); this.texBytes += tex.userData.twinBytes;
    return tex;
  }
  /** 사진 텍스처가 있으면 조명 없는(Unlit) MeshBasicMaterial — 조명이 이미 구워져 있다. 없으면(정점 색·관측 메시) 조명 재질 유지 */
  _bakedMaterial(o, orig) {
    if (orig?.map) {
      const m = new THREE.MeshBasicMaterial({ map: this._prepTexture(orig.map), color: 0xffffff, toneMapped: false });
      m.userData.baked = true; return m;
    }
    return new THREE.MeshStandardMaterial({ color: orig?.color ?? 0xcccccc, vertexColors: !!o.geometry.getAttribute('color'), roughness: 0.9 });
  }
  setLayer(key, on, opacity) {
    const L = this.layers[key]; if (on != null) L.on = on; if (opacity != null) L.opacity = opacity;
    this._applyOpacity(key); this.applyLayerState();
    if (L.on) this._ensureLayersAll();
    this.cb.onLayersChanged?.();
  }
  applyLayerState() {
    if (this.envObj) this.envObj.visible = this.layers.envelope.on;
    for (const s of this.spaces.values()) for (const [k, o] of Object.entries(s.layerObjs)) if (o) o.visible = this.layers[k].on;
    for (const r of this.assets.values()) this.applyAssetState(r);
  }
  get inferredVisible() {
    if (this.layers.inferred.on && [...this.spaces.values()].some((s) => s.layerObjs.inferred)) return true;
    return this.layers.envelope.on && this.m?.envelope?.sourceKind === 'INFERRED';
  }

  // ───────── 공간 지연 로드 ─────────
  layerUrl(space, layer) {
    const d = space.def, rev = space.revision?.files || {}, L = d.layers || {};
    const pick = (k) => { const v = rev[k]; return typeof v === 'string' ? v : v ? `/admin/api/twin/files/${k}/${space.revision.revisionId}.glb` : undefined; };
    switch (layer) {
      case 'textured': return pick('textured') || (this.mobile ? (d.lod?.mobile || d.texturedGlbUrl) : (d.texturedGlbUrl || d.lod?.web)) || null;
      case 'observed': return pick('observed') || L.observed || d.glbUrl || null;
      case 'inferred': return pick('inferred') || L.inferred || null;
      case 'design': return pick('design') || L.design || null;
      case 'replacement': return pick('replacement') || L.replacement || null;
      case 'coverageHeat': return pick('coverageHeat') || L.coverageHeat || null;
    }
    return null;
  }
  async _ensureLayer(space, layer) {
    if (!space.loaded || space.layerObjs[layer] !== undefined || space.loading.has(layer)) return;
    const url = this.layerUrl(space, layer);
    if (!url) { space.layerObjs[layer] = null; return; }
    space.loading.add(layer); this.loading++; this.cb.onLoading?.(this.loading);
    const rev = space.revision?.revisionId;
    try {
      const obj = this.mock ? this.cb.mock.buildMockSpace(space.def, layer) : await this._glb(url);
      if (!space.loaded || (space.revision?.revisionId) !== rev) { this._disposeTree(obj); return; }   // 로드 중 해제/리비전 변경
      const sk = LAYERS.find((l) => l.key === layer).sk;
      this._styleTree(obj, layer, (mesh) => {
        const ex = { ...(mesh.userData || {}), ...(mesh.parent?.userData || {}) };
        mesh.userData.info = { ...(mesh.userData.info || {}), sourceKind: mesh.userData.info?.sourceKind || ex.sourceKind || sk, confidence: mesh.userData.info?.confidence ?? ex.confidence ?? space.def.coverage, reviewStatus: mesh.userData.info?.reviewStatus || ex.reviewStatus || space.def.quality, label: mesh.userData.info?.label || `${space.def.name} · ${layer}`, unobserved: mesh.userData.info?.unobserved || (ex.unobservedFaces > 0), layer, spaceId: space.def.spaceId };
      });
      const regs = [];
      obj.traverse((o) => { for (const r of o.userData?.extras?.assetRegions || o.userData?.assetRegions || []) regs.push(r); });
      if (regs.length) {
        (space.regionsBy ||= {})[layer] = regs.map((r) => { const b = new THREE.Box3(new THREE.Vector3(...r.min), new THREE.Vector3(...r.max)).applyMatrix4(space.T); return [b.min.toArray(), b.max.toArray()]; });
        space.regions = Object.values(space.regionsBy).flat(1); this._rebuildMask();
      }
      obj.visible = this.layers[layer].on; space.layerObjs[layer] = obj; space.group.add(obj);
      if (layer === 'inferred') this.cb.onLayersChanged?.();
    } catch (e) { space.layerObjs[layer] = null; this.cb.onStatus?.(`${space.def.name} ${layer} 로드 실패: ${e.message}`, 'warn'); }
    finally { space.loading.delete(layer); this.loading--; this.cb.onLoading?.(this.loading); }
  }
  _ensureLayersAll() { for (const s of this.spaces.values()) this._ensureSpace(s); }
  _ensureSpace(s) {
    if (!s.loaded) return;
    for (const l of LAYERS) if (l.key !== 'envelope' && this.layers[l.key].on) { if (l.key === 'textured') this._ensureTextured(s); else this._ensureLayer(s, l.key); }
  }

  // ───────── 실사 레이어 점진 LOD (mobile → web → full) ─────────
  /** 사용 가능한 LOD 단계 [{t:1|2|3, url}] (URL 중복 제거, 낮은 단계 우선) */
  _texTiers(space) {
    const d = space.def, rev = space.revision?.files || {};
    const pk = (k) => { const v = rev[k]; return typeof v === 'string' ? v : v ? `/admin/api/twin/files/${k}/${space.revision.revisionId}.glb` : undefined; };
    const useDef = !space.revision || !Object.keys(rev).length;
    const raw = [[1, pk('lodmobile') || (useDef ? d.lod?.mobile : null)], [2, pk('lodweb') || (useDef ? d.lod?.web : null)], [3, pk('textured') || (useDef ? d.texturedGlbUrl : null)]];
    const out = [];
    for (const [t, url] of raw) if (url && !out.some((x) => x.url === url)) out.push({ t, url });
    return out;
  }
  _dist(space) { return space.bbox.distanceToPoint(this.camera.position); }
  /** 거리(≤3 m 전체, ≤6 m web)·선택 여부로 원하는 단계. 메모리 예산 초과 시 멀리 있는 공간은 올리지 않는다 */
  _wantTier(space) {
    const av = space.tiers; if (!av?.length) return null;
    const d = this._dist(space), sel = space.def.spaceId === this.selected.spaceId;
    let want = sel || d <= 3 ? 3 : d <= 6 ? 2 : 1;
    if (this.texBytes > this.texBudgetMB * 1048576 * 0.9 && !(sel || d <= 3)) want = Math.min(want, space.texTier || 1);
    want = Math.min(want, this.tierCap);
    let pick = av[0]; for (const t of av) if (t.t <= want) pick = t;
    return pick;
  }
  _ensureTextured(space) {
    if (!space.loaded || space.layerObjs.textured !== undefined || space.texPending) return;
    space.tiers = this._texTiers(space);
    if (this.mock) { space.tiers = [{ t: 3, url: 'mock:' }]; }
    if (!space.tiers.length) { space.layerObjs.textured = null; return; }
    this._queueTier(space, space.tiers[0]);   // 항상 가장 가벼운 것부터 — 이후 _lazyUpdate 가 거리에 따라 올린다
  }
  _queueTier(space, tier, urgent) {
    if (space.texPending) { const qi = this._q.findIndex((j) => j.space === space); if (qi < 0 || !urgent) return; this._q.splice(qi, 1); }
    space.texPending = tier.t; this._q.push({ space, tier, epoch: space.epoch, rev: space.revision?.revisionId, loaded: 0, total: 0 });
    this._pump(); this._info(true);
  }
  _pump() {
    while (this._qActive < 2 && this._q.length) {
      this._q.sort((a, b) => (b.space.def.spaceId === this.selected.spaceId) - (a.space.def.spaceId === this.selected.spaceId) || this._dist(a.space) - this._dist(b.space));
      const job = this._q.shift(); this._qActive++; this.loading++; this.cb.onLoading?.(this.loading); job.running = true; this._active = (this._active || new Set()).add(job);
      this._runJob(job).catch(() => {}).finally(() => { this._qActive--; this.loading--; this._active.delete(job); if (!job.requeued) job.space.texPending = 0; this.cb.onLoading?.(this.loading); this._info(true); this._pump(); });
    }
  }
  _stale(job) { const s = job.space; return !s.loaded || s.epoch !== job.epoch || s.revision?.revisionId !== job.rev; }
  async _runJob(job) {
    const { space, tier } = job; await nextFrame();   // 렌더 루프를 막지 않도록 프레임 사이에서 시작
    if (this._stale(job)) return;
    let obj;
    try {
      obj = this.mock ? this.cb.mock.buildMockSpace(space.def, 'textured') : (await this.loader.loadAsync(tier.url, (e) => { job.loaded = e.loaded; job.total = e.total || 0; this._info(); })).scene;
    } catch (e) {
      space.tiers = space.tiers.filter((t) => t.t !== tier.t);
      if (space.layerObjs.textured === undefined) { if (space.tiers.length) { job.requeued = true; space.texPending = 0; this._queueTier(space, space.tiers[0]); } else space.layerObjs.textured = null; }
      this.cb.onStatus?.(`${space.def.name} 실사(LOD${tier.t}) 로드 실패: ${e.message}`, 'warn'); return;
    }
    if (this._stale(job)) { this._disposeTree(obj); return; }
    await nextFrame();
    const sk = LAYERS.find((l) => l.key === 'textured').sk;
    this._styleTree(obj, 'textured', (mesh) => {
      const ex = { ...(mesh.userData || {}), ...(mesh.parent?.userData || {}) };
      mesh.userData.info = { ...(mesh.userData.info || {}), sourceKind: mesh.userData.info?.sourceKind || ex.sourceKind || sk, confidence: mesh.userData.info?.confidence ?? ex.confidence ?? space.def.coverage, reviewStatus: mesh.userData.info?.reviewStatus || ex.reviewStatus || space.def.quality, label: mesh.userData.info?.label || `${space.def.name} · textured`, unobserved: mesh.userData.info?.unobserved || (ex.unobservedFaces > 0), layer: 'textured', spaceId: space.def.spaceId };
    });
    // 텍스처를 프레임당 하나씩 GPU 에 올려 첫 렌더 때 한꺼번에 멈추는 것(hitch)을 피한다
    const texs = new Set(); obj.traverse((o) => { if (o.isMesh && o.material?.map) texs.add(o.material.map); });
    for (const t of texs) { if (this._stale(job)) break; try { this.renderer.initTexture(t); } catch { /* 업로드는 첫 렌더에서 */ } await nextFrame(); }
    if (this._stale(job)) { this._disposeTree(obj); return; }
    const regs = [];
    obj.traverse((o) => { for (const r of o.userData?.extras?.assetRegions || o.userData?.assetRegions || []) regs.push(r); });
    (space.regionsBy ||= {}).textured = regs.map((r) => { const b = new THREE.Box3(new THREE.Vector3(...r.min), new THREE.Vector3(...r.max)).applyMatrix4(space.T); return [b.min.toArray(), b.max.toArray()]; });
    space.regions = Object.values(space.regionsBy).flat(1); this._rebuildMask();
    const old = space.layerObjs.textured;
    obj.visible = this.layers.textured.on; space.texMax = this.texMax;
    if (old) {   // 이전 LOD 위에 새 LOD 를 서서히 겹쳐 올린 뒤 이전 것은 폐기 (팝핑 방지)
      obj.traverse((o) => { if (o.isMesh) { o.material.userData.fade = 0; o.material.polygonOffsetFactor = o.material.polygonOffsetUnits = -1; o.renderOrder = 1; } });
      this._applyOpacity('textured');
      this._fades.push({ space, obj, old, t0: performance.now() });
    }
    space.group.add(obj); space.layerObjs.textured = obj; space.texTier = tier.t;
    this.cb.onSpacesChanged?.();
  }
  _stepFades(now) {
    if (!this._fades.length) return;
    for (const f of [...this._fades]) {
      const k = Math.min(1, (now - f.t0) / FADE_MS);
      f.obj.traverse((o) => { if (o.isMesh) o.material.userData.fade = k; });
      if (k >= 1) this._endFade(f, true);
    }
    this._applyOpacity('textured');
  }
  _endFade(f, restore) {
    this._fades.splice(this._fades.indexOf(f), 1);
    f.space.group.remove(f.old); this._disposeTree(f.old);
    if (restore) f.obj.traverse((o) => { if (o.isMesh) { o.material.userData.fade = 1; o.material.polygonOffsetFactor = o.material.polygonOffsetUnits = 1; o.renderOrder = 0; } });
  }
  /** 공간별 LOD 승격/강등 + 메모리 예산 초과 시 가장 먼 공간부터 낮추거나 내린다 */
  _lodUpdate() {
    if (!this.layers.textured.on) return;
    for (const s of this.spaces.values()) {
      if (!s.loaded || !s.texTier || s.texPending) continue;
      const pick = this._wantTier(s); if (!pick || pick.t === s.texTier) continue;
      if (pick.t > s.texTier || s.texTier > this.tierCap) this._queueTier(s, pick);
    }
    const budget = this.texBudgetMB * 1048576;
    if (this.texBytes > budget) {
      const cand = [...this.spaces.values()].filter((s) => s.loaded && s.def.spaceId !== this.selected.spaceId && this._dist(s) > 1.5).sort((a, b) => this._dist(b) - this._dist(a));
      for (const s of cand) {
        if (this.texBytes <= budget) break;
        if (s.texTier > (s.tiers?.[0]?.t || 1) && !s.texPending) this._queueTier(s, s.tiers[0]);
        else if (this._dist(s) > 8) { this._unloadSpace(s); s.hold.visible = true; }
      }
    }
  }
  _info(force) {
    const now = performance.now(); if (!force && now - this._lastInfo < 150) return; this._lastInfo = now;
    const act = [...(this._active || [])]; const tot = act.reduce((a, j) => a + j.total, 0), ld = act.reduce((a, j) => a + j.loaded, 0);
    this.loadInfo = { active: this._qActive, queued: this._q.length, pct: tot ? Math.round((ld / tot) * 100) : null, texMB: Math.round(this.texBytes / 1048576), budgetMB: this.texBudgetMB };
    this.cb.onLoadInfo?.(this.loadInfo);
  }
  loadSpace(id) { const s = this.spaces.get(id); if (!s || s.loaded) return; s.loaded = true; s.hold.visible = false; this._ensureSpace(s); this.cb.onSpacesChanged?.(); }
  _unloadSpace(s) {
    if (!s.loaded && !Object.keys(s.layerObjs).length) return;
    s.loaded = false; s.epoch = (s.epoch || 0) + 1; s.texTier = 0; s.texPending = 0; s.tiers = null; s.regionsBy = null;
    this._q = this._q.filter((j) => j.space !== s);
    for (const f of this._fades.filter((x) => x.space === s)) this._endFade(f, false);
    for (const o of Object.values(s.layerObjs)) if (o) { s.group.remove(o); this._disposeTree(o); }
    s.layerObjs = {}; s.regions = null; this._rebuildMask();
    this.cb.onSpacesChanged?.();
  }
  _disposeTree(root) {
    root?.traverse?.((o) => {
      if (!o.isMesh) return;
      o.geometry?.dispose();
      for (const k of Object.keys(this.mats)) if (this.mats[k].delete(o.material)) break;
      const m = o.material; if (m) { for (const key of ['map', 'normalMap', 'roughnessMap', 'emissiveMap']) { const t = m[key]; if (!t) continue; if (t.userData?.twinBytes) { this.texBytes -= t.userData.twinBytes; t.userData.twinBytes = 0; } t.dispose?.(); } m.dispose(); }
    });
  }
  /** 리비전 선택: 공간 레이어를 모두 버리고 해당 리비전 파일로 다시 불러온다 */
  setRevision(spaceId, rev) {
    const s = this.spaces.get(spaceId); if (!s) return;
    s.revision = rev; const was = s.loaded; this._unloadSpace(s); if (was) this.loadSpace(spaceId);
  }
  refreshHolders() { for (const s of this.spaces.values()) s.hold.visible = !s.loaded; }
  loadedCount() { return [...this.spaces.values()].filter((s) => s.loaded).length; }

  _lazyUpdate() {
    const cam = this.camera.position;
    for (const s of this.spaces.values()) {
      const d = s.bbox.distanceToPoint(cam), sel = s.def.spaceId === this.selected.spaceId;
      const range = this.mode === 'exterior' || this.mode === 'entrance' ? this.loadRange * 1.8 : this.loadRange;
      if (!s.loaded && (sel || d < range) && this.loading < 3) { this.loadSpace(s.def.spaceId); }
      else if (s.loaded && !sel && d > range * 2.2) { this._unloadSpace(s); s.hold.visible = true; }
    }
    this._lodUpdate();
  }

  // ───────── 충돌 ─────────
  _rebuildColliders() {
    const c = [];
    for (const w of this.m?.envelope?.walls || []) {
      let box = null;
      if (w.bboxBuilding) box = new THREE.Box3(new THREE.Vector3(...w.bboxBuilding.min), new THREE.Vector3(...w.bboxBuilding.max));
      else if (this.envObj) { this.envObj.traverse((o) => { if (o.isMesh && o.userData.wallId === w.id) { const b = new THREE.Box3().setFromObject(o); box = box ? box.union(b) : b; } }); }
      if (box) c.push({ box, kind: 'wall', id: w.id });
    }
    for (const a of this.assets.values()) c.push({ box: a.bb.clone(), kind: 'asset', id: a.def.assetId });
    this.colliders = c;
  }
  /** 원(반경 R) 대 AABB 침투 해소 — 가장 얕은 축으로 밀어내므로 벽을 만나면 멈추지 않고 벽면을 따라 미끄러진다.
   *  윗면이 바닥+5 cm 이하인 낮은 문턱은 장애물이 아니라 자동으로 넘는다. 포털 구간은 통과. */
  _resolve(x, z) {
    const R = 0.25, base = this.baseY; let px = x, pz = z;
    for (let it = 0; it < 3; it++) {
      let hit = false;
      for (const c of this.colliders) {
        const b = c.box;
        if (b.max.y <= base + STEP + 1e-3 || b.min.y > base + 1.7) continue;
        if (!(px > b.min.x - R && px < b.max.x + R && pz > b.min.z - R && pz < b.max.z + R)) continue;
        if (c.kind === 'wall' && this._inPortal(px, pz, base)) continue;
        const dxl = px - (b.min.x - R), dxr = b.max.x + R - px, dzl = pz - (b.min.z - R), dzr = b.max.z + R - pz;
        const mx = Math.min(dxl, dxr), mz = Math.min(dzl, dzr);
        if (Math.min(mx, mz) > 0.6) continue;   // 이미 깊이 안쪽(스폰 위치 등) — 밀어내지 않는다
        if (mx < mz) px += dxl < dxr ? -dxl : dxr; else pz += dzl < dzr ? -dzl : dzr;
        hit = true;
      }
      if (!hit) break;
    }
    return [px, pz];
  }
  _groundY(x, z) {
    let g = this.baseY;
    for (const c of this.colliders) {
      const b = c.box, rise = b.max.y - this.baseY;
      if (rise > 0 && rise <= STEP && x >= b.min.x && x <= b.max.x && z >= b.min.z && z <= b.max.z) g = Math.max(g, b.max.y);
    }
    return g;
  }
  _inPortal(x, z, y) {
    for (const p of this.m.portals || []) {
      if (p.reviewStatus === 'REJECTED' || p.state === 'CLOSED') continue;
      const [px, py, pz] = p.positionBuilding;
      if (Math.abs(y - py) > 1.2) continue;
      const a = rad(p.yawDeg || 0), fx = -Math.sin(a), fz = -Math.cos(a);
      const dx = x - px, dz = z - pz;
      const along = dx * fx + dz * fz, lat = -dx * fz + dz * fx;
      if (Math.abs(lat) < p.widthM / 2 + 0.05 && Math.abs(along) < 0.9) return true;
    }
    return false;
  }

  // ───────── 모드 ─────────
  currentSpaceId() {
    const p = this.pos; let best = null;
    for (const s of this.spaces.values()) {
      const b = s.bbox; if (p.x >= b.min.x && p.x <= b.max.x && p.z >= b.min.z && p.z <= b.max.z && p.y + 0.5 >= b.min.y && p.y + 0.5 <= b.max.y) best = s.def.spaceId;
    }
    return best;
  }
  floorOf(y) { let best = null; for (const f of this.floors.values()) if (f.elevationM <= y + 0.3 && (!best || f.elevationM > best.elevationM)) best = f; return best; }
  setMode(mode) {
    if (this.mode === mode) return;
    const wasWalk = this.mode === 'walk' || (this.mode === 'free' && this.fly);
    this.mode = mode;
    this._setWalking(mode === 'walk' || (mode === 'free' && this.fly));
    if (!this._walking && wasWalk) this._leaveWalk();
    this.gEntr.visible = mode === 'exterior' || mode === 'entrance';
    this.grid.visible = !this.photo && (mode === 'exterior' || mode === 'entrance');
    this.cb.onMode?.(mode);
  }
  setFly(on) { this.fly = on; if (!on) { this.baseY = this.floorOf(this.pos.y)?.elevationM ?? this.pos.y; this.pos.y = this.baseY; this.stepY = 0; this._eyeY = null; } this._setWalking(this.mode === 'walk' || (this.mode === 'free' && on)); if (!this._walking) this._leaveWalk(); }
  _setWalking(on) {
    if (on && !this._walking) {
      this._walking = true; this.controls.enabled = false;
      const d = new THREE.Vector3(); this.camera.getWorldDirection(d);
      if (this.mode === 'free' || !this._walkInit) { this.pos.copy(this.camera.position); if (this.mode === 'walk') this.pos.y = this.floorOf(this.pos.y)?.elevationM ?? 0; this.yaw = Math.atan2(-d.x, -d.z); this.pitch = Math.asin(d.y); }
      this._walkInit = true; this.baseY = this.pos.y; this.stepY = 0; this.vel.set(0, 0); this.cyaw = this.yaw; this.cpitch = this.pitch; this._eyeY = null;
    } else if (!on && this._walking) { this._walking = false; }
  }
  _leaveWalk() {
    this.controls.enabled = true; const d = new THREE.Vector3(); this.camera.getWorldDirection(d);
    this.controls.target.copy(this.camera.position).addScaledVector(d, 4); this.controls.update();
  }
  /** 워크 시작/이동: 바닥 위치 p(발), 바라보는 yaw(도) */
  teleport(p, yawDeg, floorId) {
    this.stopTour();
    this.pos.set(...p); if (yawDeg != null) this.yaw = rad(yawDeg); this.pitch = 0;
    this.baseY = p[1]; this.stepY = 0; this.vel.set(0, 0); this.cyaw = this.yaw; this.cpitch = 0; this._eyeY = null;
    this.floorId = floorId ?? this.floorOf(p[1])?.id; this._walkInit = true;
    if (this.mode !== 'walk') { this.mode = 'walk'; this._setWalking(true); this.gEntr.visible = false; this.grid.visible = false; this.cb.onMode?.('walk'); }
    else { this._walking = true; this.controls.enabled = false; }
    this.cb.onFloor?.(this.floorId);
  }
  flyTo(position, target, ms = 900) {
    this.stopTour();
    if (this._walking) { this.setMode('exterior'); }
    this._tween = { t0: performance.now(), ms, p0: this.camera.position.clone(), t0v: this.controls.target.clone(), p1: new THREE.Vector3(...position), t1: new THREE.Vector3(...target) };
  }
  fitAll() {
    const box = new THREE.Box3();
    for (const s of this.spaces.values()) box.union(s.bbox);
    for (const w of this.m.envelope?.walls || []) if (w.bboxBuilding) box.expandByPoint(new THREE.Vector3(...w.bboxBuilding.min)).expandByPoint(new THREE.Vector3(...w.bboxBuilding.max));
    if (box.isEmpty()) box.set(new THREE.Vector3(-5, 0, -5), new THREE.Vector3(5, 3, 5));
    const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
    this.grid.position.set(c.x, box.min.y - 0.25, c.z);
    const dist = r / Math.sin(rad(this.camera.fov / 2)) * 0.95;
    const dir = new THREE.Vector3(0.35, 0.75, 1).normalize();
    this.flyTo(c.clone().addScaledVector(dir, dist).toArray(), c.toArray(), 700);
    this.sceneBox = box;
  }
  focusBox(box) { const c = box.getCenter(new THREE.Vector3()), r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.5); const dist = r / Math.sin(rad(this.camera.fov / 2)) * 1.1; this.flyTo(c.clone().addScaledVector(new THREE.Vector3(0.4, 0.8, 1).normalize(), dist).toArray(), c.toArray(), 700); }

  // ───────── 단면 ─────────
  setSection(on, y) { this.clipOn = on; if (y != null) this.clip.constant = y; this.renderer.clippingPlanes = on ? [this.clip] : []; }

  // ───────── 입력 ─────────
  _bind() {
    const el = this.renderer.domElement;
    this._onKey = (e) => {
      if (!this.container.isConnected) return;
      if (e.target.closest?.('input, select, textarea')) return;
      if (e.type === 'keydown') { this.keys.add(e.code); if (this._tour && /^(Key[WASDQZ]|Arrow|Space)/.test(e.code)) this.stopTour(); } else this.keys.delete(e.code);
      if (e.type === 'keydown' && e.code === 'KeyE') this.cb.onUse?.();
    };
    addEventListener('keydown', this._onKey); addEventListener('keyup', this._onKey);
    let down = null;
    el.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY, t: performance.now(), id: e.pointerId, lx: e.clientX, ly: e.clientY, moved: false }; try { el.setPointerCapture(e.pointerId); } catch { /* 합성 이벤트 */ } });
    el.addEventListener('pointermove', (e) => {
      if (down && down.id === e.pointerId) {
        const dx = e.clientX - down.lx, dy = e.clientY - down.ly; down.lx = e.clientX; down.ly = e.clientY;
        if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5) { down.moved = true; if (this._tour) this.stopTour(); }
        if (this._walking && !this._tour && !document.pointerLockElement) this._look(dx, dy, e.pointerType === 'touch' ? 0.0045 : 0.003);
      } else if (e.pointerType === 'mouse') { this._hover(e); }
      if (document.pointerLockElement === el && !this._tour) this._look(e.movementX, e.movementY, 0.0022);
    });
    el.addEventListener('pointerup', (e) => {
      const d = down; down = null;
      if (!d || d.moved || performance.now() - d.t > 600) return;
      this._click(e);
    });
    el.addEventListener('pointerleave', () => this.cb.onHover?.(null));
    el.addEventListener('dblclick', () => { if (this.mode === 'walk' && matchMedia('(pointer: fine)').matches) { try { el.requestPointerLock(); } catch { /* 지원 안 함 */ } } });
  }
  _look(dx, dy, k) { k *= this.lookSens * (this.camera.fov / 65); this.yaw -= dx * k; this.pitch = Math.max(-1.45, Math.min(1.45, this.pitch - dy * k)); }
  _ndc(e) { const r = this.renderer.domElement.getBoundingClientRect(); return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); }
  pick(e, filter) {
    this.ray.setFromCamera(this._ndc(e), this.camera);
    const hits = this.ray.intersectObjects(this.scene.children, true);
    for (const h of hits) {
      const o = h.object; if (!o.isMesh || !o.visible) continue;
      let vis = true; for (let n = o; n; n = n.parent) if (!n.visible) { vis = false; break; }
      if (!vis) continue;
      if (this.clipOn && h.point.y > this.clip.constant) continue;
      if (this.maskOn && o.userData.info?.layer && MASKED.has(o.userData.info.layer) && this._inMask(h.point)) continue;
      if (filter && !filter(h)) continue;
      return h;
    }
    return null;
  }
  _inMask(p) { for (let i = 0; i < this.maskU.uMaskN.value; i++) { const a = this.maskU.uMaskMin.value[i], b = this.maskU.uMaskMax.value[i]; if (p.x > a.x && p.y > a.y && p.z > a.z && p.x < b.x && p.y < b.y && p.z < b.z) return true; } return false; }
  _hover(e) {
    const h = this.pick(e, (x) => x.object.userData.info);
    this.cb.onHover?.(h ? { info: h.object.userData.info, x: e.clientX, y: e.clientY, point: h.point } : null);
  }
  _worldNormal(h) { const n = h.face?.normal.clone() || new THREE.Vector3(0, 1, 0); n.transformDirection(h.object.matrixWorld); return n; }
  _click(e) {
    const t = this.tool;
    if (t === 'measure' || t === 'pin' || t === 'portal' || t === 'portalMove') {
      const h = this.pick(e, (x) => !x.object.userData.info || (x.object.userData.info.kind !== 'portal' && x.object.userData.info.kind !== 'entrance'));
      if (!h) return;
      if (t === 'measure') return this._measureAdd(h.point);
      if (t === 'pin') return this.cb.onPin?.(h.point.clone());
      const n = this._worldNormal(h); if (h.face && n.dot(this.ray.ray.direction) > 0) n.negate();
      let yaw;
      if (Math.abs(n.y) > 0.7) { const d = new THREE.Vector3(); this.camera.getWorldDirection(d); yaw = Math.atan2(-d.x, -d.z) * 180 / Math.PI; }
      else yaw = Math.atan2(n.x, n.z) * 180 / Math.PI + 180;   // 벽에서 방 안쪽(법선)의 반대 = 벽을 통과하는 방향
      const fl = this.floorOf(h.point.y);
      return this.cb.onPortalPlace?.({ point: [h.point.x, fl ? fl.elevationM : Math.max(0, h.point.y), h.point.z], yawDeg: Math.round(((yaw % 360) + 360) % 360), spaceId: h.object.userData.info?.spaceId || this.currentSpaceId(), move: t === 'portalMove' });
    }
    const h = this.pick(e, (x) => x.object.userData.info);
    if (h) this.cb.onPick?.(h.object.userData.info, h.point);
  }
  _measureAdd(p) {
    if (this.measure.length >= 2) this.clearMeasure();
    this.measure.push(p.clone());
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.06, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffe14d, depthTest: false })); dot.position.copy(p); dot.renderOrder = 12; this.gTools.add(dot); (this._mObjs ||= []).push(dot);
    if (this.measure.length === 2) {
      const [a, b] = this.measure;
      const ln = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0xffe14d, depthTest: false })); ln.renderOrder = 11;
      const mm = Math.round(a.distanceTo(b) * 1000);
      const lb = labelSprite(`${mm.toLocaleString()} mm`, '#ffe14d', 0.28); lb.position.copy(a).add(b).multiplyScalar(0.5).y += 0.2;
      this.gTools.add(ln, lb); this._mObjs.push(ln, lb); this.cb.onMeasure?.(mm);
    } else this.cb.onMeasure?.(null);
  }
  clearMeasure() { for (const o of this._mObjs || []) { this.gTools.remove(o); o.geometry?.dispose(); o.material?.map?.dispose(); o.material?.dispose(); } this._mObjs = []; this.measure = []; this.cb.onMeasure?.(null); }
  addPin(id, point, text) {
    const sp = labelSprite(text || '핀', '#ffd86b', 0.26); sp.position.copy(point); sp.position.y += 0.3;
    const stick = new THREE.Mesh(new THREE.SphereGeometry(0.05, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffd86b, depthTest: false })); stick.position.copy(point); stick.renderOrder = 12;
    const g = new THREE.Group(); g.add(sp, stick); g.userData.pinId = id; this.gTools.add(g); this.pins.push(g);
  }
  removePin(id) { const i = this.pins.findIndex((g) => g.userData.pinId === id); if (i >= 0) { const g = this.pins[i]; this.gTools.remove(g); g.traverse((o) => { o.geometry?.dispose(); o.material?.map?.dispose(); o.material?.dispose(); }); this.pins.splice(i, 1); } }
  setTool(t) { this.tool = t; this.renderer.domElement.style.cursor = t ? 'crosshair' : ''; if (t !== 'measure') this.clearMeasure(); }

  // ───────── 선택 표시 ─────────
  highlightAsset(assetId) {
    this.clearSel('asset');
    const r = this.assets.get(assetId); if (!r) return;
    const h = new THREE.Box3Helper(r.bb, 0xffe14d); h.userData.kind = 'asset'; h.material.transparent = true; this.gSel.add(h); this._pulse = h;
  }
  highlightWall(wallId) {
    this.clearSel('wall');
    const w = this.m.envelope?.walls?.find((x) => x.id === wallId); if (!w) return;
    const c = this.colliders.find((x) => x.kind === 'wall' && x.id === wallId);
    if (!c) return;
    const h = new THREE.Box3Helper(c.box, 0xffe14d); h.userData.kind = 'wall'; this.gSel.add(h);
  }
  highlightPortal(id) { this.clearSel('portal'); const g = this.portalObjs.get(id); if (!g) return; const b = new THREE.Box3().setFromObject(g).expandByScalar(0.08); const h = new THREE.Box3Helper(b, 0xffe14d); h.userData.kind = 'portal'; this.gSel.add(h); }
  clearSel(kind) { if (!kind || kind === 'asset') this._pulse = null; for (const c of [...this.gSel.children]) if (!kind || c.userData.kind === kind) { this.gSel.remove(c); c.geometry?.dispose(); c.material?.dispose(); } }

  // ───────── 루프 ─────────
  resize() {
    if (!this.ok) return;
    const w = this.container.clientWidth || 600, h = this.container.clientHeight || 400;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
  _loop(now) {
    if (!this.container.isConnected) { this.dispose(); return; }
    const dt = Math.min(0.1, (now - this._last) / 1000); this._last = now;
    if (this._tween && !this._tour) {
      const k = Math.min(1, (now - this._tween.t0) / this._tween.ms), e = k * k * (3 - 2 * k), tw = this._tween;
      this.camera.position.lerpVectors(tw.p0, tw.p1, e); this.controls.target.lerpVectors(tw.t0v, tw.t1, e);
      if (k >= 1) this._tween = null;
    }
    if (this._tour) this._tourStep(now);
    else if (this._walking) this._walk(dt); else this.controls.update();
    // 근/원거리 평면: 실내 보행은 촘촘하게(깊이 정밀도 확보), 외부 조망은 넓게
    const walkish = this._walking || (this._tour && this.mode !== 'exterior' && this.mode !== 'entrance');
    const nr = walkish ? 0.05 : 0.3, fr = walkish ? 160 : 800;
    if (this.camera.near !== nr || this.camera.far !== fr) { this.camera.near = nr; this.camera.far = fr; this.camera.updateProjectionMatrix(); }
    if (this._pulse) this._pulse.material.opacity = 0.6 + 0.4 * Math.sin(now / 200);
    this._stepFades(now);
    if (now - this._lazyT > 400 && this.m) { this._lazyT = now; this._lazyUpdate(); this._notifyPos(); }
    this.renderer.render(this.scene, this.camera);
    this._frames++;
    if (now - this._fpsT > 500) {
      this.fps = Math.round((this._frames * 1000) / (now - this._fpsT)); this._frames = 0; this._fpsT = now;
      this.tris = this.renderer.info.render.triangles; this._guard(this.fps); this._info();
      this.cb.onFps?.(this.fps, this.tris, this.loadedCount());
    }
    this._raf = requestAnimationFrame(this._loop);
  }
  _notifyPos() {
    if (!this._walking || this.mode !== 'walk') return;
    const sid = this.currentSpaceId();
    if (sid !== this._curSpace) { this._curSpace = sid; this.cb.onSpaceEnter?.(sid); }
    const f = this.floorOf(this.pos.y); if (f && f.id !== this.floorId) { this.floorId = f.id; this.cb.onFloor?.(f.id); }
  }
  /** 걷기: 가속/감속 감쇠, 벽 미끄러짐, 5 cm 문턱 자동 넘기, 눈높이 1.6 m(층 바닥 기준), 시선 보간, 선택적 헤드밥 */
  _walk(dt) {
    const k = this.keys, fwd = (k.has('KeyW') || k.has('ArrowUp') ? 1 : 0) - (k.has('KeyS') || k.has('ArrowDown') ? 1 : 0) + this.joy.y;
    const str = (k.has('KeyD') || k.has('ArrowRight') ? 1 : 0) - (k.has('KeyA') || k.has('ArrowLeft') ? 1 : 0) + this.joy.x;
    if (k.has('KeyQ')) this.yaw += dt * 1.6;   // 키보드 회전 (포인터락 없이도)
    if (k.has('KeyZ')) this.yaw -= dt * 1.6;
    const vmax = k.has('ShiftLeft') ? 3.4 : 1.7, len = Math.hypot(fwd, str);
    let tx = 0, tz = 0;
    if (len > 0.01) { const n = Math.min(1, len) / len; tx = (fwd * -Math.sin(this.yaw) + str * Math.cos(this.yaw)) * n * vmax; tz = (fwd * -Math.cos(this.yaw) + str * -Math.sin(this.yaw)) * n * vmax; }
    const acc = 1 - Math.exp(-dt * (len > 0.01 ? 9 : 11));   // 가속은 약간 느리게, 감속은 더 빠르게 멈추도록
    this.vel.x += (tx - this.vel.x) * acc; this.vel.y += (tz - this.vel.y) * acc;
    if (Math.hypot(this.vel.x, this.vel.y) < 0.004 && len <= 0.01) this.vel.set(0, 0);
    const mx = this.vel.x * dt, mz = this.vel.y * dt;
    if (this.fly) {
      this.pos.x += mx; this.pos.z += mz;
      const vy = ((k.has('Space') ? 1 : 0) - (k.has('KeyC') ? 1 : 0)) * (len > 0.01 ? vmax : 1.7);
      this._vy = (this._vy || 0) + (vy - (this._vy || 0)) * (1 - Math.exp(-dt * 9)); this.pos.y += this._vy * dt;
    } else if (mx || mz) {
      const ox = this.pos.x, oz = this.pos.z;
      const [rx, rz] = this._resolve(ox + mx, oz + mz);
      this.pos.x = rx; this.pos.z = rz;
      if (dt > 0) { const ax = (rx - ox) / dt, az = (rz - oz) / dt; if (Math.hypot(ax - this.vel.x, az - this.vel.y) > 0.05) this.vel.set(ax, az); }   // 벽에 부딪힌 성분은 속도에서 제거 → 미끄러짐
    }
    if (!this.fly) {
      const gy = this._groundY(this.pos.x, this.pos.z), tgt = gy - this.baseY;
      this.stepY += (tgt - this.stepY) * (1 - Math.exp(-dt * 16)); this.pos.y = this.baseY + this.stepY;
    }
    // 시선 보간 (마우스/터치 입력의 미세 떨림 완화)
    const lk = 1 - Math.exp(-dt * 28); this.cyaw += angDiff(this.cyaw, this.yaw) * lk; this.cpitch += (this.pitch - this.cpitch) * lk;
    let bob = 0;
    if (this.headBob && !this.fly) { const sp = Math.hypot(this.vel.x, this.vel.y); this._bobPh += sp * dt * 5.2; bob = Math.sin(this._bobPh) * 0.022 * Math.min(1, sp / 1.7); }
    this.camera.position.set(this.pos.x, this.pos.y + (this.fly ? 0 : EYE) + bob, this.pos.z);
    this.camera.rotation.set(this.cpitch, this.cyaw, 0, 'YXZ');
  }

  // ───────── 투어 자동 비행 ─────────
  /** points: [{position:[x,y,z] 카메라 위치, target:[x,y,z], name?}] — 부드러운 가감속으로 이동하며 target 을 바라본다 */
  startTour(points, { dwellMs = 1600, loop = false } = {}) {
    const pts = (points || []).filter((p) => p?.position && p?.target);
    if (!pts.length) return false;
    this._tween = null;
    this._tourWalking = this._walking; this.controls.enabled = false;
    const d = new THREE.Vector3(); this.camera.getWorldDirection(d);
    this._tour = { pts, i: -1, loop, dwell: dwellMs, seg: null, wait: 0, cur: { p: this.camera.position.clone(), yaw: Math.atan2(-d.x, -d.z), pitch: Math.asin(d.y) } };
    this.cb.onTour?.({ running: true, i: -1, n: pts.length });
    return true;
  }
  get touring() { return !!this._tour; }
  _tourStep(now) {
    const T = this._tour; if (!T) return;
    if (!T.seg) {
      if (T.wait && now < T.wait) return;
      T.i++;
      if (T.i >= T.pts.length) { if (T.loop) T.i = 0; else return this.stopTour(true); }
      const vp = T.pts[T.i], p1 = new THREE.Vector3(...vp.position), tg = new THREE.Vector3(...vp.target), dv = tg.clone().sub(p1);
      const yaw1 = Math.atan2(-dv.x, -dv.z), pitch1 = Math.atan2(dv.y, Math.hypot(dv.x, dv.z));
      const dist = T.cur.p.distanceTo(p1), turn = Math.abs(angDiff(T.cur.yaw, yaw1));
      T.seg = { t0: now, ms: Math.max(1400, Math.min(7000, dist / 1.8 * 1000, 7000), turn * 500), p0: T.cur.p.clone(), y0: T.cur.yaw, pi0: T.cur.pitch, p1, dy: angDiff(T.cur.yaw, yaw1), pi1: pitch1 };
      this.cb.onTour?.({ running: true, i: T.i, n: T.pts.length, name: vp.name });
    }
    const sg = T.seg, k = Math.min(1, (now - sg.t0) / sg.ms), e = smoother(k);
    T.cur.p.lerpVectors(sg.p0, sg.p1, e); T.cur.yaw = sg.y0 + sg.dy * e; T.cur.pitch = sg.pi0 + (sg.pi1 - sg.pi0) * e;
    this.camera.position.copy(T.cur.p); this.camera.rotation.set(T.cur.pitch, T.cur.yaw, 0, 'YXZ');
    if (k >= 1) { T.seg = null; T.wait = now + T.dwell; }
  }
  stopTour(done) {
    const T = this._tour; if (!T) return;
    this._tour = null;
    const c = T.cur;
    if (this._walking && !this.fly) {   // 걷기 상태로 복귀: 눈높이 아래 바닥으로 내려놓고 시선 이어받기
      const fy = c.p.y - EYE; this.baseY = this.floorOf(fy)?.elevationM ?? fy; this.stepY = 0;
      this.pos.set(c.p.x, this.baseY, c.p.z); this.yaw = this.cyaw = c.yaw; this.pitch = this.cpitch = c.pitch; this.vel.set(0, 0);
    } else if (this._walking) { this.pos.copy(c.p); this.yaw = this.cyaw = c.yaw; this.pitch = this.cpitch = c.pitch; this.vel.set(0, 0); }
    else this._leaveWalk();
    this.cb.onTour?.({ running: false, done: !!done });
  }

  /** 현재 카메라/선택 상태를 보존한 채 PNG */
  screenshotBlob() { this.renderer.render(this.scene, this.camera); return new Promise((res) => this.renderer.domElement.toBlob(res, 'image/png')); }

  dispose() {
    if (this._disposed || !this.ok) return; this._disposed = true;
    cancelAnimationFrame(this._raf); this._ro.disconnect();
    removeEventListener('keydown', this._onKey); removeEventListener('keyup', this._onKey);
    if (document.pointerLockElement === this.renderer.domElement) document.exitPointerLock();
    for (const s of this.spaces.values()) this._unloadSpace(s);
    for (const g of [this.gEnv, this.gAssets, this.gPortals, this.gEntr, this.gTools]) g.traverse((o) => { o.geometry?.dispose?.(); o.material?.map?.dispose?.(); o.material?.dispose?.(); });
    this.controls.dispose(); this.renderer.dispose();
  }
}
