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

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtVal(setting, v) {
  if (setting.type === 'bool') return v ? 'on' : 'off';
  return String(v);
}

function controlHtml(s) {
  const key = esc(s.key);
  if (s.type === 'bool') {
    return `<label class="switch">
      <input type="checkbox" data-key="${key}" ${s.value ? 'checked' : ''}>
      <span class="slider"></span></label>`;
  }
  if (s.type === 'time') {
    return `<input class="field-input" type="time" data-key="${key}" value="${esc(s.value)}">`;
  }
  if (s.type === 'str') {
    return `<input class="field-input wide" type="text" data-key="${key}" value="${esc(s.value)}">`;
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
        <span class="panel-title">${esc(g.name)}</span>
        ${overridden ? `<span class="badge yellow grp-badge">${overridden} changed</span>` : ''}
      </div>
      ${g.settings.map(s => `
        <div class="set-row" data-row="${esc(s.key)}">
          <div class="set-info">
            <div class="set-label">
              ${s.overridden ? '<span class="dot-override" title="Differs from default"></span>' : ''}
              ${esc(s.label)}
            </div>
            ${s.help ? `<div class="set-help">${esc(s.help)}</div>` : ''}
            <div class="set-default">default: ${esc(fmtVal(s, s.default))}${s.bt ? '' : ' · live only'}</div>
          </div>
          <div class="set-ctl">
            ${controlHtml(s)}
            ${s.overridden ? `<button class="btn-mini" data-reset="${esc(s.key)}" title="Reset to default">↺</button>` : ''}
          </div>
        </div>`).join('')}
    `;
    wrap.appendChild(panel);
  });

  wrap.querySelectorAll('[data-key]').forEach(el => {
    el.addEventListener(el.type === 'checkbox' ? 'change' : 'input', onEdit);
  });
  wrap.querySelectorAll('[data-reset]').forEach(el => {
    el.addEventListener('click', () => resetKeys([el.getAttribute('data-reset')]));
  });
  updateSaveBar();
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
  if (setting.type === 'time' || setting.type === 'str') return el.value;
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

function saveChanges() {
  const changes = { ...edits };
  if (!Object.keys(changes).length) return;
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  fetch('/api/settings', {
    method:  'PUT',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ changes }),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      specData = d; edits = {}; render();
      toast('Settings saved — applied live', true);
    })
    .catch(e => toast('Save failed: ' + e.message, false))
    .finally(() => { btn.disabled = false; });
}

function discardChanges() { loadSettings(); }

function resetKeys(keys) {
  fetch('/api/settings/reset', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ keys }),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      specData = d; edits = {}; render();
      toast('Reset to default', true);
    })
    .catch(e => toast('Reset failed: ' + e.message, false));
}

function resetAll() {
  if (!confirm('Reset ALL settings to their built-in defaults?')) return;
  fetch('/api/settings/reset', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({}),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      specData = d; edits = {}; render();
      toast('All settings reset to defaults', true);
    })
    .catch(e => toast('Reset failed: ' + e.message, false));
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

function toggleTheme() {
  const root    = document.documentElement;
  const current = root.getAttribute('data-theme') || 'dark';
  const next    = current === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── Init ───────────────────────────────────────────────────────────────────────

loadSettings();
