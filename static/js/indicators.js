'use strict';

// rowsMap: symbol → indicator dict. Seeded by REST, then updated per-tick by WS.
var rowsMap  = {};
var sortKey  = 'symbol';
var sortAsc  = true;
var ws       = null;
var reconnectTimer = null;
// DOM node cache — one <tr> per symbol, updated in-place (no blink)
var _rowEls     = {};
var _rafPending = false;   // coalesce rapid WS updates into one paint frame

// Pre-instantiated formatter for maximum performance (en-IN format with 2 dp)
var _formatter2dp = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// ── Market hours (IST) ────────────────────────────────────────────────────────
function marketStatus() {
  var ist  = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  var mins = ist.getHours() * 60 + ist.getMinutes();
  if (mins < 9 * 60 + 15)  return { open: false, label: 'Pre-Market' };
  if (mins < 9 * 60 + 45)  return { open: true,  label: 'Wait Zone'  };
  if (mins < 15 * 60 + 30) return { open: true,  label: 'Active'     };
  return { open: false, label: 'Market Closed' };
}

function updateMarketBadge() {
  var s  = marketStatus();
  var el = document.getElementById('mkt-badge');
  el.textContent = s.label;
  el.className   = 'badge ' + (s.open ? 'green' : 'gray');
}

// ── localStorage cache — survives page reloads within the same trading day ────
var _CACHE_DATE_KEY = 'ind_date_v2';
var _CACHE_ROWS_KEY = 'ind_rows_v2';
var _TODAY_IST = new Date().toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' });

function _saveCache() {
  try {
    localStorage.setItem(_CACHE_DATE_KEY, _TODAY_IST);
    localStorage.setItem(_CACHE_ROWS_KEY, JSON.stringify(rowsMap));
  } catch(e) {}
}

function _loadCache() {
  try {
    if (localStorage.getItem(_CACHE_DATE_KEY) !== _TODAY_IST) return;
    var saved = localStorage.getItem(_CACHE_ROWS_KEY);
    if (!saved) return;
    var parsed = JSON.parse(saved);
    if (!parsed || typeof parsed !== 'object') return;
    var count = 0;
    Object.keys(parsed).forEach(function(sym) {
      if (!rowsMap[sym]) {
        var entry = parsed[sym];
        if (entry && typeof entry === 'object') {
          entry.symbol = entry.symbol || sym;
          entry._normSymbol = normalise(entry.symbol);
        }
        rowsMap[sym] = entry;
        count++;
      }
    });
    if (count > 0) {
      renderTable();
      renderSummary();
      document.getElementById('updated-txt').textContent =
        'Cached · ' + count + ' stocks from earlier today — live data loading…';
    }
  } catch(e) {}
}

// ── WebSocket — receives indicator updates on every scan tick (~100 ms) ────────
function connect() {
  var proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws/dashboard');

  ws.onopen = function() {
    document.getElementById('updated-txt').textContent = 'Live — waiting for first tick…';
    updateMarketBadge();
  };

  ws.onmessage = function(e) {
    try {
      var d = JSON.parse(e.data);
      // Accept full snapshot (1 s broadcast) and per-tick delta (~100 ms)
      if (d.type !== 'STATE_UPDATE' && d.type !== 'INDICATOR_UPDATE') return;
      var snap = d.indicatorSnapshot;
      if (!snap || typeof snap !== 'object') return;
      // Merge fresher WS data; always force entry.symbol = the JSON key
      // so search never accidentally sees a token string instead of a name.
      Object.keys(snap).forEach(function(sym) {
        var entry = Object.assign({}, snap[sym]);
        entry.symbol = sym;
        entry._normSymbol = normalise(sym);
        rowsMap[sym] = entry;
      });
      scheduleRender();   // coalesces into one paint frame even if ticks arrive faster
      _saveCache();
      var now = new Date();
      document.getElementById('updated-txt').textContent =
        'Live · ' + now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      updateMarketBadge();
    } catch (err) { console.error(err); }
  };

  ws.onclose = ws.onerror = function() {
    document.getElementById('updated-txt').textContent = 'Reconnecting…';
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 3000);
  };
}

// REST seed — retries every 30 s until the server returns data.
// Covers mid-session restarts where full_watchlist isn't ready yet.
function loadInitial() {
  fetch('/api/indicators')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!Array.isArray(data) || !data.length) {
        // Server still initialising (recovery in progress) — retry later
        setTimeout(loadInitial, 30000);
        return;
      }
      data.forEach(function(item) {
        var existing = rowsMap[item.symbol];
        if (item && typeof item === 'object') {
          item._normSymbol = normalise(item.symbol);
        }
        // Upgrade a stub (ltp-only) entry with real indicator data from REST
        if (!existing || (existing.rsi == null && item.rsi != null)) {
          rowsMap[item.symbol] = item;
        }
      });
      scheduleRender();
      _saveCache();
    })
    .catch(function() { setTimeout(loadInitial, 30000); });
}

