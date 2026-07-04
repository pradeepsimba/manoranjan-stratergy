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

  setStatus('ws',  d.wsStatus  || '—', d.wsStatus  === 'WS Connected' ? 'green' : 'red');
  setStatus('api', d.apiStatus || '—', d.apiStatus === 'API OK'        ? 'green' : 'red');

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
    _setCell(cells[0], p.symbol,                                               'card-title');
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
    const empty = '<tr><td colspan="3" class="empty-cell">Waiting for scans…</td></tr>';
    if (tbody._h !== empty) { tbody._h = empty; tbody.innerHTML = empty; }
    return;
  }

  const html = scans.slice(-25).reverse().map(r => {
    const passed = !!r.pass;
    const detail = passed
      ? (r.signal
          ? `@${fmt2(r.signal.ltp)} &middot; RSI ${fmt2(r.signal.rsi)} &middot; ADX ${fmt2(r.signal.adx)}`
          : 'signal')
      : (r.reason || '');
    return `<tr>
      <td>${r.symbol}</td>
      <td><span class="badge ${passed ? 'green' : 'gray'}">${passed ? 'SIGNAL' : 'skip'}</span></td>
      <td style="color:var(--txt-2);font-size:11px;max-width:240px;overflow:hidden;text-overflow:ellipsis">${detail}</td>
    </tr>`;
  }).join('');
  if (tbody._h !== html) { tbody._h = html; tbody.innerHTML = html; }
}

// ── Backtest ───────────────────────────────────────────────────────────────────

let btPoll       = null;
let currentRunId = null;

function setExportBtn(runId) {
  currentRunId = runId || null;
  const btn = document.getElementById('btn-export');
  if (btn) btn.style.display = currentRunId ? '' : 'none';
}

function exportCsv() {
  if (!currentRunId) return;
  window.location.href = `/api/backtest/${currentRunId}/export.csv`;
}

function runBacktest() {
  const from_date = document.getElementById('bt-from').value;
  const to_date   = document.getElementById('bt-to').value;
  const capital   = parseFloat(document.getElementById('bt-capital').value) || 40000;
  const slipEl    = document.getElementById('bt-slip');
  const slippage  = slipEl && slipEl.value !== '' ? parseFloat(slipEl.value) : null;

  if (!from_date || !to_date) { toast('Select both a from and to date.', 'warn'); return; }
  if (capital < 1000) { toast('Capital must be at least ₹1,000.', 'warn'); return; }

  // Optional per-run strategy overrides (JSON) — live settings stay untouched.
  let overrides = null;
  const ovrRaw = (document.getElementById('bt-overrides')?.value || '').trim();
  if (ovrRaw) {
    try {
      overrides = JSON.parse(ovrRaw);
      if (typeof overrides !== 'object' || Array.isArray(overrides)) throw new Error('not an object');
    } catch (e) {
      toast('Overrides must be a JSON object, e.g. {"RR_RATIO": 2.0}', 'err');
      return;
    }
  }

  setBtStatus('running…', 'yellow');
  setRunBtn(true);
  document.getElementById('bt-summary').innerHTML = '';
  document.getElementById('bt-viz').style.display = 'none';
  document.getElementById('bt-trades').innerHTML =
    '<tr><td colspan="14" class="empty-cell">Running…</td></tr>';

  const tfEl = document.getElementById('bt-tf');
  const timeframe = tfEl && tfEl.value ? tfEl.value : null;

  const body = { from_date, to_date, capital };
  if (slippage !== null && !Number.isNaN(slippage)) body.slippage_bps = slippage;
  if (timeframe) body.timeframe = timeframe;
  if (overrides) body.overrides = overrides;

  fetch('/api/backtest', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.run_id) throw new Error(d.detail || 'no run_id');
      startPolling(d.run_id);
    })
    .catch(e => {
      setBtStatus('error: ' + e.message, 'red');
      setRunBtn(false);
    });
}

function startPolling(runId) {
  clearInterval(btPoll);
  btPoll = setInterval(() => pollBacktest(runId), 1500);
}

function pollBacktest(runId) {
  fetch(`/api/backtest/${runId}`)
    .then(r => r.json())
    .then(run => {
      if (run.status === 'running') { setBtStatus('running…', 'yellow'); return; }
      clearInterval(btPoll);
      setRunBtn(false);

      if (run.status === 'error') {
        setBtStatus('failed', 'red');
        document.getElementById('bt-viz').style.display = 'none';
        document.getElementById('bt-summary').innerHTML =
          `<p class="pnl-neg" style="padding:8px 0;font-size:12px">${run.error || 'Backtest failed'}</p>`;
        document.getElementById('bt-trades').innerHTML =
          '<tr><td colspan="14" class="empty-cell">—</td></tr>';
        toast('Backtest failed: ' + (run.error || 'unknown error'), 'err');
        return;
      }
      setBtStatus('done', 'green');
      setExportBtn(runId);
      renderBacktestSummary(run.summary || {});
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
      // Refresh history strip so the new run appears
      fetch('/api/backtests').then(r => r.json()).then(renderBtHistory).catch(() => {});
    })
    .catch(e => {
      clearInterval(btPoll);
      setBtStatus('error: ' + e.message, 'red');
      setRunBtn(false);
    });
}

