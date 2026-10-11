// 뷰어 패널(레이어 칩·버튼·범례)과 자산 3D 로더
import { api } from './api.js';
import { h, icon, toast } from './ui.js';
import { Viewer3D, THREE, applyPose, boxEdges, cardObject, dimensionObject, linesObject, loadModel } from './viewer3d.js';

/** layerDefs: [{key,label,color,visible}] — 칩을 누르면 해당 레이어를 켜고 끈다 */
export function viewerPanel(layerDefs, { legend = [], hint = '드래그: 회전 · 휠: 확대 · 우클릭: 이동' } = {}) {
  const el = h('div', { class: 'viewer' });
  const viewer = new Viewer3D(el);
  const chips = new Map();
  const tools = h('div', { class: 'viewer-tools' });
  for (const d of layerDefs) {
    const chip = h('span', { class: `vchip ${d.visible === false ? 'off' : 'on'}`, style: { '--c': d.color }, role: 'button', tabindex: 0,
      onclick: () => { const v = !viewer.isVisible(d.key); viewer.setVisible(d.key, v); chip.classList.toggle('on', v); chip.classList.toggle('off', !v); } }, h('i'), d.label);
    chips.set(d.key, chip);
    tools.append(chip);
  }
  const btns = h('div', { class: 'viewer-btns' },
    h('button', { class: 'btn sm icon', title: '화면에 맞추기', onclick: () => viewer.fit() }, icon('fit', 16)),
    h('button', { class: 'btn sm icon', title: '스크린샷 저장', onclick: () => {
      const url = viewer.screenshot(); if (!url) return;
      const a = h('a', { href: url, download: `3d-view-${Date.now()}.png` }); document.body.append(a); a.click(); a.remove(); toast('스크린샷을 저장했습니다');
    } }, icon('camera', 16)));
  const lg = legend.length ? h('div', { class: 'viewer-legend' }, legend.map(([c, t]) => h('span', { style: { '--c': c } }, h('i'), t))) : null;
  el.append(tools, btns, lg, h('div', { class: 'viewer-hint' }, hint));
  const msg = h('div', { class: 'viewer-msg' }, '3D 모델 불러오는 중…');
  el.append(msg);
  return {
    el, viewer,
    done: (text) => { if (text) msg.textContent = text; else msg.remove(); },
    sync: () => { for (const d of layerDefs) { const v = viewer.isVisible(d.key); const c = chips.get(d.key); c.classList.toggle('on', v); c.classList.toggle('off', !v); c.style.display = viewer.has(d.key) ? '' : 'none'; } },
  };
}

/** 자산의 표면·점군·윤곽선·외곽 박스·치수·사진 카드를 뷰어에 올린다. pose가 있으면 그 포즈(검수 맞춤 결과)에 놓는다. */
export async function loadAssetLayers(viewer, assetId, { pose = null, prefix = '', outlineColor = 0x4fd1ff, ghost = false } = {}) {
  const put = (name, obj, visible = true) => viewer.add(prefix + name, pose ? applyPose(obj, pose) : obj, visible);
  const [outline, detail] = await Promise.all([
    api(`/assets/${encodeURIComponent(assetId)}/outline`).catch(() => null),
    api(`/assets/${encodeURIComponent(assetId)}`),
  ]);
  if (outline) {
    put('outline', linesObject(outline.lines, outlineColor, ghost ? 0.55 : 1));
    put('box', linesObject(boxEdges(outline.obb.corners), 0xc77dff, ghost ? 0.5 : 0.95));
    if (!ghost) put('dims', dimensionObject(outline.obb.corners, '#c77dff'), true);
  }
  if (detail.hasModel && !ghost) {
    try {
      const { surface, points } = await loadModel(`/admin/api/assets/${encodeURIComponent(assetId)}/model.glb`);
      put('surface', surface, true);
      put('points', points, false);
    } catch (e) { /* 모델 파일 손상: 선화만 보여 준다 */ }
  }
  if (!ghost) {
    const cards = new THREE.Group();
    const metas = detail.photoCards || [];
    for (let i = 0; i < metas.length; i += 8) {  // 64장을 한꺼번에 요청하면 서버 연결이 바닥난다 — 8장씩
      await Promise.all(metas.slice(i, i + 8).map(async (m) => {
        try { cards.add(await cardObject(m, `/admin/api/assets/${encodeURIComponent(assetId)}/photos/${m.side}.png`)); } catch (e) { /* 카드 누락 */ }
      }));
    }
    if (cards.children.length) put('cards', cards, true);
  }
  return { outline, detail };
}
