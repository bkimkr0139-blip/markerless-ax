// 3D 뷰어 (three.js) — 자산 모델·외곽선(와이어프레임)·치수·사진 카드·관측 점군을 레이어로 보여 준다.
// 좌표계: ARKit/glTF와 같은 Y-up 오른손 좌표계, 단위 m.
import * as THREE from '../vendor/three.module.js';
import { OrbitControls } from '../vendor/OrbitControls.js';
import { GLTFLoader } from '../vendor/GLTFLoader.js';

export { THREE };

export class Viewer3D {
  constructor(container) {
    this.container = container;
    this.layers = new Map();
    this.ok = true;
    try {
      this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    } catch (e) {
      this.ok = false;
      container.append(Object.assign(document.createElement('div'), { className: 'viewer-msg', textContent: '이 브라우저에서는 3D(WebGL)를 사용할 수 없습니다.' }));
      return;
    }
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setClearColor(0x000000, 0);
    container.append(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.01, 200);
    this.camera.position.set(0.9, 0.7, 1.1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI * 0.96;
    this.scene.add(new THREE.HemisphereLight(0xcfe3ff, 0x1b2330, 1.15));
    const dir = new THREE.DirectionalLight(0xffffff, 1.4);
    dir.position.set(2, 4, 3);
    this.scene.add(dir);
    this.grid = new THREE.GridHelper(4, 40, 0x2f4259, 0x1a2634);
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.7;
    this.scene.add(this.grid);
    this.axes = new THREE.AxesHelper(0.12);
    this.scene.add(this.axes);

    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(container);
    this.resize();
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  _loop() {
    if (!this.container.isConnected) { this.dispose(); return; }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this._raf = requestAnimationFrame(this._loop);
  }

  resize() {
    if (!this.ok) return;
    const w = this.container.clientWidth || 600, h = this.container.clientHeight || 400;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  /** 레이어 추가 (이름으로 켜고 끈다) */
  add(name, object, visible = true) {
    if (!this.ok) return;
    this.remove(name);
    object.visible = visible;
    this.layers.set(name, object);
    this.root.add(object);
  }
  remove(name) {
    const o = this.layers.get(name);
    if (o) { this.root.remove(o); o.traverse?.((c) => { c.geometry?.dispose?.(); c.material?.map?.dispose?.(); c.material?.dispose?.(); }); this.layers.delete(name); }
  }
  has(name) { return this.layers.has(name); }
  isVisible(name) { return !!this.layers.get(name)?.visible; }
  setVisible(name, v) { const o = this.layers.get(name); if (o) o.visible = v; }

  /** 보이는 레이어 전체가 들어오게 카메라를 맞춘다 */
  fit() {
    if (!this.ok) return;
    const box = new THREE.Box3();
    for (const o of this.layers.values()) if (o.visible && !o.userData.noFit) box.expandByObject(o);
    if (box.isEmpty()) return;
    const c = box.getCenter(new THREE.Vector3());
    const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.08);
    const dist = (r / Math.sin((this.camera.fov * Math.PI) / 360)) * 1.05;
    const d = new THREE.Vector3(0.85, 0.62, 1).normalize();
    this.camera.position.copy(c).addScaledVector(d, dist);
    this.camera.near = Math.max(dist / 500, 0.002);
    this.camera.far = dist * 80;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(c);
    this.controls.update();
    this.grid.position.y = box.min.y - 0.002;
    this.axes.position.set(box.min.x, box.min.y, box.min.z);
  }

  screenshot() { return this.ok ? (this.renderer.render(this.scene, this.camera), this.renderer.domElement.toDataURL('image/png')) : null; }

  dispose() {
    if (!this.ok || this._disposed) return;
    this._disposed = true;
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    for (const n of [...this.layers.keys()]) this.remove(n);
    this.controls.dispose();
    this.renderer.dispose();
  }
}

// ───────── 빌더 ─────────
export function linesObject(segments, color = 0x4fd1ff, opacity = 1) {
  const pos = new Float32Array(segments.length * 6);
  segments.forEach((s, i) => pos.set(s, i * 6));
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity }));
}

export function boxEdges(corners) {
  const segs = [];
  for (let i = 0; i < 8; i++) for (const bit of [1, 2, 4]) if (!(i & bit)) segs.push([...corners[i], ...corners[i | bit]]);
  return segs;
}

export function pointsObject(floats, color = 0xffc857, size = 0.007) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(floats, 3));
  return new THREE.Points(g, new THREE.PointsMaterial({ color, size, sizeAttenuation: true }));
}

export function b64Floats(b64) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const u8 = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return new Float32Array(buf);
}

