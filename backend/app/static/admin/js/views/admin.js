import { api } from '../api.js';
import { h, icon, toast, modal, confirmBox, field, emptyState, roleBadge, decisionBadge, avatar, fmtDate, ago, ROLE } from '../ui.js';
import { changePasswordDialog } from '../app.js';

// ───────────────────────── 공간 ─────────────────────────
export async function spacesView(root, _p, ctx) {
  const { spaces } = await api('/spaces');
  const canEdit = ctx.perms.editAssets, canDelete = ctx.perms.deleteAssets;

  // 추가 / 수정 공통 입력 창. 수정 내용은 모바일 앱에 몇 초 안에 자동 반영된다.
  const form = async (s) => {
    const adding = !s;
    const code = h('input', { value: s?.code || '', placeholder: '예: MECH-301', maxlength: 64 });
    const name = h('input', { value: s?.name || '', placeholder: '예: 지하 기계실', maxlength: 200 });
    const level = h('input', { value: s?.level || '', placeholder: '예: B2, 3F, A동', maxlength: 64 });
    const r = await modal({
      title: adding ? '공간 추가' : `공간 수정 — ${s.code}`,
      body: h('div', null,
        field('공간 ID', code, adding ? '현장에서 부르는 고유 ID입니다' : 'ID를 바꾸면 모바일에 선택된 공간도 자동으로 새 ID로 바뀝니다'),
        field('공간 이름', name), field('층/구역', level)),
      actions: [{ label: '취소', onClick: () => null }, { label: adding ? '추가' : '저장', kind: 'primary', onClick: async () => {
        if (!code.value.trim()) throw new Error('공간 ID를 입력하세요');
        if (adding) await api('/spaces', { method: 'POST', body: { code: code.value.trim(), name: name.value, level: level.value } });
        else await api(`/spaces/${s.id}`, { method: 'PATCH', body: { code: code.value.trim(), name: name.value, level: level.value } });
        return true;
      } }] });
    if (r) { toast(adding ? '공간을 추가했습니다' : '저장했습니다 — 모바일에 자동 반영됩니다'); location.reload(); }
  };

  const remove = async (s) => {
    const extra = s.assets ? `\n\n⚠ 이 공간의 자산 ${s.assets}개와 검수 기록 ${s.inspections}건, 사진 실루엣·3D 모델, 월드맵이 함께 삭제됩니다. 되돌릴 수 없습니다.` : '';
    if (!(await confirmBox(`'${s.code}${s.name ? ' · ' + s.name : ''}' 공간을 삭제할까요?${extra}${s.openIssues ? `\n\n열린 이슈 ${s.openIssues}건은 남고 공간 연결만 끊어집니다.` : ''}`,
      { title: '공간 삭제', danger: true, label: s.assets ? '자산까지 모두 삭제' : '삭제' }))) return;
    try {
      const r = await api(`/spaces/${s.id}${s.assets ? '?cascade=true' : ''}`, { method: 'DELETE' });
      toast(`삭제했습니다${r.assetsDeleted ? ` (자산 ${r.assetsDeleted}개 포함)` : ''}`); location.reload();
    } catch (e) { toast(e.message, 'bad'); }
  };

  root.append(h('div', { class: 'page-h' },
    h('div', null, h('h1', null, '공간'), h('p', null, '현장 구역별 자산·검수·이슈 현황과 월드맵(공간 좌표) 저장 상태입니다. 추가·수정·삭제는 모바일 앱에 몇 초 안에 자동 반영됩니다.')),
    h('div', { class: 'actions' }, canEdit ? h('button', { class: 'btn primary', onclick: () => form(null) }, icon('plus', 16), '공간 추가') : null)),
    spaces.length ? h('div', { class: 'grid cols-3' }, spaces.map((s) => h('div', { class: 'card' }, h('div', { class: 'card-h' }, icon('building', 18), h('h3', null, s.code), h('div', { class: 'right' }, s.hasWorldMap ? h('span', { class: 'badge ok' }, '월드맵 저장됨') : h('span', { class: 'badge' }, '월드맵 없음'))),
      h('div', { class: 'card-b' }, h('div', { style: { fontWeight: 600, marginBottom: '2px' } }, s.name || '(이름 없음)'), h('div', { class: 'muted', style: { marginBottom: '14px' } }, `${s.level || '층/구역 미지정'} · 프로젝트 ${s.projectId}`),
        h('div', { class: 'grid cols-3 keep3', style: { gap: '8px', marginBottom: '14px' } }, [['자산', s.assets], ['검수', s.inspections], ['열린 이슈', s.openIssues]].map(([l, v]) => h('div', { style: { background: 'var(--bg2)', borderRadius: '8px', padding: '10px', textAlign: 'center' } }, h('div', { style: { fontSize: '20px', fontWeight: 700 }, class: 'mono' }, v), h('div', { class: 'muted', style: { fontSize: '11.5px' } }, l)))),
        h('div', { class: 'row', style: { gap: '8px', flexWrap: 'wrap' } }, h('a', { class: 'btn sm', href: `#/assets` }, '자산 보기'),
          canEdit ? h('button', { class: 'btn sm', onclick: () => form(s) }, icon('edit', 14), '수정') : null,
          canDelete ? h('button', { class: 'btn sm danger', onclick: () => remove(s) }, icon('trash', 14), '삭제') : null))))) : h('div', { class: 'card' }, emptyState('공간이 없습니다', canEdit ? '[공간 추가]로 만들거나, 단말 앱의 [① 공간]에서 만들면 나타납니다.' : '단말 앱의 [① 공간]에서 공간을 만들면 나타납니다.', 'building')));
}

