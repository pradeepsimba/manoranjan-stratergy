'use strict';

// ── WebSocket ──────────────────────────────────────────────────────────────────

let ws = null;
let reconnectTimer = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/dashboard`);

  ws.onopen = () => {
    clearTimeout(reconnectTimer);
    setStatus('ws', 'Connected', 'green');
  };

  ws.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      // INDICATOR_UPDATE (~100 ms) only carries indicatorSnapshot — no clock/nifty/watchlist.
      // Passing it to render() would blank every scalar field.  Dashboard doesn't use
      // indicatorSnapshot at all, so skip anything that isn't a full STATE_UPDATE.
      if (d.type !== 'STATE_UPDATE') return;
      scheduleRender(d);
    } catch (err) { console.error(err); }
  };

  ws.onclose = ws.onerror = () => {
    setStatus('ws', 'Disconnected', 'red');
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 3000);
  };
}

// ── Render state ──────────────────────────────────────────────────────────────

let _pendingData = null;
let _rafPending  = false;
let _posRowEls   = {};   // symbol → <tr> for DOM diffing
let _scanRowEls  = {};   // symbol → <tr> for DOM diffing (last_scan_results is deduped by symbol)

function scheduleRender(d) {
  _pendingData = d;
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(() => {
    _rafPending = false;
    if (_pendingData) { render(_pendingData); _pendingData = null; }
  });
}

// Update a single <td> only when its content or class actually changed.
function _setCell(td, html, cls) {
  if (td._h !== html) { td._h = html; td.innerHTML = html; }
  if (td._c !== cls)  { td._c = cls;  td.className  = cls;  }
}

// ── Render ─────────────────────────────────────────────────────────────────────

const PHASE_CLS = {
  pre_market: 'gray', wait_zone: 'yellow', active: 'green',
  cutoff: 'yellow',   closed: 'gray',
};

function render(d) {
  document.getElementById('clock').textContent = d.clock || '—';

  const lbEl = document.getElementById('last-bar-time');
  if (lbEl) lbEl.textContent = d.lastBarTime ? `Last bar ${d.lastBarTime}` : 'Live';

  // The server derives ws_status from the tradeable feed and may suffix a
  // degraded-aux note ("WS Connected (1 aux down)") — still connected, so
  // prefix-match; strict equality would paint the healthy feed red.
  const wsUp = (d.wsStatus || '').startsWith('WS Connected');
  const wsDegraded = wsUp && d.wsStatus !== 'WS Connected';
  setStatus('ws',  d.wsStatus  || '—', wsUp ? (wsDegraded ? 'yellow' : 'green') : 'red');
  const apiSt = d.apiStatus || '—';
  setStatus('api', apiSt, apiSt === 'API OK' ? 'green'
                        : apiSt.startsWith('API partial') ? 'yellow' : 'red');

  const phase = d.phase || '—';
  const pb = document.getElementById('phase-badge');
  pb.textContent = phase.replace(/_/g, ' ').toUpperCase();
  pb.className   = 'badge ' + (PHASE_CLS[phase] || 'gray');

  document.getElementById('stat-nifty').textContent =
    d.niftyLtp ? fmt2(d.niftyLtp) : '—';

  // P&L — update value + card accent
  const pnl    = d.dailyPnl || 0;
  const pnlEl  = document.getElementById('stat-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '₹' + fmt2(pnl);
  pnlEl.className   = 'stat-value' + (pnl > 0 ? ' pnl-pos' : pnl < 0 ? ' pnl-neg' : '');

  const pnlCard = document.getElementById('card-pnl');
  if (pnlCard) {
    pnlCard.classList.remove('is-pos', 'is-neg');
    if (pnl > 0) pnlCard.classList.add('is-pos');
    else if (pnl < 0) pnlCard.classList.add('is-neg');
  }

  const positions = d.positions || [];
  const openCount = positions.filter(p => p.status === 'OPEN').length;
  document.getElementById('stat-open').textContent  = openCount;
  document.getElementById('stat-watch').textContent = (d.watchlist || []).length;

  renderPositions(positions, openCount);
  renderWatchlist(d.watchlist || [], d.geminiList || []);
  renderScans(d.scanResults || []);
}

// (escHtml lives in the shared /js/util.js)

function renderPositions(positions, openCount) {
  document.getElementById('pos-count').textContent = openCount;
  const tbody = document.getElementById('positions-tbody');

  if (!positions.length) {
    _posRowEls = {};
    tbody.innerHTML = '<tr><td colspan="9" class="empty-cell">No positions yet</td></tr>';
    return;
  }

  // Clear the empty-state placeholder on first real data
  if (tbody.querySelector('.empty-cell')) tbody.innerHTML = '';

  const seen = {};

  // Step 1: update cells in-place — no DOM moves
  positions.forEach(p => {
    seen[p.symbol] = true;
    let tr = _posRowEls[p.symbol];
    if (!tr) {
      tr = document.createElement('tr');
      for (let i = 0; i < 9; i++) {
        const td = document.createElement('td');
        td._h = null; td._c = null;
        tr.appendChild(td);
      }
      tr._cls = null;
      _posRowEls[p.symbol] = tr;
    }
    const rowCls = p.status === 'OPEN' ? 'bull-row' : '';
    if (tr._cls !== rowCls) { tr._cls = rowCls; tr.className = rowCls; }

    const pnlCls = p.livePnl > 0 ? 'pnl-pos' : p.livePnl < 0 ? 'pnl-neg' : '';
    const stCls  = p.status === 'OPEN' ? 'badge green' : 'badge gray';
    const cells  = tr.children;
    _setCell(cells[0], escHtml(p.symbol),                                      'card-title');
    _setCell(cells[1], `<span class="${stCls}">${p.status}</span>`,            '');
    _setCell(cells[2], fmt2(p.entry),                                          '');
    _setCell(cells[3], (p.entryTime || '').substring(11, 19),                  '');
    _setCell(cells[4], String(p.qty),                                          '');
    _setCell(cells[5], fmt2(p.sl),                                             '');
    _setCell(cells[6], fmt2(p.target),                                         '');
    _setCell(cells[7], fmt2(p.ltp),                                            '');
    _setCell(cells[8], (p.livePnl >= 0 ? '+' : '') + fmt2(p.livePnl),         pnlCls);
    // data-label drives the mobile card layout (table.cardify td::before)
    const LBL = ['Symbol','Status','Entry','Time','Qty','SL','Target','LTP','P&L ₹'];
    for (let i = 0; i < cells.length; i++) {
      if (cells[i].getAttribute('data-label') !== LBL[i]) cells[i].setAttribute('data-label', LBL[i]);
    }
  });

  // Step 2: remove rows for positions that left
  Object.keys(_posRowEls).forEach(sym => {
    if (!seen[sym]) {
      if (_posRowEls[sym].parentNode) _posRowEls[sym].remove();
      delete _posRowEls[sym];
    }
  });

  // Step 3: reorder only when DOM order differs from data order
  const children = tbody.children;
  let needReorder = children.length !== positions.length;
  if (!needReorder) {
    for (let i = 0; i < positions.length; i++) {
      if (children[i] !== _posRowEls[positions[i].symbol]) { needReorder = true; break; }
    }
  }
  if (needReorder) {
    const frag = document.createDocumentFragment();
    positions.forEach(p => frag.appendChild(_posRowEls[p.symbol]));
    tbody.appendChild(frag);   // single atomic DOM write
  }
}

function renderWatchlist(list, aiList) {
  document.getElementById('gemini-count').textContent = list.length;
  const el = document.getElementById('gemini-list');
  const ai = new Set(aiList);
  const html = !list.length
    ? '<span class="muted-text">Building at 09:00 IST…</span>'
    : list.map(s => {
        const sym = escHtml(s);
        return `<span class="stock-chip removable ${ai.has(s) ? 'chip-ai' : ''}" data-sym="${sym}">` +
               `${sym}<button class="chip-x" data-remove="${sym}" title="Remove from watchlist">×</button></span>`;
      }).join('');
  if (el._h !== html) { el._h = html; el.innerHTML = html; }
}

// ── Runtime watchlist control ─────────────────────────────────────────────────

// Chip × clicks — delegated so the diffed innerHTML needs no re-binding.
document.getElementById('gemini-list').addEventListener('click', (e) => {
  const sym = e.target.getAttribute && e.target.getAttribute('data-remove');
  if (sym) wlRemove(sym);
});

let _universeLoaded = 0;
function loadUniverse() {
  // Refresh the add-symbol datalist at most every 60s.
  if (Date.now() - _universeLoaded < 60_000) return;
  _universeLoaded = Date.now();
  fetch('/api/watchlist/full')
    .then(r => r.json())
    .then(rows => {
      if (!Array.isArray(rows)) return;
      document.getElementById('wl-options').innerHTML = rows
        .filter(r => !r.active)
        .map(r => `<option value="${escHtml(r.symbol)}"></option>`)
        .join('');
    })
    .catch(() => { _universeLoaded = 0; });
}

function wlAdd() {
  const input = document.getElementById('wl-input');
  const sym = (input.value || '').trim();
  if (!sym) return;
  fetch('/api/watchlist/add', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ symbol: sym }),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      input.value = '';
      _universeLoaded = 0;   // datalist is stale now
      toast(d.changed === false ? `${d.symbol} already tradeable` : `Added ${d.symbol}`, 'ok');
    })
    .catch(e => toast('Add failed: ' + e.message, 'err'));
}

function wlRemove(sym) {
  if (!confirm(`Remove ${sym} from the tradeable watchlist?`)) return;
  fetch('/api/watchlist/remove', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ symbol: sym }),
  })
    .then(async r => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      _universeLoaded = 0;
      toast(`Removed ${d.symbol}`, 'ok');
    })
    .catch(e => toast('Remove failed: ' + e.message, 'err'));
}

function renderScans(scans) {
  const passCount = scans.filter(r => r.pass).length;
  const skipCount = scans.length - passCount;

  const passEl = document.getElementById('scan-pass');
  const skipEl = document.getElementById('scan-skip');
  if (passEl) passEl.textContent = passCount + ' signal' + (passCount !== 1 ? 's' : '');
  if (skipEl) skipEl.textContent = skipCount + ' skipped';

  const tbody = document.getElementById('scans-tbody');

  if (!scans.length) {
    _scanRowEls = {};
    const empty = '<tr><td colspan="3" class="empty-cell">Waiting for scans…</td></tr>';
    if (tbody._h !== empty) { tbody._h = empty; tbody.innerHTML = empty; }
    return;
  }
  if (tbody.querySelector('.empty-cell')) tbody.innerHTML = '';

  // last_scan_results is deduped by symbol server-side (record_scan pops then
  // reinserts), so each symbol appears at most once here — safe to key the
  // row cache by symbol and patch cells in-place instead of rebuilding the
  // whole tbody's innerHTML every push (which flashed every ~1s while active).
  const rows = scans.slice(-25).reverse();
  const seen = {};

  rows.forEach(r => {
    seen[r.symbol] = true;
    let tr = _scanRowEls[r.symbol];
    if (!tr) {
      tr = document.createElement('tr');
      for (let i = 0; i < 3; i++) {
        const td = document.createElement('td');
        td._h = null; td._c = null;
        tr.appendChild(td);
      }
      tr.children[2].style.cssText =
        'color:var(--txt-2);font-size:11px;max-width:240px;overflow:hidden;text-overflow:ellipsis';
      _scanRowEls[r.symbol] = tr;
    }
    const passed = !!r.pass;
    // Escape server-supplied strings (symbols like "M&M", failure reasons) —
    // the pass-branch is numbers + intentional &middot; entities, left as-is.
    const detail = passed
      ? (r.signal
          ? `@${fmt2(r.signal.ltp)} &middot; RSI ${fmt2(r.signal.rsi)} &middot; ADX ${fmt2(r.signal.adx)}`
          : 'signal')
      : escHtml(r.reason || '');
    const cells = tr.children;
    _setCell(cells[0], escHtml(r.symbol), '');
    _setCell(cells[1], `<span class="badge ${passed ? 'green' : 'gray'}">${passed ? 'SIGNAL' : 'skip'}</span>`, '');
    _setCell(cells[2], detail, '');
  });

  Object.keys(_scanRowEls).forEach(sym => {
    if (!seen[sym]) {
      if (_scanRowEls[sym].parentNode) _scanRowEls[sym].remove();
      delete _scanRowEls[sym];
    }
  });

  let needReorder = tbody.children.length !== rows.length;
  if (!needReorder) {
    for (let i = 0; i < rows.length; i++) {
      if (tbody.children[i] !== _scanRowEls[rows[i].symbol]) { needReorder = true; break; }
    }
  }
  if (needReorder) {
    const frag = document.createDocumentFragment();
    rows.forEach(r => frag.appendChild(_scanRowEls[r.symbol]));
    tbody.appendChild(frag);
  }
}

function setStatus(which, text, cls) {
  const el = document.getElementById(which + '-status');
  if (el) { el.textContent = text; el.className = 'badge ' + cls; }
}

connect();
