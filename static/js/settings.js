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

function render() {
  const groups = (specData && specData.groups) || [];
  wrap.querySelectorAll('.panel').forEach(p => p.remove());
  const loading = document.getElementById('loading-note');
  if (loading) loading.remove();

  groups.forEach(g => {
    const overridden = g.settings.filter(s => s.overridden).length;
    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.innerHTML = `
      <div class="panel-header">
        <span class="panel-title">${escHtml(g.name)}</span>
        ${overridden ? `<span class="badge yellow grp-badge">${overridden} changed</span>` : ''}
      </div>
      ${g.settings.map(s => `
        <div class="set-row" data-row="${escHtml(s.key)}">
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
        </div>`).join('')}
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
