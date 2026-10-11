import { api, qs } from '../api.js';
import { h, icon, toast, modal, confirmBox, field, emptyState, debounce, decisionBadge, statusBadge, severityBadge, fmtDate, ago, sizeText, num, DECISION } from '../ui.js';
import { viewerPanel, loadAssetLayers } from '../viewerui.js';

const thumbCache = new Map();
function thumb(a) {
  const box = h('div', { class: 'thumb' }, icon('cube', 20));
  if (a.photos > 0) {
    const img = h('img', { alt: '', loading: 'lazy', src: `/admin/api/assets/${encodeURIComponent(a.assetId)}/photos/front.png`, onerror: () => img.remove() });
    box.replaceChildren(img);
  }
  return box;
}

/** GPS 좌표 + 지도 링크 (없으면 '—') */
function geoText(g) {
  if (!g) return '—';
  const txt = `${g.lat.toFixed(6)}, ${g.lon.toFixed(6)}` + (g.accuracyM != null ? ` (±${Math.round(g.accuracyM)}m)` : '');
  return h('a', { href: `https://maps.google.com/?q=${g.lat},${g.lon}`, target: '_blank', rel: 'noopener noreferrer' }, txt);
}

export async function assetsView(root, _p, ctx, alive) {
  const f = { q: '', space: '', status: '', decision: '', sort: '-created' };
  let selected = new Set();
  let data = null;
  const spaces = (await api('/spaces')).spaces;
  const list = h('div');
  const bulk = h('div');

  const q = h('input', { placeholder: '자산 ID·이름·종류·단말 검색', oninput: debounce((e) => { f.q = e.target.value; load(); }) });
  const sel = (key, opts, ph) => h('select', { onchange: (e) => { f[key] = e.target.value; load(); } }, ph ? h('option', { value: '' }, ph) : null, opts.map(([v, l]) => h('option', { value: v }, l)));
  const toolbar = h('div', { class: 'toolbar' },
    h('div', { class: 'search' }, icon('search', 16), q),
    sel('space', spaces.map((s) => [s.id, `${s.code}${s.name ? ' · ' + s.name : ''}`]), '모든 공간'),
    sel('status', [['ACTIVE', '운영 중'], ['ARCHIVED', '보관됨']], '모든 상태'),
    sel('decision', Object.entries(DECISION).map(([k, v]) => [k, `마지막 판정: ${v.label}`]), '마지막 판정'),
    sel('sort', [['-created', '최근 등록순'], ['name', '이름순'], ['-fails', '불합격 많은 순'], ['-inspected', '최근 검수순']], null));

  function renderBulk() {
    bulk.replaceChildren();
    if (!selected.size) return;
    const canEdit = ctx.perms.editAssets, canDel = ctx.perms.deleteAssets;
    bulk.append(h('div', { class: 'bulkbar' }, h('b', null, `${selected.size}개 선택`),
      h('div', { class: 'grow' }),
      canEdit ? h('button', { class: 'btn', onclick: () => bulkDo('archive') }, icon('archive', 16), '보관') : null,
      canEdit ? h('button', { class: 'btn', onclick: () => bulkDo('activate') }, icon('refresh', 16), '운영 복원') : null,
      canDel ? h('button', { class: 'btn danger', onclick: () => bulkDo('delete') }, icon('trash', 16), '삭제') : null,
      h('button', { class: 'btn ghost', onclick: () => { selected.clear(); draw(); renderBulk(); } }, '선택 해제')));
  }
  async function bulkDo(action) {
    const label = { archive: '보관', activate: '운영 복원', delete: '삭제' }[action];
    const ok = await confirmBox(`선택한 ${selected.size}개 자산을 ${label}할까요?${action === 'delete' ? '\n\n모델·사진·검수 이력이 함께 삭제되며 되돌릴 수 없습니다.' : ''}`, { title: `자산 ${label}`, danger: action === 'delete', label });
    if (!ok) return;
    try {
      const r = await api('/assets/bulk', { method: 'POST', body: { action, ids: [...selected] } });
      toast(`${r.done.length}개 ${label} 완료`);
      selected.clear(); await load();
    } catch (e) { toast(e.message, 'bad'); }
  }

  // 목록에서 이름을 바로 고친다 — 연필(또는 이름 더블클릭) → 입력 → Enter/바깥 클릭 저장, Esc 취소. 저장하면 모바일에 바로 반영된다.
  function nameCell(a) {
    const box = h('div', { style: { fontWeight: 600, display: 'flex', alignItems: 'center', gap: '6px' } });
    const show = () => {
      box.replaceChildren(...[
        h('span', { ondblclick: (e) => { e.stopPropagation(); if (ctx.perms.editAssets) begin(); }, title: ctx.perms.editAssets ? '더블클릭하면 이름을 수정합니다' : '' }, a.name),
        a.modelOk === false ? h('span', { class: 'badge bad nodot', title: '구버전 앱이 만든 깨진 모델입니다' }, '모델 손상') : null,
        ctx.perms.editAssets ? h('button', { class: 'btn ghost sm icon', title: '이름 수정', style: { padding: '2px 5px', opacity: '.7' }, onclick: (e) => { e.stopPropagation(); begin(); } }, icon('edit', 13)) : null].filter(Boolean));
    };
    function begin() {
      let done = false;
      const inp = h('input', { value: a.name, maxlength: 200, style: { minWidth: '180px' }, onclick: (e) => e.stopPropagation() });
      const finish = async (save) => {
        if (done) return; done = true;
        const v = inp.value.trim();
        if (!save || !v || v === a.name) { show(); return; }
        inp.disabled = true;
        try {
          await api(`/assets/${encodeURIComponent(a.assetId)}`, { method: 'PATCH', body: { name: v } });
          a.name = v; toast('이름을 저장했습니다 — 모바일에 바로 반영됩니다');
        } catch (e) { toast(e.message, 'bad'); }
        show();
      };
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
      inp.addEventListener('blur', () => finish(true));
      box.replaceChildren(inp);
      inp.focus(); inp.select();
    }
    show();
    return box;
  }

  function draw() {
    list.replaceChildren();
    if (!data.assets.length) { list.append(h('div', { class: 'card' }, emptyState('조건에 맞는 자산이 없습니다', '단말 앱의 [② 스캔·등록]에서 사물을 스캔해 등록하면 자산이 나타납니다.'))); return; }
    const allOn = data.assets.every((a) => selected.has(a.assetId));
    const head = h('tr', null,
      h('th', { style: { width: '36px' } }, h('input', { type: 'checkbox', checked: allOn, onchange: (e) => { data.assets.forEach((a) => (e.target.checked ? selected.add(a.assetId) : selected.delete(a.assetId))); draw(); renderBulk(); } })),
      ['자산', '공간', '크기', '공차(위치/회전)', '사진', '검수', '마지막 판정', '이슈', '단말', '스캔 일시·GPS', '상태'].map((t) => h('th', null, t)));
    const rows = data.assets.map((a) => h('tr', { class: `click ${selected.has(a.assetId) ? 'sel' : ''}`, onclick: (e) => { if (e.target.closest('input, button')) return; location.hash = `#/assets/${encodeURIComponent(a.assetId)}`; } },
      h('td', null, h('input', { type: 'checkbox', checked: selected.has(a.assetId), onchange: (e) => { e.target.checked ? selected.add(a.assetId) : selected.delete(a.assetId); draw(); renderBulk(); } })),
      h('td', null, h('div', { class: 'cell-asset' }, thumb(a), h('div', null, nameCell(a), h('div', { class: 'id' }, a.assetId)))),
      h('td', null, a.spaceCode || '—'), h('td', { class: 'mono nowrap' }, sizeText(a.sizeCm)),
      h('td', { class: 'mono nowrap' }, a.tolerance ? `${a.tolerance.positionMm}mm / ${a.tolerance.rotationDeg}°` : '—'),
      h('td', null, h('span', { class: `badge nodot ${a.photos >= 48 ? 'ok' : a.photos ? 'warn' : ''}` }, `${a.photos}컷`)),
      h('td', { class: 'num' }, a.inspections), h('td', null, a.lastDecision ? decisionBadge(a.lastDecision) : h('span', { class: 'dim' }, '—')),
      h('td', null, a.openIssues ? h('span', { class: 'badge bad' }, `${a.openIssues}건`) : h('span', { class: 'dim' }, '—')),
      h('td', { class: 'sub' }, a.deviceId || '—'), h('td', { class: 'sub nowrap' }, fmtDate(a.scannedAt || a.createdAt), a.geo ? h('div', { class: 'mono' }, `${a.geo.lat.toFixed(5)}, ${a.geo.lon.toFixed(5)}`) : null),
      h('td', null, a.status === 'ARCHIVED' ? h('span', { class: 'badge' }, '보관됨') : h('span', { class: 'badge ok' }, '운영 중'))));
    list.append(h('div', { class: 'card' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, head), h('tbody', null, rows))),
      h('div', { class: 'pager' }, `총 ${data.total}개`)));
  }
  async function load() {
    data = await api('/assets' + qs(f));
    if (!alive()) return;
    selected = new Set([...selected].filter((id) => data.assets.some((a) => a.assetId === id)));
    draw(); renderBulk();
  }
  root.append(
    h('div', { class: 'page-h' }, h('div', null, h('h1', null, '자산 관리'), h('p', null, '단말에서 스캔·등록된 자산을 조회·수정·삭제하고 3D로 확인합니다.')),
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: load }, icon('refresh', 16), '새로고침'))),
    toolbar, list, bulk);
  await load();
}

