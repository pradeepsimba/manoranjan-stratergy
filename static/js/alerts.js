'use strict';

// ── Leader-stock price-move alerts ────────────────────────────────────────────
// Client-side only: fires a single browser Notification (falling back to the
// in-page toast if permission isn't granted/supported) only on the CONSENSUS
// condition — at least BN_ALERT_CONSENSUS_REQUIRED/NF_ALERT_CONSENSUS_REQUIRED
// leader stocks have EACH crossed their own configured per-stock threshold, in
// raw POINTS (Settings page, "BN/NF Alerts" groups; mirrors app/config.py's
// BN_PRICE_ALERT_ATTR/NF_PRICE_ALERT_ATTR wiring — points, not %), AND agree
// on direction. An individual stock crossing its own threshold alone no
// longer fires a notification by itself (explicit user decision, 2026-09-01)
// — it only feeds into the consensus count and the live threshold badge
// below the Global Signal. Reads the SAME liveLeaderRows/liveLeaderRowsNf
// fields the Entry Loop Monitor's leader table already renders from — no
// new server payload needed.
//
// Edge-triggered: fires once when consensus is first reached, not on every
// tick it holds (otherwise every ~1s STATE_UPDATE would re-fire).
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
const _wasConsensus = {};        // {"BankNifty:up": true/false, ...} — consensus alert edge

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

// Returns [{stock, crossed, dir}, ...] for the consensus check below. Does
// NOT fire a notification per stock — only feeds the consensus count/badge;
// see checkConsensusAlert for the one thing that actually notifies.
function checkPriceAlerts(leaderRows, instrLabel, keyByStock) {
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

// "Leader consensus" alert — at least `required` leaders BOTH crossed their
// own threshold AND agree on direction. Edge-triggered per direction, so it
// re-fires only after the count drops back below `required` and crosses
// again (not every tick while consensus holds).
function checkConsensusAlert(results, instrLabel, required) {
  const up = results.filter(r => r.crossed && r.dir === 'up');
  const down = results.filter(r => r.crossed && r.dir === 'down');

  ['up', 'down'].forEach(dir => {
    const matching = dir === 'up' ? up : down;
    const key = `${instrLabel}:${dir}`;
    const met = matching.length >= required;
    if (met && !_wasConsensus[key]) {
      const names = matching.map(r => r.stock).join(', ');
      _fireAlert(
        `${instrLabel}: ${matching.length}/${results.length} leaders moved ${dir} together`,
        `${names} — each crossed its own move-alert threshold (need ≥${required})`
      );
    }
    _wasConsensus[key] = met;
  });
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
  const bnResults = checkPriceAlerts(liveLeaderRows, 'BankNifty', BN_PRICE_ALERT_KEY);
  checkConsensusAlert(bnResults, 'BankNifty', _consensusRequired.BankNifty);
  _renderThresholdBadge('alert-threshold-badge', bnResults, _consensusRequired.BankNifty);

  const nfResults = checkPriceAlerts(liveLeaderRowsNf, 'Nifty 50', NF_PRICE_ALERT_KEY);
  checkConsensusAlert(nfResults, 'Nifty 50', _consensusRequired['Nifty 50']);
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

// ── Leader-consensus signal study (see app/backtest/signal_study.py) ─────────
// Synchronous — one request, no run_id/polling like the real backtest needs.

function runBnSignalStudy() {
  const btn = document.getElementById('signal-study-run-btn');
  const resultEl = document.getElementById('signal-study-result');
  if (!resultEl) return;
  if (btn) btn.disabled = true;
  resultEl.innerHTML = '<div class="muted-text">Running…</div>';

  fetch('/api/signal-study/bn', { method: 'POST' })
    .then(r => r.json())
    .then(renderBnSignalStudy)
    .catch(e => { resultEl.innerHTML = `<div class="muted-text pnl-neg">Error: ${escHtml(e.message)}</div>`; })
    .finally(() => { if (btn) btn.disabled = false; });
}

function renderBnSignalStudy(d) {
  const el = document.getElementById('signal-study-result');
  if (!el) return;
  if (!d.total_signals) {
    el.innerHTML = `<div class="muted-text">${escHtml(d.note || 'No consensus signals found in the available history.')}</div>`;
    return;
  }
  const pct = v => v != null ? (v * 100).toFixed(1) + '%' : '—';
  const pts = v => v != null ? v.toFixed(2) + ' pts' : '—';
  el.innerHTML = `
    <div class="muted-text">Range: ${escHtml(d.from_date)} to ${escHtml(d.to_date)} |
      Consensus required: ${d.consensus_required} of 6 | Total signals: ${d.total_signals}</div>
    <div class="bt-grid" style="margin-top:8px">
      <div class="bt-cell"><div class="bt-cell-label">Up signals</div><div class="bt-cell-val">${d.signals_up}</div></div>
      <div class="bt-cell"><div class="bt-cell-label">Up win rate</div>
        <div class="bt-cell-val ${d.win_rate_up == null ? '' : d.win_rate_up >= 0.5 ? 'pnl-pos' : 'pnl-neg'}">${pct(d.win_rate_up)}</div></div>
      <div class="bt-cell"><div class="bt-cell-label">Avg next-bar move (up)</div><div class="bt-cell-val">${pts(d.avg_move_points_up)}</div></div>
      <div class="bt-cell"><div class="bt-cell-label">Down signals</div><div class="bt-cell-val">${d.signals_down}</div></div>
      <div class="bt-cell"><div class="bt-cell-label">Down win rate</div>
        <div class="bt-cell-val ${d.win_rate_down == null ? '' : d.win_rate_down >= 0.5 ? 'pnl-pos' : 'pnl-neg'}">${pct(d.win_rate_down)}</div></div>
      <div class="bt-cell"><div class="bt-cell-label">Avg next-bar move (down)</div><div class="bt-cell-val">${pts(d.avg_move_points_down)}</div></div>
    </div>
  `;
}
