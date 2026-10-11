// 3D 디지털 트윈 — 건물 외부 조망 → 출입문 선택 → 실내 워크 투어 / 자유 투어, 레이어·출처 구분·벽두께/포털 검수.
// 데이터: /admin/api/twin/* (docs/INTERFACES.md §6). 백엔드가 없으면(404) 또는 ?mock=1 이면 내장 데모(twinmock.js)로 동작한다.
import { api, ApiError } from '../api.js';
import { h, icon, toast, modal, field, emptyState, pct, fmtDate, decisionBadge } from '../ui.js';
import { TwinEngine, LAYERS, QUALITIES, isMobileDevice } from '../twin3d.js';
import { THREE } from '../viewer3d.js';
import * as mock from '../twinmock.js';

const RANK = { viewer: 0, inspector: 1, manager: 2, admin: 3 };
const SK = { OBSERVED: ['관측', 'ok'], INFERRED: ['추정', 'warn'], DESIGN: ['설계', 'info'], REPLACEMENT: ['치환', 'violet'] };
const REV = { AUTO: ['자동', ''], REVIEW_REQUIRED: ['검수 필요', 'warn'], APPROVED: ['승인', 'ok'], REJECTED: ['반려', 'bad'] };
const METHODS = ['BIM', 'MEASURED', 'CONFIG_DEFAULT', 'INFERRED'];
const METHOD_CLS = { BIM: 'info', MEASURED: 'ok', CONFIG_DEFAULT: '', INFERRED: 'warn' };
const MODES = [['exterior', '외부 조망'], ['entrance', '출입문 선택'], ['walk', '실내 투어'], ['free', '자유 투어']];
const skBadge = (k) => { const x = SK[k] || [k || '—', '']; return h('span', { class: `badge nodot ${x[1]}` }, x[0]); };
const revBadge = (r) => { const x = REV[r] || [r || '—', '']; return h('span', { class: `badge ${x[1]}` }, x[0]); };
const fx = (v, d = 2) => (v == null ? '—' : Number(v).toFixed(d));
const rc = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random()));

