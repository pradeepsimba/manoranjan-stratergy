'use strict';

// rowsMap: symbol → indicator dict. Seeded by REST, then updated per-tick by WS.
var rowsMap  = {};
var sortKey  = 'symbol';
var sortAsc  = true;
// Timeframe viewer: 'live' = the WS-driven 5m stream; any other value shows an
// on-demand snapshot fetched for that timeframe (polled, no live WS merges).
var viewTF   = 'live';
var tfPoll   = null;
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

// The cache only needs to survive a reload — sub-second freshness buys
// nothing, and stringifying ~500 rows + a sync localStorage write on every
// ~100ms WS delta was the page's single largest main-thread cost. Debounce
// to one write per 5s, plus a final flush when the page is hidden/closed.
var _cacheTimer = null;

function _writeCacheNow() {
  _cacheTimer = null;
  if (viewTF !== 'live') return;   // never cache a timeframe view as the live seed
  try {
    localStorage.setItem(_CACHE_DATE_KEY, _TODAY_IST);
    localStorage.setItem(_CACHE_ROWS_KEY, JSON.stringify(rowsMap));
  } catch(e) {}
}

function _saveCache() {
  if (_cacheTimer) return;
  _cacheTimer = setTimeout(_writeCacheNow, 5000);
}

window.addEventListener('pagehide', _writeCacheNow);
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'hidden') _writeCacheNow();
});

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
      // Keep the clock + market badge live on EVERY message — STATE_UPDATE now
      // carries indicatorSnapshot only every 10th push, so gating these behind
      // snapshot presence would freeze them between snapshots outside market
      // hours (when no ~100ms INDICATOR_UPDATE deltas flow).
      updateMarketBadge();
      // Ignore live 5m WS updates while comparing timeframes or viewing a
      // non-live timeframe (those views are polled, not streamed).
      if (mtfMode || viewTF !== 'live') return;
      var now = new Date();
      document.getElementById('updated-txt').textContent =
        'Live · ' + now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      var snap = d.indicatorSnapshot;
      if (!snap || typeof snap !== 'object') return;
      // Merge fresher WS data; always force entry.symbol = the JSON key
      // so search never accidentally sees a token string instead of a name.
      Object.keys(snap).forEach(function(sym) {
        if (!rowsMap[sym]) {
          rowsMap[sym] = { symbol: sym, _normSymbol: normalise(sym) };
          _needFull = true;              // new row → full render (sort/filter)
        }
        var target = rowsMap[sym];
        var source = snap[sym];
        _dirtySyms.add(sym);
        // An update touching the active sort column can reorder the table —
        // only then is the O(n log n) full render needed.
        if (sortKey !== 'symbol' && sortKey in source) _needFull = true;
        Object.keys(source).forEach(function(key) {
          if (source[key] !== null && source[key] !== undefined) {
            target[key] = source[key];
          } else if (target[key] === undefined) {
            target[key] = null;
          }
        });
        // Always copy core fields even if they are null in update
        if ('ltp' in source) target.ltp = source.ltp;
        if ('bar_time' in source && source.bar_time !== '—') target.bar_time = source.bar_time;
        if ('spread' in source) target.spread = source.spread;
        if ('bid' in source) target.bid = source.bid;
        if ('ask' in source) target.ask = source.ask;
      });
      scheduleRender();   // coalesces into one paint frame even if ticks arrive faster
      _saveCache();
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
  // The live REST seed must never repopulate rowsMap while a non-live
  // timeframe is being viewed — a pending retry firing mid-view would splice
  // live 5m rows into the TF snapshot. switchTF('live') re-invokes this.
  if (viewTF !== 'live') return;
  fetch('/api/indicators')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (viewTF !== 'live') return;   // user switched away during the fetch
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
      scheduleFullRender();   // REST seed adds rows — needs sort/filter pass
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

var _COL_LABELS = ['Symbol','LTP ₹','Bar','RSI','ADX','+DI','−DI','MACD Hist',
                   'Support ₹','VWAP ₹','VWAP Pos','Pattern','Bid ₹','Ask ₹','Spread','B/S Ratio'];

