'use strict';

// ── Scalping page ─────────────────────────────────────────────────────────────
// Two data sources, deliberately split:
//
//   /ws/dashboard  (1 Hz STATE_UPDATE)  → header, and the `scalp` block the
//        server already builds for every page: window, counters, open book,
//        aggregated rejection reasons, signal log.
//   /api/scalp/scan (polled 1 Hz)       → the PER-SYMBOL scanner rows. Kept out
//        of the WS payload on purpose: it would be dead weight on the dashboard
//        and indicators pages, which never show it.
//
// /api/scalp is fetched once (and refreshed slowly) purely for the session
// window boundaries, which only change when someone edits the settings.

var ws             = null;
var reconnectTimer = null;
var scanTimer      = null;
var windowsTimer   = null;
var _rowEls        = {};    // symbol → <tr>, patched in place (no blink)
var _wins          = [];    // window boundaries, from /api/scalp
var _winSig        = null;  // signature of what the strip currently shows

// Arm/window state, merged from whichever source reported last. Both the WS and
// the scan poll carry it, so the controls keep working if either one is down.
var S = {
  enabled: false, dryRun: true, window: '—', execute: false,
  requiredRatio: null, note: '', caps: {},
};

var PHASE_CLS = {
  pre_market: 'gray', wait_zone: 'yellow', active: 'green',
  cutoff: 'yellow',   closed: 'gray',
};
var WINDOW_CLS = {
  closed: 'gray', warmup: 'yellow', morning: 'green',
  midday: 'yellow', afternoon: 'green', squareoff: 'red',
};

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connect() {
  var proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws/dashboard');

  ws.onmessage = function (ev) {
    var d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    // INDICATOR_UPDATE carries ONLY indicatorSnapshot — rendering the header
    // from it would blank the clock/phase/scalp fields (same trap dashboard.js
    // documents), so ignore anything that isn't a full state push.
    if (d.type !== 'STATE_UPDATE') return;
    renderHeader(d);
    if (d.scalp) applyScalp(d.scalp);
  };

  ws.onclose = function () {
    setBadge('ws-status', 'WS Disconnected', 'red');
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connect();
      }, 3000);
    }
  };
  ws.onerror = function () { try { ws.close(); } catch (e) {} };
}

// ── Header ────────────────────────────────────────────────────────────────────

function setBadge(id, text, cls) {
  var el = document.getElementById(id);
  if (!el) return;
  if (el._t !== text) { el._t = text; el.textContent = text; }
  var c = 'badge ' + cls;
  if (el._c !== c) { el._c = c; el.className = c; }
}

function renderHeader(d) {
  var clk = document.getElementById('clock');
  if (clk) clk.textContent = d.clock || '—';

  // Bar clock: on a scalping page this doubles as a feed-liveness cue, so it
  // shows the stale-feed warning when the server raises one.
  var sub = document.getElementById('last-bar-time');
  if (sub) {
    sub.textContent = d.feedStaleWarning ? 'FEED STALE'
                    : d.lastBarTime ? 'Last bar ' + d.lastBarTime : 'Scalping';
    sub.className = 'brand-sub' + (d.feedStaleWarning ? ' pnl-neg' : '');
  }

  var phase = d.phase || '—';
  setBadge('phase-badge', phase.replace(/_/g, ' ').toUpperCase(),
           PHASE_CLS[phase] || 'gray');

  var up = (d.wsStatus || '').startsWith('WS Connected');
  var degraded = up && d.wsStatus !== 'WS Connected';
  setBadge('ws-status', d.wsStatus || '—',
           up ? (degraded ? 'yellow' : 'green') : 'red');
}

// ── Scalp state (WS block) ────────────────────────────────────────────────────

function applyScalp(s) {
  S.enabled = !!s.enabled;
  S.dryRun  = !!s.dryRun;
  S.window  = s.window || '—';
  S.execute = !!s.execute;
  S.requiredRatio = s.requiredRatio;
  S.note    = s.note || '';
  S.caps    = s.caps || {};
  renderArm();

  var caps = S.caps;
  var pnl  = s.scalpPnl || 0;
  var pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '₹' + fmt2(pnl);
  pnlEl.className = 'stat-value' + (pnl > 0 ? ' pnl-pos' : pnl < 0 ? ' pnl-neg' : '');

  var open = s.openScalps || [];
  document.getElementById('stat-open').textContent =
    open.length + (caps.maxConcurrent ? ' / ' + caps.maxConcurrent : '');
  document.getElementById('stat-trades').textContent =
    (s.tradesToday || 0) + (caps.maxTradesPerDay ? ' / ' + caps.maxTradesPerDay : '');
  document.getElementById('stat-signals').textContent =
    (s.signals || 0) + ' / ' + (s.evaluated || 0);
  document.getElementById('stat-fills').textContent = s.fills || 0;
  document.getElementById('stat-books').textContent = s.booksTracked || 0;

  renderOpen(open);
  renderRejects(s.rejects || []);
  renderLog(s.log || []);
}