// ───────────────────────── 자산 상세 (3D + 편집) ─────────────────────────
export async function assetDetailView(root, [rawId], ctx, alive) {
  const id = decodeURIComponent(rawId);
  let a = await api(`/assets/${encodeURIComponent(id)}`);
  if (!alive()) return;
  const canEdit = ctx.perms.editAssets;

  const layers = [
    { key: 'surface', label: '모델 표면', color: '#8fa6bf' }, { key: 'points', label: '점군', color: '#6fd3ff', visible: false },
    { key: 'outline', label: '외곽선', color: '#4fd1ff' }, { key: 'box', label: '외곽 박스', color: '#c77dff' },
    { key: 'dims', label: '치수(mm)', color: '#c77dff' }, { key: 'cards', label: '사진 실루엣', color: '#c9d4e0' },
  ];
  const vp = viewerPanel(layers, { legend: [['#4fd1ff', '높이별 윤곽선'], ['#c77dff', '외곽 박스·치수 mm']] });
  vp.viewer.container.style.height = '580px';
  // 3D 보기의 기본: 외곽선·외곽 박스·치수 (표면·점군·사진 실루엣은 칩으로 켜서 본다). 검수용 설정과는 별개다.
  const VIEWER_DEFAULT = { surface: false, points: false, outline: true, box: true, dims: true, cards: false };
  loadAssetLayers(vp.viewer, id).then(() => { for (const [k, v] of Object.entries(VIEWER_DEFAULT)) vp.viewer.setVisible(k, v); vp.viewer.fit(); vp.sync(); vp.done(); }).catch((e) => vp.done(`3D를 불러오지 못했습니다: ${e.message}`));

  // ── 편집 폼
  const name = h('input', { value: a.name }), family = h('input', { value: a.family || '', placeholder: '예: 설비, 배관, 기둥' });
  const type = h('input', { value: a.type || '' }), notes = h('textarea', null, a.notes || '');
  const t = a.tolerance || {};
  const pos = h('input', { type: 'number', step: '0.5', min: '1', value: t.positionMm ?? 15 }), z = h('input', { type: 'number', step: '0.5', min: '1', value: t.zToleranceMm ?? '' , placeholder: '위치와 동일'});
  const rot = h('input', { type: 'number', step: '0.1', min: '0', value: t.rotationDeg ?? 1 });
  const status = h('select', null, h('option', { value: 'ACTIVE', selected: a.status === 'ACTIVE' }, '운영 중'), h('option', { value: 'ARCHIVED', selected: a.status === 'ARCHIVED' }, '보관됨'));
  if (!canEdit) [name, family, type, notes, pos, z, rot, status].forEach((e) => { e.disabled = true; });
  const save = h('button', { class: 'btn primary', disabled: !canEdit, onclick: async () => {
    try {
      const body = { name: name.value, family: family.value, type: type.value, notes: notes.value, status: status.value, inspectView: readView(),
        tolerance: { positionMm: +pos.value, rotationDeg: +rot.value, ...(z.value ? { zToleranceMm: +z.value } : {}) } };
      a = await api(`/assets/${encodeURIComponent(id)}`, { method: 'PATCH', body });
      toast('자산 정보를 저장했습니다'); head.querySelector('h1').textContent = a.name;
    } catch (e) { toast(e.message, 'bad'); }
  } }, icon('check', 16), '저장');
  // ── 검수 표시 설정: 앱이 검수할 때 이 자산에 대해 보여 줄 레이어
  const IV = [
    ['surface', '모델 표면', '스캔한 표면(조각 메시) — 거칠어서 CAD/BIM 대용으로는 비권장', 'surface'],
    ['points', '점군', '스캔 점 — 위치 확인용', 'points'],
    ['outline', '외곽선', '높이별 윤곽 선화', 'outline'],
    ['box', '외곽 박스', '사물을 감싸는 상자', 'box'],
    ['dimensions', '치수(mm)', '길이·깊이·높이 치수선', 'dims'],
    ['silhouette', '사진 실루엣', '둘레 64방향 흑백 외곽선 — 카메라를 돌려도 같은 방향·크기로 맞춰 봄', 'cards'],
  ];
  const DEFAULT_IV = { surface: false, points: false, outline: true, box: true, dimensions: true, silhouette: true };
  const ivBoxes = {};
  function applyView(v) {  // 체크박스와 3D 미리보기를 설정값대로
    for (const [k, , , layer] of IV) { if (ivBoxes[k]) ivBoxes[k].checked = !!v[k]; vp.viewer.setVisible(layer, !!v[k]); }
    vp.sync();
  }
  const ivRows = IV.map(([k, label, desc, layer]) => {
    const cb = h('input', { type: 'checkbox', checked: !!a.inspectView[k], disabled: !canEdit, style: { width: '18px', height: '18px', flex: 'none' },
      onchange: () => { vp.viewer.setVisible(layer, cb.checked); vp.sync(); autoSaveView(); } });
    ivBoxes[k] = cb;
    return h('label', { style: { display: 'flex', gap: '10px', alignItems: 'flex-start', padding: '7px 0', cursor: canEdit ? 'pointer' : 'default' } }, cb,
      h('span', null, h('b', { style: { fontWeight: 600 } }, label), h('div', { class: 'muted', style: { fontSize: '12px' } }, desc)));
  });
  const readView = () => Object.fromEntries(IV.map(([k]) => [k, ivBoxes[k].checked]));
  // 체크하면 바로 저장한다 (별도 저장 버튼을 놓쳐서 설정이 안 먹는 일이 없게)
  const viewStatus = h('span', { class: 'muted', style: { fontSize: '12px' } }, '체크하면 자동 저장됩니다');
  let saveTimer = null;
  function autoSaveView() {
    if (!canEdit) return;
    viewStatus.textContent = '저장 중…';
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        a = await api(`/assets/${encodeURIComponent(id)}`, { method: 'PATCH', body: { inspectView: readView() } });
        viewStatus.textContent = '✓ 저장됨 — 다음 검수부터 앱에 적용됩니다';
      } catch (e) { viewStatus.textContent = '저장 실패'; toast(e.message, 'bad'); }
    }, 350);
  }
  const resetView = h('button', { class: 'btn sm', disabled: !canEdit, onclick: () => { applyView(DEFAULT_IV); autoSaveView(); } }, '기본값으로');
  const allView = h('button', { class: 'btn sm', disabled: !canEdit, onclick: async () => {
    if (!(await confirmBox('지금 선택한 표시 설정을 모든 자산에 적용할까요?\n\n자산마다 따로 정한 설정은 덮어써집니다.', { title: '전체 자산에 적용', label: '적용' }))) return;
    try { const r = await api('/assets/inspect-view', { method: 'POST', body: { inspectView: readView() } }); toast(`${r.updated}개 자산에 적용했습니다`); }
    catch (e) { toast(e.message, 'bad'); }
  } }, '전체 자산에 적용');
  const viewCard = h('div', { style: { border: '1px solid var(--line)', borderRadius: '10px', padding: '12px 14px', margin: '0 0 18px', background: 'var(--panel-2, transparent)' } },
    h('div', { style: { fontWeight: 650, marginBottom: '2px' } }, '검수 표시 설정'),
    h('div', { class: 'muted', style: { fontSize: '12.5px', marginBottom: '6px' } }, '앱에서 이 자산을 검수할 때 그릴 항목입니다. 기본은 외곽 박스·치수·사진 실루엣이고, 나머지는 필요할 때만 켜세요. 체크하면 왼쪽 3D에서 바로 미리 볼 수 있습니다.'),
    ivRows, h('div', { class: 'row', style: { gap: '8px', marginTop: '8px', flexWrap: 'wrap', alignItems: 'center' } }, resetView, allView, viewStatus));

  const del = h('button', { class: 'btn danger', onclick: async () => {
    if (!(await confirmBox(`'${a.name}' 자산을 삭제할까요?\n\n3D 모델·사진 실루엣·검수 이력·관련 이슈 연결이 함께 삭제되며 되돌릴 수 없습니다.`, { title: '자산 삭제', danger: true, label: '삭제' }))) return;
    try { await api(`/assets/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('삭제했습니다'); location.hash = '#/assets'; } catch (e) { toast(e.message, 'bad'); }
  } }, icon('trash', 16), '자산 삭제');

  const head = h('div', { class: 'page-h' },
    h('div', null, h('div', { class: 'muted', style: { fontSize: '12px', marginBottom: '4px' } }, h('a', { href: '#/assets' }, '← 자산 목록')), h('h1', null, a.name),
      h('p', null, h('span', { class: 'mono' }, a.assetId), ` · ${a.spaceCode || '공간 미지정'} · `, a.status === 'ARCHIVED' ? '보관됨' : '운영 중')),
    h('div', { class: 'actions' }, ctx.perms.createIssues ? h('button', { class: 'btn', onclick: () => newIssue(a) }, icon('alert', 16), '이슈 등록') : null));

  const brokenBanner = a.modelOk === false ? h('div', { class: 'notice', style: { margin: '0 0 12px', borderColor: 'var(--bad, #ff5d5d)' } },
    h('b', null, '모델 손상 — 재등록이 필요합니다. '), '구버전 앱이 만든 모델 파일의 좌표가 뒤섞여 있어 크기·외곽 박스·정합이 모두 틀립니다. 최신 앱(홈 화면 빌드 번호 확인)으로 같은 사물을 다시 스캔해 등록하세요.') : null;
  const tabs = ['정보·수정', `검수 이력 (${a.inspections})`, `이슈 (${a.issues.length})`];
  const panels = [infoPanel(), histPanel(), issuePanel()];
  const body = h('div');
  let cur = 0;
  const tabBar = h('div', { class: 'tabs' }, tabs.map((tx, i) => h('button', { class: `tab ${i === 0 ? 'active' : ''}`, onclick: () => { cur = i; show(); } }, tx)));
  function show() { tabBar.querySelectorAll('.tab').forEach((b, i) => b.classList.toggle('active', i === cur)); body.replaceChildren(panels[cur]); }

  function infoPanel() {
    return h('div', null,
      h('dl', { class: 'kv', style: { marginBottom: '18px' } },
        h('dt', null, '크기'), h('dd', { class: 'mono' }, sizeText(a.sizeCm)), h('dt', null, '등록 단말'), h('dd', null, a.deviceId || '—'),
        h('dt', null, '스캔 일시'), h('dd', null, fmtDate(a.scannedAt || a.createdAt)),
        h('dt', null, '스캔 위치(GPS)'), h('dd', { class: 'mono' }, geoText(a.geo)), h('dt', null, '수정일'), h('dd', null, fmtDate(a.updatedAt)),
        h('dt', null, '사진 실루엣'), h('dd', null, `${a.photos}컷 (수평 360°, 권장 64컷)`), h('dt', null, '마지막 판정'), h('dd', null, a.lastDecision ? [decisionBadge(a.lastDecision), ` ${ago(a.lastInspectedAt)}`] : '—')),
      viewCard,
      field('자산 이름', name), h('div', { class: 'row' }, field('종류', family), field('형식', type)),
      h('div', { class: 'notice', style: { margin: '0 0 12px', fontSize: '12.5px' } }, '공차는 이 자산을 검수할 때 합격/불합격을 가르는 기준입니다 (월드맵이 복원된 검수에 적용).'),
      h('div', { class: 'row' }, field('위치 공차 (mm)', pos), field('높이(Z) 공차 (mm)', z), field('회전 공차 (°)', rot)),
      field('상태', status), field('메모', notes),
      h('div', { class: 'row', style: { justifyContent: 'space-between', marginTop: '6px' } }, save, ctx.perms.deleteAssets ? del : null));
  }
  function histPanel() {
    if (!a.recentInspections.length) return emptyState('검수 이력이 없습니다', '아직 이 자산을 검수한 기록이 없습니다.', 'clipboard');
    return h('div', { class: 'card', style: { overflow: 'hidden' } }, a.recentInspections.map((o) => h('a', { class: 'listrow', href: `#/inspections/${o.id}`, style: { color: 'inherit', textDecoration: 'none' } },
      decisionBadge(o.decision), h('div', { class: 'grow' }, h('div', { class: 's' }, `${fmtDate(o.createdAt)} · ${o.deviceId || '단말 미상'}`), h('div', { class: 's' }, `신뢰도 ${o.confidence} · ${o.matchedBy === 'auto' ? '자동 매칭' : o.matchedBy === 'pinned' ? '자산 지정' : '—'}`)), icon('chevron', 16))));
  }
  function issuePanel() {
    if (!a.issues.length) return emptyState('이슈가 없습니다', '이 자산과 연결된 이슈가 없습니다.', 'alert');
    return h('div', { class: 'card', style: { overflow: 'hidden' } }, a.issues.map((i) => h('a', { class: 'listrow', href: `#/issues/${i.id}`, style: { color: 'inherit', textDecoration: 'none' } },
      severityBadge(i.severity), h('div', { class: 'grow' }, h('div', { class: 't' }, i.title), h('div', { class: 's' }, `${i.code} · ${ago(i.createdAt)}`)), statusBadge(i.status))));
  }

  if (brokenBanner) root.append(brokenBanner);
  root.append(head, h('div', { class: 'split' }, h('div', null, vp.el,
    h('div', { class: 'row', style: { marginTop: '10px', color: 'var(--muted)', fontSize: '12.5px' } }, icon('eye', 14), '처음엔 외곽선·외곽 박스·치수로 열리고, 레이어 칩으로 표면·점군·사진 실루엣을 켜고 끌 수 있습니다. 3D 좌표는 등록 당시 세션 기준(m)입니다.')),
    h('div', { class: 'card' }, h('div', { class: 'card-b' }, tabBar, body))));
  show();
}