function setRunBtn(running) {
  const controls = document.querySelector('.bt-controls');
  if (controls) controls.classList.toggle('hidden', running);
}

function renderBacktestSummary(s) {
  const pf = s.profit_factor != null ? s.profit_factor : '—';
  const net = s.net_pnl ?? 0;
  const cells = [
    ['Trades',        s.total_trades ?? 0, ''],
    ['Win rate',      ((s.win_rate ?? 0) * 100).toFixed(1) + '%', ''],
    ['Net P&L',       '₹' + fmt2(net), net > 0 ? 'pnl-pos' : net < 0 ? 'pnl-neg' : ''],
    ['Profit factor', pf, ''],
    ['Max DD',        '₹' + fmt2(s.max_drawdown ?? 0), 'pnl-neg'],
    ['Avg R',         s.avg_r_multiple ?? 0, ''],
    ['Costs',         '₹' + fmt2(s.total_costs ?? 0), ''],
    ['Days',          s.days_traded ?? 0, ''],
  ];
  document.getElementById('bt-summary').innerHTML =
    '<div class="bt-grid">' +
    cells.map(([l, v, cls]) =>
      `<div class="bt-cell"><div class="bt-cell-label">${l}</div><div class="bt-cell-val ${cls}">${v}</div></div>`
    ).join('') +
    '</div>';
  renderBacktestViz(s);
}

// Equity curve (single series) + outcome breakdown. Uses the equity_curve and
// win/loss counts the metrics endpoint already returns — no extra fetch.
function renderBacktestViz(s) {
  const viz = document.getElementById('bt-viz');
  const curve = Array.isArray(s.equity_curve) ? s.equity_curve : [];
  const trades = s.total_trades ?? 0;
  if (!trades) { viz.style.display = 'none'; return; }
  viz.style.display = '';

  // Equity curve: prepend a 0 start so the line begins at flat, x = trade index.
  const pts = [[ '', 0 ]].concat(curve.map((row, i) =>
    [ (row[0] || '').toString().substring(0, 10) + ' · #' + (i + 1), Number(row[1]) ]));
  const net = s.net_pnl ?? 0;
  const netEl = document.getElementById('bt-eq-net');
  netEl.textContent = (net >= 0 ? '+₹' : '−₹') + fmt2(Math.abs(net));
  netEl.className = 'cnum ' + (net > 0 ? 'pnl-pos' : net < 0 ? 'pnl-neg' : '');
  lineChart(document.getElementById('bt-equity'), pts, {
    fmt:  v => (v >= 0 ? '+₹' : '−₹') + fmt2(Math.abs(v)),
    yfmt: v => (Math.abs(v) >= 1000 ? (v / 1000).toFixed(0) + 'k' : Math.round(v)),
  });

  const wins   = s.winning_trades ?? 0;
  const losses = s.losing_trades ?? 0;
  const flat   = Math.max(0, trades - wins - losses);
  outcomeBars(document.getElementById('bt-outcomes'), [
    { label: 'Wins',   value: wins,   kind: 'win'  },
    { label: 'Losses', value: losses, kind: 'loss' },
    { label: 'Flat',   value: flat,   kind: 'flat' },
  ]);
}

function renderBacktestTrades(trades) {
  const tbody = document.getElementById('bt-trades');
  if (!Array.isArray(trades) || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="14" class="empty-cell">No trades</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlCls  = Number(t.net_pnl) > 0 ? 'pnl-pos' : Number(t.net_pnl) < 0 ? 'pnl-neg' : '';
    const ocCls   = t.outcome === 'TARGET' ? 'oc-target' : t.outcome === 'STOP' ? 'oc-stop' : 'oc-eod';
    const entryT  = fmtDT(t.entry_time);
    const exitT   = fmtDT(t.exit_time);
    const rsi     = t.rsi  != null ? Number(t.rsi).toFixed(1)  : '—';
    const adx     = t.adx  != null ? Number(t.adx).toFixed(1)  : '—';
    const macdVal = t.macd != null ? Number(t.macd)            : null;
    const macd    = macdVal != null ? macdVal.toFixed(3)        : '—';
    const macdCls = macdVal != null ? (macdVal >= 0 ? 'pnl-pos' : 'pnl-neg') : '';
    const sup     = t.support_level != null ? fmt2(t.support_level) : '—';
    const pat     = t.candle_pattern || '—';
    return `<tr>
      <td class="sym-col">${t.symbol}</td>
      <td>${fmt2(t.entry_price)}</td>
      <td class="time-col">${entryT}</td>
      <td>${fmt2(t.exit_price)}</td>
      <td class="time-col">${exitT}</td>
      <td class="num-col">${t.quantity}</td>
      <td class="${ocCls}">${t.outcome}</td>
      <td class="num-col">${rsi}</td>
      <td class="num-col">${adx}</td>
      <td class="num-col ${macdCls}">${macd}</td>
      <td class="num-col">${sup}</td>
      <td class="pat-col">${pat}</td>
      <td class="${pnlCls}">${fmt2(t.net_pnl)}</td>
      <td class="num-col">${t.r_multiple}</td>
    </tr>`;
  }).join('');
}

