import { api, qs } from '../api.js';
import { h, icon, toast, confirmBox, emptyState, debounce, decisionBadge, statusBadge, fmtDate, ago, num, pct, DECISION } from '../ui.js';
import { viewerPanel, loadAssetLayers } from '../viewerui.js';
import { pointsObject, linesObject, b64Floats } from '../viewer3d.js';
import { newIssue } from './assets.js';

const DEC_COLOR = { PASS: 0x2ecc71, FAIL: 0xff5a5f, REVIEW: 0xf5a623, RETAKE: 0x8b9aad };

export async function inspectionsView(root, _p, ctx, alive) {
  const f = { decision: '', asset: '', device: '', q: '', date_from: '', date_to: '', page: 1, size: 25 };
  const [assets, devices] = await Promise.all([api('/assets'), api('/devices')]);
  const list = h('div');
  const chips = h('div', { class: 'chips' });
  const drawChips = () => chips.replaceChildren(
    h('span', { class: `chip ${f.decision === '' ? 'on' : ''}`, onclick: () => { f.decision = ''; f.page = 1; drawChips(); load(); } }, '전체'),
    ...Object.entries(DECISION).map(([k, v]) => h('span', { class: `chip ${f.decision === k ? 'on' : ''}`, onclick: () => { f.decision = k; f.page = 1; drawChips(); load(); } }, v.label)));
  drawChips();
  const sel = (key, opts, ph) => h('select', { onchange: (e) => { f[key] = e.target.value; f.page = 1; load(); } }, h('option', { value: '' }, ph), opts.map(([v, l]) => h('option', { value: v }, l)));
  const toolbar = h('div', { class: 'toolbar' }, chips,
    h('div', { class: 'search' }, icon('search', 16), h('input', { placeholder: '자산·단말 검색', oninput: debounce((e) => { f.q = e.target.value; f.page = 1; load(); }) })),
    sel('asset', assets.assets.map((a) => [a.assetId, a.name]), '모든 자산'), sel('device', devices.devices.map((d) => [d.deviceId, d.deviceId]), '모든 단말'),
    h('input', { type: 'date', title: '시작일', onchange: (e) => { f.date_from = e.target.value; f.page = 1; load(); } }),
    h('input', { type: 'date', title: '종료일', onchange: (e) => { f.date_to = e.target.value; f.page = 1; load(); } }));

  // 선택 삭제 (통합관리자만). 페이지를 넘겨도 선택은 유지된다.
  const canDelete = !!ctx.perms.deleteAssets;
  const selected = new Set();
  const bulk = h('div');
  function renderBulk() {
    bulk.replaceChildren();
    if (!selected.size) return;
    bulk.append(h('div', { class: 'bulkbar' }, h('b', null, `${selected.size}건 선택`), h('div', { class: 'grow' }),
      h('button', { class: 'btn danger', onclick: removeSelected }, icon('trash', 16), '선택 삭제'),
      h('button', { class: 'btn ghost', onclick: () => { selected.clear(); renderBulk(); render(); } }, '선택 해제')));
  }
  async function removeSelected() {
    const n = selected.size;
    if (!(await confirmBox(`선택한 검수 기록 ${n}건을 삭제할까요?\n\n판정 결과와 3D 리뷰 데이터가 함께 삭제되며 되돌릴 수 없습니다. 연결된 이슈는 남고 검수 연결만 끊어집니다.`,
      { title: '검수 이력 삭제', danger: true, label: `${n}건 삭제` }))) return;
    try {
      const r = await api('/inspections/bulk-delete', { method: 'POST', body: { ids: [...selected] } });
      toast(`${r.deleted.length}건을 삭제했습니다${r.notFound.length ? ` (이미 없는 ${r.notFound.length}건 제외)` : ''}`);
      selected.clear(); renderBulk(); await load();
    } catch (e) { toast(e.message, 'bad'); }
  }

  let cur = null;
  async function load() {
    const d = await api('/inspections' + qs(f));
    if (!alive()) return;
    cur = d;
    render();
  }
  function render() {  // 체크박스를 누를 때는 서버에서 다시 받지 않고 화면만 다시 그린다
    const d = cur;
    list.replaceChildren();
    if (!d.inspections.length) { list.append(h('div', { class: 'card' }, emptyState('검수 이력이 없습니다', '단말 앱의 [③ 검수]에서 검수하면 이력이 쌓입니다.', 'clipboard'))); return; }
    const allOn = d.inspections.every((o) => selected.has(o.id));
    const rows = d.inspections.map((o) => h('tr', { class: `click ${selected.has(o.id) ? 'sel' : ''}`, onclick: (e) => { if (e.target.closest('input, a, button')) return; location.hash = `#/inspections/${o.id}`; } },
      canDelete ? h('td', null, h('input', { type: 'checkbox', checked: selected.has(o.id), onchange: (e) => { e.target.checked ? selected.add(o.id) : selected.delete(o.id); renderBulk(); render(); } })) : null,
      h('td', { class: 'nowrap sub' }, fmtDate(o.createdAt)),
      h('td', null, h('div', { style: { fontWeight: 600 } }, o.assetName), h('div', { class: 'id' }, o.assetId)),
      h('td', null, decisionBadge(o.decision)), h('td', null, o.confidence || '—'),
      h('td', null, o.matchedBy === 'auto' ? h('span', { class: 'badge nodot info' }, '자동 매칭') : o.matchedBy === 'pinned' ? h('span', { class: 'badge nodot' }, '자산 지정') : '—'),
      h('td', { class: 'num' }, o.inlier != null ? pct(o.inlier) : '—'), h('td', { class: 'num' }, o.rmseMm != null ? `${o.rmseMm}mm` : '—'),
      h('td', { class: 'num' }, o.restored && o.distMm != null ? `${num(o.distMm, 1)}mm / ${num(o.rotDeg, 1)}°` : h('span', { class: 'dim' }, '형상 판정')),
      h('td', { class: 'sub' }, o.deviceId || '—'),
      h('td', null, o.issueId ? h('a', { href: `#/issues/${o.issueId}`, onclick: (e) => e.stopPropagation() }, `ISS-${String(o.issueId).padStart(4, '0')}`) : h('span', { class: 'dim' }, '—'))));
    const pages = Math.max(1, Math.ceil(d.total / d.size));
    list.append(h('div', { class: 'card' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null,
        canDelete ? h('th', { style: { width: '36px' } }, h('input', { type: 'checkbox', checked: allOn, title: '이 페이지 전체 선택', onchange: (e) => { d.inspections.forEach((o) => (e.target.checked ? selected.add(o.id) : selected.delete(o.id))); renderBulk(); render(); } })) : null,
        ['일시', '자산', '판정', '신뢰도', '매칭', '일치율', '잔차', '위치/회전 편차', '단말', '이슈'].map((t) => h('th', null, t)))), h('tbody', null, rows))),
      h('div', { class: 'pager' }, h('span', null, `총 ${d.total}건`), h('span', { class: 'grow' }),
        h('button', { class: 'btn sm', disabled: f.page <= 1, onclick: () => { f.page--; load(); } }, '이전'), h('span', null, `${f.page} / ${pages}`),
        h('button', { class: 'btn sm', disabled: f.page >= pages, onclick: () => { f.page++; load(); } }, '다음'))));
  }
  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '검수 이력'), h('p', null, '단말이 수행한 모든 검수의 판정과 근거를 조회하고, 3D로 검수 과정을 리뷰합니다.')),
    h('div', { class: 'actions' }, h('a', { class: 'btn', href: '/admin/api/inspections.csv', download: true }, icon('download', 16), 'CSV 내보내기'))), toolbar, bulk, list);
  await load();
}

