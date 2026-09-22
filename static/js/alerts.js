'use strict';

// ── Leader-stock price-move alerts ────────────────────────────────────────────
// The actual CONSENSUS alert condition — at least BN_ALERT_CONSENSUS_REQUIRED/
// NF_ALERT_CONSENSUS_REQUIRED leader stocks have EACH crossed their own
// configured per-stock threshold, in raw POINTS (Settings page, "BN/NF
// Alerts" groups; mirrors app/config.py's BN_PRICE_ALERT_ATTR/
// NF_PRICE_ALERT_ATTR wiring), AND agree on direction — is now checked
// SERVER-SIDE, every tick, in app/services/price_alerts.py (explicit user
// decision, 2026-09-09: moved off the browser so it doesn't depend on a
// dashboard tab being open, and fires the instant the condition is met
// rather than only once/sec off STATE_UPDATE). The server pushes a
// `{type: "ALERT"}` WebSocket message when it fires; handleServerAlert
// below just displays it — see dashboard.js's ws.onmessage.
//
// What STAYS client-side: the live "X/N leaders crossed" badge next to the
// Global Signal (checkPriceAlerts/_renderThresholdBadge below) — a coarser,
// once/sec visual readout of the same underlying condition, purely cosmetic,
// not the alert-firing path. Reads the SAME liveLeaderRows/liveLeaderRowsNf
// fields the dashboard already renders from — no extra server payload needed
// for the badge.
//
// Browser note: the Notification API is restricted to secure contexts
// (https, or http://localhost) in current Chrome/Firefox — opening the
// dashboard as http://<lan-ip>:8001 will silently fail to request
// permission. Use http://localhost:8001 (or set up HTTPS) if alerts don't
// appear to do anything when enabled.

// Name -> settings key, mirroring app/config.py's BN_PRICE_ALERT_ATTR/
// NF_PRICE_ALERT_ATTR exactly (kept in sync by hand, same as this file's
// own QTY_AUDIT_LEADER_STOCKS* pattern in qtyAudit.js).
const BN_PRICE_ALERT_KEY = {
  'HDFC BANK': 'BN_PRICE_ALERT_PTS_HDFC', 'ICICI BANK': 'BN_PRICE_ALERT_PTS_ICICI',
  'STATE BANK OF INDIA': 'BN_PRICE_ALERT_PTS_SBI', 'AXIS BANK': 'BN_PRICE_ALERT_PTS_AXIS',
  'KOTAK BANK': 'BN_PRICE_ALERT_PTS_KOTAK', 'INDUSIND BANK': 'BN_PRICE_ALERT_PTS_INDUSIND',
};
const NF_PRICE_ALERT_KEY = {
  'HDFC BANK': 'NF_PRICE_ALERT_PTS_HDFC', 'RELIANCE INDUSTRIES': 'NF_PRICE_ALERT_PTS_RELIANCE',
  'ICICI BANK': 'NF_PRICE_ALERT_PTS_ICICI', 'INFOSYS': 'NF_PRICE_ALERT_PTS_INFY',
  'BHARTI AIRTEL': 'NF_PRICE_ALERT_PTS_BHARTIARTL', 'ITC': 'NF_PRICE_ALERT_PTS_ITC',
  'HCL TECHNOLOGIES': 'NF_PRICE_ALERT_PTS_HCLTECH', 'LARSEN & TOUBRO': 'NF_PRICE_ALERT_PTS_LT',
  'KOTAK BANK': 'NF_PRICE_ALERT_PTS_KOTAK', 'AXIS BANK': 'NF_PRICE_ALERT_PTS_AXIS',
  'STATE BANK OF INDIA': 'NF_PRICE_ALERT_PTS_SBI', 'HINDUSTAN UNILEVER': 'NF_PRICE_ALERT_PTS_HUL',
};

let _alertPtsByKey = {};   // {settings_key: value}, refreshed from /api/settings
let _consensusRequired = { BankNifty: 4, 'Nifty 50': 8 };

function _refreshAlertThresholds() {
  fetch('/api/settings')
    .then(r => r.json())
    .then(d => {
      const flat = (d.groups || []).flatMap(g => g.settings);
      const next = {};
      flat.forEach(s => { if (s.key.includes('_PRICE_ALERT_PTS_')) next[s.key] = Number(s.value); });
      _alertPtsByKey = next;
      const bn = flat.find(s => s.key === 'BN_ALERT_CONSENSUS_REQUIRED');
      const nf = flat.find(s => s.key === 'NF_ALERT_CONSENSUS_REQUIRED');
      if (bn) _consensusRequired.BankNifty = Number(bn.value);
      if (nf) _consensusRequired['Nifty 50'] = Number(nf.value);
    })
    .catch(() => { /* keep last known values */ });
}

function _fireAlert(title, body) {
  if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try { new Notification(title, { body, tag: title }); return; } catch (e) { /* fall through to toast */ }
  }
  if (typeof toast === 'function') toast(`${title}: ${body}`, 'warn');
}