export async function twinView(root, params, ctx, alive) {
  let isMock = /[?&]mock=1/.test(location.hash + location.search);
  const canEdit = RANK[ctx.user.role] >= RANK.manager;
  const mockQ = () => (isMock ? '?mock=1' : '');
  root.className = 'twin-root';
  root.append(h('div', { class: 'skeleton', style: { height: '60vh' } }));

  // ───────── 데이터 로드 ─────────
  let buildings, backendMissing = false;
  if (isMock) buildings = mock.mockBuildings();
  else {
    try { buildings = await api('/twin/buildings'); }
    catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) { isMock = true; backendMissing = true; buildings = mock.mockBuildings(); }
      else { rc(root, h('div', { class: 'notice bad' }, `디지털 트윈 목록을 불러오지 못했습니다: ${e.message}`)); return; }
    }
  }
  if (!alive()) return;
  if (!buildings.length) { rc(root, pageHead(), emptyState('등록된 건물이 없습니다', '모바일 앱에서 건물·층을 등록하고 공간을 스캔하면 여기에 표시됩니다.', 'building')); return; }
  const bId = params[0] ? decodeURIComponent(params[0]) : String(buildings[0].id);
  const binfo = buildings.find((b) => String(b.id) === bId) || buildings[0];
  let man;
  try { man = isMock ? mock.mockScene() : await api(`/twin/buildings/${encodeURIComponent(binfo.id)}/scene`); }
  catch (e) { rc(root, pageHead(), h('div', { class: 'notice bad' }, `장면을 불러오지 못했습니다: ${e.message}`)); return; }
  if (!alive()) return;

  function pageHead(extra) {
    return h('div', { class: 'page-h' }, h('div', null, h('h1', null, '3D 디지털 트윈'), h('p', null, '외부 조망 → 출입문 → 실내 투어. 관측·추정·설계·치환 데이터를 구분해 보여 줍니다.')), extra ? h('div', { class: 'actions' }, extra) : null);
  }

  // ───────── 상태 ─────────
  const st = { mode: 'exterior', floorId: man.floors[0]?.id, spaceId: params[1] ? decodeURIComponent(params[1]) : null, wall: null, portal: null, asset: null, tool: null, unobs: false, photo: false, purpose: 'tour', touring: false, tourName: '', entrance: null, ceilingHide: false, sectionOn: false, sectionY: 0, nearPortal: null, from: '', to: '' };
  const spaceDef = (id) => man.spaces.find((s) => s.spaceId === id);
  const floorDef = (id) => man.floors.find((f) => f.id === id);
  const walls = () => man.envelope?.walls || [];
  const topY = Math.max(3, ...man.floors.map((f) => f.elevationM + f.heightM));
  st.sectionY = topY;

  // ───────── DOM ─────────
  const noticeBar = h('div', { class: 'tw-notice hidden', role: 'status' });
  const cv = h('div', { class: 'tw-canvas' });
  const tip = h('div', { class: 'tw-tip hidden' });
  const fps = h('div', { class: 'tw-fps mono' }, 'FPS —');
  const msg = h('div', { class: 'viewer-msg' }, '3D 장면을 준비하는 중…');
  const measureOut = h('span', { class: 'tw-measure hidden mono' });
  const popup = h('div', { class: 'tw-popup hidden' });
  const entrCard = h('div', { class: 'tw-entr hidden' });
  const nearBtn = h('button', { class: 'btn primary sm tw-near hidden', onclick: () => usePortal() });
  const joyKnob = h('i');
  const joy = h('div', { class: 'tw-joy hidden', 'aria-label': '이동 조이스틱' }, joyKnob);
  const lookHint = h('div', { class: 'tw-lookhint hidden' }, '');
  const loadPill = h('div', { class: 'tw-load hidden mono', role: 'status', 'aria-live': 'off' });
  const tourPill = h('div', { class: 'tw-tourpill hidden', role: 'status' });
  const photoBar = h('div', { class: 'tw-photobar' },
    h('button', { class: 'btn sm', title: '스크린샷 저장', onclick: () => shot() }, icon('camera', 16), h('span', { class: 'tw-lbl' }, '캡처')),
    h('button', { class: 'btn sm', title: '사진 모드 종료 (Esc)', onclick: () => setPhoto(false) }, icon('x', 16), h('span', { class: 'tw-lbl' }, '종료')));
  const toolBtns = {}; let tourBtn, infoLine = null;
  const mkTool = (key, ic, title, label) => { const b = h('button', { class: 'btn sm', title, 'aria-label': title, onclick: () => setTool(st.tool === key ? null : key) }, icon(ic, 16), label ? h('span', { class: 'tw-lbl' }, label) : null); toolBtns[key] = b; return b; };
  const toolbar = h('div', { class: 'tw-tools' },
    mkTool('measure', 'fit', '두 점 거리 측정 (mm)', '측정'),
    mkTool('pin', 'pin', '메모 핀 추가 (이 브라우저에만 저장)', '핀'),
    h('button', { class: 'btn sm', title: '화면 전체 맞추기', 'aria-label': '전체 보기', onclick: () => { setMode('exterior'); eng.fitAll(); } }, icon('building', 16), h('span', { class: 'tw-lbl' }, '전체')),
    h('button', { class: 'btn sm', title: '스크린샷 저장', 'aria-label': '스크린샷', onclick: shot }, icon('camera', 16), h('span', { class: 'tw-lbl' }, '캡처')),
    tourBtn = h('button', { class: 'btn sm tw-tourbtn', title: '뷰포인트를 따라 자동 비행 (P=사진 모드)', 'aria-label': '자동 투어', onclick: () => toggleTour() }, icon('eye', 16), h('span', { class: 'tw-lbl' }, '투어')),
    h('button', { class: 'btn sm', title: '사진 모드 — UI·HUD 숨김 (P)', 'aria-label': '사진 모드', onclick: () => setPhoto(true) }, icon('fit', 16), h('span', { class: 'tw-lbl' }, '사진')));
  const legend = h('div', { class: 'tw-legend' }, legendItems());
  const viewer = h('div', { class: 'viewer tw-viewer' }, cv, noticeBar, toolbar, measureOut, fps, popup, entrCard, nearBtn, joy, lookHint, legend, loadPill, tourPill, photoBar, tip, msg);
  const modeBar = h('div', { class: 'tw-modes', role: 'tablist' });
  const floorBar = h('div', { class: 'tw-floors' });
  const side = h('aside', { class: 'tw-side' });
  const handle = h('button', { class: 'tw-handle btn', onclick: () => side.classList.toggle('open') }, icon('menu', 16), h('span', null, '레이어·자산·편집'));

  const bsel = h('select', { 'aria-label': '건물 선택', onchange: (e) => { location.hash = `#/twin/${encodeURIComponent(e.target.value)}${mockQ()}`; } },
    buildings.map((b) => h('option', { value: b.id, selected: String(b.id) === String(binfo.id) }, `${b.name}${b.projectCode ? ` · ${b.projectCode}` : ''}`)));
  const head = pageHead(h('div', { class: 'field', style: { margin: 0, minWidth: '220px' } }, bsel));
  rc(root, head,
    backendMissing ? h('div', { class: 'notice warn', style: { marginBottom: '10px' } }, '백엔드의 /admin/api/twin API가 아직 없어 내장 데모 데이터로 표시합니다.') : null,
    isMock && !backendMissing ? h('div', { class: 'notice warn', style: { marginBottom: '10px' } }, '데모(mock) 모드입니다. 실제 데이터가 아닙니다.') : null,
    h('div', { class: 'tw' }, h('div', { class: 'tw-main' }, h('div', { class: 'tw-bar' }, modeBar, floorBar), viewer), side), handle);

  let ready = false;
  const R = (fn) => (...a) => (ready ? fn(...a) : undefined);
  const eng = new TwinEngine(cv, {
    mock,
    onHover: showTip, onPick, onFps: (f, tri, n) => { fps.textContent = `FPS ${f} · ${(tri / 1000).toFixed(0)}k△ · 공간 ${n}/${man.spaces.length}`; fps.classList.toggle('low', f < 24); },
    onMode: R((m) => { st.mode = m; syncMode(); }), onFloor: R((id) => { st.floorId = id; syncFloors(); }),
    onSpaceEnter: R((sid) => { if (sid) { st.spaceId = sid; renderSpaces(); } }), onLayersChanged: R(() => { syncNotice(); renderLayers(false); }),
    onSpacesChanged: R(() => { renderSpaces(); syncNotice(); }), onStatus: (t, k) => toast(t, k),
    onMeasure: (mm) => { measureOut.classList.toggle('hidden', mm == null); measureOut.textContent = mm == null ? '' : `거리 ${mm.toLocaleString()} mm`; },
    onPin: addPinDialog, onPortalPlace: placePortal, onUse: () => usePortal(),
    onLoadInfo: (i) => { const busy = i.active + i.queued > 0; loadPill.classList.toggle('hidden', !busy); if (busy) loadPill.textContent = `텍스처 로딩 ${i.active}${i.queued ? ` (+${i.queued})` : ''}${i.pct != null ? ` · ${i.pct}%` : ''} · ${i.texMB}/${i.budgetMB} MB`; if (infoLine) infoLine.textContent = `텍스처 메모리 추정 ${i.texMB} / ${i.budgetMB} MB · 화질 ${eng.qualityLabel}${eng.guardSteps ? ` · 성능 보호 ${eng.guardSteps}단계 적용` : ''}`; },
    onTour: (t) => { st.touring = !!t.running; if (t.name) st.tourName = t.name; tourBtn.classList.toggle('primary', st.touring); rc(tourBtn, icon('eye', 16), h('span', { class: 'tw-lbl' }, st.touring ? '정지' : '투어')); tourPill.classList.toggle('hidden', !st.touring); if (st.touring) tourPill.textContent = `투어 ${Math.max(1, (t.i ?? 0) + 1)}/${t.n ?? '—'}${st.tourName ? ` · ${st.tourName}` : ''}`; if (t.done) toast('투어를 마쳤습니다'); },
  });
  if (!eng.ok) { rc(viewer, h('div', { class: 'viewer-msg' }, '이 브라우저에서는 3D(WebGL)를 사용할 수 없습니다.')); return; }
  eng.sceneRef = man;
  window.__twin = eng;   // 디버그/점검용

  // 기본 레이어: 실사 + 외피 + 추정 + 치환
  for (const [k, on, op] of [['textured', true, 1], ['observed', false, 0.9], ['inferred', true, 0.45], ['design', false, 0.6], ['replacement', true, 1], ['coverageHeat', false, 0.55], ['envelope', true, 0.85]]) {
    eng.layers[k].on = on; eng.layers[k].opacity = op;
  }
  await eng.setManifest(man, isMock);
  for (const k of Object.keys(eng.layers)) eng._applyOpacity(k);
  eng.applyLayerState();
  msg.remove();
  eng.fitAll();
  if (st.spaceId && spaceDef(st.spaceId)) eng.selected.spaceId = st.spaceId;

  // ───────── 공통 동작 ─────────
  function legendItems() {
    const it = [['#8fa6bf', '관측 OBSERVED'], ['#ffa94d', '추정 INFERRED'], ['#70757c', '미관측(회색)'], st.unobs ? ['#ff8a10', '미관측 강조(주황 빗금)'] : null, ['#4fd1ff', '설계 DESIGN'], ['#2ecc71', '치환 CAD'], ['#c77dff', '자산'], ['#f5a623', '포털(검수 전)']];
    return it.filter(Boolean).map(([c, t]) => h('span', { style: { '--c': c } }, h('i'), t));
  }
  function syncLegend() { rc(legend, legendItems()); const sl = secLegend.querySelector('.tw-legend'); if (sl) rc(sl, legendItems()); }
  function syncNotice() {
    const parts = [];
    if (man.building?.notice) parts.push(man.building.notice);
    if (eng.inferredVisible && !parts.some((p) => p.includes('추정'))) parts.push('외관/벽두께 일부 추정');
    else if (eng.inferredVisible && !man.building?.notice) parts.push('외관/벽두께 일부 추정');
    noticeBar.classList.toggle('hidden', !parts.length);
    rc(noticeBar, icon('alert', 15), h('span', null, parts.join(' · ')), eng.inferredVisible ? h('span', { class: 'badge nodot warn' }, '추정 레이어 표시 중') : null);
  }
  function showTip(t) {
    if (!t) { tip.classList.add('hidden'); return; }
    const i = t.info, r = cv.getBoundingClientRect();
    rc(tip, h('b', null, i.label || i.kind || '—'),
      h('div', { class: 'tw-tip-r' }, h('span', { class: 'muted' }, 'sourceKind'), skBadge(i.sourceKind)),
      i.confidence != null ? h('div', { class: 'tw-tip-r' }, h('span', { class: 'muted' }, '신뢰도'), h('span', { class: 'mono' }, pct(i.confidence))) : null,
      i.reviewStatus ? h('div', { class: 'tw-tip-r' }, h('span', { class: 'muted' }, '검수'), revBadge(i.reviewStatus)) : null,
      i.unobserved ? h('div', { class: 'tw-tip-warn' }, '미관측 영역 — 값이 추정입니다') : null);
    tip.classList.remove('hidden');
    tip.style.left = `${Math.min(t.x - r.left + 14, r.width - 200)}px`; tip.style.top = `${Math.min(t.y - r.top + 14, r.height - 110)}px`;
  }
  function setTool(t) {
    st.tool = t; eng.setTool(t === 'portalMove' || t === 'portalAdd' ? (t === 'portalAdd' ? 'portal' : 'portalMove') : t);
    for (const [k, b] of Object.entries(toolBtns)) b.classList.toggle('primary', k === t);
    if (t) toast(t === 'measure' ? '측정할 두 점을 차례로 누르세요' : t === 'pin' ? '핀을 놓을 위치를 누르세요' : t === 'portalAdd' ? '문 위치(바닥/벽)를 누르세요' : '새 위치를 누르세요', 'ok', 2200);
    renderPortals();
  }
  async function shot() {
    const b = await eng.screenshotBlob(); if (!b) return;
    const a = h('a', { href: URL.createObjectURL(b), download: `twin-${binfo.id}-${Date.now()}.png` }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast('스크린샷을 저장했습니다');
  }

  // ───────── 모드 ─────────
  function syncMode() {
    rc(modeBar, ...MODES.map(([k, l]) => h('button', { class: `tw-mode ${st.mode === k ? 'active' : ''}`, role: 'tab', 'aria-selected': st.mode === k ? 'true' : 'false', onclick: () => setMode(k) }, l)));
    const walking = st.mode === 'walk' || (st.mode === 'free' && eng.fly);
    const touchUI = isMobileDevice();
    joy.classList.toggle('hidden', !(walking && touchUI));
    lookHint.classList.toggle('hidden', !walking);
    lookHint.textContent = touchUI ? '왼쪽 조이스틱: 이동 · 화면 드래그: 시선' : 'WASD 이동 · 화면 드래그 또는 더블클릭(포인터 잠금) 시선 · Shift 달리기 · E 문 이동 · Q/Z 회전';
    entrCard.classList.toggle('hidden', st.mode !== 'entrance'); if (st.mode === 'entrance') renderEntrances();
    renderOptions(); syncFloors();
  }
  function setMode(m) {
    if (m === st.mode && m !== 'walk') return;
    hidePopup();
    if (m === 'walk') { if (!eng._walkInit) return startWalkDefault(); eng.teleport(eng.pos.toArray(), null); }
    else if (m === 'free') { eng.setMode('free'); }
    else { eng.setMode(m); if (m === 'exterior' || m === 'entrance') eng.fitAll(); }
    st.mode = eng.mode; syncMode(); applyClip();
  }
  function startWalkDefault() {
    const e = (man.entrances || [])[0];
    if (st.spaceId) return walkInSpace(st.spaceId);
    if (e) return walkAtEntrance(e);
    const s = man.spaces[0]; if (s) walkInSpace(s.spaceId);
  }
  function walkAtEntrance(e) {
    const a = ((e.yawDeg || 0) * Math.PI) / 180, p = e.positionBuilding;
    eng.teleport([p[0] - Math.sin(a) * 0.9, p[1], p[2] - Math.cos(a) * 0.9], e.yawDeg || 0);
    if (e.spaceId) { st.spaceId = e.spaceId; eng.selected.spaceId = e.spaceId; eng.loadSpace(e.spaceId); }
    st.mode = 'walk'; syncMode(); applyClip(); renderSpaces();
  }
  function walkInSpace(sid) {
    const s = eng.spaces.get(sid); if (!s) return;
    const c = s.bbox.getCenter(new THREE.Vector3());
    st.spaceId = sid; eng.selected.spaceId = sid; eng.loadSpace(sid);
    eng.teleport([c.x, s.bbox.min.y, c.z], 0, s.def.floorId);
    st.mode = 'walk'; syncMode(); applyClip(); renderSpaces();
  }
  function renderEntrances() {
    const es = man.entrances || [];
    rc(entrCard, h('b', null, '출입문을 선택하세요'), h('div', { class: 'muted', style: { fontSize: '12px' } }, '지도의 파란 화살표를 누르거나 목록에서 고르세요.'),
      es.length ? es.map((e) => h('div', { class: `tw-entr-row ${st.entrance === e.id ? 'sel' : ''}` }, h('span', null, `${e.name}`, h('small', { class: 'muted' }, ` ${e.spaceId || ''}`)),
        h('span', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, h('button', { class: 'btn sm', onclick: () => previewEntrance(e) }, '전경'), h('button', { class: 'btn sm primary', onclick: () => walkAtEntrance(e) }, '입장')))) : h('div', { class: 'muted' }, '등록된 출입문이 없습니다. 공간 목록에서 직접 입장하세요.'));
  }
  function previewEntrance(e) { st.entrance = e.id; eng.flyTo(e.exteriorViewpoint || [e.positionBuilding[0], 2, e.positionBuilding[2] + 8], [e.positionBuilding[0], 1.4, e.positionBuilding[2]]); renderEntrances(); }

  // 단면/천장 숨김 → 렌더러 클리핑 평면 하나로 처리
  function applyClip() {
    const f = floorDef(st.floorId);
    if (st.ceilingHide && f && (st.mode === 'walk' || (st.mode === 'free' && eng.fly))) eng.setSection(true, f.elevationM + f.heightM - 0.06);
    else if (st.sectionOn) eng.setSection(true, st.sectionY);
    else eng.setSection(false);
  }
  function syncFloors() {
    rc(floorBar, ...[...man.floors].sort((a, b) => b.elevationM - a.elevationM).map((f) => h('button', { class: `chip ${st.floorId === f.id ? 'on' : ''}`, onclick: () => gotoFloor(f.id), title: `${f.level} (+${f.elevationM} m)` }, f.level)));
  }
  function gotoFloor(id) {
    st.floorId = id; syncFloors(); renderSpaces();
    const sp = man.spaces.find((s) => s.floorId === id);
    if (st.mode === 'walk' && sp) { walkInSpace(sp.spaceId); return; }
    const f = floorDef(id); if (!f) return;
    const box = eng.sceneBox?.clone(); if (box) { box.min.y = f.elevationM; box.max.y = f.elevationM + f.heightM; eng.focusBox(box); }
    applyClip();
  }

  // ───────── 포털 이동 ─────────
  function nearestPortal() {
    let best = null, bd = 2.6;
    for (const p of man.portals || []) {
      if (p.reviewStatus === 'REJECTED' || p.state === 'CLOSED') continue;
      const d = Math.hypot(p.positionBuilding[0] - eng.pos.x, p.positionBuilding[2] - eng.pos.z);
      if (d < bd && Math.abs(p.positionBuilding[1] - eng.pos.y) < 1.5) { bd = d; best = p; }
    }
    return best;
  }
  function usePortal(p) {
    p = p || nearestPortal(); if (!p) return;
    const a = ((p.yawDeg || 0) * Math.PI) / 180, fx_ = -Math.sin(a), fz_ = -Math.cos(a);
    const along = (eng.pos.x - p.positionBuilding[0]) * fx_ + (eng.pos.z - p.positionBuilding[2]) * fz_;
    const forward = along <= 0;                       // 호출한 쪽이 from 쪽이면 앞(to 쪽)으로
    const dest = forward ? p.toSpace : p.fromSpace, sd = dest && spaceDef(dest);
    if (sd && sd.floorId !== floorOfSpaceNow()) return walkInSpace(dest);   // 층이 다른 포털(계단/엘리베이터) → 도착 공간 중심
    const k = forward ? 1 : -1;
    eng.teleport([p.positionBuilding[0] + fx_ * 0.9 * k, p.positionBuilding[1], p.positionBuilding[2] + fz_ * 0.9 * k], (p.yawDeg || 0) + (forward ? 0 : 180));
    if (dest) { st.spaceId = dest; eng.selected.spaceId = dest; eng.loadSpace(dest); renderSpaces(); }
  }
  const floorOfSpaceNow = () => eng.floorOf(eng.pos.y)?.id;
  const portalTimer = setInterval(() => {
    if (!viewer.isConnected) { clearInterval(portalTimer); return; }
    if (st.mode !== 'walk') { nearBtn.classList.add('hidden'); return; }
    const p = nearestPortal(); st.nearPortal = p;
    nearBtn.classList.toggle('hidden', !p);
    if (p) rc(nearBtn, icon('chevron', 14), `${p.toSpace ? spaceDef(p.toSpace)?.name || p.toSpace : '외부'} 로 이동 (E)`);
  }, 400);

  // 조이스틱
  {
    let id = null; const R = 44;
    const upd = (e) => { const r = joy.getBoundingClientRect(); let dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2); const l = Math.hypot(dx, dy); if (l > R) { dx *= R / l; dy *= R / l; } joyKnob.style.transform = `translate(${dx}px,${dy}px)`; eng.joy.x = dx / R; eng.joy.y = -dy / R; };
    joy.addEventListener('pointerdown', (e) => { id = e.pointerId; try { joy.setPointerCapture(id); } catch { /* 합성 이벤트 */ } upd(e); e.preventDefault(); });
    joy.addEventListener('pointermove', (e) => { if (e.pointerId === id) upd(e); });
    const end = (e) => { if (e.pointerId !== id) return; id = null; joyKnob.style.transform = ''; eng.joy.x = 0; eng.joy.y = 0; };
    joy.addEventListener('pointerup', end); joy.addEventListener('pointercancel', end);
  }

  // ───────── 클릭 선택 ─────────
  function onPick(info) {
    if (info.kind === 'entrance') { const e = man.entrances.find((x) => x.id === info.id); if (e) { if (st.mode === 'exterior') setMode('entrance'); previewEntrance(e); } return; }
    if (info.kind === 'asset') return selectAsset(info.assetId, false);
    if (info.kind === 'wall') return selectWall(info.wallId);
    if (info.kind === 'portal') return selectPortal(info.portalId);
    if (info.spaceId) selectSpace(info.spaceId, false);
  }

  // ───────── 사이드 패널 ─────────
  const secs = {};
  const section = (key, title, open = true) => { const body = h('div', { class: 'tw-sec-b' }); const d = h('details', { class: 'tw-sec', open }, h('summary', null, title), body); secs[key] = { d, body }; side.append(d); return body; };
  const secBuilding = section('b', '건물 · 층 · 공간');
  const secOpts = section('o', '이동·단면 옵션');
  const secView = section('v', '화질 · 카메라 · 투어');
  const secLayers = section('l', '레이어');
  const secAssets = section('a', '자산');
  const secWalls = section('w', '외피 벽 · 두께', false);
  const secPortals = section('p', '포털 (문)', false);
  const secPins = section('n', '메모 핀', false);
  const secLegend = section('g', '범례');
  secLegend.classList.add('tw-only-m'); secs.g.d.classList.add('tw-only-m');
  secLegend.append(h('div', { class: 'tw-legend static' }, legendItems()));

  // 공간
  let revs = [];
  function spaceRows(f) {
    const info = (binfo.floors || []).find((x) => x.id === f.id);
    const list = info?.spaces?.length ? info.spaces : man.spaces.filter((s) => s.floorId === f.id).map((s) => ({ spaceId: s.spaceId, name: s.name, coverage: s.coverage, hasMesh: true, quality: s.quality }));
    return list.map((s) => h('div', { class: `tw-row ${st.spaceId === s.spaceId ? 'sel' : ''}`, role: 'button', tabindex: 0, onclick: () => selectSpace(s.spaceId, true), onkeydown: (e) => { if (e.key === 'Enter') selectSpace(s.spaceId, true); } },
      h('div', { class: 'tw-row-t' }, h('b', null, s.name || s.spaceId), h('small', { class: 'mono muted' }, s.spaceId)),
      h('div', { class: 'tw-row-m' }, s.hasMesh === false ? h('span', { class: 'badge nodot' }, '메시 없음') : h('span', { class: `badge nodot ${(s.coverage ?? 0) >= 0.8 ? 'ok' : (s.coverage ?? 0) >= 0.5 ? 'warn' : 'bad'}` }, `커버 ${pct(s.coverage)}`),
        s.reviewStatus ? revBadge(s.reviewStatus) : null, eng.spaces.get(s.spaceId)?.loaded ? h('span', { class: 'badge nodot info', title: '3D 적재됨' }, '적재') : null)));
  }
  function renderSpaces() {
    const cur = spaceDef(st.spaceId);
    rc(secBuilding, 
      h('div', { class: 'tw-kv' }, h('b', null, binfo.name), h('span', { class: 'muted' }, `${binfo.projectCode || ''} · 출입구 ${man.entrances?.length ?? binfo.entranceCount ?? 0} · 외피 ${binfo.hasEnvelope === false ? '없음' : '있음'}`)),
      [...man.floors].sort((a, b) => b.elevationM - a.elevationM).map((f) => h('div', { class: 'tw-floor' }, h('div', { class: 'tw-floor-h' }, `${f.level}`, h('small', { class: 'muted' }, ` +${f.elevationM} m · 높이 ${f.heightM} m`)), spaceRows(f))),
      cur ? h('div', { class: 'tw-revs' },
        h('div', { class: 'field', style: { margin: '10px 0 0' } }, h('label', null, `리비전 — ${cur.name}`),
          h('select', { id: 'tw-rev', onchange: (e) => pickRevision(e.target.value) }, revs.length ? revs.map((r) => h('option', { value: r.revisionId, selected: (eng.spaces.get(cur.spaceId)?.revision?.revisionId || cur.revisionId) === r.revisionId }, `${fmtDate(r.createdAt)} · 커버 ${pct(r.coverage)}${r.approved ? ' · 승인' : ''}`)) : h('option', null, cur.revisionId ? `현재: ${String(cur.revisionId).slice(0, 8)}` : '리비전 없음'))),
        h('div', { class: 'row', style: { marginTop: '8px' } }, h('button', { class: 'btn sm', onclick: () => walkInSpace(cur.spaceId) }, icon('eye', 14), '여기서 투어'), h('button', { class: 'btn sm', onclick: () => { setMode('exterior'); eng.focusBox(eng.spaces.get(cur.spaceId).bbox); } }, '공간 보기'))) : h('div', { class: 'muted', style: { marginTop: '8px', fontSize: '12px' } }, '공간을 선택하면 리비전을 고를 수 있습니다.'));
  }
  async function selectSpace(sid, fly) {
    st.spaceId = sid; eng.selected.spaceId = sid; eng.loadSpace(sid);
    const s = spaceDef(sid); if (!s) return;
    if (fly) { if (st.mode === 'walk') walkInSpace(sid); else if (st.mode !== 'entrance') eng.focusBox(eng.spaces.get(sid).bbox); }
    revs = [];
    try { revs = isMock ? mock.mockRevisions(sid) : await api(`/twin/spaces/${encodeURIComponent(sid)}/revisions`); } catch { revs = []; }
    if (st.spaceId === sid) renderSpaces();
  }
  function pickRevision(id) {
    const r = revs.find((x) => x.revisionId === id); if (!r) return;
    eng.setRevision(st.spaceId, r); toast(`리비전 ${fmtDate(r.createdAt)} 적용${r.approved ? '' : ' (미승인)'}`, r.approved ? 'ok' : 'warn');
  }

  // 옵션
  function renderOptions() {
    const walking = st.mode === 'walk' || (st.mode === 'free' && eng.fly);
    const sv = h('span', { class: 'mono' }, `${st.sectionY.toFixed(2)} m`);
    rc(secOpts, 
      st.mode === 'free' ? h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: eng.fly, onchange: (e) => { eng.setFly(e.target.checked); syncMode(); applyClip(); } }), '비행 모드 (WASD + Space/C 상하)') : null,
      h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: st.ceilingHide, onchange: (e) => { st.ceilingHide = e.target.checked; applyClip(); } }), `천장 숨김${walking ? '' : ' (투어 중에만 적용)'}`),
      h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: st.sectionOn, onchange: (e) => { st.sectionOn = e.target.checked; applyClip(); } }), '단면(수평 절단) 사용'),
      h('div', { class: 'tw-slide' }, h('span', { class: 'muted' }, '높이'), h('input', { type: 'range', min: 0, max: topY, step: 0.05, value: st.sectionY, 'aria-label': '단면 높이', oninput: (e) => { st.sectionY = Number(e.target.value); sv.textContent = `${st.sectionY.toFixed(2)} m`; if (st.sectionOn) applyClip(); } }), sv),
      h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: eng.maskOn, onchange: (e) => eng.setMask(e.target.checked) }), `이중 표면 제거 (자산 영역 ${eng.maskCount ?? 0}곳)`),
      h('div', { class: 'tw-hint' }, '추정·설계 레이어는 실제 관측이 아닙니다. 마우스를 올리면 출처와 신뢰도가 표시됩니다.'));
  }

  // 화질 · 카메라 · 투어 · 사진 모드 (정적 DOM — 슬라이더 조작 중 다시 그리지 않는다)
  function tourPoints() {
    const vps = (man.viewpoints || []).filter((v) => v.position && v.target).map((v) => ({ position: v.position, target: v.target, name: v.name }));
    if (vps.length) return vps;
    const pts = [];   // 매니페스트에 viewpoints 가 없으면 출입구 외관 → 공간별 모서리 시점으로 자동 구성
    for (const e of man.entrances || []) if (e.exteriorViewpoint) pts.push({ position: e.exteriorViewpoint, target: [e.positionBuilding[0], e.positionBuilding[1] + 1.4, e.positionBuilding[2]], name: `${e.name} 외관` });
    const sps = [...man.spaces].sort((a, b) => (floorDef(a.floorId)?.elevationM ?? 0) - (floorDef(b.floorId)?.elevationM ?? 0));
    for (const sp of sps) {
      const sc = eng.spaces.get(sp.spaceId); if (!sc) continue; const bb = sc.bbox, sz = bb.getSize(new THREE.Vector3()), fl = floorDef(sp.floorId)?.elevationM ?? bb.min.y;
      pts.push({ position: [bb.min.x + sz.x * 0.2, fl + 1.6, bb.min.z + sz.z * 0.2], target: [bb.max.x - sz.x * 0.25, fl + 1.2, bb.max.z - sz.z * 0.25], name: sp.name });
    }
    return pts;
  }
  function toggleTour() {
    if (st.touring) { eng.stopTour(); return; }
    const pts = tourPoints(); if (!pts.length) { toast('투어할 뷰포인트가 없습니다', 'warn'); return; }
    if (st.mode === 'exterior' || st.mode === 'entrance') { eng.setMode('free'); st.mode = 'free'; syncMode(); }
    hidePopup(); eng.startTour(pts); toast('자동 투어 시작 — 화면을 드래그하거나 이동키를 누르면 멈춥니다', 'ok', 2400);
  }
  function setPhoto(on) {
    st.photo = on; root.classList.toggle('tw-photo', on); eng.setPhoto(on); if (on) { setTool(null); hidePopup(); tip.classList.add('hidden'); side.classList.remove('open'); } syncNotice();
    if (on) toast('사진 모드 — Esc 로 종료', 'ok', 1800);
  }
  function renderView() {
    const q = h('select', { 'aria-label': '화질', onchange: (e) => { eng.setQuality(e.target.value); toast(`화질: ${eng.qualityLabel}${e.target.value === 'low' ? '' : ' (MSAA 변경은 새로고침 후 적용)'}`, 'ok', 2200); } },
      QUALITIES.map(([k, l]) => h('option', { value: k, selected: eng.qualityPref === k }, l)));
    const fv = h('span', { class: 'mono' }, `${Math.round(eng.camera.fov)}°`), sv = h('span', { class: 'mono' }, `×${eng.lookSens.toFixed(1)}`);
    const purpose = h('select', { 'aria-label': '보기 목적', onchange: (e) => { st.purpose = e.target.value; eng.setPurpose(st.purpose); st.unobs = eng.unobservedOn; unobsChk.checked = st.unobs; syncLegend(); } },
      [['tour', '투어 (자연스러운 사진)'], ['review', '검수 (미관측 강조)']].map(([k, l]) => h('option', { value: k, selected: st.purpose === k }, l)));
    const unobsChk = h('input', { type: 'checkbox', checked: st.unobs, onchange: (e) => { st.unobs = e.target.checked; eng.setUnobserved(st.unobs); syncLegend(); } });
    infoLine = h('div', { class: 'tw-hint mono' }, `텍스처 메모리 추정 0 / ${eng.texBudgetMB} MB · 화질 ${eng.qualityLabel}`);
    rc(secView,
      h('div', { class: 'field', style: { margin: '2px 0 8px' } }, h('label', null, '화질'), q),
      h('div', { class: 'tw-slide' }, h('span', { class: 'muted' }, '시야'), h('input', { type: 'range', min: 60, max: 90, step: 1, value: Math.round(eng.camera.fov), 'aria-label': '시야각(FOV)', oninput: (e) => { eng.setFov(e.target.value); fv.textContent = `${Math.round(eng.camera.fov)}°`; } }), fv),
      h('div', { class: 'tw-slide' }, h('span', { class: 'muted' }, '감도'), h('input', { type: 'range', min: 0.4, max: 2.5, step: 0.1, value: eng.lookSens, 'aria-label': '마우스 시선 감도', oninput: (e) => { eng.setLookSens(e.target.value); sv.textContent = `×${eng.lookSens.toFixed(1)}`; } }), sv),
      h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: eng.headBob, onchange: (e) => eng.setHeadBob(e.target.checked) }), '걸을 때 헤드밥 (기본 꺼짐)'),
      h('div', { class: 'field', style: { margin: '8px 0 4px' } }, h('label', null, '보기 목적'), purpose),
      h('label', { class: 'tw-chk' }, unobsChk, '미관측 영역 강조 (회색 텍셀 → 주황 빗금)'),
      h('div', { class: 'tw-hint' }, '실사 텍스처에서 회색(128)으로 남은 곳은 촬영되지 않은 영역이며 만들어 채우지 않았습니다. 투어에서는 꺼 두고, 검수에서는 켜서 확인하세요.'),
      h('div', { class: 'row', style: { gap: '6px', marginTop: '10px' } },
        h('button', { class: 'btn sm', onclick: () => toggleTour() }, icon('eye', 14), '자동 투어 시작/정지'),
        h('button', { class: 'btn sm', onclick: () => setPhoto(true) }, icon('fit', 14), '사진 모드')),
      infoLine);
  }

  // 레이어
  function renderLayers(full = true) {
    const keep = secLayers.querySelector('input:focus');
    if (!full && keep) return;   // 슬라이더 조작 중에는 다시 그리지 않는다
    rc(secLayers, ...LAYERS.map((l) => {
      const L = eng.layers[l.key];
      return h('div', { class: 'tw-layer' },
        h('label', { class: 'tw-chk' }, h('input', { type: 'checkbox', checked: L.on, onchange: (e) => eng.setLayer(l.key, e.target.checked) }), h('i', { class: 'dot', style: { background: l.color } }), l.label,
          l.sk === 'INFERRED' ? h('span', { class: 'badge nodot warn' }, '추정') : l.sk === 'DESIGN' ? h('span', { class: 'badge nodot info' }, '설계') : null),
        h('input', { type: 'range', min: 0.1, max: 1, step: 0.05, value: L.opacity, 'aria-label': `${l.label} 투명도`, oninput: (e) => eng.setLayer(l.key, null, Number(e.target.value)) }));
    }), h('div', { class: 'tw-hint' }, `기기 최적화: ${eng.mobile ? '모바일 LOD(lod.mobile) 사용' : '웹 LOD 사용'} · 공간은 가까이 가거나 선택할 때만 불러옵니다.`));
  }

  // 자산
  let assetQ = '';
  function renderAssets() {
    const q = assetQ.toLowerCase();
    const list = (man.assets || []).filter((a) => !q || `${a.assetId} ${a.name} ${a.spaceId}`.toLowerCase().includes(q));
    rc(secAssets, 
      h('div', { class: 'search', style: { marginBottom: '8px' } }, icon('search', 16), h('input', { placeholder: '자산 검색', value: assetQ, oninput: (e) => { assetQ = e.target.value; const p = e.target.selectionStart; renderAssets(); const i = secAssets.querySelector('input'); i.focus(); i.setSelectionRange(p, p); } })),
      list.length ? list.map((a) => h('div', { class: `tw-row ${st.asset === a.assetId ? 'sel' : ''}`, role: 'button', tabindex: 0, onclick: () => selectAsset(a.assetId, true), onkeydown: (e) => { if (e.key === 'Enter') selectAsset(a.assetId, true); } },
        h('div', { class: 'tw-row-t' }, h('b', null, a.name || a.assetId), h('small', { class: 'mono muted' }, `${a.assetId} · ${a.spaceId || '—'}`)),
        h('div', { class: 'tw-row-m' }, a.extractedMask ? h('span', { class: 'badge nodot', title: '공간 메시에서 이 자산 영역을 숨깁니다' }, '이중표면 제거') : null,
          a.replaced || a.cadGlbUrl ? h('label', { class: 'tw-chk sm', onclick: (e) => e.stopPropagation(), title: 'CAD 치환 모델 표시' }, h('input', { type: 'checkbox', checked: eng.assets.get(a.assetId)?.showCad, onchange: (e) => eng.setAssetCad(a.assetId, e.target.checked) }), 'CAD 치환') : null))) : h('div', { class: 'muted' }, '자산이 없습니다.'));
  }
  function hidePopup() { popup.classList.add('hidden'); eng.clearSel('asset'); st.asset = null; }
  async function selectAsset(id, fly) {
    const a = (man.assets || []).find((x) => x.assetId === id); if (!a) return;
    st.asset = id; eng.highlightAsset(id); eng.ensureAssetModel(id); renderAssets();
    if (fly && st.mode !== 'walk') eng.focusBox(eng.assets.get(id).bb.clone().expandByScalar(1.2));
    const sz = ['min', 'max'].map((k) => a.bboxBuilding[k]); const dim = [0, 1, 2].map((i) => Math.round((sz[1][i] - sz[0][i]) * 1000));
    const hist = h('div', { class: 'muted' }, '불러오는 중…');
    rc(popup, 
      h('div', { class: 'tw-popup-h' }, h('div', null, h('b', null, a.name || a.assetId), h('div', { class: 'mono muted', style: { fontSize: '11.5px' } }, a.assetId)), h('button', { class: 'btn icon ghost sm', 'aria-label': '닫기', onclick: hidePopup }, icon('x', 16))),
      h('dl', { class: 'kv tw-dl' }, h('dt', null, '위치'), h('dd', { class: 'mono' }, a.positionBuilding.map((v) => fx(v)).join(', ') + ' m'), h('dt', null, '치수'), h('dd', { class: 'mono' }, `${dim[0]}×${dim[1]}×${dim[2]} mm`),
        h('dt', null, '속성'), h('dd', null, h('span', { class: 'row', style: { gap: '4px' } }, skBadge(a.replaced ? 'REPLACEMENT' : 'OBSERVED'), a.extractedMask ? h('span', { class: 'badge nodot' }, '공간 메시 분리됨') : null, `공간 ${spaceDef(a.spaceId)?.name || a.spaceId || '—'}`)),
        h('dt', null, '점검이력'), h('dd', null, hist)),
      h('div', { class: 'tw-links' }, h('a', { class: 'btn sm', href: `#/assets/${encodeURIComponent(a.assetId)}` }, '자산 상세'), h('a', { class: 'btn sm', href: '#/inspections' }, '검수 이력'), h('a', { class: 'btn sm', href: '#/issues' }, '이슈·조치')));
    popup.classList.remove('hidden');
    try {
      const rows = isMock ? (mock.MOCK_INSPECTIONS[id] || []) : ((await api(`/assets/${encodeURIComponent(id)}`)).recentInspections || []);
      rc(hist, ...(rows.length ? rows.slice(0, 4).map((o) => h('div', { class: 'row', style: { gap: '6px' } }, decisionBadge(o.decision), h('a', { href: `#/inspections/${o.id}` }, fmtDate(o.createdAt)))) : [h('span', { class: 'muted' }, '이력 없음')]));
    } catch { hist.textContent = '불러오지 못했습니다'; }
  }

  // 벽
  const patchLocal = (obj, resp, keys) => { if (resp && typeof resp === 'object') for (const k of keys) if (resp[k] !== undefined) obj[k] = resp[k]; };
  function renderWalls() {
    const w = walls().find((x) => x.id === st.wall);
    const editor = w ? (() => {
      const t = h('input', { type: 'number', min: 0.02, max: 2, step: 0.01, value: w.thicknessM, 'aria-label': '두께(m)' });
      const m = h('select', null, METHODS.map((x) => h('option', { value: x, selected: x === (w.thicknessMethod === 'CONFIG_DEFAULT' ? 'MEASURED' : w.thicknessMethod) }, x)));
      const n = h('input', { placeholder: '메모(근거)' });
      return h('div', { class: 'tw-edit' },
        h('div', { class: 'row', style: { gap: '6px' } }, h('b', null, `벽 #${w.id}`), h('span', { class: 'badge nodot' }, w.kind === 'EXTERIOR' ? '외벽' : '내벽'), h('span', { class: `badge nodot ${METHOD_CLS[w.thicknessMethod] || ''}` }, w.thicknessMethod), revBadge(w.reviewStatus), h('span', { class: 'mono muted' }, `신뢰도 ${pct(w.confidence)}`)),
        w.thicknessMethod !== 'BIM' && w.thicknessMethod !== 'MEASURED' ? h('div', { class: 'notice warn', style: { padding: '6px 10px', fontSize: '12px' } }, '두께가 추정값입니다. 실측/BIM 값으로 갱신하세요.') : null,
        h('div', { class: 'row' }, field('두께 (m)', t), field('방법', m)), field('메모', n),
        h('div', { class: 'row', style: { gap: '6px' } },
          h('button', { class: 'btn sm primary', disabled: !canEdit, onclick: async (e) => { e.currentTarget.disabled = true; try { const body = { thicknessM: Number(t.value), method: m.value, note: n.value }; const r = isMock ? null : await api(`/twin/walls/${w.id}/thickness`, { method: 'PATCH', body }); w.thicknessM = body.thicknessM; w.thicknessMethod = body.method; if (isMock) { w.confidence = 0.9; } patchLocal(w, r, ['thicknessM', 'thicknessMethod', 'confidence', 'reviewStatus']); toast('두께를 저장했습니다 (외피 메시는 재생성 후 반영)'); } catch (ex) { toast(ex.message, 'bad'); } renderWalls(); } }, '저장'),
          canEdit ? [h('button', { class: 'btn sm ok', onclick: () => reviewWall(w, 'APPROVED') }, '승인'), h('button', { class: 'btn sm danger', onclick: () => reviewWall(w, 'REJECTED') }, '반려')] : h('span', { class: 'muted', style: { fontSize: '12px' } }, '수정·승인은 현장관리자 이상')));
    })() : h('div', { class: 'muted', style: { fontSize: '12px' } }, '3D에서 외피 벽을 누르거나 목록에서 선택하세요.');
    rc(secWalls, 
      canEdit ? h('div', { class: 'row', style: { gap: '6px', marginBottom: '8px' } }, h('input', { id: 'tw-thk', type: 'number', min: 0.05, max: 1, step: 0.01, value: 0.2, style: { width: '84px' }, 'aria-label': '기본 두께' }),
        h('button', { class: 'btn sm', onclick: genEnvelope }, '추정 외피 생성')) : null,
      walls().length ? h('div', { class: 'tw-wl' }, walls().map((x) => h('div', { class: `tw-row compact ${st.wall === x.id ? 'sel' : ''}`, role: 'button', tabindex: 0, onclick: () => selectWall(x.id), onkeydown: (e) => { if (e.key === 'Enter') selectWall(x.id); } },
        h('div', { class: 'tw-row-t' }, h('b', null, `#${x.id} ${x.kind === 'EXTERIOR' ? '외벽' : '내벽'}`), h('small', { class: 'mono muted' }, `${x.thicknessM} m`)),
        h('div', { class: 'tw-row-m' }, h('span', { class: `badge nodot ${METHOD_CLS[x.thicknessMethod] || ''}` }, x.thicknessMethod), revBadge(x.reviewStatus))))) : h('div', { class: 'muted' }, '외피가 없습니다.'),
      editor);
  }
  function selectWall(id) { st.wall = id; eng.highlightWall(id); secs.w.d.open = true; renderWalls(); if (isMobileDevice()) toast(`벽 #${id} 선택 — 패널에서 두께 편집`, 'ok', 1800); }
  async function reviewWall(w, reviewStatus) {
    try { const r = isMock ? null : await api(`/twin/walls/${w.id}/review`, { method: 'POST', body: { reviewStatus } }); w.reviewStatus = reviewStatus; patchLocal(w, r, ['reviewStatus']); toast(reviewStatus === 'APPROVED' ? '승인했습니다' : '반려했습니다'); }
    catch (ex) { toast(ex.message, 'bad'); }
    renderWalls();
  }
  async function genEnvelope() {
    const v = Number(document.getElementById('tw-thk').value) || 0.2;
    try { if (isMock) { toast('데모: 추정 외피 생성 요청을 보냈다고 가정합니다', 'warn'); return; } await api(`/twin/buildings/${binfo.id}/generate-envelope`, { method: 'POST', body: { defaultThicknessM: v } }); toast('추정 외피를 생성했습니다. 화면을 새로 고칩니다.'); setTimeout(() => location.reload(), 600); }
    catch (ex) { toast(ex.message, 'bad'); }
  }

  // 포털
  function renderPortals() {
    const p = (man.portals || []).find((x) => x.id === st.portal);
    const spOpts = (v) => [h('option', { value: '' }, v === 'to' ? '도착: 미지정' : '출발: 현재 공간'), ...man.spaces.map((s) => h('option', { value: s.spaceId, selected: s.spaceId === st[v] }, s.name))];
    rc(secPortals, 
      canEdit ? h('div', { class: 'tw-edit' }, h('div', { class: 'row', style: { gap: '6px' } }, h('select', { onchange: (e) => { st.from = e.target.value; }, 'aria-label': '출발 공간' }, spOpts('from')), h('select', { onchange: (e) => { st.to = e.target.value; }, 'aria-label': '도착 공간' }, spOpts('to'))),
        h('div', { class: 'row', style: { gap: '6px', marginTop: '6px' } }, h('button', { class: `btn sm ${st.tool === 'portalAdd' ? 'primary' : ''}`, onclick: () => setTool(st.tool === 'portalAdd' ? null : 'portalAdd') }, icon('plus', 14), '포털 추가 (3D에서 위치 클릭)'))) : h('div', { class: 'muted', style: { fontSize: '12px' } }, '포털 편집은 현장관리자 이상'),
      (man.portals || []).map((x) => h('div', { class: `tw-row compact ${st.portal === x.id ? 'sel' : ''}`, role: 'button', tabindex: 0, onclick: () => selectPortal(x.id) },
        h('div', { class: 'tw-row-t' }, h('b', null, `${spaceDef(x.fromSpace)?.name || x.fromSpace || '외부'} → ${spaceDef(x.toSpace)?.name || x.toSpace || '?'}`), h('small', { class: 'mono muted' }, `${x.widthM}×${x.heightM} m · ${x.state}`)),
        h('div', { class: 'tw-row-m' }, revBadge(x.reviewStatus), st.mode === 'walk' ? h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); usePortal(x); } }, '이동') : null))),
      p ? (() => {
        const wi = h('input', { type: 'number', step: 0.05, min: 0.4, max: 4, value: p.widthM }), hi = h('input', { type: 'number', step: 0.05, min: 1, max: 5, value: p.heightM });
        const sts = h('select', null, ['OPEN', 'CLOSED', 'UNKNOWN'].map((s) => h('option', { value: s, selected: s === p.state }, s)));
        return h('div', { class: 'tw-edit' }, h('b', null, `포털 ${String(p.id).slice(0, 8)}`),
          h('div', { class: 'row' }, field('폭 (m)', wi), field('높이 (m)', hi), field('상태', sts)),
          canEdit ? h('div', { class: 'row', style: { gap: '6px' } },
            h('button', { class: 'btn sm primary', onclick: () => patchPortal(p, { widthM: Number(wi.value), heightM: Number(hi.value), state: sts.value }) }, '저장'),
            h('button', { class: `btn sm ${st.tool === 'portalMove' ? 'primary' : ''}`, onclick: () => setTool(st.tool === 'portalMove' ? null : 'portalMove') }, '위치 이동'),
            h('button', { class: 'btn sm ok', onclick: () => patchPortal(p, { reviewStatus: 'APPROVED' }) }, '승인'), h('button', { class: 'btn sm danger', onclick: () => patchPortal(p, { reviewStatus: 'REJECTED' }) }, '반려')) : null);
      })() : null);
  }
  function selectPortal(id) { st.portal = id; eng.highlightPortal(id); secs.p.d.open = true; renderPortals(); }
  async function patchPortal(p, body) {
    try { const r = isMock ? null : await api(`/twin/portals/${encodeURIComponent(p.id)}`, { method: 'PATCH', body }); Object.assign(p, body); patchLocal(p, r, ['widthM', 'heightM', 'state', 'reviewStatus', 'positionBuilding', 'yawDeg']); eng.refreshPortals(); eng.highlightPortal(p.id); toast('포털을 저장했습니다'); }
    catch (ex) { toast(ex.message, 'bad'); }
    renderPortals();
  }
  async function placePortal({ point, yawDeg, spaceId, move }) {
    try {
      if (move && st.portal) { const p = man.portals.find((x) => x.id === st.portal); await patchPortal(p, { positionBuilding: point, yawDeg }); setTool(null); return; }
      const body = { fromSpace: st.from || spaceId || null, toSpace: st.to || null, positionBuilding: point, yawDeg, widthM: 0.9, heightM: 2.1 };
      const r = isMock ? { id: uuid(), state: 'UNKNOWN', reviewStatus: 'REVIEW_REQUIRED' } : await api('/twin/portals', { method: 'POST', body });
      const np = { state: 'UNKNOWN', reviewStatus: 'REVIEW_REQUIRED', ...body, ...(r && typeof r === 'object' ? r : {}) };
      man.portals.push(np); eng.refreshPortals(); st.portal = np.id; eng.highlightPortal(np.id); setTool(null); toast('포털을 추가했습니다 (검수 필요)');
    } catch (ex) { toast(ex.message, 'bad'); }
  }

  // 핀 (클라이언트 전용 — localStorage)
  const pinKey = `twin.pins.${binfo.id}`; let pins = [];
  try { pins = JSON.parse(localStorage.getItem(pinKey) || '[]'); } catch { pins = []; }
  for (const p of pins) eng.addPin(p.id, new THREE.Vector3(...p.p), p.text);
  const savePins = () => { try { localStorage.setItem(pinKey, JSON.stringify(pins)); } catch { /* 저장 불가 */ } };
  async function addPinDialog(point) {
    const inp = h('input', { placeholder: '메모', maxlength: 60 });
    const ok = await modal({ title: '핀 메모', width: 380, body: field('내용', inp, '이 브라우저에만 저장됩니다 (서버 미연동).'), actions: [{ label: '취소', onClick: () => null }, { label: '추가', kind: 'primary', onClick: () => true }] });
    if (ok !== true) return;
    const p = { id: uuid(), p: point.toArray(), text: inp.value.trim() || '핀' };
    pins.push(p); savePins(); eng.addPin(p.id, point, p.text); renderPins(); setTool(null);
  }
  function renderPins() {
    rc(secPins, pins.length ? pins.map((p) => h('div', { class: 'tw-row compact' }, h('div', { class: 'tw-row-t' }, h('b', null, p.text), h('small', { class: 'mono muted' }, p.p.map((v) => v.toFixed(1)).join(', '))),
      h('button', { class: 'btn icon ghost sm', 'aria-label': '핀 삭제', onclick: () => { eng.removePin(p.id); pins = pins.filter((x) => x.id !== p.id); savePins(); renderPins(); } }, icon('trash', 14)))) : h('div', { class: 'muted' }, '핀이 없습니다. 도구의 "핀"으로 3D 위치에 메모를 남기세요.'));
  }

  // ───────── 초기 렌더 ─────────
  syncMode(); syncNotice(); renderSpaces(); renderOptions(); renderView(); renderLayers(); renderAssets(); renderWalls(); renderPortals(); renderPins();
  ready = true;
  if (st.spaceId && spaceDef(st.spaceId)) selectSpace(st.spaceId, true);
  document.addEventListener('keydown', function esc(e) { if (!viewer.isConnected) { document.removeEventListener('keydown', esc); return; } if (e.key === 'Escape') { if (st.photo) setPhoto(false); setTool(null); hidePopup(); }
    else if ((e.key === 'p' || e.key === 'P') && !e.target.closest?.('input, select, textarea') && !e.ctrlKey && !e.metaKey) setPhoto(!st.photo); });
}
