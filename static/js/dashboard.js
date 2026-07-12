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
    const empty = '<tr><td colspan="3" class="empty-cell">Waiting for scans…</td></tr>';
    if (tbody._h !== empty) { tbody._h = empty; tbody.innerHTML = empty; }
    return;
  }

  const html = scans.slice(-25).reverse().map(r => {
    const passed = !!r.pass;
    // Escape server-supplied strings (symbols like "M&M", failure reasons) —
    // the pass-branch is numbers + intentional &middot; entities, left as-is.
    const detail = passed
      ? (r.signal
          ? `@${fmt2(r.signal.ltp)} &middot; RSI ${fmt2(r.signal.rsi)} &middot; ADX ${fmt2(r.signal.adx)}`
          : 'signal')
      : escHtml(r.reason || '');
    return `<tr>
      <td>${escHtml(r.symbol)}</td>
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

  // Optional per-run strategy overrides from the picker — live settings stay untouched.
  const overrides = Object.keys(_btOvr).length ? { ..._btOvr } : null;

  const tfEl = document.getElementById('bt-tf');
  const timeframe = tfEl && tfEl.value ? tfEl.value : null;
  const modeEl = document.getElementById('bt-mode');
  const mode = modeEl && modeEl.value ? modeEl.value : null;

  // Pre-flight inert-combo warning: a % risk value does nothing unless the
  // MATCHING Risk basis is capital_pct (delivery runs read the DELIVERY pair).
  // Effective basis = per-run override if present, else the saved setting.
  if (_btSpecs) {
    const eff = k => (overrides && k in overrides) ? overrides[k]
                    : (_btSpecs[k] ? _btSpecs[k].value : undefined);
    const positional = (timeframe === '1d') || ((mode || eff('BACKTEST_MODE')) === 'delivery');
    const basisKey = positional ? 'DELIVERY_RISK_MODE' : 'RISK_MODE';
    const pctKey   = positional ? 'DELIVERY_RISK_CAPITAL_PERCENT' : 'RISK_CAPITAL_PERCENT';
    const wrongPairPct = positional ? 'RISK_CAPITAL_PERCENT' : 'DELIVERY_RISK_CAPITAL_PERCENT';
    if (overrides && wrongPairPct in overrides && !(pctKey in overrides)) {
      toast(`This ${positional ? 'delivery' : 'intraday'} run reads ${pctKey}, ` +
            `not ${wrongPairPct} — your override will be ignored`, 'warn');
    }
    if (overrides && pctKey in overrides && eff(basisKey) !== 'capital_pct') {
      toast(`Risk % has NO effect: set ${positional ? 'Risk basis (delivery)' : 'Risk basis'} ` +
            `= capital_pct (add it as an override or change it in Settings)`, 'warn');
    }
  }

  setBtStatus('running…', 'yellow');
  setRunBtn(true);
  document.getElementById('bt-summary').innerHTML = '';
  document.getElementById('bt-viz').style.display = 'none';
  document.getElementById('bt-meta').style.display = 'none';
  document.getElementById('bt-trades').innerHTML =
    '<tr><td colspan="14" class="empty-cell">Running…</td></tr>';

  // The server prefers the direct form fields over the overrides JSON — so
  // when the user typed one of these keys into the overrides box, DON'T send
  // the corresponding (always-populated) form field, or the override would be
  // validated, recorded in the run's params, shown in the meta tags… and
  // silently never applied.
  const hasOvr = k => !!overrides && Object.prototype.hasOwnProperty.call(overrides, k);
  const body = { from_date, to_date };
  if (!hasOvr('ACCOUNT_BALANCE')) body.capital = capital;
  if (slippage !== null && !Number.isNaN(slippage) && !hasOvr('SLIPPAGE_BPS')) body.slippage_bps = slippage;
  if (timeframe && !hasOvr('BACKTEST_TIMEFRAME')) body.timeframe = timeframe;
  if (mode && !hasOvr('BACKTEST_MODE')) body.mode = mode;
  if (overrides) body.overrides = overrides;

  fetch('/api/backtest', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.run_id) throw new Error(d.detail || 'no run_id');
      // Server may shorten oversized ranges instead of rejecting — say so.
      if (d.note) toast(d.note, 'warn');
      startPolling(d.run_id);
    })
    .catch(e => {
      setBtStatus('error: ' + e.message, 'red');
      setRunBtn(false);
    });
}

// Which run the poller currently owns. In-flight responses for any OTHER run
// are dropped — a stale response from a previous run must not clear the new
// run's interval or paint the old run's numbers as final.
let _activePollRun = null;
let _pollFails     = 0;
const _POLL_MAX_FAILS = 8;   // ~12s of consecutive failures before giving up

function startPolling(runId) {
  clearInterval(btPoll);
  _activePollRun = runId;
  _pollFails = 0;
  btPoll = setInterval(() => pollBacktest(runId), 1500);
}

function pollBacktest(runId) {
  fetch(`/api/backtest/${runId}`)
    .then(async r => {
      const run = await r.json();
      if (runId !== _activePollRun) return;          // stale run — ignore
      // A non-OK or shapeless response (e.g. 404 {detail}) is a FAILURE, not a
      // completed run — without this it fell through to the "done" path and
      // rendered a fake successful result.
      if (!r.ok || !run || !['running', 'done', 'error'].includes(run.status)) {
        throw new Error((run && run.detail) || r.statusText || 'bad response');
      }
      _pollFails = 0;
      if (run.status === 'running') { setBtStatus('running…', 'yellow'); return; }
      clearInterval(btPoll);
      _activePollRun = null;
      setRunBtn(false);

      if (run.status === 'error') {
        setBtStatus('failed', 'red');
        document.getElementById('bt-viz').style.display = 'none';
        document.getElementById('bt-meta').style.display = 'none';
        document.getElementById('bt-summary').innerHTML =
          `<p class="pnl-neg" style="padding:8px 0;font-size:12px">${escHtml(run.error || 'Backtest failed')}</p>`;
        document.getElementById('bt-trades').innerHTML =
          '<tr><td colspan="14" class="empty-cell">—</td></tr>';
        toast('Backtest failed: ' + (run.error || 'unknown error'), 'err');
        return;
      }
      setBtStatus('done', 'green');
      setExportBtn(runId);
      renderBacktestSummary(run);
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
      // Refresh history strip so the new run appears
      fetch('/api/backtests').then(r => r.json()).then(renderBtHistory).catch(() => {});
    })
    .catch(e => {
      if (runId !== _activePollRun) return;          // stale run — ignore
      // Transient failures (network blip, laptop resume, server restart) must
      // NOT kill the poll — the run finishes server-side. Give up only after
      // several consecutive failures.
      if (++_pollFails < _POLL_MAX_FAILS) {
        setBtStatus('running… (retrying)', 'yellow');
        return;
      }
      clearInterval(btPoll);
      _activePollRun = null;
      setBtStatus('error: ' + e.message, 'red');
      setRunBtn(false);
      toast('Lost contact with the backtest — reload the page to resume.', 'err');
    });
}

function setRunBtn(running) {
  const controls = document.querySelector('.bt-controls');
  if (controls) controls.classList.toggle('hidden', running);
}

// Caption above the summary: the timeframe this run used + a readable note of
// which strategy was applied (the per-run overrides, or "Default strategy").
function renderBacktestMeta(run) {
  const el = document.getElementById('bt-meta');
  if (!el) return;
  const p = run.params || {};
  const tf = p.timeframe || '5m';
  const tags = [`<span class="tag tf">Timeframe <b>${escHtml(tf)}</b></span>`];
  // Older runs have no mode in params — they all ran intraday (or positional for 1d).
  const mode = p.mode || (tf === '1d' ? 'delivery' : 'intraday');
  tags.push(`<span class="tag">Mode <b>${escHtml(mode)}</b></span>`);
  if (p.capital != null)      tags.push(`<span class="tag">Capital <b>₹${fmt2(p.capital)}</b></span>`);
  if (p.risk)                 tags.push(`<span class="tag">Risk <b>${escHtml(p.risk)}</b></span>`);
  if (p.slippage_bps != null) tags.push(`<span class="tag">Slippage <b>${p.slippage_bps} bps</b></span>`);
  // Data source held less history than requested — show where the replay
  // really began so a short days_traded doesn't look like a bug.
  const sm = run.summary || {};
  if (sm.data_from && run.from_date && sm.data_from > run.from_date) {
    tags.push(`<span class="tag ovr">data starts <b>${escHtml(sm.data_from)}</b></span>`);
  }

  const ovr = p.overrides || {};
  const keys = Object.keys(ovr);
  let strat;
  if (!keys.length) {
    strat = '<span class="strat">Strategy: <b>Default</b> (conditions &amp; gates as configured)</span>';
  } else {
    const parts = keys.map(k => {
      let v = ovr[k];
      if (v === true) v = 'on'; else if (v === false) v = 'off';
      else if (typeof v === 'object' && v !== null) {
        // Structured overrides (e.g. CUSTOM_ENTRY_RULES) — summarize, don't dump.
        v = k === 'CUSTOM_ENTRY_RULES'
          ? `${v.mode || 'and'}·${(v.groups || []).length}g`
          : JSON.stringify(v);
      }
      return `${escHtml(k)}=${escHtml(String(v))}`;   // key & value escaped
    });
    strat = `<span class="strat">Strategy: <b>Custom</b> — ${parts.join(' · ')}</span>`;
    tags.push(`<span class="tag ovr">${keys.length} override${keys.length !== 1 ? 's' : ''}</span>`);
  }
  el.innerHTML = tags.join('') + strat;
  el.style.display = '';
}

// Accepts the full run object ({summary, params, from_date, to_date}) so it can
// show which timeframe and which strategy overrides the run actually used.
function renderBacktestSummary(run) {
  const s = (run && run.summary) || {};
  renderBacktestMeta(run || {});
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
      <td class="sym-col">${escHtml(t.symbol)}</td>
      <td>${fmt2(t.entry_price)}</td>
      <td class="time-col">${entryT}</td>
      <td>${fmt2(t.exit_price)}</td>
      <td class="time-col">${exitT}</td>
      <td class="num-col">${t.quantity}</td>
      <td class="${ocCls}">${escHtml(t.outcome || '')}</td>
      <td class="num-col">${rsi}</td>
      <td class="num-col">${adx}</td>
      <td class="num-col ${macdCls}">${macd}</td>
      <td class="num-col">${sup}</td>
      <td class="pat-col">${escHtml(pat)}</td>
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
  fetch(`/api/backtest/${runId}`)
    .then(async r => {
      const run = await r.json();
      // Validate BEFORE painting "done" — a 404 {detail} or non-done status
      // must not render as a completed run with an Export button.
      if (!r.ok || !run || run.status !== 'done') {
        throw new Error((run && (run.detail || run.error)) || 'run not available');
      }
      setBtStatus('done', 'green');
      setExportBtn(runId);
      renderBacktestSummary(run);
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
    })
    .catch(e => { setBtStatus('error: ' + e.message, 'red'); setExportBtn(null); });
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
    syncBtMode();
  })
  .catch(() => { /* leave empty; backend falls back to the default */ });

// 1d bars ARE days — that replay is positional by construction, so lock the
// mode selector to Delivery while 1d is chosen (the server enforces it too).
function syncBtMode() {
  const tfEl = document.getElementById('bt-tf');
  const modeEl = document.getElementById('bt-mode');
  if (!tfEl || !modeEl) return;
  const is1d = tfEl.value === '1d';
  if (is1d) modeEl.value = 'delivery';
  modeEl.disabled = is1d;
  modeEl.title = is1d ? '1d bars replay positionally — mode is fixed to Delivery' : '';
  // Positional runs behave differently in ways that surprise people — say so
  // up front instead of letting the results confuse.
  const hint = document.getElementById('bt-mode-hint');
  if (hint) {
    const positional = is1d || modeEl.value === 'delivery';
    hint.style.display = positional ? '' : 'none';
    if (positional) {
      hint.innerHTML =
        '<b>Positional (delivery) run:</b> one portfolio across the whole range — ' +
        'positions carry overnight and gaps fill at the next open. ' +
        '<b>Each symbol trades at most once per run</b>, the loss limit acts as a ' +
        'run-level stop (not daily), and the <a href="/settings#delivery">Delivery Mode</a> ' +
        'settings (stop, risk, leverage, costs, conditions) apply instead of the intraday ones.';
    }
  }
}
const _btTfEl = document.getElementById('bt-tf');
if (_btTfEl) _btTfEl.addEventListener('change', syncBtMode);
const _btModeEl = document.getElementById('bt-mode');
if (_btModeEl) _btModeEl.addEventListener('change', syncBtMode);
syncBtMode();

// ── Backtest overrides picker ─────────────────────────────────────────────────
// No-code replacement for the old raw-JSON textarea: pick a setting, get a
// type-aware input (toggle / choices / time / bounded number), add it as a
// removable chip. _btOvr is exactly the {SPEC_KEY: value} object the API takes.
let _btOvr   = {};
let _btSpecs = null;   // key → settings-describe entry (bt-able, non-rules)

function loadBtSpecs() {
  const sel = document.getElementById('bt-ovr-key');
  if (!sel || _btSpecs) return;
  fetch('/api/settings').then(r => r.json()).then(d => {
    _btSpecs = {};
    (d.groups || []).forEach(g => {
      const grp = document.createElement('optgroup');
      grp.label = g.name;
      (g.settings || []).forEach(s => {
        if (!s.bt || s.type === 'rules') return;
        _btSpecs[s.key] = s;
        const o = document.createElement('option');
        o.value = s.key;
        o.textContent = s.label;
        if (s.help) o.title = s.help;
        grp.appendChild(o);
      });
      if (grp.children.length) sel.appendChild(grp);
    });
  }).catch(() => { _btSpecs = null; });
}

function _btOvrValueControl(s) {
  if (s.type === 'bool')
    return '<select class="field-input" id="bt-ovr-value" style="width:80px">' +
           '<option value="true">on</option><option value="false">off</option></select>';
  if (s.type === 'choice')
    return '<select class="field-input" id="bt-ovr-value" style="width:150px">' +
           (s.choices || []).map(c =>
             `<option${String(c) === String(s.value) ? ' selected' : ''}>${escHtml(String(c))}</option>`
           ).join('') + '</select>';
  if (s.type === 'time')
    return `<input class="field-input" id="bt-ovr-value" type="time" value="${escHtml(String(s.value ?? ''))}" style="width:110px">`;
  const step = s.step != null ? s.step : (s.type === 'int' ? 1 : 'any');
  return '<input class="field-input" id="bt-ovr-value" type="number" style="width:120px"' +
         (s.min != null ? ` min="${s.min}"` : '') + (s.max != null ? ` max="${s.max}"` : '') +
         ` step="${step}" placeholder="${escHtml(String(s.value ?? ''))}" ` +
         `title="current: ${escHtml(String(s.value ?? '—'))} · default: ${escHtml(String(s.default ?? '—'))}">`;
}

const _btOvrKeyEl = document.getElementById('bt-ovr-key');
if (_btOvrKeyEl) {
  loadBtSpecs();
  _btOvrKeyEl.addEventListener('change', () => {
    const slot = document.getElementById('bt-ovr-value-slot');
    const s = _btSpecs && _btSpecs[_btOvrKeyEl.value];
    if (slot) slot.innerHTML = s ? _btOvrValueControl(s) : '';
  });
}

function btOvrAdd() {
  const keyEl = document.getElementById('bt-ovr-key');
  const valEl = document.getElementById('bt-ovr-value');
  const s = _btSpecs && keyEl ? _btSpecs[keyEl.value] : null;
  if (!s) { toast('Pick a setting first', 'warn'); return; }
  if (!valEl) return;
  let v;
  if (s.type === 'bool') v = valEl.value === 'true';
  else if (s.type === 'choice' || s.type === 'time') {
    v = valEl.value;
    if (!v) { toast('Enter a value', 'warn'); return; }
  } else {
    v = parseFloat(valEl.value);
    if (Number.isNaN(v)) { toast('Enter a number', 'warn'); return; }
    if (s.min != null && v < s.min) { toast(`${s.label}: minimum is ${s.min}`, 'warn'); return; }
    if (s.max != null && v > s.max) { toast(`${s.label}: maximum is ${s.max}`, 'warn'); return; }
  }
  _btOvr[s.key] = v;
  renderBtOvrChips();
}

function btOvrRemove(key) { delete _btOvr[key]; renderBtOvrChips(); }

function renderBtOvrChips() {
  const box = document.getElementById('bt-ovr-chips');
  const cnt = document.getElementById('bt-ovr-count');
  if (!box) return;
  const keys = Object.keys(_btOvr);
  box.innerHTML = keys.map(k => {
    const s = (_btSpecs && _btSpecs[k]) || { label: k };
    let v = _btOvr[k];
    if (v === true) v = 'on'; else if (v === false) v = 'off';
    return `<span class="bt-chip">${escHtml(s.label)} = <b>${escHtml(String(v))}</b>` +
           `<button class="bt-chip-x" type="button" onclick="btOvrRemove('${escHtml(k)}')" title="Remove">×</button></span>`;
  }).join('');
  if (cnt) {
    cnt.style.display = keys.length ? '' : 'none';
    cnt.textContent = keys.length + ' override' + (keys.length !== 1 ? 's' : '');
  }
}

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
