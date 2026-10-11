import { api, qs } from '../api.js';
import { h, icon, toast, modal, field, emptyState, debounce, severityBadge, statusBadge, avatar, fmtDate, ago, decisionBadge, STATUS, SEVERITY, WORKFLOW, EVENT_COLOR } from '../ui.js';

const BOARD = ['OPEN', 'CONFIRMED', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'REINSPECT', 'CLOSED'];
const VERB = { CONFIRMED: '문제 확인', ASSIGNED: '조치 지시', IN_PROGRESS: '조치 시작', RESOLVED: '조치 완료', REINSPECT: '재검수 요청', CLOSED: '종결', DISMISSED: '오탐·무시', OPEN: '다시 열기' };
const verbFor = (from, to) => (to === 'IN_PROGRESS' && (from === 'RESOLVED' || from === 'REINSPECT') ? '재조치 요청' : to === 'CONFIRMED' && from === 'ASSIGNED' ? '지시 취소' : to === 'OPEN' && from === 'CONFIRMED' ? '발견으로 되돌리기' : VERB[to] || to);

export async function issuesView(root, [idParam], ctx, alive) {
  const f = { q: '', severity: '', assignee: '', mine: false, overdue: false };
  let mode = localStorage.getItem('issues.mode') || 'board';
  let showDismissed = false;
  let data = null;
  const body = h('div');
  const users = (await api('/users?assignable=true')).users;

  const q = h('input', { placeholder: '번호·제목·자산 검색', oninput: debounce((e) => { f.q = e.target.value; load(); }) });
  const sevSel = h('select', { onchange: (e) => { f.severity = e.target.value; load(); } }, h('option', { value: '' }, '모든 심각도'), Object.entries(SEVERITY).map(([k, v]) => h('option', { value: k }, v.label)));
  const asSel = h('select', { onchange: (e) => { f.assignee = e.target.value; load(); } }, h('option', { value: '' }, '모든 담당자'), users.map((u) => h('option', { value: u.username }, u.displayName)));
  const chip = (label, key) => { const c = h('span', { class: 'chip', onclick: () => { f[key] = !f[key]; c.classList.toggle('on', f[key]); load(); } }, label); return c; };
  const modeBtns = h('div', { class: 'chips' },
    ['board', 'list'].map((m) => h('span', { class: `chip ${mode === m ? 'on' : ''}`, onclick: (e) => { mode = m; localStorage.setItem('issues.mode', m); modeBtns.querySelectorAll('.chip').forEach((x) => x.classList.remove('on')); e.currentTarget.classList.add('on'); draw(); } }, m === 'board' ? '보드' : '목록')));
  const toolbar = h('div', { class: 'toolbar' }, modeBtns, h('div', { class: 'search' }, icon('search', 16), q), sevSel, asSel,
    chip('내 이슈', 'mine'), chip('기한 초과', 'overdue'));

  function card(i) {
    const sev = SEVERITY[i.severity];
    return h('div', { class: `kcard ${i.overdue ? 'overdue' : ''}`, style: { '--sev': sev.color }, onclick: () => open(i.id) },
      h('div', { class: 'm' }, h('span', { class: 'tag' }, i.code), severityBadge(i.severity), i.source === 'auto' ? h('span', { class: 'tag', title: '검수 결과에서 자동 생성' }, 'AUTO') : null),
      h('div', { class: 't' }, i.title),
      h('div', { class: 'm' }, h('span', { class: 'mono' }, i.assetName || '—'), h('span', { class: 'grow' }),
        i.dueDate ? h('span', { class: `due ${i.overdue ? 'late' : ''}`, title: '조치 기한' }, `${i.overdue ? '⚠ ' : ''}${i.dueDate.slice(5)}`) : null,
        i.assignee ? avatar(i.assignee, true) : h('span', { class: 'dim', title: '담당자 미지정' }, '—')));
  }
  function draw() {
    body.replaceChildren();
    if (!data.issues.length) { body.append(h('div', { class: 'card' }, emptyState('이슈가 없습니다', '검수에서 불합격·사람 확인 결과가 나오면 자동으로 이슈가 생성되고, 직접 등록할 수도 있습니다.', 'alert'))); return; }
    if (mode === 'board') {
      const cols = [...BOARD, ...(showDismissed ? ['DISMISSED'] : [])];
      body.append(h('div', { class: 'board' }, cols.map((s) => {
        const items = data.issues.filter((i) => i.status === s);
        const shown = s === 'CLOSED' ? items.slice(0, 12) : items;
        return h('div', { class: 'col' }, h('div', { class: 'col-h', style: { '--c': STATUS[s].color } }, h('span', { class: 'bar' }), STATUS[s].label, h('span', { class: 'n' }, items.length)),
          h('div', { class: 'col-b' }, shown.length ? shown.map(card) : h('div', { class: 'dim', style: { textAlign: 'center', padding: '14px 0', fontSize: '12px' } }, '없음'),
            s === 'CLOSED' && items.length > 12 ? h('div', { class: 'dim', style: { textAlign: 'center', fontSize: '12px' } }, `외 ${items.length - 12}건 — 목록 보기에서 확인`) : null));
      })), h('div', { style: { marginTop: '8px' } }, h('button', { class: 'btn sm ghost', onclick: () => { showDismissed = !showDismissed; draw(); } }, showDismissed ? '오탐·무시 열 숨기기' : `오탐·무시 열 보기 (${data.counts.DISMISSED || 0})`)));
    } else {
      body.append(h('div', { class: 'card' }, h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
        h('thead', null, h('tr', null, ['번호', '제목', '자산', '심각도', '상태', '담당자', '기한', '생성'].map((t) => h('th', null, t)))),
        h('tbody', null, data.issues.map((i) => h('tr', { class: 'click', onclick: () => open(i.id) },
          h('td', { class: 'id' }, i.code), h('td', { style: { fontWeight: 600, maxWidth: '360px' } }, i.title, i.source === 'auto' ? h('span', { class: 'tag', style: { marginLeft: '8px' } }, 'AUTO') : null),
          h('td', { class: 'sub' }, i.assetName || '—'), h('td', null, severityBadge(i.severity)), h('td', null, statusBadge(i.status)), h('td', null, i.assignee || h('span', { class: 'dim' }, '—')),
          h('td', { class: `mono ${i.overdue ? 'due late' : ''}` }, i.dueDate || '—'), h('td', { class: 'sub nowrap' }, ago(i.createdAt)))))))));
    }
  }
  async function load() {
    data = await api('/issues' + qs(f));
    if (!alive()) return;
    draw();
  }

  // ───────── 상세 드로어 ─────────
  function open(id) {
    if (location.hash !== `#/issues/${id}`) history.replaceState(null, '', `#/issues/${id}`);
    renderDrawer(id);
  }
  function closeDrawer() { document.querySelectorAll('.drawer-mask,.drawer').forEach((e) => e.remove()); if (/^#\/issues\/\d+/.test(location.hash)) history.replaceState(null, '', '#/issues'); }

  async function renderDrawer(id) {
    document.querySelectorAll('.drawer-mask,.drawer').forEach((e) => e.remove());
    let i;
    try { i = await api(`/issues/${id}`); } catch (e) { toast(e.message, 'bad'); return; }
    const mask = h('div', { class: 'drawer-mask', onclick: closeDrawer });
    const refresh = async () => { await renderDrawer(id); load(); };

    // 진행 단계 바
    const idx = WORKFLOW.indexOf(i.status);
    const flow = h('div', { class: 'pipe' }, WORKFLOW.flatMap((s, k) => [
      h('span', { class: `p ${i.status === 'DISMISSED' ? '' : k < idx ? 'done' : k === idx ? 'cur' : ''}` }, STATUS[s].label), k < WORKFLOW.length - 1 ? h('span', { class: 'arrow' }, '›') : null]));

    const comment = h('textarea', { placeholder: '코멘트를 남기세요 (조치 상황·확인 내용)', style: { minHeight: '56px' } });
    const send = h('button', { class: 'btn', disabled: !ctx.perms.createIssues, onclick: async () => {
      if (!comment.value.trim()) return;
      try { await api(`/issues/${id}/comment`, { method: 'POST', body: { comment: comment.value } }); await refresh(); } catch (e) { toast(e.message, 'bad'); }
    } }, '코멘트 등록');

    const events = h('div', { class: 'timeline' }, i.events.map((e) => {
      const what = e.type === 'created' ? (e.data?.source === 'auto' ? '검수 결과로 자동 생성' : '이슈 등록') : e.type === 'status' ? `${e.fromLabel || ''} → ${e.toLabel}` : e.type === 'comment' ? '코멘트' : e.type === 'remeasure' ? '재측정 결과' : '수정';
      let extra = '';
      if (e.type === 'status' && e.data?.assignee) extra = `담당 ${e.data.assignee}${e.data.dueDate ? ` · 기한 ${e.data.dueDate}` : ''}`;
      if (e.data?.forced) extra = '재검수 없이 강제 종결';
      if (e.data?.reinspectPass) extra = '재검수 합격 확인';
      if (e.type === 'edit') extra = Object.entries(e.data || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(' → ') : v}`).join(' · ');
      return h('div', { class: 'ev', style: { '--c': EVENT_COLOR[e.type] || '#2c3e55' } },
        h('div', { class: 'eh' }, avatar(e.actor, true), h('b', null, e.actor === 'system' ? '시스템' : e.actor), h('span', { class: 'muted' }, what), h('span', { class: 'et' }, fmtDate(e.at))),
        (e.comment || extra) ? h('div', { class: 'ec' }, [e.comment, extra && h('div', { class: 'muted', style: { marginTop: e.comment ? '4px' : 0, fontSize: '12px' } }, extra)]) : null);
    }));

    const actions = h('div', { class: 'drawer-f' }, i.allowedTransitions.length ? i.allowedTransitions.map((t) => h('button', {
      class: `btn ${t.to === 'DISMISSED' ? 'ghost' : ['CLOSED', 'RESOLVED'].includes(t.to) ? 'ok' : t.to === 'IN_PROGRESS' && ['RESOLVED', 'REINSPECT'].includes(i.status) ? '' : 'primary'}`,
      onclick: () => transitionDialog(i, t.to, refresh) }, verbFor(i.status, t.to)))
      : h('span', { class: 'muted' }, ctx.perms.createIssues ? '지금 단계에서 내가 할 수 있는 조치가 없습니다.' : '조회 전용 계정입니다.'));

    const hint = i.status === 'REINSPECT'
      ? h('div', { class: `notice ${i.reinspectPass ? 'ok' : ''}`, style: { marginBottom: '14px' } }, i.reinspectPass ? '✓ 조치 완료 이후 같은 자산의 재검수 합격(PASS)이 확인되었습니다. 종결할 수 있습니다.' : '단말에서 이 자산을 다시 검수해 합격(PASS)이 나오면 종결할 수 있습니다. (관리자는 사유를 적고 강제 종결 가능)')
      : i.status === 'RESOLVED' ? h('div', { class: 'notice', style: { marginBottom: '14px' } }, '조치가 완료되었습니다. 재검수를 요청해 실제로 해결됐는지 단말로 다시 확인하세요.') : null;

    const drawer = h('aside', { class: 'drawer', role: 'dialog', 'aria-label': `${i.code} 상세` },
      h('div', { class: 'drawer-h' }, h('span', { class: 'tag' }, i.code), statusBadge(i.status), severityBadge(i.severity), i.overdue ? h('span', { class: 'badge bad nodot' }, '기한 초과') : null, h('div', { class: 'grow' }),
        h('button', { class: 'btn icon ghost', onclick: closeDrawer, 'aria-label': '닫기' }, icon('x', 20))),
      h('div', { class: 'drawer-b' },
        h('h2', { style: { fontSize: '19px', marginBottom: '12px' } }, i.title), flow, hint,
        h('dl', { class: 'kv', style: { marginBottom: '16px' } },
          h('dt', null, '자산'), h('dd', null, i.assetId ? h('a', { href: `#/assets/${encodeURIComponent(i.assetId)}`, onclick: closeDrawer }, `${i.assetName} (${i.assetId})`) : '—'),
          h('dt', null, '담당자'), h('dd', null, i.assignee || h('span', { class: 'muted' }, '미지정')), h('dt', null, '조치 기한'), h('dd', { class: i.overdue ? 'due late' : '' }, i.dueDate || '—'),
          h('dt', null, '등록'), h('dd', null, `${i.reporter === 'system' ? '시스템(자동)' : i.reporter} · ${fmtDate(i.createdAt)}`),
          h('dt', null, '연결 검수'), h('dd', null, i.inspection ? h('a', { href: `#/inspections/${i.inspection.id}`, onclick: closeDrawer }, [decisionBadge(i.inspection.decision), ` ${fmtDate(i.inspection.createdAt)} 3D 리뷰 →`]) : '—'),
          i.closedAt ? [h('dt', null, '종결'), h('dd', null, fmtDate(i.closedAt))] : null),
        i.description ? h('div', { class: 'card', style: { marginBottom: '14px' } }, h('div', { class: 'card-h' }, h('h3', null, '내용')), h('div', { class: 'card-b', style: { whiteSpace: 'pre-wrap' } }, i.description)) : null,
        i.resolution ? h('div', { class: 'notice ok', style: { marginBottom: '14px' } }, h('b', null, '조치 내용  '), i.resolution) : null,
        h('h3', { style: { margin: '18px 0 12px', fontSize: '14px' } }, '처리 이력'), events,
        ctx.perms.createIssues ? h('div', { style: { marginTop: '10px' } }, comment, h('div', { style: { marginTop: '8px' } }, send)) : null),
      actions);
    document.body.append(mask, drawer);
  }

  async function transitionDialog(i, to, refresh) {
    const comment = h('textarea', { placeholder: to === 'DISMISSED' ? '오탐·무시 사유 (필수)' : '메모 (선택)' });
    const parts = [];
    let assignee, due, resolution, force;
    if (to === 'ASSIGNED') {
      assignee = h('select', null, h('option', { value: '' }, '담당자 선택'), users.map((u) => h('option', { value: u.username, selected: u.username === i.assignee }, `${u.displayName} (${u.roleLabel})`)));
      due = h('input', { type: 'date', value: i.dueDate || '' });
      parts.push(field('조치 담당자 *', assignee), field('조치 기한', due));
    }
    if (to === 'RESOLVED') { resolution = h('textarea', { placeholder: '어떻게 조치했는지 (필수) — 예: 배관 지지대 재설치, 위치 보정 완료' }); parts.push(field('조치 내용 *', resolution)); }
    if (to === 'CLOSED' && !i.reinspectPass && (ctx.user.role === 'manager' || ctx.user.role === 'admin')) {
      force = h('input', { type: 'checkbox' });
      parts.push(h('div', { class: 'notice warn', style: { marginBottom: '12px' } }, '재검수 합격(PASS) 기록이 없습니다. 재검수 없이 종결하려면 아래를 체크하고 사유를 입력하세요.'),
        h('label', { class: 'row', style: { marginBottom: '10px', cursor: 'pointer' } }, force, '재검수 없이 강제 종결 (감사 로그에 기록됨)'));
    } else if (to === 'CLOSED' && !i.reinspectPass) {
      parts.push(h('div', { class: 'notice warn', style: { marginBottom: '12px' } }, '재검수 합격(PASS) 기록이 없어 종결할 수 없습니다. 단말에서 이 자산을 다시 검수하세요.'));
    }
    parts.push(field(to === 'DISMISSED' ? '사유 *' : '메모', comment));
    const r = await modal({ title: `${verbFor(i.status, to)} — ${i.code}`, width: 500,
      body: h('div', null, h('div', { class: 'muted', style: { marginBottom: '12px' } }, `${STATUS[i.status].label} → ${STATUS[to].label}`), parts),
      actions: [{ label: '취소', onClick: () => null }, { label: verbFor(i.status, to), kind: to === 'DISMISSED' ? 'danger' : 'primary', onClick: async () => {
        const body = { to, comment: comment.value };
        if (assignee) { body.assignee = assignee.value; if (due.value) body.dueDate = due.value; }
        if (resolution) body.resolution = resolution.value;
        if (force) body.force = force.checked;
        await api(`/issues/${i.id}/transition`, { method: 'POST', body });
        return true;
      } }] });
    if (r) { toast(`${i.code}: ${verbFor(i.status, to)} 처리했습니다`); await refresh(); }
  }

  async function newIssueDialog() {
    const assets = (await api('/assets')).assets;
    const as = h('select', null, h('option', { value: '' }, '자산 선택 (선택 사항)'), assets.map((a) => h('option', { value: a.assetId }, `${a.name} (${a.assetId})`)));
    const t = h('input', { placeholder: '예: 2호기 펌프 기초 볼트 위치 이탈', maxlength: 300 });
    const d = h('textarea', { placeholder: '현상·위치·영향' });
    const sev = h('select', null, Object.entries(SEVERITY).map(([k, v]) => h('option', { value: k, selected: k === 'MEDIUM' }, v.label)));
    const due = h('input', { type: 'date' });
    const asg = h('select', null, h('option', { value: '' }, '미지정'), users.map((u) => h('option', { value: u.username }, u.displayName)));
    const r = await modal({ title: '이슈 등록', width: 540, body: h('div', null, field('대상 자산', as), field('제목 *', t), field('내용', d), h('div', { class: 'row' }, field('심각도', sev), field('조치 기한', due)), field('조치 담당자', asg, '담당자 지정은 현장관리자 이상만 반영됩니다')),
      actions: [{ label: '취소', onClick: () => null }, { label: '등록', kind: 'primary', onClick: async () => {
        const body = { assetId: as.value || null, title: t.value, description: d.value, severity: sev.value };
        if (due.value) body.dueDate = due.value;
        const c = await api('/issues', { method: 'POST', body });
        if (asg.value && ctx.user.role !== 'inspector') await api(`/issues/${c.id}`, { method: 'PATCH', body: { assignee: asg.value } }).catch(() => {});
        return c;
      } }] });
    if (r && r.id) { toast(`${r.code} 이슈를 등록했습니다`); await load(); open(r.id); }
  }

  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '이슈·조치 워크플로우'), h('p', null, '검수에서 나온 문제를 확인 → 조치 지시 → 조치 → 재검수 → 종결까지 추적합니다.')),
    h('div', { class: 'actions' }, ctx.perms.createIssues ? h('button', { class: 'btn primary', onclick: newIssueDialog }, icon('plus', 16), '이슈 등록') : null)), toolbar, body);
  await load();
  if (idParam) renderDrawer(idParam);
}
