/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = { user: '', lab: '', items: [], selected: null, plantPanel: null, draft: { left: '', right: '' } };

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.detail || j.error || '请求失败');
    err.payload = j;
    err.status = r.status;
    throw err;
  }
  return j;
}

async function loadPanel() {
  try {
    state.plantPanel = await api('/api/merge/panel' + (state.selected ? '?flow_id=' + state.selected.id : ''));
  } catch (e) {
    state.plantPanel = { error: String(e) };
  }
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    state.items = (await api('/api/flows')).items;
    await loadPanel();
    render();
  } catch (e) {
    location.href = '/login';
  }
}

async function select(id) {
  state.selected = await api('/api/flows/' + id);
  // 切换编排流时用该流的编码/来源作为两路源初值，之后保留用户改动。
  state.draft = { left: state.selected.code || '', right: state.selected.source_uri || '' };
  await loadPanel();
  render();
}

async function doExtract() {
  state.selected = await api('/api/flows/' + state.selected.id + '/extract', { method: 'POST' });
  state.items = (await api('/api/flows')).items;
  render();
}

async function doLand() {
  try {
    state.selected = await api('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
  } catch (e) {
    alert(e.message || '入库被拒绝');
  }
  state.items = (await api('/api/flows')).items;
  render();
}

function escapeAttr(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function logout() {  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

async function doMerge() {
  const flowId = state.selected && state.selected.id;
  if (!flowId) { alert('请先在左侧选择一条编排流'); return; }
  const leftEl = document.getElementById('merge-left');
  const rightEl = document.getElementById('merge-right');
  const left = leftEl ? leftEl.value : state.draft.left;
  const right = rightEl ? rightEl.value : state.draft.right;
  state.draft = { left, right };
  const payload = {
    flow_id: flowId,
    left,
    right,
    // 打开合并页时拿到的快照；确认合并时后端会按最新源值重算冲突集。
    snapshot: state.plantPanel && state.plantPanel.snapshot,
  };
  try {
    const out = await api('/api/merge/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    state.plantPanel = { ...state.plantPanel, lastAction: out };
  } catch (e) {
    state.plantPanel = { ...state.plantPanel, lastAction: e.payload || { error: e.message } };
  }
  await loadPanel();
  state.items = (await api('/api/flows')).items;
  render();
}

function mergePanelHtml() {
  const p = state.plantPanel;
  if (!p) return '';
  if (p.error) return `<section class="detail plant-panel"><h3>双源合并</h3><p class="err">${p.error}</p></section>`;
  const jobs = p.jobs || [];
  const box = p.box || [];
  const conflicts = p.conflict_count != null ? p.conflict_count : jobs.filter((j) => j.box).length;
  const rows = jobs.map((j) => `
      <tr>
        <td class="mono">${j.key}</td>
        <td>${j.winner}</td>
        <td>${j.box ? '冲突留主' : '无冲突'}</td>
        <td>${j.status || '已合并'}</td>
      </tr>`).join('');
  const boxRows = box.map((b) => {
    let parsed = b.body;
    try { parsed = JSON.parse(b.body); } catch (e) { /* 保留原文 */ }
    return `<li class="mono">#${b.job_id} ${typeof parsed === 'object' ? JSON.stringify(parsed) : parsed}</li>`;
  }).join('');
  const last = p.lastAction ? `<p class="hint">上次合并：${JSON.stringify(p.lastAction)}</p>` : '';
  return `
    <section class="detail plant-panel">
      <h3>${p.list_label || '双源列表'}</h3>
      <p class="hint">合并单 ${jobs.length} · 冲突 ${conflicts} · 冲突匣 ${p.box_count != null ? p.box_count : box.length} · 策略 ${p.strategy_name || '留主'}</p>
      <table class="merge-table">
        <thead><tr><th>合并键</th><th>留存</th><th>冲突</th><th>状态</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="hint">暂无合并单</td></tr>'}</tbody>
      </table>
      <h4>冲突匣</h4>
      <ul class="conflict-box">${boxRows || '<li class="hint">匣内暂无内容</li>'}</ul>
      <label>主源<input id="merge-left" value="${escapeAttr(state.draft.left)}" /></label>
      <label>次源<input id="merge-right" value="${escapeAttr(state.draft.right)}" /></label>
      <button type="button" id="plant-act">确认合并</button>
      ${last}
    </section>`;
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${it.title}</strong>
      <span class="mono">${it.code}</span>
      <span class="chip">${it.status}</span>
    </button>`).join('');
  const detail = state.selected ? `
    <section class="detail">
      <h2>${state.selected.title}</h2>
      <p class="mono">${state.selected.source_uri}</p>
      <p>状态 <span class="chip">${state.selected.status}</span> · 已抽取 ${state.selected.extracted} · 已落库 ${state.selected.landed}</p>
      <div class="actions">
        <button type="button" id="btn-ex">执行抽取</button>
        <button type="button" id="btn-land" class="ghost">执行落库</button>
      </div>
    </section>` : '<p class="hint">从左侧选择一条编排流</p>';
  el.innerHTML = `
    <header class="bar"><div class="brand">${state.lab}</div><div>${state.user} · <button type="button" id="out" class="link">退出</button></div></header>
    <div class="layout"><aside>${rows}</aside><main>${detail}${mergePanelHtml()}</main></div>`;
  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const pa = el.querySelector('#plant-act'); if (pa) pa.onclick = doMerge;
}

boot();