export function labelSprite(text, color = '#7fdcff', worldHeight = 0.028) {
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const font = 'bold 56px ui-monospace, Menlo, monospace';
  ctx.font = font;
  const w = Math.ceil(ctx.measureText(text).width) + 24;
  c.width = w; c.height = 84;
  ctx.font = font; ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(8,12,18,.72)';
  ctx.beginPath(); ctx.roundRect(2, 6, w - 4, 72, 14); ctx.fill();
  ctx.fillStyle = color; ctx.fillText(text, w / 2, 44);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  sp.scale.set((worldHeight * w) / 84, worldHeight, 1);
  sp.renderOrder = 10;
  return sp;
}

/** 외곽 박스 치수선 (제도식): 보조선 + 사선 눈금 + mm 표기. corners 인덱스 = sx*4+sy*2+sz */
export function dimensionObject(corners, color = '#5ad1ff') {
  const g = new THREE.Group();
  const C = corners.map((c) => new THREE.Vector3(...c));
  const c0 = C[0], cU = C[4], cV = C[1], cH = C[2];
  const L = c0.distanceTo(cU), W = c0.distanceTo(cV), H = c0.distanceTo(cH);
  if (L < 0.01 || H < 0.01) return g;
  const u = cU.clone().sub(c0).normalize(), v = cV.clone().sub(c0).normalize();
  const scale = Math.max(1, Math.max(L, W, H) / 0.3); // 큰 사물은 치수선 간격·글자를 키운다
  const off = 0.05 * scale, gap = 0.008 * scale, over = 0.012 * scale, tick = 0.014 * scale;
  const segs = [];
  const dim = (a, b, d, value) => {
    const a2 = a.clone().addScaledVector(d, off), b2 = b.clone().addScaledVector(d, off);
    segs.push([...a2.toArray(), ...b2.toArray()]);
    segs.push([...a.clone().addScaledVector(d, gap).toArray(), ...a2.clone().addScaledVector(d, over).toArray()]);
    segs.push([...b.clone().addScaledVector(d, gap).toArray(), ...b2.clone().addScaledVector(d, over).toArray()]);
    const td = b.clone().sub(a).normalize().add(d).normalize().multiplyScalar(tick / 2);
    segs.push([...a2.clone().sub(td).toArray(), ...a2.clone().add(td).toArray()]);
    segs.push([...b2.clone().sub(td).toArray(), ...b2.clone().add(td).toArray()]);
    const sp = labelSprite(`${Math.round(value * 1000)}`, color, 0.026 * scale);
    sp.position.copy(a2.clone().add(b2).multiplyScalar(0.5)).addScaledVector(d, 0.02 * scale);
    g.add(sp);
  };
  dim(c0, cU, v.clone().negate(), L);
  dim(c0, cV, u.clone().negate(), W);
  dim(c0, cH, u.clone().add(v).negate().normalize(), H);
  g.add(linesObject(segs, new THREE.Color(color).getHex(), 0.95));
  return g;
}

export function applyPose(obj, pose) {
  if (!pose) return obj;
  const p = pose.positionM || { x: 0, y: 0, z: 0 };
  obj.position.set(p.x, p.y, p.z);
  if (pose.quat) obj.quaternion.set(pose.quat.x, pose.quat.y, pose.quat.z, pose.quat.w);
  else if (pose.rpyDeg) {
    const d = Math.PI / 180;
    obj.quaternion.setFromEuler(new THREE.Euler(pose.rpyDeg.roll * d, pose.rpyDeg.pitch * d, pose.rpyDeg.yaw * d, 'ZYX'));
  }
  return obj;
}

/** 등록 모델(GLB): 표면(mesh)과 점군을 만든다 */
export async function loadModel(url) {
  const gltf = await new GLTFLoader().loadAsync(url);
  const surface = new THREE.Group();
  const pos = [];
  gltf.scene.traverse((o) => {
    if (o.isMesh) {
      o.material = new THREE.MeshStandardMaterial({ color: 0x8fa6bf, roughness: 0.82, metalness: 0.05, flatShading: true, side: THREE.DoubleSide, transparent: true, opacity: 0.92 });
      const a = o.geometry.getAttribute('position');
      for (let i = 0; i < a.count; i += 3) pos.push(a.getX(i), a.getY(i), a.getZ(i)); // 삼각형당 첫 정점만 = surfel 중심 근처
    }
  });
  surface.add(gltf.scene);
  const points = pointsObject(new Float32Array(pos), 0x6fd3ff, 0.006);
  return { surface, points };
}

/** 사진 실루엣 카드 (자산 로컬 프레임): 평면의 +Z가 촬영 카메라 쪽을 향한다 */
export async function cardObject(meta, url) {
  const tex = await new THREE.TextureLoader().loadAsync(url);
  tex.colorSpace = THREE.SRGBColorSpace;
  const m = new THREE.Mesh(new THREE.PlaneGeometry(meta.widthM, meta.heightM),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.92, side: THREE.DoubleSide, depthWrite: false }));
  const r = new THREE.Vector3(...meta.right), u = new THREE.Vector3(...meta.up);
  const z = new THREE.Vector3().crossVectors(r, u);
  m.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(r, u, z));
  m.position.set(...meta.center);
  m.userData = { side: meta.side };
  return m;
}