function _createTR() {
  var tr = document.createElement('tr');
  for (var i = 0; i < 16; i++) {
    var td = document.createElement('td');
    td._h = null; td._c = null;
    td.setAttribute('data-label', _COL_LABELS[i]);   // drives mobile card layout
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
  var ratioCls = r.ratio == null ? 'muted' : r.ratio >= 0.5 ? 'pos-num' : r.ratio >= 0.4 ? 'rsi-mid' : 'neg-num';

  // RSI 0–100 gauge + value; color by oversold/overbought zone.
  var rsiHtml;
  if (r.rsi == null) {
    rsiHtml = '<span class="muted">—</span>';
  } else {
    var rc = r.rsi < 30 ? 'var(--neg)' : r.rsi > 70 ? 'var(--pos)' : 'var(--prime)';
    rsiHtml = '<span class="meter-cell"><span class="meter">' +
      '<span class="meter-fill" style="width:' + Math.max(0, Math.min(100, r.rsi)) + '%;background:' + rc + '"></span>' +
      '</span><span class="meter-num ' + rsiCls + '">' + r.rsi.toFixed(1) + '</span></span>';
  }
  // Buy/sell depth as a dual bar + percent.
  var ratioHtml;
  if (r.ratio == null) {
    ratioHtml = '<span class="muted">—</span>';
  } else {
    var bp = Math.max(0, Math.min(100, r.ratio * 100));
    ratioHtml = '<span class="meter-cell"><span class="dbar">' +
      '<span class="buy" style="width:' + bp + '%"></span>' +
      '<span class="sell" style="width:' + (100 - bp) + '%"></span></span>' +
      '<span class="meter-num ' + ratioCls + '">' + bp.toFixed(0) + '%</span></span>';
  }

  _setCell(c[0],  r.symbol,                                                        'col-sym card-title');
  _setCell(c[1],  fmtINR(r.ltp),                                                   'ta-r');
  _setCell(c[2],  r.bar_time || '<span class="muted">—</span>',                    '');
  _setCell(c[3],  rsiHtml,                                                          'ta-r');
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
  _setCell(c[15], ratioHtml,                                                        'ta-r');
}

// ── scheduleRender — coalesce multiple rapid WS ticks into one paint frame ────
// Fast path: when only cell VALUES changed (no new rows, sort column
// untouched), patch just the dirty rows in place — O(dirty) instead of
// re-filtering, re-sorting and re-formatting all ~500 rows per delta.
var _dirtySyms = new Set();
var _needFull  = true;    // first paint (and structural changes) render fully

function scheduleRender() {
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(function() {
    _rafPending = false;
    if (_needFull) {
      _needFull = false;
      _dirtySyms.clear();
      renderTable();        // sort + filter + reorder + every row
    } else {
      _dirtySyms.forEach(function(sym) {
        var tr = _rowEls[sym];          // absent when filtered out — skip;
        var r  = rowsMap[sym];          // the next full render catches it up
        if (tr && r) _updateTR(tr, r);
      });
      _dirtySyms.clear();
    }
    renderSummary();
  });
}

function scheduleFullRender() { _needFull = true; scheduleRender(); }

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

// (toggleTheme lives in the shared /js/util.js)

// ── Timeframe viewer ───────────────────────────────────────────────────────────
function _resetTable(msg) {
  rowsMap = {};
  _rowEls = {};
  var tbody = document.getElementById('ind-tbody');
  if (tbody) tbody.innerHTML = '<tr class="empty-row"><td colspan="16">' + msg + '</td></tr>';
}

function switchTF(tf) {
  if (mtfMode) _leaveCompare();   // picking a TF exits compare mode cleanly
  viewTF = tf;
  clearInterval(tfPoll); tfPoll = null;
  var toolbar = document.querySelector('.ind-toolbar');
  if (tf === 'live') {
    if (toolbar) toolbar.classList.remove('viewing-tf');
    _resetTable('Loading live data…');
    document.getElementById('updated-txt').textContent = 'Live — waiting for next tick…';
    loadInitial();               // WS merges resume automatically once flowing
  } else {
    if (toolbar) toolbar.classList.add('viewing-tf');
    _resetTable('Loading ' + tf + '…');
    document.getElementById('updated-txt').textContent = tf + ' · loading…';
    loadTF(tf);
    tfPoll = setInterval(function () { loadTF(tf); }, 30000);
  }
}

// On-demand snapshot for a non-live timeframe (computed server-side from fresh
// history). Replaces rowsMap wholesale; the DOM diff reuses rows by symbol.
function loadTF(tf) {
  fetch('/api/indicators/tf/' + encodeURIComponent(tf))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (viewTF !== tf) return;             // user switched away mid-fetch
      if (!Array.isArray(data)) return;
      var next = {};
      data.forEach(function (item) {
        if (item && typeof item === 'object' && item.symbol) {
          item._normSymbol = normalise(item.symbol);
          next[item.symbol] = item;
        }
      });
      rowsMap = next;
      scheduleFullRender();
      document.getElementById('updated-txt').textContent =
        tf + ' · ' + new Date().toLocaleTimeString('en-IN',
          { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    })
    .catch(function () {
      if (viewTF === tf) document.getElementById('updated-txt').textContent = tf + ' · fetch failed — retrying…';
    });
}

function _populateTFs() {
  fetch('/api/timeframes')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (Array.isArray(d.timeframes)) { _allTFs = d.timeframes; _buildCompareChecks(); }
      var sel = document.getElementById('tf-select');
      if (!sel || !Array.isArray(d.timeframes)) return;
      d.timeframes.forEach(function (tf) {
        var o = document.createElement('option');
        o.value = tf; o.textContent = tf;
        sel.appendChild(o);
      });
    })
    .catch(function () { /* dropdown keeps just the live option */ });
}

// ── Multi-timeframe comparison ─────────────────────────────────────────────────
var mtfMode = false;
var mtfPoll = null;
var _allTFs = [];

