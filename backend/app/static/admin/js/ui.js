// UI 유틸 — 요소 생성(h), 아이콘, 배지, 포맷, 모달, 토스트
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') {
      for (const [sk, sv] of Object.entries(v)) { if (sk.startsWith('--')) el.style.setProperty(sk, sv); else el.style[sk] = sv; } // CSS 변수는 setProperty로
    }
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v; // 신뢰된 정적 SVG에만 사용
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

// ───────── 아이콘 (24×24 stroke) ─────────
const P = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  cube: '<path d="M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3z"/><path d="M12 12l8.5-4.7M12 12v9.5M12 12L3.5 7.3"/>',
  clipboard: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 11h6M9 15h4"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.01"/>',
  building: '<rect x="4" y="3" width="10" height="18" rx="1.5"/><path d="M14 9h6v12h-6M8 7h2M8 11h2M8 15h2"/>',
  device: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.4 3.2-5.5 6.5-5.5s5.9 2.1 6.5 5.5M16 4.8a3.4 3.4 0 010 6.4M18 14.8c2 .6 3.3 2.4 3.6 5.2"/>',
  log: '<path d="M5 4h14v16H5z"/><path d="M8 9h8M8 13h8M8 17h5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 14a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  download: '<path d="M12 3v12m0 0l-4-4m4 4l4-4M4 20h16"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>', check: '<path d="M4 12.5l5 5L20 6.5"/>',
  refresh: '<path d="M20 11a8 8 0 10-1.5 5.5M20 4v7h-7"/>', trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>', print: '<path d="M7 9V3h10v6M7 17H4v-7h16v7h-3M7 14h10v7H7z"/>',
  logout: '<path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10"/>', menu: '<path d="M4 6h16M4 12h16M4 18h16"/>', chevron: '<path d="M9 6l6 6-6 6"/>',
  fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', camera: '<path d="M4 8h3l2-3h6l2 3h3v12H4z"/><circle cx="12" cy="14" r="3.5"/>',
  link: '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3A4 4 0 0011 18.7l1-1"/>', archive: '<path d="M3 5h18v4H3zM5 9v11h14V9M10 13h4"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M9 12l2.2 2.2L15.5 10"/>', pin: '<path d="M12 21s7-6.5 7-12a7 7 0 10-14 0c0 5.5 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', filter: '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>', copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 00-1-1H5a1 1 0 00-1 1v10a1 1 0 001 1h3"/>',
  twin: '<path d="M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3z"/><path d="M3.5 7.3L12 12l8.5-4.7M12 12v9.5"/><circle cx="12" cy="12" r="2"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};
