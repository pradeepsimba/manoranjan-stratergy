'use strict';

// ── Shared page utilities ──────────────────────────────────────────────────────
// Included by every page BEFORE its page script (index/dashboard.js,
// indicators/indicators.js, settings/settings.js). Keep helpers that must
// behave identically across pages here — duplicating them per page lets a
// fix land on one page and miss the others.

// HTML-escape for interpolating untrusted text (stock symbols, setting values)
// into innerHTML templates.
function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Light/dark theme flip — the boot snippet in each page's <head> applies the
// persisted choice before first paint; this toggles and persists it.
function toggleTheme() {
  const root    = document.documentElement;
  const current = root.getAttribute('data-theme') || 'dark';
  const next    = current === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}