function _buildCompareChecks() {
  var host = document.getElementById('cmp-tfs');
  if (!host || !_allTFs.length) return;
  var def = { '5m': 1, '15m': 1, '1hr': 1 };   // sensible default selection
  host.innerHTML = _allTFs.map(function (tf) {
    var on = def[tf] ? ' checked' : '';
    return '<label class="cmp-tf' + (def[tf] ? ' on' : '') + '">' +
      '<input type="checkbox" value="' + escHtml(tf) + '"' + on +
      ' onchange="this.parentNode.classList.toggle(\'on\', this.checked)">' + escHtml(tf) + '</label>';
  }).join('');
}

function toggleComparePanel() {
  var bar = document.getElementById('cmp-bar');
  if (!bar) return;
  if (!document.querySelector('#cmp-tfs .cmp-tf')) _buildCompareChecks();
  bar.style.display = (bar.style.display === 'none') ? 'flex' : 'none';
}

function startCompare() {
  var tfs = Array.prototype.slice
    .call(document.querySelectorAll('#cmp-tfs input:checked'))
    .map(function (el) { return el.value; });
  if (tfs.length < 2) { alert('Pick at least 2 timeframes to compare.'); return; }
  mtfMode = true;
  clearInterval(mtfPoll); mtfPoll = null;
  clearInterval(tfPoll);  tfPoll = null;   // stop any single-TF poll
  document.querySelector('.ind-wrap').style.display = 'none';
  var ss = document.querySelector('.sum-strip'); if (ss) ss.style.display = 'none';
  document.getElementById('mtf-wrap').style.display = '';
  document.getElementById('updated-txt').textContent = 'Comparing ' + tfs.join(' · ') + ' …';
  loadMTF(tfs);
  mtfPoll = setInterval(function () { loadMTF(tfs); }, 30000);
}

// Tear down compare-mode UI (show the single view again) WITHOUT reloading —
// shared by exitCompare and switchTF so selecting a TF mid-compare is clean.
function _leaveCompare() {
  mtfMode = false;
  clearInterval(mtfPoll); mtfPoll = null;
  var w = document.getElementById('mtf-wrap');  if (w) w.style.display = 'none';
  var b = document.getElementById('cmp-bar');   if (b) b.style.display = 'none';
  var iw = document.querySelector('.ind-wrap'); if (iw) iw.style.display = '';
  var ss = document.querySelector('.sum-strip'); if (ss) ss.style.display = '';
}

function exitCompare() {
  _leaveCompare();
  // Restore whichever single view was active (mtfMode is already false, so the
  // switchTF guard is a no-op — no recursion).
  if (viewTF === 'live') loadInitial(); else switchTF(viewTF);
}

function loadMTF(tfs) {
  fetch('/api/indicators/mtf?tfs=' + encodeURIComponent(tfs.join(',')))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!mtfMode) return;
      renderMTF(d);
      document.getElementById('updated-txt').textContent =
        'Compare ' + (d.timeframes || []).join(' · ') + ' · ' +
        new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    })
    .catch(function () {
      if (mtfMode) document.getElementById('updated-txt').textContent = 'compare fetch failed — retrying…';
    });
}

function _dirChip(s) {
  if (!s) return '<span class="muted">—</span>';
  var arrow = s.score >= 2 ? '▲' : s.score === 1 ? '•' : '▼';
  var rsi = (s.rsi != null) ? '<span class="mtf-rsi">' + Math.round(s.rsi) + '</span>' : '';
  return '<span class="dir s' + s.score + '">' + arrow + '</span>' + rsi;
}

function renderMTF(d) {
  var tfs = d.timeframes || [];
  var rows = d.rows || [];
  var head = '<thead><tr><th style="text-align:left">Symbol</th><th>LTP ₹</th>' +
    tfs.map(function (tf) { return '<th class="tf-group">' + escHtml(tf) + '</th>'; }).join('') +
    '<th>Confluence</th></tr></thead>';
  var table = document.getElementById('mtf-table');
  if (!rows.length) {
    table.innerHTML = head + '<tbody><tr><td colspan="' + (tfs.length + 3) +
      '" style="text-align:center;padding:30px;color:var(--txt-3)">' +
      'No data — is the market universe loaded? (built at 09:00 IST or on demand)</td></tr></tbody>';
    return;
  }
  var body = rows.map(function (r) {
    var confCls = (r.bull === r.n) ? 'full' : (r.bull === 0) ? 'none' : 'some';
    var cells = tfs.map(function (tf) {
      return '<td class="tf-cell">' + _dirChip(r.tf ? r.tf[tf] : null) + '</td>';
    }).join('');
    return '<tr><td class="mtf-sym">' + escHtml(r.symbol) + '</td>' +
      '<td>' + (r.ltp != null ? fmtINR(r.ltp) : '—') + '</td>' + cells +
      '<td><span class="conf ' + confCls + '">' + r.bull + '/' + r.n + '</span></td></tr>';
  }).join('');
  table.innerHTML = head + '<tbody>' + body + '</tbody>';
}

// ── Init — cache first (instant), then REST seed, then live WS ───────────────
_populateTFs();
_loadCache();
loadInitial();
connect();
