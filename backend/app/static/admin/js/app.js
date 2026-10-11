// 앱 셸: 로그인 전환 · 사이드바 내비 · 해시 라우터
import { api } from './api.js';
import { h, clear, icon, toast, modal, field, avatar, roleBadge, ROLE } from './ui.js';
import { renderLogin, renderForcedChange } from './views/login.js';
import { dashboardView } from './views/dashboard.js';
import { assetsView, assetDetailView } from './views/assets.js';
import { inspectionsView, inspectionDetailView } from './views/inspections.js';
import { issuesView } from './views/issues.js';
import { twinView } from './views/twin.js';
import { spacesView, devicesView, usersView, auditView, systemView } from './views/admin.js';

const root = document.getElementById('app');
export const ctx = { user: null, perms: {}, counts: {} };

const NAV = [
  { group: '운영' },
  { href: '#/dashboard', label: '대시보드', icon: 'dashboard', key: 'dashboard' },
  { href: '#/inspections', label: '검수 이력', icon: 'clipboard', key: 'inspections' },
  { href: '#/issues', label: '이슈·조치', icon: 'alert', key: 'issues', badge: 'issues' },
  { group: '자산' },
  { href: '#/assets', label: '자산 관리', icon: 'cube', key: 'assets' },
  { href: '#/spaces', label: '공간', icon: 'building', key: 'spaces' },
  { href: '#/twin', label: '3D 디지털 트윈', icon: 'twin', key: 'twin' },
  { href: '#/devices', label: '단말기', icon: 'device', key: 'devices' },
  { group: '시스템', min: 'manager' },
  { href: '#/users', label: '사용자·권한', icon: 'users', key: 'users', admin: true },
  { href: '#/audit', label: '감사 로그', icon: 'log', key: 'audit', min: 'manager' },
  { href: '#/system', label: '시스템 설정', icon: 'settings', key: 'system' },
];
const RANK = { viewer: 0, inspector: 1, manager: 2, admin: 3 };

