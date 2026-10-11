// 백오피스 API 클라이언트 — 세션 쿠키 + CSRF 헤더
export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function detailText(d) {
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => x.msg || JSON.stringify(x)).join(', ');
  return JSON.stringify(d);
}

export async function api(path, { method = 'GET', body, raw = false } = {}) {
  const headers = { 'X-Requested-With': 'markerless-admin' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch('/admin/api' + path, { method, credentials: 'same-origin', headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (e) {
    throw new ApiError(0, '서버에 연결할 수 없습니다. 네트워크를 확인하세요.');
  }
  if (res.status === 401 && !path.startsWith('/login')) {
    window.dispatchEvent(new Event('auth:expired'));
    throw new ApiError(401, '로그인이 필요합니다');
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = detailText((await res.json()).detail); } catch { /* 본문 없음 */ }
    throw new ApiError(res.status, msg);
  }
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

export const qs = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== '' && v != null && v !== false) p.set(k, v);
  const s = p.toString();
  return s ? '?' + s : '';
};