function renderArm() {
  var bar   = document.getElementById('arm-bar');
  var state = document.getElementById('arm-state');
  var note  = document.getElementById('arm-note');
  var bEn   = document.getElementById('btn-enable');
  var bArm  = document.getElementById('btn-arm');

  var cls = 'arm-bar ' + (!S.enabled ? 'is-off' : S.dryRun ? 'is-dry' : 'is-armed');
  if (bar._c !== cls) { bar._c = cls; bar.className = cls; }

  if (!S.enabled) {
    state.textContent = 'SCALPER OFF';
    note.textContent  = 'No book parsing, no tape, no signals — zero cost on the tick path.';
  } else if (S.dryRun) {
    state.textContent = 'DRY RUN — logging only';
    note.textContent  = 'Signals are evaluated and logged. No orders are placed. ' + S.note;
  } else {
    state.textContent = 'ARMED — placing paper orders';
    note.textContent  = S.note;
  }

  bEn.textContent  = S.enabled ? 'Disable' : 'Enable';
  bEn.className    = 'arm-btn' + (S.enabled ? ' danger' : ' go');
  bArm.textContent = S.dryRun ? 'Arm (place orders)' : 'Back to dry run';
  bArm.className   = 'arm-btn' + (S.dryRun ? ' go' : ' danger');
  bArm.disabled    = !S.enabled;

  setBadge('scalp-window-badge',
           (S.enabled ? '' : 'OFF · ') + String(S.window).toUpperCase() +
           (S.requiredRatio ? ' ≥ ' + S.requiredRatio : ''),
           !S.enabled ? 'gray' : (WINDOW_CLS[S.window] || 'gray'));
}

// ── Controls (write straight through the settings API, so the change is
// validated, persisted and applied exactly as it would be from /settings) ─────

function saveSetting(changes, label) {
  fetch('/api/settings', {
    method:  'PUT',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ changes: changes }),
  })
    .then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || 'save failed');
        return j;
      });
    })
    .then(function (j) {
      // The API returns any inert-field / "now ARMED" warnings — surface them
      // rather than silently succeeding.
      var w = (j.warnings || []).join(' · ');
      toast(w || label, w ? 'warn' : 'ok', w ? 6000 : 2500);
      loadScan();
    })
    .catch(function (e) { toast(String(e.message || e), 'err', 6000); });
}

function toggleEnabled() {
  var next = !S.enabled;
  saveSetting({ SCALP_ENABLED: next }, next ? 'Scalper enabled' : 'Scalper disabled');
}

function toggleArmed() {
  var nextDry = !S.dryRun;
  if (!nextDry && !confirm(
      'Arm the scalper?\n\nIt will place paper orders on live order-book ' +
      'signals from now on. Fills are simulated (no broker), but position, ' +
      'risk and P&L state are all real.')) {
    return;
  }
  saveSetting({ SCALP_DRY_RUN: nextDry },
              nextDry ? 'Back to dry run' : 'Scalper ARMED');
}

// ── Session window strip ──────────────────────────────────────────────────────

function loadWindows() {
  fetch('/api/scalp')
    .then(function (r) { return r.json(); })
    .then(function (d) { renderWindows(d.windows || []); })
    .catch(function () {});
}

// Called with fresh boundaries by loadWindows, and with no argument by loadScan
// so the highlight follows the active window between boundary refreshes. Repaints
// only when the boundaries OR the active window actually changed.
function renderWindows(wins) {
  if (wins) _wins = wins;
  var sig = JSON.stringify(_wins) + '|' + S.window;
  if (_winSig === sig) return;
  _winSig = sig;
  var strip = document.getElementById('win-strip');
  strip.innerHTML = _wins.map(function (w) {
    // A window with a configured ratio that still can't execute is PAUSED (the
    // midday toggle) — saying "W-OBI ≥ 5" there would imply it trades.
    var sub = w.requiredRatio == null
      ? (w.window === 'warmup' ? 'scan only' : 'flatten')
      : (w.execute === false ? 'paused' : 'W-OBI ≥ ' + w.requiredRatio);
    return '<div class="win' + (w.window === S.window ? ' active' : '') + '">' +
      '<div class="w-name">' + escHtml(w.window) + '</div>' +
      '<div class="w-time">' + escHtml(w.start) + '</div>' +
      '<div class="w-ratio">' + sub + '</div></div>';
  }).join('');
}

// ── Scanner table ─────────────────────────────────────────────────────────────

