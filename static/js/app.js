'use strict';

// ── Shared bootstrap for every AUTHENTICATED page ──────────────────────────────
// Loaded after util.js on index/holdings/positions/orders/settings.html (NOT
// login.html, which is public). Handles: the auth gate (redirect to /login if
// no session), the header clock/phase/ws-status wiring, the funds stat, and
// the two live channels every page needs:
//   /ws/market  — public, shared prices/candles/status (see app/api/market.py)
//   /ws/account — authenticated, this user's order fills / position changes
// Page-specific scripts (watchlist.js, holdings.js, ...) listen for the
// 'market:tick' / 'account:update' CustomEvents this file dispatches, rather
// than each opening their own WebSocket.

window._prices = {};       // token -> ltp, kept current from WATCHLIST_TICK
window._currentUser = null;

function logout() {
  apiPost('/api/auth/logout').finally(function () { location.href = '/login'; });
}

function _refreshFundsDisplay(funds) {
  var el = document.getElementById('stat-funds');
  if (el) el.textContent = '₹' + fmt2(funds);
}

function _setBadge(id, text, cls) {
  var el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '<span class="badge-dot"></span>' + escHtml(text);
  el.className = 'badge ' + cls;
}

function _wsStatusClass(s) {
  if (!s) return 'gray';
  if (s.indexOf('Connected') === 0) return 'green';
  if (s.indexOf('Degraded') === 0) return 'yellow';
  return 'red';
}

var _PHASE_LABEL = { pre_market: 'Pre-Market', open: 'Market Open', closed: 'Market Closed' };
var _PHASE_CLASS = { pre_market: 'yellow', open: 'green', closed: 'gray' };

async function _initAuthGate() {
  try {
    var me = await apiGet('/api/auth/me');
    window._currentUser = me;
    var nameEl = document.getElementById('user-name');
    if (nameEl) nameEl.textContent = me.username;
    _refreshFundsDisplay(me.funds);
    try {
      var fresh = await apiGet('/api/funds');
      _refreshFundsDisplay(fresh.funds);
    } catch (e) { /* fall back to the /me snapshot above */ }
  } catch (e) {
    location.href = '/login';
  }
}

function _initMarketWS() {
  connectWS('/ws/market', function (d) {
    if (d.type === 'MARKET_STATE') {
      var clockEl = document.getElementById('clock');
      if (clockEl) clockEl.textContent = d.clock || '—';
      _setBadge('phase-badge', _PHASE_LABEL[d.phase] || d.phase, _PHASE_CLASS[d.phase] || 'gray');
      _setBadge('ws-status', d.wsStatus || '—', _wsStatusClass(d.wsStatus));
    } else if (d.type === 'WATCHLIST_TICK') {
      Object.assign(window._prices, d.prices || {});
      window.dispatchEvent(new CustomEvent('market:tick', { detail: d.prices || {} }));
    }
  });
}

function _initAccountWS() {
  connectWS('/ws/account', function (d) {
    window.dispatchEvent(new CustomEvent('account:update', { detail: d }));
    if (d && d.order) {
      apiGet('/api/funds').then(function (r) { _refreshFundsDisplay(r.funds); }).catch(function () {});
    }
  });
}

document.addEventListener('DOMContentLoaded', function () {
  _initAuthGate();
  _initMarketWS();
  _initAccountWS();
});