// ── Summary strip ─────────────────────────────────────────────────────────────
function renderSummary() {
  var total = 0, aboveV = 0, rsiLo = 0, rsiHi = 0, adxOn = 0, pats = 0;
  for (var sym in rowsMap) {
    if (Object.prototype.hasOwnProperty.call(rowsMap, sym)) {
      var r = rowsMap[sym];
      total++;
      if (r.above_vwap) aboveV++;
      if (r.rsi != null) {
        if (r.rsi < 30) rsiLo++;
        else if (r.rsi > 70) rsiHi++;
      }
      if (r.adx != null && r.adx > 20) adxOn++;
      if (r.pattern) pats++;
    }
  }

  document.getElementById('s-total').textContent  = total;
  document.getElementById('s-vwap').textContent   = aboveV;
  document.getElementById('s-rsi-lo').textContent = rsiLo;
  document.getElementById('s-rsi-hi').textContent = rsiHi;
  document.getElementById('s-adx').textContent    = adxOn;
  document.getElementById('s-pat').textContent    = pats;
}

// ── Sort ──────────────────────────────────────────────────────────────────────
function sortBy(key) {
  if (sortKey === key) { sortAsc = !sortAsc; }
  else { sortKey = key; sortAsc = true; }
  renderTable();
}

document.querySelectorAll('#thead-row th').forEach(function(th) {
  th.addEventListener('click', function() { sortBy(th.getAttribute('data-key')); });
});

function updateSortHeaders() {
  document.querySelectorAll('#thead-row th').forEach(function(th) {
    var k   = th.getAttribute('data-key');
    var txt = th.getAttribute('data-label') || th.textContent.replace(/[▲▼]\s*$/, '').trim();
    if (!th.getAttribute('data-label')) th.setAttribute('data-label', txt);
    th.classList.toggle('sorted', k === sortKey);
    th.textContent = k === sortKey ? txt + (sortAsc ? ' ▲' : ' ▼') : txt;
  });
}

// ── Format helpers ────────────────────────────────────────────────────────────
function fmtINR(v) {
  if (v == null) return '—';
  return _formatter2dp.format(v);
}