function loadScan() {
  var onlyPass = document.getElementById('only-pass').checked;
  fetch('/api/scalp/scan' + (onlyPass ? '?only_passing=true' : ''))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      // The scan response also carries the arm/window state, so the controls and
      // badge stay correct even while the WebSocket is down.
      S.enabled = !!d.enabled; S.dryRun = !!d.dryRun;
      S.window  = d.window || '—'; S.execute = !!d.execute;
      S.requiredRatio = d.requiredRatio; S.note = d.note || '';
      renderArm();
      renderWindows();
      renderScan(d);
    })
    .catch(function () {});
}

function num(v, dp) {
  if (v === null || v === undefined) return '—';
  if (typeof v !== 'number') return escHtml(String(v));
  return dp === 0 ? String(Math.round(v)) : v.toFixed(dp === undefined ? 2 : dp);
}

function _cell(td, html, cls) {
  if (td._h !== html) { td._h = html; td.innerHTML = html; }
  if (cls !== undefined && td._c !== cls) { td._c = cls; td.className = cls; }
}

function renderScan(d) {
  var rows  = d.rows || [];
  var tbody = document.getElementById('scan-tbody');
  document.getElementById('scan-count').textContent =
    rows.length + ' / ' + (d.tradeable || 0);

  // A table full of "—" has two very different causes, and leaving the user to
  // guess which is a real usability failure: the scalper being OFF (no parsing
  // happens at all, by design) versus being ON but receiving no depth from the
  // feed (the snap-format question, which /api/scalp/snap answers). `metrics` is
  // empty for a symbol with no book, so bookAgeS is the reliable marker.
  var withBook = rows.filter(function (r) {
    return r.metrics && r.metrics.bookAgeS != null;
  }).length;
  var state = !d.enabled ? 'off' : (!withBook && rows.length) ? 'nodata' : 'ok';
  var notice = document.getElementById('scan-notice');
  // Rebuild ONLY on a state change. This block runs at 1 Hz and contains a
  // button: re-writing innerHTML every second would reset its :hover styling and
  // could swap the node out from under an in-flight click.
  if (notice._state !== state) {
    notice._state = state;
    if (state === 'off') {
      notice.className = 'scan-notice off';
      notice.innerHTML =
        '<span><strong>Scalper is off.</strong> No order books or tape are being ' +
        'parsed, so every book column below is empty — that is by design (it keeps ' +
        'the tick path free when the strategy is disabled). LTP still updates.</span>' +
        '<button class="arm-btn go" onclick="toggleEnabled()">Enable now</button>';
    } else if (state === 'nodata') {
      notice.className = 'scan-notice warn';
      notice.innerHTML =
        '<span><strong>Enabled, but no order-book data has arrived yet.</strong> ' +
        'If this persists, the feed\'s <code>snap</code> field may not carry depth ' +
        'for these symbols — open <code>/api/scalp/snap</code> to see the raw ' +
        'payload next to its parse.</span>';
    } else {
      notice.className = 'scan-notice';
      notice.innerHTML = '';
    }
  }

  if (!rows.length) {
    _rowEls = {};
    // Three distinct causes, three messages — "no data" would be wrong for a
    // filter that simply matched nothing, which is the common case.
    var msg = !d.enabled
      ? 'Scalper is off — enable it above to start parsing books and the tape.'
      : document.getElementById('only-pass').checked
        ? 'No symbol currently passes every filter. Untick “only passing” to see ' +
          'each symbol and the reason it was rejected.'
        : 'No tradeable symbols yet — the watchlist is built at pre-market.';
    tbody.innerHTML = '<tr><td colspan="11" class="empty-cell">' + msg + '</td></tr>';
    return;
  }

  var frag = document.createDocumentFragment();
  var seen = {};
  rows.forEach(function (r) {
    var m  = r.metrics || {};
    var tr = _rowEls[r.symbol];
    if (!tr) {
      tr = document.createElement('tr');
      for (var i = 0; i < 11; i++) tr.appendChild(document.createElement('td'));
      _rowEls[r.symbol] = tr;
    }
    var td = tr.children;
    var hit = m.obiRatio != null && m.requiredRatio != null
              && m.obiRatio >= m.requiredRatio;

    _cell(td[0], escHtml(r.symbol));
    _cell(td[1], num(r.ltp));
    _cell(td[2], num(m.obiRatio), hit ? 'ratio-hit' : 'ratio-miss');
    _cell(td[3], num(m.weightedBids, 0));
    _cell(td[4], num(m.weightedAsks, 0));
    _cell(td[5], num(m.spreadPct, 3));
    _cell(td[6], m.bidOrders == null ? '—' : num(m.bidOrders, 0));
    _cell(td[7], num(m.tapeBuyQty, 0));
    _cell(td[8], m.tapeBuyRatio == null ? '—'
                 : Math.round(m.tapeBuyRatio * 100) + '%');
    _cell(td[9], num(m.bookAgeS, 1));
    _cell(td[10], r.ok ? '<strong>SIGNAL</strong>' : escHtml(r.reason || '—'),
          'reason-col');

    var cls = r.held === 'scalp' ? 'row-held' : (r.ok ? 'row-pass' : '');
    if (tr._c !== cls) { tr._c = cls; tr.className = cls; }

    seen[r.symbol] = 1;
    frag.appendChild(tr);          // moving an existing node also reorders it
  });

  Object.keys(_rowEls).forEach(function (sym) {
    if (!seen[sym]) delete _rowEls[sym];
  });
  // One atomic append: the fragment holds every row in its new order.
  tbody.innerHTML = '';
  tbody.appendChild(frag);
}