// ───────────────────────── 단말기 ─────────────────────────
export async function devicesView(root) {
  const { devices } = await api('/devices');
  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '단말기'), h('p', null, '자산을 등록하고 검수를 수행한 단말(스마트폰)별 활동 현황입니다.'))),
    h('div', { class: 'card' }, devices.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['단말', '등록한 자산', '수행한 검수', '불합격', '마지막 판정', '마지막 활동'].map((t) => h('th', null, t)))),
      h('tbody', null, devices.map((d) => h('tr', null, h('td', null, h('div', { class: 'cell-asset' }, h('div', { class: 'thumb' }, icon('device', 20)), h('div', { style: { fontWeight: 600 } }, d.deviceId))),
        h('td', { class: 'num' }, d.assetsRegistered), h('td', { class: 'num' }, d.inspections), h('td', { class: 'num' }, d.fails ? h('span', { style: { color: 'var(--bad)' } }, d.fails) : 0),
        h('td', null, d.lastDecision ? decisionBadge(d.lastDecision) : '—'), h('td', { class: 'sub' }, `${ago(d.lastSeen)} · ${fmtDate(d.lastSeen)}`)))))) : emptyState('단말 활동이 없습니다', '단말 앱이 자산을 등록하거나 검수하면 단말별 현황이 나타납니다.', 'device')));
}

// ───────────────────────── 사용자·권한 (통합관리자) ─────────────────────────
export async function usersView(root, _p, ctx) {
  const load = async () => (await api('/users')).users;
  let users = await load();
  const list = h('div');
  const create = async () => {
    const un = h('input', { placeholder: '영문·숫자 3~32자', autocomplete: 'off' }), dn = h('input', { placeholder: '표시 이름 (예: 김현장)' });
    const role = h('select', null, ['inspector', 'manager', 'viewer', 'admin'].map((r) => h('option', { value: r }, ROLE[r])));
    const pw = h('input', { type: 'password', autocomplete: 'new-password' });
    const r = await modal({ title: '사용자 추가', width: 460, body: h('div', null, field('아이디', un), field('표시 이름', dn), field('역할', role), field('초기 비밀번호', pw, '8자 이상. 사용자에게 전달 후 첫 로그인에서 변경하도록 안내하세요.')),
      actions: [{ label: '취소', onClick: () => null }, { label: '추가', kind: 'primary', onClick: async () => { await api('/users', { method: 'POST', body: { username: un.value.trim(), displayName: dn.value, role: role.value, password: pw.value } }); return true; } }] });
    if (r) { toast('사용자를 추가했습니다'); users = await load(); draw(); }
  };
  const resetPw = async (u) => {
    const pw = h('input', { type: 'password', autocomplete: 'new-password' });
    const r = await modal({ title: `비밀번호 초기화 — ${u.username}`, width: 420, body: h('div', null, field('새 비밀번호', pw, '초기화하면 이 사용자의 모든 로그인 세션이 해제됩니다.')),
      actions: [{ label: '취소', onClick: () => null }, { label: '초기화', kind: 'primary', onClick: async () => { await api(`/users/${u.id}/password`, { method: 'POST', body: { password: pw.value } }); return true; } }] });
    if (r) toast('비밀번호를 초기화했습니다');
  };
  const patch = async (u, body, msg) => { try { await api(`/users/${u.id}`, { method: 'PATCH', body }); toast(msg); users = await load(); draw(); } catch (e) { toast(e.message, 'bad'); draw(); } };
  async function toggleActive(u) {
    if (u.active && !(await confirmBox(`'${u.username}' 계정을 비활성화할까요?\n\n로그인 중인 세션이 즉시 종료됩니다.`, { danger: true, label: '비활성화' }))) return;
    patch(u, { active: !u.active }, u.active ? '비활성화했습니다' : '다시 활성화했습니다');
  }
  function userRow(u) {
    const roleSel = h('select', { style: { width: '150px' }, disabled: u.id === ctx.user.id, onchange: (e) => patch(u, { role: e.target.value }, '역할을 변경했습니다') },
      ['admin', 'manager', 'inspector', 'viewer'].map((r) => h('option', { value: r, selected: r === u.role }, ROLE[r])));
    const actions = h('div', { class: 'row', style: { justifyContent: 'flex-end' } },
      h('button', { class: 'btn sm', onclick: () => resetPw(u) }, '비밀번호 초기화'),
      u.id !== ctx.user.id
        ? h('button', { class: `btn sm ${u.active ? 'danger' : ''}`, onclick: () => toggleActive(u) }, u.active ? '비활성화' : '활성화')
        : h('span', { class: 'dim', style: { fontSize: '12px' } }, '내 계정'));
    return h('tr', null,
      h('td', null, h('div', { class: 'cell-asset' }, avatar(u.displayName), h('div', null, h('div', { style: { fontWeight: 600 } }, u.displayName), h('div', { class: 'id' }, `@${u.username}`)))),
      h('td', null, roleSel),
      h('td', null, u.active ? h('span', { class: 'badge ok' }, '사용 중') : h('span', { class: 'badge bad' }, '비활성')),
      h('td', { class: 'sub' }, u.lastLoginAt ? ago(u.lastLoginAt) : '로그인 기록 없음'),
      h('td', { class: 'sub' }, fmtDate(u.createdAt, false)),
      h('td', null, actions));
  }
  function draw() {
    const head = h('tr', null, ['사용자', '역할', '상태', '마지막 로그인', '가입', ''].map((t) => h('th', null, t)));
    list.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'tbl-wrap' },
      h('table', { class: 'tbl' }, h('thead', null, head), h('tbody', null, users.map(userRow))))));
  }
  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '사용자·권한'), h('p', null, '통합관리자·현장관리자·현장 검수자·조회 전용 계정을 관리합니다.')),
    h('div', { class: 'actions' }, h('button', { class: 'btn primary', onclick: create }, icon('plus', 16), '사용자 추가'))), list, h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'card-h' }, h('h3', null, '역할별 권한')), h('div', { class: 'card-b' }, permTable())));
  draw();
}

