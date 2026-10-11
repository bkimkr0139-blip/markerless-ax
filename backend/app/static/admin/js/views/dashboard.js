import { api } from '../api.js';
import { h, icon, decisionBadge, severityBadge, statusBadge, ago, fmtDate, emptyState, STATUS, SEVERITY, DECISION } from '../ui.js';

const COLORS = { PASS: '#2ecc71', FAIL: '#ff5a5f', REVIEW: '#f5a623', RETAKE: '#5c6b7e' };
const charts = [];

function chartDefaults() {
  Chart.defaults.color = '#8b9aad';
  Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif';
  Chart.defaults.borderColor = '#223042';
}

function kpi(label, value, { sub, color, ic, unit } = {}) {
  return h('div', { class: 'card kpi', style: { '--kc': color || '#38a3ff' } },
    h('div', { class: 'ico' }, icon(ic || 'dashboard', 22)),
    h('div', { class: 'lbl' }, label),
    h('div', { class: 'val' }, value, unit ? h('small', null, unit) : null),
    h('div', { class: 'sub' }, sub || ' '));
}

export async function dashboardView(root, _p, ctx, alive) {
  chartDefaults();
  root.append(h('div', { class: 'grid cols-6' }, ...Array.from({ length: 6 }, () => h('div', { class: 'card skeleton', style: { height: '112px' } }))));
  const draw = async () => {
    const d = await api('/dashboard?days=14');
    if (!alive()) return;
    charts.splice(0).forEach((c) => c.destroy());
    root.replaceChildren();
    const k = d.kpi;
    root.append(
      h('div', { class: 'page-h' }, h('div', null, h('h1', null, '운영 현황'), h('p', null, `${fmtDate(new Date().toISOString())} 기준 · 30초마다 자동 갱신`)),
        h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => draw() }, icon('refresh', 16), '새로고침'),
          h('a', { class: 'btn', href: '/admin/api/inspections.csv', download: true }, icon('download', 16), '검수 이력 CSV'))),
      h('div', { class: 'grid cols-6', style: { marginBottom: '16px' } },
        kpi('등록 자산', k.assets, { sub: `활성 ${k.activeAssets} · 공간 ${k.spaces} · 단말 ${k.devices}`, color: '#38a3ff', ic: 'cube' }),
        kpi('누적 검수', k.inspections, { sub: `오늘 ${k.inspectionsToday}건`, color: '#a394ff', ic: 'clipboard' }),
        kpi('합격률', k.passRate == null ? '—' : k.passRate, { unit: k.passRate == null ? '' : '%', sub: '합격 ÷ (합격+불합격+확인)', color: '#2ecc71', ic: 'check' }),
        kpi('열린 이슈', k.openIssues, { sub: k.overdueIssues ? `기한 초과 ${k.overdueIssues}건` : '기한 초과 없음', color: k.overdueIssues ? '#ff5a5f' : '#f5a623', ic: 'alert' }),
        kpi('심각·치명 이슈', k.criticalIssues, { sub: '즉시 조치 필요', color: k.criticalIssues ? '#ff5a5f' : '#5c6b7e', ic: 'shield' }),
        kpi('연결 단말', k.devices, { sub: `공간 ${k.spaces}곳에서 수집 중`, color: '#38a3ff', ic: 'device' })),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0,1.6fr) minmax(0,1fr)', marginBottom: '16px' } },
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '일별 검수 추이'), h('span', { class: 'sub' }, '최근 14일 · 판정별')), h('div', { class: 'card-b' }, h('div', { class: 'chartbox' }, h('canvas', { id: 'c-daily' })))),
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '판정 분포'), h('span', { class: 'sub' }, '누적')), h('div', { class: 'card-b' }, h('div', { class: 'chartbox' }, h('canvas', { id: 'c-dec' }))))),
      h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', marginBottom: '16px' } },
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '조치 워크플로우 현황'), h('div', { class: 'right' }, h('a', { href: '#/issues' }, '이슈 보드 →'))), h('div', { class: 'card-b' }, h('div', { class: 'chartbox sm' }, h('canvas', { id: 'c-issue' })))),
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '불합격 상위 자산'), h('span', { class: 'sub' }, '재점검 우선순위')),
          d.topFailAssets.length ? h('div', null, d.topFailAssets.map((a, i) => h('a', { class: 'listrow', href: `#/assets/${encodeURIComponent(a.assetId)}`, style: { color: 'inherit', textDecoration: 'none' } },
            h('span', { class: 'tag' }, `#${i + 1}`), h('div', { class: 'grow' }, h('div', { class: 't' }, a.name), h('div', { class: 's mono' }, a.assetId)),
            h('span', { class: 'badge bad' }, `불합격 ${a.fails}회`))))
            : emptyState('불합격 이력이 없습니다', '검수에서 불합격이 나온 자산이 여기에 나타납니다.', 'check'))),
      h('div', { class: 'grid', style: { gridTemplateColumns: d.recentAudit ? 'minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr)' : 'minmax(0,1.3fr) minmax(0,1fr)' } },
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '최근 검수'), h('div', { class: 'right' }, h('a', { href: '#/inspections' }, '전체 보기 →'))),
          d.recentInspections.length ? h('div', null, d.recentInspections.map((o) => h('a', { class: 'listrow', href: `#/inspections/${o.id}`, style: { color: 'inherit', textDecoration: 'none' } },
            decisionBadge(o.decision), h('div', { class: 'grow' }, h('div', { class: 't' }, o.assetName), h('div', { class: 's' }, `${o.deviceId || '단말 미상'} · ${ago(o.createdAt)}`)), icon('chevron', 16))))
            : emptyState('검수 이력이 없습니다', '단말 앱에서 검수하면 이곳에 표시됩니다.', 'clipboard')),
        h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '최근 이슈'), h('div', { class: 'right' }, h('a', { href: '#/issues' }, '보드 →'))),
          d.recentIssues.length ? h('div', null, d.recentIssues.map((i) => h('a', { class: 'listrow', href: `#/issues/${i.id}`, style: { color: 'inherit', textDecoration: 'none' } },
            severityBadge(i.severity), h('div', { class: 'grow' }, h('div', { class: 't' }, i.title), h('div', { class: 's' }, `${i.code} · ${ago(i.createdAt)}`)), statusBadge(i.status))))
            : emptyState('이슈가 없습니다', '불합격·확인 필요 검수는 자동으로 이슈가 됩니다.', 'alert')),
        d.recentAudit ? h('div', { class: 'card' }, h('div', { class: 'card-h' }, h('h3', null, '최근 관리 활동'), h('div', { class: 'right' }, h('a', { href: '#/audit' }, '감사 로그 →'))),
          h('div', null, d.recentAudit.map((a) => h('div', { class: 'listrow', style: { cursor: 'default' } }, h('div', { class: 'grow' }, h('div', { class: 't', style: { fontSize: '13px' } }, `${a.actor} · ${a.action}`), h('div', { class: 's' }, `${a.target} · ${ago(a.at)}`)))))) : null));

    // 차트
    const daily = d.daily;
    charts.push(new Chart(root.querySelector('#c-daily'), {
      type: 'bar',
      data: { labels: daily.map((x) => x.date.slice(5)), datasets: ['PASS', 'REVIEW', 'FAIL', 'RETAKE'].map((dec) => ({ label: DECISION[dec].label, data: daily.map((x) => x[dec]), backgroundColor: COLORS[dec], borderRadius: 3, maxBarThickness: 26 })) },
      options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } } }, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } } },
    }));
    charts.push(new Chart(root.querySelector('#c-dec'), {
      type: 'doughnut',
      data: { labels: Object.keys(d.decisions).map((x) => DECISION[x].label), datasets: [{ data: Object.values(d.decisions), backgroundColor: Object.keys(d.decisions).map((x) => COLORS[x]), borderColor: '#121923', borderWidth: 3 }] },
      options: { maintainAspectRatio: false, cutout: '66%', plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } } } },
    }));
    const order = ['OPEN', 'CONFIRMED', 'ASSIGNED', 'IN_PROGRESS', 'RESOLVED', 'REINSPECT', 'CLOSED', 'DISMISSED'];
    charts.push(new Chart(root.querySelector('#c-issue'), {
      type: 'bar',
      data: { labels: order.map((s) => STATUS[s].label), datasets: [{ data: order.map((s) => d.issuesByStatus[s]), backgroundColor: order.map((s) => STATUS[s].color), borderRadius: 4, maxBarThickness: 24 }] },
      options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } }, y: { grid: { display: false } } } },
    }));
  };
  await draw();
  const timer = setInterval(() => { if (!alive() || !root.isConnected) { clearInterval(timer); return; } draw().catch(() => {}); }, 30000);
}