// ── Right column ──────────────────────────────────────────────────────────────

function renderOpen(open) {
  document.getElementById('open-count').textContent = open.length;
  var tbody = document.getElementById('open-tbody');
  if (!open.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">No open scalps</td></tr>';
    return;
  }
  var maxHold = S.caps.maxHoldS;
  tbody.innerHTML = open.map(function (p) {
    var live = (p.ltp - p.entry) * p.qty;
    // Held time turns red past the configured max hold — that position is about
    // to be time-stopped.
    var late = maxHold && p.heldS != null && p.heldS >= maxHold;
    return '<tr><td>' + escHtml(p.symbol) + '</td><td>' + p.qty + '</td><td>' +
      fmt2(p.entry) + '</td><td>' + fmt2(p.sl) + '</td><td>' + fmt2(p.target) +
      '</td><td>' + fmt2(p.ltp) + '</td><td class="' + (late ? 'pnl-neg' : '') + '">' +
      (p.heldS != null ? p.heldS + 's' : '—') + '</td>' +
      '<td class="' + (live >= 0 ? 'pnl-pos' : 'pnl-neg') + '">' +
      (live >= 0 ? '+' : '') + fmt2(live) + '</td></tr>';
  }).join('');
}

function renderRejects(rejects) {
  var el = document.getElementById('rejects-body');
  if (!rejects.length) {
    el.innerHTML = '<div class="muted-text">Nothing rejected yet.</div>';
    return;
  }
  var max = rejects[0].symbols || 1;
  el.innerHTML = rejects.map(function (r) {
    var pct = Math.max(4, Math.round((r.symbols / max) * 100));
    return '<div class="scalp-row" title="' + escHtml(r.reason) + '">' +
      '<span class="r-detail" style="text-align:left;flex:1 1 auto">' +
        escHtml(r.reason) + '</span>' +
      '<span style="flex:0 0 60px;height:6px;border-radius:3px;background:' +
        'var(--edge-soft);overflow:hidden;align-self:center">' +
        '<span style="display:block;width:' + pct + '%;height:100%;' +
        'background:var(--prime-dim)"></span></span>' +
      '<span class="r-count">' + r.symbols + '</span></div>';
  }).join('');
}

function renderLog(log) {
  var el = document.getElementById('log-body');
  if (!log.length) {
    el.innerHTML = '<div class="muted-text">No signals yet.</div>';
    return;
  }
  el.innerHTML = log.slice(-14).reverse().map(function (e) {
    var m = e.metrics || {};
    var detail = e.mode === 'exit'
      ? escHtml(e.note || '') + (e.pnl != null ? ' · ₹' + fmt2(e.pnl) : '')
      : 'W-OBI ' + (m.obiRatio != null ? m.obiRatio : '—') +
        ' · tape ' + (m.tapeBuyQty != null ? m.tapeBuyQty : '—') +
        (e.qty ? ' · ' + e.qty + ' @ ' + fmt2(e.entry) : '');
    return '<div class="scalp-row">' +
      '<span class="r-sym scalp-mode-' + escHtml(e.mode) + '">' +
        escHtml(e.symbol) + '</span>' +
      '<span class="r-detail">' + detail + '</span>' +
      '<span class="r-count">' + escHtml(e.time || '') + '</span></div>';
  }).join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────────

connect();
loadWindows();
loadScan();
scanTimer    = setInterval(loadScan, 1000);
// Window BOUNDARIES only change when someone edits the settings — slow refresh.
windowsTimer = setInterval(loadWindows, 30000);

// Stop polling while the tab is hidden; resume (and refresh immediately) on
// return, so a backgrounded page isn't hitting the server every second.
document.addEventListener('visibilitychange', function () {
  if (document.hidden) {
    if (scanTimer) { clearInterval(scanTimer); scanTimer = null; }
  } else if (!scanTimer) {
    loadScan();
    scanTimer = setInterval(loadScan, 1000);
  }
});