function permTable() {
  const Y = h('span', { class: 'yes' }, '●'), N = h('span', { class: 'no' }, '–');
  const rows = [
    ['대시보드·자산·검수 이력·3D 조회', 1, 1, 1, 1], ['이슈 등록·코멘트·문제 확인·오탐 처리·재검수 요청·종결', 1, 1, 1, 0], ['조치 지시(담당자 배정)·조치 완료·강제 종결', 1, 1, 0, 0],
    ['자산 정보·공차 수정, 보관/복원, 공간 수정', 1, 1, 0, 0], ['자산 삭제(단건·일괄)', 1, 0, 0, 0], ['감사 로그·시스템 정보 조회', 1, 1, 0, 0], ['사용자 추가·역할 변경·비밀번호 초기화', 1, 0, 0, 0],
  ];
  return h('table', { class: 'perm' }, h('thead', null, h('tr', null, h('th', null, '기능'), ['admin', 'manager', 'inspector', 'viewer'].map((r) => h('th', null, ROLE[r])))),
    h('tbody', null, rows.map((r) => h('tr', null, h('td', null, r[0]), r.slice(1).map((v) => h('td', null, v ? Y.cloneNode(true) : N.cloneNode(true)))))));
}

// ───────────────────────── 감사 로그 ─────────────────────────
const ACTION = { login: '로그인', logout: '로그아웃', 'login.fail': '로그인 실패', 'asset.update': '자산 수정', 'asset.delete': '자산 삭제', 'asset.bulk_delete': '자산 일괄 삭제', 'asset.bulk_archive': '자산 일괄 보관',
  'asset.bulk_activate': '자산 일괄 복원', 'issue.create': '이슈 등록', 'issue.transition': '이슈 상태 변경', 'user.create': '사용자 추가', 'user.update': '사용자 수정', 'user.password_reset': '비밀번호 초기화',
  'password.change': '비밀번호 변경', 'space.update': '공간 수정',
  'twin.wall_thickness': '벽 두께 수정', 'twin.wall_review': '벽 검토 상태', 'twin.envelope': '추정 외피 생성', 'twin.portal_create': '포털 추가', 'twin.portal_update': '포털 수정',
  'twin.revision_review': '리비전 검토', 'twin.geometry_review': '설계 레이어 검토', 'twin.revision_restore': '리비전 복원', 'twin.cad_approve': 'CAD 치환 승인', 'twin.cad_revert': 'CAD 치환 되돌림' };