const ROUTES = [
  [/^#?\/?$|^#\/dashboard$/, 'dashboard', '대시보드', dashboardView],
  [/^#\/assets$/, 'assets', '자산 관리', assetsView],
  [/^#\/assets\/(.+)$/, 'assets', '자산 상세', assetDetailView],
  [/^#\/inspections$/, 'inspections', '검수 이력', inspectionsView],
  [/^#\/inspections\/(.+)$/, 'inspections', '검수 리뷰', inspectionDetailView],
  [/^#\/issues(?:\/(\d+))?$/, 'issues', '이슈·조치', issuesView],
  [/^#\/spaces$/, 'spaces', '공간', spacesView],
  [/^#\/twin(?:\/([^/?]+))?(?:\/space\/([^/?]+))?(?:\?.*)?$/, 'twin', '3D 디지털 트윈', twinView],
  [/^#\/devices$/, 'devices', '단말기', devicesView],
  [/^#\/users$/, 'users', '사용자·권한', usersView],
  [/^#\/audit$/, 'audit', '감사 로그', auditView],
  [/^#\/system$/, 'system', '시스템 설정', systemView],
];

let pageEl, crumbEl, navEl, sideEl, sideMask;

/** 모바일 사이드바 열고 닫기 (바깥을 누르면 닫힌다) */
function toggleSide(open) {
  const on = open === undefined ? !sideEl.classList.contains('open') : open;
  sideEl.classList.toggle('open', on);
  sideMask.classList.toggle('open', on);
}

/** 좁은 화면에서 표를 카드로 보여 주려고 칸마다 머리글 이름(data-label)을 붙인다 — 화면이 바뀔 때마다 자동 */
function labelTables() {
  for (const t of pageEl.querySelectorAll('table.tbl:not(.perm)')) {
    const heads = [...t.querySelectorAll('thead th')].map((th) => th.textContent.trim());
    for (const tr of t.querySelectorAll('tbody tr')) {
      let main = false;
      [...tr.children].forEach((td, i) => {
        if (td.dataset.lbl) return;
        td.dataset.lbl = '1';
        const lab = heads[i] || '';
        const onlyCheck = td.children.length === 1 && td.firstElementChild.matches('input[type=checkbox]') && !td.textContent.trim();
        if (onlyCheck) { td.classList.add('cb'); return; }
        if (!main) { td.classList.add('main'); main = true; return; }  // 첫 칸(자산·검수 이름)은 카드 제목
        if (lab) td.dataset.label = lab; else td.classList.add('full');
        if (td.textContent.trim().length > 24) td.classList.add('long');  // 긴 내용(상세 JSON 등)은 한 줄 전체 폭으로
      });
    }
  }
}
function watchTables() {
  let queued = false;
  new MutationObserver(() => {
    if (queued) return; queued = true;
    requestAnimationFrame(() => { queued = false; labelTables(); });
  }).observe(pageEl, { childList: true, subtree: true });
}

function allowed(item) {
  if (item.admin) return ctx.user.role === 'admin';
  if (item.min) return RANK[ctx.user.role] >= RANK[item.min];
  return true;
}

function buildShell() {
  clear(root);
  navEl = h('nav', { class: 'nav' });
  for (const n of NAV) {
    if (n.group) {
      if (!n.min || RANK[ctx.user.role] >= RANK[n.min]) navEl.append(h('h6', null, n.group));
      continue;
    }
    if (!allowed(n)) continue;
    navEl.append(h('a', { href: n.href, 'data-key': n.key, onclick: () => toggleSide(false) },
      icon(n.icon, 18), n.label, n.badge ? h('span', { class: 'count', id: `cnt-${n.badge}` }, '·') : null));
  }
  sideEl = h('aside', { class: 'sidebar' },
    h('div', { class: 'brand' }, h('div', { class: 'logo', html: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3z"/><path d="M12 12l8.5-4.7M12 12v9.5M12 12L3.5 7.3"/></svg>' }),
      h('div', null, h('b', null, 'MARKERLESS'), h('small', null, 'INSPECT · 통합 백오피스'))),
    navEl,
    h('div', { class: 'side-foot' }, h('span', { class: 'pulse' }), h('span', null, '서비스 정상 운영 중')));
  crumbEl = h('div', { class: 'crumbs' });
  const menu = h('div', { class: 'menu hidden' },
    h('div', { class: 'who' }, h('b', null, ctx.user.displayName), h('div', { class: 'muted', style: { fontSize: '12px' } }, `@${ctx.user.username}`), h('div', { style: { marginTop: '6px' } }, roleBadge(ctx.user.role))),
    h('button', { onclick: () => { menu.classList.add('hidden'); location.hash = '#/system'; } }, '내 계정 · 비밀번호 변경'),
    h('button', { onclick: logout }, '로그아웃'));
  document.addEventListener('click', (e) => { if (!e.target.closest('.userbtn') && !e.target.closest('.menu')) menu.classList.add('hidden'); });
  const top = h('header', { class: 'topbar' },
    h('button', { class: 'btn icon ghost hamburger', 'aria-label': '메뉴', onclick: () => toggleSide() }, icon('menu', 20)),
    crumbEl, h('div', { class: 'grow' }),
    h('div', { class: 'userbtn', role: 'button', tabindex: 0, onclick: () => menu.classList.toggle('hidden') },
      avatar(ctx.user.displayName), h('div', { class: 'who-txt', style: { lineHeight: '1.15' } }, h('div', { style: { fontWeight: 600, fontSize: '13px' } }, ctx.user.displayName),
        h('div', { class: 'muted', style: { fontSize: '11px' } }, ROLE[ctx.user.role]))));
  pageEl = h('main', { class: 'page', id: 'page' });
  sideMask = h('div', { class: 'side-mask', onclick: () => toggleSide(false) });
  root.append(h('div', { class: 'shell' }, sideEl, sideMask, h('div', { class: 'main' }, top, menu, pageEl)));
  watchTables();
}

async function logout() {
  try { await api('/logout', { method: 'POST' }); } catch { /* 이미 만료 */ }
  ctx.user = null;
  disconnectEvents();
  start();
}

let seq = 0;
async function route() {
  if (!ctx.user) return;
  const hash = location.hash || '#/dashboard';
  const my = ++seq;
  for (const [re, key, title, view] of ROUTES) {
    const m = hash.match(re);
    if (!m) continue;
    navEl.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a.dataset.key === key));
    crumbEl.replaceChildren(h('span', { class: 'crumb-pre' }, 'Markerless Inspect  /  '), h('b', null, title));
    document.title = `${title} — Markerless Inspect`;
    const holder = h('div');
    clear(pageEl).append(holder);
    window.scrollTo(0, 0);
    try { await view(holder, m.slice(1), ctx, () => my === seq); }
    catch (e) { if (my === seq) holder.replaceChildren(h('div', { class: 'notice bad' }, `화면을 불러오지 못했습니다: ${e.message}`)); }
    return;
  }
  clear(pageEl).append(h('div', { class: 'empty' }, h('b', null, '페이지를 찾을 수 없습니다'), h('a', { href: '#/dashboard' }, '대시보드로 이동')));
}

// 실시간 반영: 모바일에서 등록·검수하거나 다른 관리자가 저장하면, 서버가 그 시점에 이벤트를 보낸다 (주기 확인 없음).
// 목록·보드·대시보드는 새로 그리고, 입력 중이거나 상세 화면(3D 카메라 유지)은 건드리지 않는다.
let events = null;
const ROUTE_SCOPES = {
  dashboard: ['assets', 'inspections', 'issues', 'spaces'], assets: ['assets', 'inspections', 'issues'],
  inspections: ['inspections', 'assets'], issues: ['issues'], spaces: ['spaces', 'assets', 'inspections', 'issues'],
  devices: ['assets', 'inspections'],
};
function pageBusy() {
  if (document.querySelector('.modal-mask, .drawer')) return true;  // 창·서랍이 열려 있음
  const a = document.activeElement;
  if (a && pageEl && pageEl.contains(a) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName) && a.type !== 'checkbox') return true;  // 입력 중
  if (pageEl && pageEl.querySelector('input[type=checkbox]:checked')) return true;  // 일괄 선택 중
  return false;
}
function onServerChange(scopes) {
  refreshCounts();
  const hash = location.hash || '#/dashboard';
  if (/^#\/(assets|inspections)\/.+/.test(hash)) return;  // 상세 화면은 그대로 둔다
  const hit = ROUTES.find(([re]) => re.test(hash));
  const wanted = hit ? ROUTE_SCOPES[hit[1]] : null;
  if (!wanted || !scopes.some((s) => wanted.includes(s))) return;
  if (pageBusy()) return;  // 작업 중이면 화면을 바꾸지 않는다 (다음 이동·새로고침 때 반영)
  route();
}
function connectEvents() {
  if (events || !window.EventSource) return;
  events = new EventSource('/admin/api/events');
  events.onmessage = (m) => {
    try { const d = JSON.parse(m.data); if (d.type === 'change') onServerChange(d.scopes || []); } catch { /* 형식 오류 무시 */ }
  };
  // 네트워크가 끊기면 브라우저가 알아서 다시 연결한다. 세션이 만료돼 거절되면(401) 연결이 닫히므로 로그인 화면으로.
  events.onerror = () => { if (events && events.readyState === EventSource.CLOSED) { events = null; } };
}
function disconnectEvents() { if (events) { events.close(); events = null; } }

async function refreshCounts() {
  if (!ctx.user) return;
  try {
    const r = await api('/issues?status=OPEN,CONFIRMED,ASSIGNED,IN_PROGRESS,RESOLVED,REINSPECT');
    const el = document.getElementById('cnt-issues');
    if (el) { el.textContent = r.total; el.classList.toggle('alert', r.issues.some((i) => i.overdue || i.severity === 'HIGH' || i.severity === 'CRITICAL')); }
  } catch { /* 로그아웃 직후 */ }
}

export async function changePasswordDialog() {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' });
  const nw = h('input', { type: 'password', autocomplete: 'new-password' });
  const nw2 = h('input', { type: 'password', autocomplete: 'new-password' });
  await modal({
    title: '비밀번호 변경', width: 440,
    body: h('div', null, field('현재 비밀번호', cur), field('새 비밀번호', nw, '8자 이상. 변경하면 다른 기기의 로그인은 해제됩니다.'), field('새 비밀번호 확인', nw2)),
    actions: [{ label: '취소', onClick: () => null }, { label: '변경', kind: 'primary', onClick: async () => {
      if (nw.value !== nw2.value) throw new Error('새 비밀번호가 서로 다릅니다');
      await api('/me/password', { method: 'POST', body: { current: cur.value, new: nw.value } });
      toast('비밀번호를 변경했습니다');
    } }],
  });
}

async function start() {
  try {
    const s = await api('/session');  // 미로그인이어도 200 (콘솔에 401이 쌓이지 않게)
    if (s.user) { const me = await api('/me'); ctx.user = me.user; ctx.perms = me.permissions; } else ctx.user = null;
  } catch {
    ctx.user = null;
  }
  if (!ctx.user) {
    renderLogin(root, async () => { await start(); });
    return;
  }
  if (ctx.user.mustChangePassword) {  // 기본/임시 비밀번호 — 바꾸기 전에는 앱 화면을 열지 않는다
    renderForcedChange(root, ctx.user, async () => { toast('비밀번호를 변경했습니다'); await start(); });
    return;
  }
  buildShell();
  refreshCounts();
  route();
  connectEvents();
}

window.addEventListener('hashchange', () => { route(); refreshCounts(); });
window.addEventListener('auth:expired', () => { if (ctx.user) { ctx.user = null; disconnectEvents(); toast('세션이 만료되었습니다. 다시 로그인하세요.', 'warn'); start(); } });
start();
