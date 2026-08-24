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
  return String(v);
}

function controlHtml(s) {
  const key = escHtml(s.key);
  // Show the PENDING edit when one exists — a re-render (e.g. after resetting a
  // different key) must not repaint controls with server values and visually
  // discard the user's unsaved input.
  const v = (s.key in edits) ? edits[s.key] : s.value;
  if (s.type === 'bool') {
    return `<label class="switch">
      <input type="checkbox" data-key="${key}" ${v ? 'checked' : ''}>
      <span class="slider"></span></label>`;
  }
  if (s.type === 'time') {
    return `<input class="field-input" type="time" data-key="${key}" value="${escHtml(v)}">`;
  }
  if (s.type === 'str') {
    return `<input class="field-input wide" type="text" data-key="${key}" value="${escHtml(v)}">`;
  }
  if (s.type === 'choice') {
    const opts = (s.choices || []).map(c =>
      `<option value="${escHtml(c)}"${c === v ? ' selected' : ''}>${escHtml(c)}</option>`).join('');
    return `<select class="field-input" data-key="${key}">${opts}</select>`;
  }
  const step = s.step != null ? s.step : (s.type === 'int' ? 1 : 'any');
  const min  = s.min  != null ? `min="${s.min}"` : '';
  const max  = s.max  != null ? `max="${s.max}"` : '';
  return `<input class="field-input" type="number" data-key="${key}" value="${v}"
          step="${step}" ${min} ${max}>`;
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
    </div>`;
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
    // BN Strategy/BN Qty Surge/BN Risk/BN Options Pricing/BN Options Costs →
    // "bn"; the NF mirrors → "nf"; Session Timings/Engine/Backtest are
    // shared (no tag — always shown regardless of the instrument filter).
    if (g.name.startsWith('BN ')) panel.dataset.instr = 'bn';
    else if (g.name.startsWith('NF ')) panel.dataset.instr = 'nf';
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

  // Pending (unsaved) edits survive a re-render — restore their dirty markers.
  Object.keys(edits).forEach(k => {
    const row = wrap.querySelector(`[data-row="${CSS.escape(k)}"]`);
    if (row) row.classList.add('dirty');
  });

  updateSaveBar();
  applyFilter();   // re-apply the active text filter to the freshly built panels
}

// ── Live filter ────────────────────────────────────────────────────────────────
let _filter = '';

function filterSettings(q) { _filter = (q || '').trim().toLowerCase(); applyFilter(); }

// Which instrument's groups to show — independent of the dashboard's own
// Both/BankNifty/Nifty 50 toggle (separate localStorage key), and defaults
// to Nifty 50 here rather than Both.
let _settingsInstrFilter = localStorage.getItem('settingsInstrFilter') || 'nf';

function setSettingsInstrFilter(which) {
  _settingsInstrFilter = which;
  localStorage.setItem('settingsInstrFilter', which);
  ['both', 'bn', 'nf'].forEach(w => {
    const btn = document.getElementById('settings-instr-btn-' + w);
    if (btn) btn.classList.toggle('active', w === which);
  });
  applyFilter();
}

function applyFilter() {
  const q = _filter;
  let anyVisible = false;
  wrap.querySelectorAll('.panel').forEach(panel => {
    const instrOk = _settingsInstrFilter === 'both'
      || !panel.dataset.instr || panel.dataset.instr === _settingsInstrFilter;
    let shown = 0;
    panel.querySelectorAll('.set-row').forEach(row => {
      const hay = (row.textContent + ' ' + (row.getAttribute('data-row') || '')).toLowerCase();
      const hit = !q || hay.indexOf(q) !== -1;
      row.classList.toggle('filtered-out', !hit);
      if (hit) shown++;
    });
    const visible = instrOk && shown > 0;
    panel.classList.toggle('filtered-out', !visible);
    if (visible) anyVisible = true;
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
// keepEdits: pending edits to PRESERVE across the re-render — resetting one key
// must not silently discard the user's unsaved changes to OTHER keys (the
// controls re-paint from `edits` first, so kept values stay visible).
function submitSettings(method, url, body, okMsg, failLabel, keepEdits) {
  return fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText);
      specData = d; edits = keepEdits || {}; render();
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
  const keep = { ...edits };
  keys.forEach(k => delete keep[k]);   // only the reset key's own pending edit goes
  submitSettings('POST', '/api/settings/reset', { keys },
                 'Reset to default', 'Reset failed', keep);
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

['both', 'bn', 'nf'].forEach(w => {
  const btn = document.getElementById('settings-instr-btn-' + w);
  if (btn) btn.classList.toggle('active', w === _settingsInstrFilter);
});
loadSettings();
