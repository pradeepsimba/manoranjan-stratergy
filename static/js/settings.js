'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
// specData: the last server describe() payload; edits: {key: newValue} not yet
// saved (removed again when the input returns to the server value).

let specData = null;
let edits = {};

const wrap = document.getElementById('settings-wrap');

// ── Fetch + render ─────────────────────────────────────────────────────────────

function loadSettings() {
  fetch('/api/settings')
    .then(r => r.json())
    .then(d => { specData = d; edits = {}; render(); })
    .catch(e => toast('Failed to load settings: ' + e.message, false));
}

// (escHtml lives in the shared /js/util.js)

function fmtVal(setting, v) {
  if (setting.type === 'bool') return v ? 'on' : 'off';
  if (setting.type === 'rules') {
    if (!v || !v.enabled) return 'off';
    const n = (v.groups || []).length;
    return `${v.mode === 'replace' ? 'replace' : 'and'} · ${n} group${n !== 1 ? 's' : ''}`;
  }
  return String(v);
}

function controlHtml(s) {
  const key = escHtml(s.key);
  if (s.type === 'rules') return '';   // rendered by the dedicated builder below
  if (s.type === 'bool') {
    return `<label class="switch">
      <input type="checkbox" data-key="${key}" ${s.value ? 'checked' : ''}>
      <span class="slider"></span></label>`;
  }
  if (s.type === 'time') {
    return `<input class="field-input" type="time" data-key="${key}" value="${escHtml(s.value)}">`;
  }
  if (s.type === 'str') {
    return `<input class="field-input wide" type="text" data-key="${key}" value="${escHtml(s.value)}">`;
  }
  if (s.type === 'choice') {
    const opts = (s.choices || []).map(c =>
      `<option value="${escHtml(c)}"${c === s.value ? ' selected' : ''}>${escHtml(c)}</option>`).join('');
    return `<select class="field-input" data-key="${key}">${opts}</select>`;
  }
  const step = s.step != null ? s.step : (s.type === 'int' ? 1 : 'any');
  const min  = s.min  != null ? `min="${s.min}"` : '';
  const max  = s.max  != null ? `max="${s.max}"` : '';
  return `<input class="field-input" type="number" data-key="${key}" value="${s.value}"
          step="${step}" ${min} ${max}>`;
}

// ── Custom-rule builder (settings type "rules") ────────────────────────────────
// Working drafts keyed by setting key; mutations sync into `edits` so the rule
// set saves through the same PUT /api/settings pipeline as every other setting.
const _ruleDrafts = {};

const _OP_LABEL = { lt: '<', lte: '≤', gt: '>', gte: '≥', eq: '=', neq: '≠', between: 'between' };
const _BOOL_OPS = ['eq', 'neq'];