export function icon(name, size = 18) {
  const s = h('span', { style: { display: 'inline-flex', flex: 'none' } });
  s.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${P[name] || ''}</svg>`;
  return s;
}

// ───────── 라벨 · 배지 ─────────
export const DECISION = {
  PASS: { label: '합격', cls: 'ok' }, FAIL: { label: '불합격', cls: 'bad' },
  REVIEW: { label: '사람 확인', cls: 'warn' }, RETAKE: { label: '재측정', cls: '' },
};
export const SEVERITY = {
  LOW: { label: '경미', cls: 'info', color: '#38a3ff' }, MEDIUM: { label: '보통', cls: 'warn', color: '#f5a623' },
  HIGH: { label: '심각', cls: 'bad', color: '#ff5a5f' }, CRITICAL: { label: '치명', cls: 'bad', color: '#ff2d55' },
};
export const STATUS = {
  OPEN: { label: '발견', cls: 'bad', color: '#ff5a5f' }, CONFIRMED: { label: '확인됨', cls: 'warn', color: '#f5a623' },
  ASSIGNED: { label: '조치 지시', cls: 'violet', color: '#a394ff' }, IN_PROGRESS: { label: '조치 중', cls: 'info', color: '#38a3ff' },
  RESOLVED: { label: '조치 완료', cls: 'ok', color: '#2ecc71' }, REINSPECT: { label: '재검수 요청', cls: 'violet', color: '#7c8cff' },
  CLOSED: { label: '종결', cls: '', color: '#5c6b7e' }, DISMISSED: { label: '오탐·무시', cls: '', color: '#44566b' },
};
export const WORKFLOW = ['OPEN', 'CONFIRMED', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'REINSPECT', 'CLOSED'];
export const ROLE = { admin: '통합관리자', manager: '현장관리자', inspector: '현장 검수자', viewer: '조회 전용' };
export const EVENT_COLOR = { created: '#ff5a5f', status: '#38a3ff', comment: '#8b9aad', edit: '#f5a623', remeasure: '#a394ff' };

export const decisionBadge = (d, lg = false) => { const x = DECISION[d] || { label: d || '—', cls: '' }; return h('span', { class: `badge ${x.cls} ${lg ? 'lg' : ''}` }, x.label); };
export const severityBadge = (s) => { const x = SEVERITY[s] || { label: s, cls: '' }; return h('span', { class: `badge ${x.cls}` }, x.label); };
export const statusBadge = (s) => { const x = STATUS[s] || { label: s, cls: '' }; return h('span', { class: `badge ${x.cls}` }, x.label); };
export const roleBadge = (r) => h('span', { class: `badge nodot ${r === 'admin' ? 'violet' : r === 'manager' ? 'info' : r === 'inspector' ? 'ok' : ''}` }, ROLE[r] || r);
export const avatar = (name, sm = false) => h('span', { class: `avatar ${sm ? 'sm' : ''}`, title: name }, (name || '?').slice(0, 1).toUpperCase());

// ───────── 포맷 ─────────
const pad = (n) => String(n).padStart(2, '0');
export function fmtDate(iso, withTime = true) {
  if (!iso) return '—';
  const d = new Date(iso);
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return withTime ? `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}` : day;
}
export function ago(iso) {
  if (!iso) return '—';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return '방금 전'; if (s < 3600) return `${Math.floor(s / 60)}분 전`; if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}일 전`; return fmtDate(iso, false);
}
export const num = (v, d = 1) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
export const pct = (v, d = 0) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`);
export const sizeText = (s) => (s ? `${s[0].toFixed(0)}×${s[1].toFixed(0)}×${s[2].toFixed(0)} cm` : '—');

// ───────── 토스트 · 모달 ─────────
export function toast(message, kind = 'ok', ms = 3600) {
  const box = document.getElementById('toasts');
  const t = h('div', { class: `toast ${kind}` }, icon(kind === 'bad' ? 'alert' : kind === 'warn' ? 'alert' : 'check', 18), h('div', null, message));
  box.append(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, ms);
}

export function modal({ title, body, actions = [], width = 480 }) {
  return new Promise((resolve) => {
    const close = (v) => { mask.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    const err = h('div', { class: 'notice bad hidden', style: { margin: '0 22px 6px' } });
    const btns = actions.map((a) => h('button', {
      class: `btn ${a.kind || ''}`,
      onclick: async (e) => {
        const b = e.currentTarget; b.disabled = true;
        try {
          const r = a.onClick ? await a.onClick() : true;
          if (r === false) { b.disabled = false; return; }
          close(r ?? true);
        } catch (ex) { err.textContent = ex.message || String(ex); err.classList.remove('hidden'); b.disabled = false; }
      },
    }, a.label));
    const mask = h('div', { class: 'modal-mask', onmousedown: (e) => { if (e.target === mask) close(null); } },
      h('div', { class: 'modal', style: { '--w': `${width}px` }, role: 'dialog', 'aria-modal': 'true' },
        h('div', { class: 'modal-h' }, title), h('div', { class: 'modal-b' }, body), err, h('div', { class: 'modal-f' }, btns)));
    document.body.append(mask);
    document.addEventListener('keydown', onKey);
    mask.querySelector('input,select,textarea')?.focus();
  });
}
export const confirmBox = (message, { title = '확인', danger = false, label = '확인' } = {}) =>
  modal({ title, body: h('div', { style: { whiteSpace: 'pre-wrap', color: '#c8d3e0' } }, message),
    actions: [{ label: '취소', onClick: () => null }, { label, kind: danger ? 'danger' : 'primary', onClick: () => true }] }).then((v) => v === true);

export function field(label, control, hint) {
  return h('div', { class: 'field' }, h('label', null, label), control, hint ? h('div', { class: 'hint' }, hint) : null);
}
export function emptyState(title, text, ic = 'cube') {
  return h('div', { class: 'empty' }, icon(ic, 42), h('b', null, title), h('div', null, text));
}
export function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function download(url) { const a = h('a', { href: url }); document.body.append(a); a.click(); a.remove(); }
