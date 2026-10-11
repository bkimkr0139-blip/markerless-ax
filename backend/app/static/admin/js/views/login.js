import { api } from '../api.js';
import { h, clear, icon } from '../ui.js';

export function renderLogin(root, onSuccess) {
  clear(root);
  const err = h('div', { class: 'notice bad hidden', style: { marginBottom: '14px' }, role: 'alert' });
  const user = h('input', { id: 'u', name: 'username', autocomplete: 'username', autofocus: true, placeholder: '아이디' });
  const pw = h('input', { id: 'p', name: 'password', type: 'password', autocomplete: 'current-password', placeholder: '비밀번호' });
  const btn = h('button', { class: 'btn primary', type: 'submit', style: { width: '100%', padding: '11px', fontSize: '14.5px' } }, '로그인');
  const form = h('form', { onsubmit: async (e) => {
    e.preventDefault();
    btn.disabled = true; btn.textContent = '확인 중…'; err.classList.add('hidden');
    try {
      await api('/login', { method: 'POST', body: { username: user.value.trim(), password: pw.value } });
      await onSuccess();
    } catch (ex) {
      err.textContent = ex.message; err.classList.remove('hidden');
      btn.disabled = false; btn.textContent = '로그인'; pw.value = ''; pw.focus();
    }
  } },
  err,
  h('div', { class: 'field' }, h('label', { for: 'u' }, '아이디'), user),
  h('div', { class: 'field' }, h('label', { for: 'p' }, '비밀번호'), pw),
  btn);
  root.append(h('div', { class: 'login' },
    h('div', { class: 'login-card' },
      h('div', { class: 'logo', html: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5l8.5 4.8v9.4L12 21.5l-8.5-4.8V7.3z"/><path d="M12 12l8.5-4.7M12 12v9.5M12 12L3.5 7.3"/></svg>' }),
      h('h1', null, 'MARKERLESS INSPECT'),
      h('p', null, '마커리스 현장검수 자동화 · 통합 백오피스'),
      form,
      h('div', { class: 'login-foot' }, icon('shield', 14), ' 승인된 관리자·현장 검수자만 접속할 수 있습니다.', h('br'), '모든 접속과 변경 이력은 감사 로그에 기록됩니다.'))));
}

/** 기본/임시 비밀번호로 로그인한 계정 — 새 비밀번호로 바꾸기 전에는 아무 화면도 열지 않는다 (서버도 같은 규칙으로 막는다). */
export function renderForcedChange(root, user, onDone) {
  clear(root);
  const err = h('div', { class: 'notice bad hidden', style: { marginBottom: '14px' }, role: 'alert' });
  const cur = h('input', { type: 'password', autocomplete: 'current-password', placeholder: '현재(기본/임시) 비밀번호' });
  const nw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: '새 비밀번호' });
  const nw2 = h('input', { type: 'password', autocomplete: 'new-password', placeholder: '새 비밀번호 확인' });
  const btn = h('button', { class: 'btn primary', type: 'submit', style: { width: '100%', padding: '11px', fontSize: '14.5px' } }, '비밀번호 변경하고 시작');
  const form = h('form', { onsubmit: async (e) => {
    e.preventDefault();
    err.classList.add('hidden');
    if (nw.value !== nw2.value) { err.textContent = '새 비밀번호가 서로 다릅니다'; err.classList.remove('hidden'); return; }
    btn.disabled = true;
    try { await api('/me/password', { method: 'POST', body: { current: cur.value, new: nw.value } }); await onDone(); }
    catch (ex) { err.textContent = ex.message; err.classList.remove('hidden'); btn.disabled = false; }
  } }, err,
  h('div', { class: 'field' }, h('label', null, '현재 비밀번호'), cur),
  h('div', { class: 'field' }, h('label', null, '새 비밀번호'), nw, h('div', { class: 'muted', style: { fontSize: '12px', marginTop: '4px' } }, '10자 이상 · 영문 대/소문자·숫자·기호 중 3가지 이상 · 아이디 포함 금지')),
  h('div', { class: 'field' }, h('label', null, '새 비밀번호 확인'), nw2), btn);
  root.append(h('div', { class: 'login' }, h('div', { class: 'login-card' },
    h('div', { class: 'logo', html: icon('shield', 26).outerHTML || '' }),
    h('h1', null, '비밀번호 변경 필요'),
    h('p', null, `${user.displayName || user.username} 계정은 기본/임시 비밀번호를 쓰고 있습니다. 이 사이트는 외부 인터넷에서 접속할 수 있어 지금 바꿔야 합니다.`),
    form)));
}