export async function auditView(root) {
  const { logs } = await api('/audit?limit=300');
  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '감사 로그'), h('p', null, '누가·언제·무엇을 했는지 기록합니다 (최근 300건). 로그인 실패도 남습니다.'))),
    h('div', { class: 'card' }, logs.length ? h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['일시', '사용자', '행위', '대상', '상세', 'IP'].map((t) => h('th', null, t)))),
      h('tbody', null, logs.map((l) => h('tr', null, h('td', { class: 'sub nowrap' }, fmtDate(l.at)), h('td', null, l.actor), h('td', null, h('span', { class: `badge nodot ${l.action === 'login.fail' ? 'bad' : l.action.includes('delete') ? 'warn' : ''}` }, ACTION[l.action] || l.action)),
        h('td', { class: 'id' }, `${l.targetType}${l.targetId ? ':' + l.targetId : ''}`.slice(0, 40)), h('td', { class: 'sub', style: { maxWidth: '380px', wordBreak: 'break-all' } }, Object.keys(l.detail || {}).length ? JSON.stringify(l.detail).slice(0, 160) : ''), h('td', { class: 'sub mono' }, l.ip || ''))))))
      : emptyState('기록이 없습니다', '', 'log')));
}

// ───────────────────────── 시스템 설정 ─────────────────────────
export async function systemView(root, _p, ctx) {
  let sys = null;
  if (ctx.user.role === 'admin' || ctx.user.role === 'manager') sys = await api('/system');
  const copy = async (text) => { try { await navigator.clipboard.writeText(text); toast('복사했습니다'); } catch { toast(text, 'warn', 8000); } };
  root.append(h('div', { class: 'page-h' }, h('div', null, h('h1', null, '시스템 설정'), h('p', null, '서비스 정보와 내 계정 설정입니다.'))),
    h('div', { class: 'grid cols-2' },
      h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '내 계정')), h('div', { class: 'card-b' },
        h('div', { class: 'row', style: { marginBottom: '14px' } }, avatar(ctx.user.displayName), h('div', null, h('b', null, ctx.user.displayName), h('div', { class: 'muted' }, `@${ctx.user.username}`)), h('div', { class: 'grow' }), roleBadge(ctx.user.role)),
        h('button', { class: 'btn', onclick: changePasswordDialog }, icon('shield', 16), '비밀번호 변경'),
        h('div', { class: 'notice warn', style: { marginTop: '14px' } }, '기본 관리자 비밀번호를 사용 중이라면 운영 전에 반드시 변경하세요. 이 사이트는 외부 인터넷에서 접속할 수 있습니다.'))),
      sys ? h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '서비스 정보')), h('div', { class: 'card-b' }, h('dl', { class: 'kv' },
        h('dt', null, '외부 접속 주소'), h('dd', null, h('div', { class: 'row' }, h('span', { class: 'mono', style: { wordBreak: 'break-all' } }, sys.externalUrl), h('button', { class: 'btn sm icon', onclick: () => copy(sys.externalUrl), title: '복사' }, icon('copy', 14)))),
        h('dt', null, '버전'), h('dd', { class: 'mono' }, `v${sys.version} · Python ${sys.python}`), h('dt', null, '가동 시간'), h('dd', null, `${Math.floor(sys.uptimeSec / 3600)}시간 ${Math.floor((sys.uptimeSec % 3600) / 60)}분`),
        h('dt', null, '저장소'), h('dd', null, `${sys.storageMB} MB`), h('dt', null, '데이터'), h('dd', null, `자산 ${sys.counts.assets} · 검수 ${sys.counts.inspections} · 이슈 ${sys.counts.issues} · 사용자 ${sys.counts.users}`),
        h('dt', null, '디버그 로그'), h('dd', null, sys.debugLogging ? h('span', { class: 'badge warn' }, '켜짐 (개발 모드)') : h('span', { class: 'badge' }, '꺼짐'))))) : null),
    sys ? h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'card-h' }, h('h3', null, '개발 내역서')), h('div', { class: 'card-b' },
      h('p', { class: 'muted', style: { marginTop: 0 } }, '지금까지 구성한 모든 메뉴·기능·특징과 한계, 추가 고도화 과제를 정리한 문서입니다. 받을 때마다 현재 운영 현황이 함께 붙습니다.'),
      h('a', { class: 'btn primary', href: '/admin/api/system/devdoc.md', download: true }, icon('download', 16), '개발 내역서 다운로드 (.md)'))) : null,
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'card-h' }, h('h3', null, '역할별 권한')), h('div', { class: 'card-b' }, permTable())));
}