function normalise(s) {
  return (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
}

// ── DOM-diffing row helpers — no blink ────────────────────────────────────────

function _setCell(td, html, cls) {
  if (td._h !== html) { td._h = html; td.innerHTML = html; }
  if (td._c !== cls)  { td._c = cls;  td.className  = cls;  }
}

function _createTR() {
  var tr = document.createElement('tr');
  for (var i = 0; i < 16; i++) {
    var td = document.createElement('td');
    td._h = null; td._c = null;
    tr.appendChild(td);
  }
  return tr;
}

function _updateTR(tr, r) {
  var c = tr.cells;

  var rsiCls  = r.rsi       == null ? 'muted' : r.rsi < 30  ? 'rsi-lo' : r.rsi > 70 ? 'rsi-hi' : 'rsi-mid';
  var adxCls  = r.adx       == null ? 'muted' : r.adx >= 20 ? 'adx-on' : 'adx-off';
  var histCls = r.macd_hist == null ? 'muted' : r.macd_hist >= 0 ? 'pos-num' : 'neg-num';
  var histTxt = r.macd_hist != null ? (r.macd_hist >= 0 ? '+' : '') + r.macd_hist.toFixed(4) : '—';
  var vposCls = r.above_vwap == null ? 'muted' : r.above_vwap ? 'pos-num' : 'neg-num';
  var vposTxt = r.above_vwap == null ? '—'     : r.above_vwap ? '▲ Above' : '▼ Below';
  var ratioTxt = r.ratio != null ? (r.ratio * 100).toFixed(1) + '%' : '—';
  var ratioCls = r.ratio == null ? 'muted' : r.ratio >= 0.5 ? 'pos-num' : r.ratio >= 0.4 ? 'rsi-mid' : 'neg-num';

  _setCell(c[0],  r.symbol,                                                        'col-sym');
  _setCell(c[1],  fmtINR(r.ltp),                                                   'ta-r');
  _setCell(c[2],  r.bar_time || '<span class="muted">—</span>',                    '');
  _setCell(c[3],  r.rsi      != null ? r.rsi.toFixed(1)      : '—',               rsiCls  + ' ta-r');
  _setCell(c[4],  r.adx      != null ? r.adx.toFixed(1)      : '—',               adxCls  + ' ta-r');
  _setCell(c[5],  r.plus_di  != null ? r.plus_di.toFixed(1)  : '<span class="muted">—</span>', 'pos-num ta-r');
  _setCell(c[6],  r.minus_di != null ? r.minus_di.toFixed(1) : '<span class="muted">—</span>', 'neg-num ta-r');
  _setCell(c[7],  histTxt,                                                          histCls + ' ta-r');
  _setCell(c[8],  fmtINR(r.support),                                               'ta-r');
  _setCell(c[9],  fmtINR(r.vwap),                                                  'ta-r');
  _setCell(c[10], vposTxt,                                                          vposCls);
  _setCell(c[11], r.pattern || '—',                                                 r.pattern ? 'pat-td' : 'muted');
  _setCell(c[12], fmtINR(r.bid),                                                   'ta-r');
  _setCell(c[13], fmtINR(r.ask),                                                   'ta-r');
  _setCell(c[14], r.spread != null ? r.spread.toFixed(2) : '—',                   'ta-r muted');
  _setCell(c[15], ratioTxt,                                                         ratioCls + ' ta-r');
}

// ── scheduleRender — coalesce multiple rapid WS ticks into one paint frame ────
function scheduleRender() {
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(function() {
    _rafPending = false;
    renderTable();
    renderSummary();
  });
}

// ── Render table — DOM-diffing, zero blink ────────────────────────────────────
function renderTable() {
  try {
    updateSortHeaders();

    var searchEl = document.getElementById('search');
    var q = searchEl ? normalise(searchEl.value) : '';
    var all = Object.values(rowsMap);
    var data;

    if (q) {
      var words = q.split(' ').filter(Boolean);
      data = all.filter(function(r) {
        var sym = r ? r._normSymbol : '';
        if (!sym) return false;
        return words.every(function(w) { return sym.indexOf(w) !== -1; });
      });
    } else {
      data = all;
    }

    data.sort(function(a, b) {
      var va = a[sortKey], vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      var cmp = (typeof va === 'string') ? va.localeCompare(vb) : va - vb;
      return sortAsc ? cmp : -cmp;
    });

    var tbody = document.getElementById('ind-tbody');

    if (!data.length) {
      Object.keys(_rowEls).forEach(function(sym) {
        var tr = _rowEls[sym];
        if (tr.parentNode) tr.parentNode.removeChild(tr);
      });
      _rowEls = {};
      var total = all.length;
      var msg = total === 0
        ? 'Stocks loading — the table will populate automatically'
        : 'No match for "' + (searchEl ? searchEl.value : '') + '" in ' + total + ' stocks';
      tbody.innerHTML = '<tr class="empty-row"><td colspan="16">' + msg + '</td></tr>';
      return;
    }

    var emptyRow = tbody.querySelector('.empty-row');
    if (emptyRow) tbody.removeChild(emptyRow);

    // Step 1: update cell content in-place (no DOM reorder yet)
    var seen = {};
    data.forEach(function(r) {
      var sym = r.symbol || '';
      seen[sym] = true;
      var tr = _rowEls[sym];
      if (!tr) {
        tr = _createTR();
        _rowEls[sym] = tr;
      }
      _updateTR(tr, r);
    });

    // Step 2: remove rows no longer in the filtered set
    Object.keys(_rowEls).forEach(function(sym) {
      if (!seen[sym]) {
        var tr = _rowEls[sym];
        if (tr.parentNode) tr.parentNode.removeChild(tr);
        delete _rowEls[sym];
      }
    });

    // Step 3: reorder only when the DOM order differs from desired sort order
    var children = tbody.children;
    var needReorder = (children.length !== data.length);
    if (!needReorder) {
      for (var i = 0; i < data.length; i++) {
        var sym = data[i].symbol || '';
        if (children[i] !== _rowEls[sym]) { needReorder = true; break; }
      }
    }
    if (needReorder) {
      var frag = document.createDocumentFragment();
      data.forEach(function(r) {
        var sym = r.symbol || '';
        if (_rowEls[sym]) frag.appendChild(_rowEls[sym]);
      });
      tbody.appendChild(frag);
    }
    
    // Render complete
  } catch (err) {
    console.error('Error rendering table:', err);
  }
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function toggleTheme() {
  var root = document.documentElement;
  var next = (root.getAttribute('data-theme') || 'dark') === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── Init — cache first (instant), then REST seed, then live WS ───────────────
_loadCache();
loadInitial();
connect();