function setBtStatus(text, cls) {
  const el = document.getElementById('bt-status');
  el.textContent = text;
  el.className   = 'badge ' + cls;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function setStatus(which, text, cls) {
  const el = document.getElementById(which + '-status');
  if (el) { el.textContent = text; el.className = 'badge ' + cls; }
}

function fmt2(n) {
  if (n == null || n === '') return '—';
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDT(s) {
  if (!s) return '—';
  // s: "2024-06-15T09:45:00" or "2024-06-15 09:45:00"
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  try {
    const d = s.substring(0, 10);         // "2024-06-15"
    const t = s.substring(11, 16);        // "09:45"
    const [yy, mm, dd] = d.split('-');
    return `${parseInt(dd,10)} ${MON[parseInt(mm,10)-1]} ${yy} ${t}`;
  } catch { return s; }
}

// ── Init ───────────────────────────────────────────────────────────────────────
// (toggleTheme lives in the shared /js/util.js)

// ── Backtest history ───────────────────────────────────────────────────────────

function fmtShortDate(s) {
  if (!s) return '';
  try {
    const [, m, d] = String(s).split('-');
    return `${d} ${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(m,10)-1]}`;
  } catch { return s; }
}

function renderBtHistory(runs) {
  if (!Array.isArray(runs)) return;
  const done = runs.filter(r => r.status === 'done');
  const wrap = document.getElementById('bt-history');
  const list = document.getElementById('bt-hist-list');
  if (!done.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  list.innerHTML = done.map(r => {
    const net = r.summary && r.summary.net_pnl != null ? r.summary.net_pnl : null;
    const cls = net != null ? (net >= 0 ? 'pnl-pos' : 'pnl-neg') : '';
    const label = `${fmtShortDate(r.from_date)} – ${fmtShortDate(r.to_date)}`;
    const pnl   = net != null ? `<span class="${cls}">₹${fmt2(net)}</span>` : '';
    return `<span class="bt-hist-item">
      <button class="bt-hist-load" onclick="loadRun('${r.run_id}')">${label} ${pnl}</button>
      <button class="bt-hist-del" onclick="deleteRun('${r.run_id}')" title="Delete">×</button>
    </span>`;
  }).join('');
}

function loadRun(runId) {
  setBtStatus('done', 'green');
  setExportBtn(runId);
  fetch(`/api/backtest/${runId}`)
    .then(r => r.json())
    .then(run => {
      renderBacktestSummary(run.summary || {});
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
    })
    .catch(e => setBtStatus('error: ' + e.message, 'red'));
}

function deleteRun(runId) {
  fetch(`/api/backtest/${runId}`, { method: 'DELETE' })
    .then(r => r.json())
    .then(() => {
      if (currentRunId === runId) setExportBtn(null);
      fetch('/api/backtests').then(r => r.json()).then(renderBtHistory);
    })
    .catch(e => console.error('Delete failed:', e));
}

// Populate the backtest timeframe dropdown from the server's supported set.
fetch('/api/timeframes')
  .then(r => r.json())
  .then(d => {
    const sel = document.getElementById('bt-tf');
    const list = Array.isArray(d.backtest_timeframes) ? d.backtest_timeframes : d.timeframes;
    if (!sel || !Array.isArray(list)) return;
    sel.innerHTML = list
      .map(tf => `<option value="${tf}"${tf === d.backtest_default ? ' selected' : ''}>${tf}</option>`)
      .join('');
  })
  .catch(() => { /* leave empty; backend falls back to the default */ });

// On page load: check for a running backtest (resume polling if found), then
// load the most recent done run so results survive a refresh — no localStorage.
fetch('/api/backtests')
  .then(r => r.json())
  .then(runs => {
    renderBtHistory(runs);
    const active = Array.isArray(runs) && runs.find(r => r.status === 'running');
    if (active) {
      setBtStatus('running…', 'yellow');
      setRunBtn(true);
      document.getElementById('bt-trades').innerHTML =
        '<tr><td colspan="14" class="empty-cell">Running…</td></tr>';
      startPolling(active.run_id);
      return;
    }
    // Auto-load the most recent completed run so the page is never blank.
    const latest = Array.isArray(runs) && runs.find(r => r.status === 'done');
    if (latest) loadRun(latest.run_id);
  })
  .catch(() => { /* server not ready yet — form stays visible */ });

connect();