// Updates the persistent on-page banner (next to the Global Signal box) with
// the last-fired alert for one instrument — unlike the Notification/toast
// (which disappears on its own), this stays visible until the next alert for
// that same instrument replaces it. Purely a display of what _fireAlert
// already fired; never re-evaluates the condition itself.
function _renderAlertBanner(elId, title, body) {
  const el = document.getElementById(elId);
  if (!el) return;
  const dir = /moved up together/.test(title) ? 'up' : /moved down together/.test(title) ? 'down' : null;
  const time = new Date().toLocaleTimeString('en-IN', { hour12: false });
  el.textContent = `[${time}] ${title}${body ? ' — ' + body : ''}`;
  el.classList.remove('up', 'down');
  if (dir) el.classList.add(dir);
}

// Handles a server-pushed {type: "ALERT", title, body} WebSocket message
// (see dashboard.js's ws.onmessage) — the server already did the consensus
// check and edge-triggering in app/services/price_alerts.py; this just
// displays it, the same way a client-fired alert used to. title always
// starts with the instr_label price_alerts.py was called with ("BankNifty"
// or "Nifty 50" — see scheduler.py's _tick_alerts), which is how the banner
// picks the right panel.
function handleServerAlert(d) {
  if (!d || !d.title) return;
  _fireAlert(d.title, d.body || '');
  const elId = d.title.startsWith('Nifty 50:') ? 'alert-banner-nf' : 'alert-banner-bn';
  _renderAlertBanner(elId, d.title, d.body || '');
}

// Returns [{stock, crossed, dir}, ...] — feeds ONLY the live threshold badge
// below (_renderThresholdBadge); the actual alert-firing consensus check now
// runs server-side, see the file header comment.
function checkPriceAlerts(leaderRows, keyByStock) {
  const results = [];
  if (!Array.isArray(leaderRows)) return results;
  leaderRows.forEach(r => {
    const settingsKey = keyByStock[r.stock];
    if (!settingsKey || r.open == null || r.close == null) return;
    const pts = _alertPtsByKey[settingsKey];
    if (pts == null) return;   // thresholds not loaded yet
    const movePts = Math.abs(r.close - r.open);
    const dir = r.close > r.open ? 'up' : r.close < r.open ? 'down' : null;
    results.push({ stock: r.stock, crossed: movePts >= pts, dir });
  });
  return results;
}

// Live "X/N leaders crossed" readout next to the Global Signal badge — same
// crossed/direction data the alerts themselves use, just always visible
// instead of only surfacing when a notification fires.
function _renderThresholdBadge(elId, results, required) {
  const el = document.getElementById(elId);
  if (!el) return;
  const up = results.filter(r => r.crossed && r.dir === 'up').length;
  const down = results.filter(r => r.crossed && r.dir === 'down').length;
  const crossed = up + down;
  const total = results.length;
  const dirText = crossed ? ` (${up} up, ${down} down)` : '';
  el.textContent = `${crossed}/${total} leaders crossed their alert threshold${dirText} — need ${required} for a consensus alert`;
  el.classList.toggle('has-crossed', crossed > 0);
}

function checkAllPriceAlerts(liveLeaderRows, liveLeaderRowsNf) {
  const bnResults = checkPriceAlerts(liveLeaderRows, BN_PRICE_ALERT_KEY);
  _renderThresholdBadge('alert-threshold-badge', bnResults, _consensusRequired.BankNifty);

  const nfResults = checkPriceAlerts(liveLeaderRowsNf, NF_PRICE_ALERT_KEY);
  _renderThresholdBadge('alert-threshold-badge-nf', nfResults, _consensusRequired['Nifty 50']);
}

function enableAlerts() {
  if (typeof Notification === 'undefined') {
    toast('Browser notifications are not supported here.', 'err');
    return;
  }
  Notification.requestPermission().then(perm => {
    _updateAlertButton();
    if (perm === 'granted') toast('Price-move alerts enabled.', 'ok');
    else if (perm === 'denied') toast('Notification permission denied in the browser.', 'err');
  });
}

function _updateAlertButton() {
  const btn = document.getElementById('alerts-btn');
  if (!btn) return;
  const supported = typeof Notification !== 'undefined';
  const perm = supported ? Notification.permission : 'unsupported';
  btn.textContent = perm === 'granted' ? '🔔 Alerts on' : '🔕 Enable alerts';
  btn.classList.toggle('active', perm === 'granted');
  btn.title = !supported
    ? 'Notifications not supported in this browser'
    : perm === 'denied'
      ? 'Notification permission denied — re-enable it in your browser\'s site settings'
      : 'Click to enable browser alerts for leader-stock price moves';
}

_updateAlertButton();
_refreshAlertThresholds();
setInterval(_refreshAlertThresholds, 60000);

// (runBnSignalStudy/renderBnSignalStudy removed 2026-09-22 — the
// #signal-study-* panel they targeted was removed from index.html in an
// earlier, unrelated commit; confirmed zero matching element ids anywhere
// in the HTML and no other caller. The backend endpoint they called,
// POST /api/signal-study/bn (app/backtest/signal_study.py), is untouched
// and still works if called directly — only this dead UI glue is gone.)