export async function newIssue(asset, observationId = null, title = '') {
  const users = (await api('/users?assignable=true')).users;
  const t = h('input', { value: title || `${asset?.name || ''} 점검 필요`, maxlength: 300 });
  const d = h('textarea', { placeholder: '현상·위치·영향을 적어 주세요' });
  const sev = h('select', null, ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((s) => h('option', { value: s, selected: s === 'MEDIUM' }, { LOW: '경미', MEDIUM: '보통', HIGH: '심각', CRITICAL: '치명' }[s])));
  const as = h('select', null, h('option', { value: '' }, '미지정 (조치 지시 때 지정)'), users.map((u) => h('option', { value: u.username }, `${u.displayName} (${u.roleLabel})`)));
  const due = h('input', { type: 'date' });
  const r = await modal({ title: '이슈 등록', width: 520,
    body: h('div', null, field('제목', t), field('내용', d), h('div', { class: 'row' }, field('심각도', sev), field('조치 기한', due)), field('조치 담당자', as)),
    actions: [{ label: '취소', onClick: () => null }, { label: '등록', kind: 'primary', onClick: async () => {
      const body = { assetId: asset?.assetId, observationId, title: t.value, description: d.value, severity: sev.value };
      if (due.value) body.dueDate = due.value;
      const created = await api('/issues', { method: 'POST', body });
      if (as.value) await api(`/issues/${created.id}`, { method: 'PATCH', body: { assignee: as.value } }).catch(() => {});
      return created;
    } }] });
  if (r && r.id) { toast(`${r.code} 이슈를 등록했습니다`); location.hash = `#/issues/${r.id}`; }
}