// ───────────────────────── 검수 리뷰 (3D + 과정) ─────────────────────────
export async function inspectionDetailView(root, [rawId], ctx, alive) {
  const id = decodeURIComponent(rawId);
  const d = await api(`/inspections/${encodeURIComponent(id)}`);
  if (!alive()) return;
  let review = null, reviewErr = null;
  if (d.hasReview) { try { review = await api(`/inspections/${encodeURIComponent(id)}/review`); } catch (e) { reviewErr = e.message; } }
  else reviewErr = '이 검수는 3D 리뷰 데이터가 저장되기 전에 수행되어 3D 재생을 지원하지 않습니다.';

  const restored = !!(d.icp && d.icp.worldMapRestored);
  const layers = [
    { key: 'obs', label: '관측 점군', color: '#ffc857' }, { key: 'obsline', label: '관측 윤곽', color: '#ffd166' },
    { key: 'surface', label: '등록 모델', color: '#8fa6bf' }, { key: 'outline', label: '맞춘 선화', color: '#4fd1ff' },
    { key: 'box', label: '외곽 박스', color: '#c77dff' }, { key: 'dims', label: '치수(mm)', color: '#c77dff' },
    { key: 'cards', label: '사진 실루엣', color: '#c9d4e0' }, { key: 'design-outline', label: '설계 위치(월드맵)', color: '#4c7dff' },
  ];
  const vp = viewerPanel(layers, { legend: [['#ffd166', '관측한 사물 윤곽'], ['#4fd1ff', '맞춘 외곽선'], ['#c77dff', '외곽 박스·치수'], ['#4c7dff', '설계 위치']] });
  vp.viewer.container.style.height = '560px';

  // ── 검수 과정 5단계
  const steps = [
    { n: 1, t: '스캔 입력', show: ['obs'] }, { n: 2, t: '사물 분리', show: ['obs', 'obsline'] },
    { n: 3, t: '자산 매칭', show: ['obsline', 'outline', 'box'] }, { n: 4, t: '정합', show: ['obsline', 'outline', 'surface', 'cards', 'dims', 'box'] },
    { n: 5, t: '판정', show: ['obsline', 'outline', 'surface', 'cards', 'dims', 'box', 'design-outline'] },
  ];
  let cur = 4;
  const stepBar = h('div', { class: 'steps' });
  const stepBody = h('div', { class: 'card-b' });
  const match = d.match || {};
  const seg = match.segmentation || {};
  const al = match.alignment || d.icp || {};
  function drawSteps() {
    stepBar.replaceChildren(...steps.map((s, i) => h('div', { class: `step ${i < cur ? 'done' : i === cur ? 'active' : ''}`, 'data-n': s.n, onclick: () => { cur = i; drawSteps(); applyPreset(); } }, s.t)));
    const kv = (rows) => h('dl', { class: 'kv' }, rows.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
    const body = [
      () => h('div', null, h('p', { class: 'muted', style: { margin: '0 0 10px' } }, '단말이 현장에서 수집한 3D 특징점입니다. 아래는 분리 후 남은 사물 점군입니다.'),
        kv([['사용 점 수', review ? `${review.observedPoints.count.toLocaleString()}개 (사물)` : '—'], ['단말', d.deviceId || '—'], ['촬영 시각', fmtDate(d.createdAt)]])),
      () => h('div', null, h('p', { class: 'muted', style: { margin: '0 0 10px' } }, '바닥·탁자·벽 같은 배경을 제거하고 중심(POI)과 이어진 사물만 남겼습니다.'),
        kv([['분리 결과', seg.segmented === false ? '분리 실패 → 조준점 반경 사용' : '성공'], ['사물 점', seg.nIn != null ? `${seg.nIn}개` : '—'], ['제거한 배경 점', seg.nRemoved != null ? `${seg.nRemoved}개` : '—'],
          ['분리 품질', seg.quality || '—'], ['관측 크기', match.observedSizeM ? match.observedSizeM.map((x) => (x * 100).toFixed(0)).join(' × ') + ' cm' : '—']]),
        (seg.warnings || []).map((w) => h('div', { class: 'notice warn', style: { marginTop: '8px' } }, w))),
      () => h('div', null, h('p', { class: 'muted', style: { margin: '0 0 10px' } }, match.matchedBy === 'auto' ? '크기가 비슷한 등록 자산을 후보로 골라 정합하고 가장 닮은 자산을 선택했습니다.' : '사용자가 지정한 자산에 맞췄습니다.'),
        rankingTable(match.ranking || [], d.asset?.assetId)),
      () => h('div', null, h('p', { class: 'muted', style: { margin: '0 0 10px' } }, '관측한 사물 윤곽(노랑)에 등록 모델을 방위·위치만 풀어 맞췄습니다 (기울기는 중력으로 고정).'),
        kv([['일치율', al.inlierRatio != null ? pct(al.inlierRatio, 1) : '—'], ['잔차(RMSE)', al.rmseM != null ? `${(al.rmseM * 1000).toFixed(1)} mm` : '—'], ['방위', al.yawDeg != null ? `${al.yawDeg.toFixed(1)}°` : '—'],
          ['모델 덮임', al.modelCoverage != null ? pct(al.modelCoverage) : '—'], ['크기비 (길이/높이)', match.sizeRatio ? `${match.sizeRatio.footprint.toFixed(2)} / ${match.sizeRatio.height.toFixed(2)}` : '—'], ['일치 기준 τ', al.tauM != null ? `${(al.tauM * 1000).toFixed(0)} mm` : '—']])),
      () => h('div', null, h('div', { style: { marginBottom: '10px' } }, decisionBadge(d.decision, true), h('span', { class: 'muted', style: { marginLeft: '10px' } }, `신뢰도 ${d.confidence}`)),
        (d.reasons || []).map((r) => h('div', { class: `notice ${d.decision === 'PASS' ? 'ok' : d.decision === 'FAIL' ? 'bad' : 'warn'}`, style: { marginBottom: '6px' } }, r))),
    ][cur]();
    stepBody.replaceChildren(body);
  }
  function applyPreset() {
    const on = new Set(steps[cur].show);
    for (const l of layers) if (vp.viewer.has(l.key)) vp.viewer.setVisible(l.key, on.has(l.key) && !(l.key === 'design-outline' && !restored));
    vp.sync();
  }

  // ── 3D 로딩
  (async () => {
    if (!review) { vp.done(reviewErr || '3D 데이터가 없습니다'); return; }
    try {
      const v = vp.viewer;
      v.add('obs', pointsObject(b64Floats(review.observedPoints.points), 0xffc857, 0.008));
      if (review.observedOutline) v.add('obsline', linesObject(review.observedOutline.lines, 0xffd166, 1));
      await loadAssetLayers(v, d.asset.assetId, { pose: review.actualPose, outlineColor: DEC_COLOR[d.decision] || 0x4fd1ff });
      if (restored) await loadAssetLayers(v, d.asset.assetId, { pose: review.designPose, prefix: 'design-', outlineColor: 0x4c7dff, ghost: true });
      v.remove('design-box');
      applyPreset(); v.fit(); vp.done();
    } catch (e) { vp.done(`3D 리뷰를 불러오지 못했습니다: ${e.message}`); }
  })();

  // ── 편차 게이지 (월드맵 복원 시에만 의미 있음)
  const tol = d.tolerance || {};
  const gauge = (label, val, limit, unit) => {
    const ratio = limit ? val / limit : 0;
    return h('div', { class: 'gauge' }, h('div', { class: 'gh' }, h('span', null, label), h('b', null, `${num(val, 1)} / ${num(limit, 1)} ${unit}`)),
      h('div', { class: `meter ${ratio > 1 ? 'bad' : ratio > 0.7 ? 'warn' : ''}` }, h('i', { style: { width: `${Math.min(100, ratio * 70)}%` } }), h('span', { class: 'tol', style: { left: '70%' }, title: '공차' })));
  };
  const dev = d.deviation || {};
  const devCard = h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '설계 대비 편차'), h('span', { class: 'sub' }, restored ? '공간 복원됨' : '공간 미복원')),
    h('div', { class: 'card-b' }, restored
      ? [gauge('위치 편차', dev.distMm || 0, tol.positionMm || 15, 'mm'), gauge('높이(Z) 편차', Math.abs(dev.dzMm || 0), tol.zToleranceMm || tol.positionMm || 15, 'mm'), gauge('회전 편차', dev.maxRotDeg || 0, tol.rotationDeg || 1, '°'),
        h('div', { class: 'muted', style: { fontSize: '12px' } }, '흰 눈금 = 공차 한계. 막대가 눈금을 넘으면 불합격 후보입니다.')]
      : [h('div', { class: 'notice' }, '공간(월드맵)이 복원되지 않은 검수라 설계 위치 대비 편차는 판정에서 제외하고, 저장된 모델과의 형상 일치도로 판정했습니다.'),
        h('div', { style: { marginTop: '12px' } }, d.margins?.shapeFit ? h('dl', { class: 'kv' }, h('dt', null, '일치율'), h('dd', null, pct(d.margins.shapeFit.inlierRatio, 1)), h('dt', null, '잔차'), h('dd', null, `${num(d.margins.shapeFit.rmseMm, 1)} mm`)) : null)]));

  // ── 이슈 연결
  const issueCard = h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '조치 워크플로우'),
    h('div', { class: 'right' }, ctx.perms.createIssues && !d.issues.length && d.decision !== 'PASS' ? h('button', { class: 'btn sm primary', onclick: () => newIssue(d.asset, d.id, `${d.asset?.name || d.assetId} — ${DECISION[d.decision]?.label || ''}`) }, icon('plus', 14), '이슈 등록') : null)),
    d.issues.length ? h('div', null, d.issues.map((i) => h('a', { class: 'listrow', href: `#/issues/${i.id}`, style: { color: 'inherit', textDecoration: 'none' } }, h('span', { class: 'tag' }, i.code), h('div', { class: 'grow' }, h('div', { class: 't' }, i.title)), statusBadge(i.status))))
      : h('div', { class: 'card-b muted' }, d.decision === 'PASS' ? '합격이라 조치가 필요 없습니다.' : '연결된 이슈가 없습니다. 불합격·사람 확인 결과는 자동으로 이슈가 되지만, 이 검수는 이전에 처리되었습니다.'));

  root.append(
    h('div', { class: 'page-h' }, h('div', null, h('div', { class: 'muted', style: { fontSize: '12px', marginBottom: '4px' } }, h('a', { href: '#/inspections' }, '← 검수 이력')),
      h('h1', { style: { display: 'flex', alignItems: 'center', gap: '12px' } }, '검수 리뷰', decisionBadge(d.decision, true)),
      h('p', null, h('a', { href: `#/assets/${encodeURIComponent(d.assetId)}` }, d.assetName), ` · ${fmtDate(d.createdAt)} · ${d.deviceId || '단말 미상'} · ${match.matchedBy === 'auto' ? '자동 매칭' : match.matchedBy === 'pinned' ? '자산 지정' : '—'}`)),
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => window.print() }, icon('print', 16), '리포트 인쇄 / PDF'))),
    h('div', { class: 'split' },
      h('div', null, vp.el, h('div', { class: 'card', style: { marginTop: '14px' } }, h('div', { class: 'card-h' }, h('h3', null, '검수 과정 재생'), h('span', { class: 'sub' }, '단계를 누르면 3D 레이어가 바뀝니다')), h('div', { class: 'card-b' }, stepBar, stepBody))),
      h('div', { class: 'grid' }, devCard,
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '판정 근거')),
          h('div', { class: 'card-b' }, (d.reasons || []).length ? d.reasons.map((r) => h('div', { class: `notice ${d.decision === 'PASS' ? 'ok' : d.decision === 'FAIL' ? 'bad' : 'warn'}`, style: { marginBottom: '8px' } }, r)) : h('span', { class: 'muted' }, '근거 정보가 없습니다.'),
            h('dl', { class: 'kv', style: { marginTop: '12px' } }, h('dt', null, '신뢰도'), h('dd', null, d.confidence), h('dt', null, '불확도'), h('dd', { class: 'mono' }, d.uncertainty ? `위치 ±${num(d.uncertainty.positionMm, 1)}mm · 회전 ±${num(d.uncertainty.rotationDeg, 1)}°` : '—'),
              h('dt', null, 'BIM 리비전'), h('dd', { class: 'mono' }, d.bimRevision || '—')))),
        issueCard)));
  drawSteps();
}

function rankingTable(ranking, chosen) {
  if (!ranking.length) return h('div', { class: 'muted' }, '매칭 후보 정보가 없습니다.');
  return h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['자산', '유사도', '일치율', '크기 벌점', '잔차', ''].map((t) => h('th', null, t)))),
    h('tbody', null, ranking.map((r) => h('tr', null, h('td', { class: 'id' }, r.assetId), h('td', { class: 'num' }, r.similarity != null ? `${r.similarity}%` : '—'),
      h('td', { class: 'num' }, r.inlier != null ? pct(r.inlier) : '크기 탈락'), h('td', { class: 'num' }, r.sizePenalty), h('td', { class: 'num' }, r.rmseMm != null ? `${r.rmseMm}mm` : '—'),
      h('td', null, r.assetId === chosen ? h('span', { class: 'badge ok' }, '선택') : ''))))));
}