function _deepCopy(o) { return JSON.parse(JSON.stringify(o)); }
function _rulesEqual(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

function _fieldMeta(s, key) {
  return (s.fields || []).find(f => f.key === key) || { key, label: key, kind: 'num' };
}

function ruleBuilderHtml(s, draft) {
  const fieldOpts = fld => (s.fields || []).map(f =>
    `<option value="${escHtml(f.key)}"${f.key === fld ? ' selected' : ''}>${escHtml(f.label)}</option>`).join('');
  const opOpts = (kind, op) => (kind === 'bool' ? _BOOL_OPS : (s.ops || [])).map(o =>
    `<option value="${o}"${o === op ? ' selected' : ''}>${_OP_LABEL[o] || o}</option>`).join('');

  const groupsHtml = (draft.groups || []).map((g, gi) => {
    const rows = g.map((cl, ci) => {
      const kind = _fieldMeta(s, cl.field).kind;
      const valCtl = kind === 'bool'
        ? `<select class="field-input rb-in" data-g="${gi}" data-c="${ci}" data-prop="value">
             <option value="true"${cl.value === true ? ' selected' : ''}>true</option>
             <option value="false"${cl.value === false ? ' selected' : ''}>false</option></select>`
        : `<input class="field-input rb-in rb-num" type="number" step="any" data-g="${gi}" data-c="${ci}"
                  data-prop="value" value="${cl.value}">` +
          (cl.op === 'between'
            ? ` <span class="rb-and">and</span>
                <input class="field-input rb-in rb-num" type="number" step="any" data-g="${gi}" data-c="${ci}"
                       data-prop="value2" value="${cl.value2 != null ? cl.value2 : ''}">`
            : '');
      return `<div class="rb-clause">
        <select class="field-input rb-in" data-g="${gi}" data-c="${ci}" data-prop="field">${fieldOpts(cl.field)}</select>
        <select class="field-input rb-in" data-g="${gi}" data-c="${ci}" data-prop="op">${opOpts(kind, cl.op)}</select>
        ${valCtl}
        <button class="btn-mini rb-del" data-g="${gi}" data-c="${ci}" title="Remove condition">×</button>
      </div>`;
    }).join('');
    return `<div class="rb-group">
      <div class="rb-group-head"><span>Group ${gi + 1} — all must match</span>
        <button class="btn-mini rb-del-group" data-g="${gi}" title="Remove group">×</button></div>
      ${rows}
      <button class="btn-mini rb-add-clause" data-g="${gi}">+ AND condition</button>
    </div>`;
  }).join('<div class="rb-or">— OR —</div>');

  return `
    <div class="rb-head">
      <label class="switch"><input type="checkbox" class="rb-enabled"${draft.enabled ? ' checked' : ''}>
        <span class="slider"></span></label>
      <span class="rb-head-lbl">${draft.enabled ? 'Enabled' : 'Disabled'}</span>
      <select class="field-input rb-mode">
        <option value="and"${draft.mode === 'and' ? ' selected' : ''}>Add to fixed conditions (AND)</option>
        <option value="replace"${draft.mode === 'replace' ? ' selected' : ''}>Replace fixed conditions</option>
      </select>
    </div>
    ${groupsHtml || '<div class="rb-empty">No rule groups yet.</div>'}
    <button class="btn-mini rb-add-group">+ OR group</button>`;
}

function renderRuleBuilder(s) {
  const host = wrap.querySelector(`[data-rules="${CSS.escape(s.key)}"]`);
  if (!host) return;
  host.innerHTML = ruleBuilderHtml(s, _ruleDrafts[s.key]);

  // Bind ONCE per host element. sync() re-renders into the SAME host, so an
  // unconditional addEventListener would stack a new handler pair on every
  // edit — each subsequent click/change would then apply N times (e.g. one
  // "+ AND condition" click adding several clauses). Handlers read the draft
  // through _ruleDrafts[s.key] (not a closure) so a later render()'s reseed
  // is always picked up.
  if (host._rbBound) return;
  host._rbBound = true;

  const sync = () => {
    const draft = _ruleDrafts[s.key];
    if (_rulesEqual(draft, s.value)) delete edits[s.key];
    else edits[s.key] = _deepCopy(draft);
    const row = wrap.querySelector(`[data-row="${CSS.escape(s.key)}"]`);
    if (row) row.classList.toggle('dirty', s.key in edits);
    updateSaveBar();
    renderRuleBuilder(s);            // repaint (structure may have changed)
  };

  host.addEventListener('change', e => {
    const t = e.target;
    const draft = _ruleDrafts[s.key];
    if (t.classList.contains('rb-enabled')) { draft.enabled = t.checked; sync(); return; }
    if (t.classList.contains('rb-mode'))    { draft.mode = t.value; sync(); return; }
    if (!t.classList.contains('rb-in')) return;
    const cl = draft.groups[+t.dataset.g][+t.dataset.c];
    const prop = t.dataset.prop;
    if (prop === 'field') {
      cl.field = t.value;
      const kind = _fieldMeta(s, cl.field).kind;
      if (kind === 'bool') { cl.op = 'eq'; cl.value = true; delete cl.value2; }
      else if (typeof cl.value === 'boolean') { cl.op = 'gt'; cl.value = 0; }
    } else if (prop === 'op') {
      cl.op = t.value;
      if (cl.op === 'between' && cl.value2 == null) cl.value2 = Number(cl.value) + 1;
      if (cl.op !== 'between') delete cl.value2;
    } else {
      const kind = _fieldMeta(s, cl.field).kind;
      cl[prop] = kind === 'bool' ? (t.value === 'true') : parseFloat(t.value);
      if (Number.isNaN(cl[prop])) cl[prop] = 0;
    }
    sync();
  });

  host.addEventListener('click', e => {
    const t = e.target;
    const draft = _ruleDrafts[s.key];
    if (t.classList.contains('rb-add-group')) {
      draft.groups.push([{ field: 'rsi', op: 'lt', value: 30 }]); sync();
    } else if (t.classList.contains('rb-add-clause')) {
      draft.groups[+t.dataset.g].push({ field: 'rsi', op: 'lt', value: 30 }); sync();
    } else if (t.classList.contains('rb-del-group')) {
      draft.groups.splice(+t.dataset.g, 1); sync();
    } else if (t.classList.contains('rb-del')) {
      const g = draft.groups[+t.dataset.g];
      g.splice(+t.dataset.c, 1);
      if (!g.length) draft.groups.splice(+t.dataset.g, 1);
      sync();
    }
  });
}

// One setting row. `nested` = rendered indented under its condition toggle.
function rowHtml(s, nested) {
  return `
    <div class="set-row${nested ? ' set-nested' : ''}" data-row="${escHtml(s.key)}">
      <div class="set-info">
        <div class="set-label">
          ${s.overridden ? '<span class="dot-override" title="Differs from default"></span>' : ''}
          ${escHtml(s.label)}
        </div>
        ${s.help ? `<div class="set-help">${escHtml(s.help)}</div>` : ''}
        <div class="set-default">default: ${escHtml(fmtVal(s, s.default))}${s.bt ? '' : ' · live only'}</div>
      </div>
      <div class="set-ctl">
        ${controlHtml(s)}
        ${s.overridden ? `<button class="btn-mini" data-reset="${escHtml(s.key)}" title="Reset to default">↺</button>` : ''}
      </div>
    </div>${s.type === 'rules' ? `<div class="rb-wrap" data-rules="${escHtml(s.key)}"></div>` : ''}`;
}

function render() {
  const groups = (specData && specData.groups) || [];
  wrap.querySelectorAll('.panel').forEach(p => p.remove());
  const loading = document.getElementById('loading-note');
  if (loading) loading.remove();

  // Map COND_* toggle key → its linked value settings (gathered across ALL
  // groups) so each condition's thresholds render inline under its toggle.
  const linked = {};
  groups.forEach(g => g.settings.forEach(s => {
    if (s.cond) (linked[s.cond] = linked[s.cond] || []).push(s);
  }));

  groups.forEach(g => {
    // Skip settings that are shown nested under a condition elsewhere.
    const own = g.settings.filter(s => !s.cond);
    // Count "changed" over what actually RENDERS in this panel: own rows plus
    // any linked value rows nested under this group's condition toggles (which
    // may live in another group). Otherwise the badge and visible rows disagree.
    const rendered = own.concat(...own.map(s => linked[s.key] || []));
    const overridden = rendered.filter(s => s.overridden).length;
    const panel = document.createElement('div');
    panel.className = 'panel';
    const rows = own.map(s => {
      let html = rowHtml(s, false);
      // A condition toggle with linked values: render them indented below it.
      if (linked[s.key]) html += linked[s.key].map(ls => rowHtml(ls, true)).join('');
      return html;
    }).join('');
    panel.innerHTML = `
      <div class="panel-header">
        <span class="panel-title">${escHtml(g.name)}</span>
        ${overridden ? `<span class="badge yellow grp-badge">${overridden} changed</span>` : ''}
      </div>
      ${rows}
    `;
    wrap.appendChild(panel);
  });

  wrap.querySelectorAll('[data-key]').forEach(el => {
    const evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
    el.addEventListener(evt, onEdit);
  });
  wrap.querySelectorAll('[data-reset]').forEach(el => {
    el.addEventListener('click', () => resetKeys([el.getAttribute('data-reset')]));
  });

  // Rule-builder settings: (re)seed the draft from pending edits or the server
  // value, then paint the builder into its host container.
  groups.forEach(g => g.settings.forEach(s => {
    if (s.type !== 'rules') return;
    _ruleDrafts[s.key] = _deepCopy(s.key in edits ? edits[s.key] : s.value);
    renderRuleBuilder(s);
  }));

  updateSaveBar();
  applyFilter();   // re-apply the active text filter to the freshly built panels
}

// ── Live filter ────────────────────────────────────────────────────────────────
let _filter = '';

function filterSettings(q) { _filter = (q || '').trim().toLowerCase(); applyFilter(); }

function applyFilter() {
  const q = _filter;
  let anyVisible = false;
  wrap.querySelectorAll('.panel').forEach(panel => {
    let shown = 0;
    panel.querySelectorAll('.set-row').forEach(row => {
      const hay = (row.textContent + ' ' + (row.getAttribute('data-row') || '')).toLowerCase();
      const hit = !q || hay.indexOf(q) !== -1;
      row.classList.toggle('filtered-out', !hit);
      // A rule-builder body lives OUTSIDE its .set-row — mirror the row's
      // visibility onto it so filtering hides/shows the whole widget.
      const key = row.getAttribute('data-row');
      const rb = key && panel.querySelector(`[data-rules="${CSS.escape(key)}"]`);
      if (rb) rb.classList.toggle('filtered-out', !hit);
      if (hit) shown++;
    });
    panel.classList.toggle('filtered-out', shown === 0);
    if (shown > 0) anyVisible = true;
  });
  let nr = document.getElementById('no-results');
  if (q && !anyVisible) {
    if (!nr) {
      nr = document.createElement('div');
      nr.id = 'no-results'; nr.className = 'no-results';
      wrap.appendChild(nr);
    }
    nr.textContent = 'No settings match “' + q + '”';
    nr.style.display = '';
  } else if (nr) {
    nr.style.display = 'none';
  }
}

// ── Edit tracking ──────────────────────────────────────────────────────────────

function findSetting(key) {
  for (const g of specData.groups)
    for (const s of g.settings)
      if (s.key === key) return s;
  return null;
}

function readControl(el, setting) {
  if (setting.type === 'bool') return el.checked;
  if (setting.type === 'time' || setting.type === 'str' || setting.type === 'choice') return el.value;
  const n = parseFloat(el.value);
  return Number.isNaN(n) ? null : n;
}

function onEdit(e) {
  const key = e.target.getAttribute('data-key');
  const s   = findSetting(key);
  if (!s) return;
  const val = readControl(e.target, s);
  if (val === null || val === s.value || String(val) === String(s.value)) {
    delete edits[key];
  } else {
    edits[key] = val;
  }
  const row = wrap.querySelector(`[data-row="${CSS.escape(key)}"]`);
  if (row) row.classList.toggle('dirty', key in edits);
  updateSaveBar();
}

function updateSaveBar() {
  const n   = Object.keys(edits).length;
  const bar = document.getElementById('save-bar');
  bar.classList.toggle('visible', n > 0);
  document.getElementById('save-count').textContent =
    n + ' unsaved change' + (n !== 1 ? 's' : '');
}

// ── Actions ────────────────────────────────────────────────────────────────────

// Shared submit: every mutation endpoint returns the fresh describe() payload,
// so success handling is identical — re-render from the response.
function submitSettings(method, url, body, okMsg, failLabel) {
  return fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText);
      specData = d; edits = {}; render();
      toast(okMsg, true);
    })
    .catch(e => toast(failLabel + ': ' + e.message, false));
}

function saveChanges() {
  const changes = { ...edits };
  if (!Object.keys(changes).length) return;
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  submitSettings('PUT', '/api/settings', { changes },
                 'Settings saved — applied live', 'Save failed')
    .finally(() => { btn.disabled = false; });
}

function discardChanges() { loadSettings(); }

function resetKeys(keys) {
  submitSettings('POST', '/api/settings/reset', { keys },
                 'Reset to default', 'Reset failed');
}

function resetAll() {
  if (!confirm('Reset ALL settings to their built-in defaults?')) return;
  submitSettings('POST', '/api/settings/reset', {},
                 'All settings reset to defaults', 'Reset failed');
}

// ── Toast / theme ──────────────────────────────────────────────────────────────

let toastTimer = null;
function toast(msg, ok) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

// (toggleTheme lives in the shared /js/util.js)

// ── Init ───────────────────────────────────────────────────────────────────────

loadSettings();
