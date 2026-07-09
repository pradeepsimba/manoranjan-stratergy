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
      // TICK_UPDATE (~100ms) only carries a price delta — not rendered here,
      // the 1s STATE_UPDATE already refreshes everything this page shows.
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
  if (lbEl) lbEl.textContent = (d.entryLoop && d.entryLoop.time) ? `Last bar ${d.entryLoop.time.substring(11,16)}` : 'Live';

  setStatus('ws',  d.wsStatus  || '—', d.wsStatus  === 'WS Connected' ? 'green' : 'red');
  setStatus('api', d.apiStatus || '—', d.apiStatus === 'API OK'        ? 'green' : 'red');

  const phase = d.phase || '—';
  const pb = document.getElementById('phase-badge');
  pb.textContent = phase.replace(/_/g, ' ').toUpperCase();
  pb.className   = 'badge ' + (PHASE_CLS[phase] || 'gray');

  document.getElementById('stat-bnltp').textContent = d.bnLtp ? fmt2(d.bnLtp) : '—';

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

  document.getElementById('stat-funds').textContent = d.funds != null ? '₹' + fmt2(d.funds) : '—';

  const activeEl = document.getElementById('stat-active');
  if (d.activeTrade) {
    activeEl.textContent = `${d.activeTrade.direction} ${d.activeTrade.optionType}`;
    activeEl.className = 'stat-value ' + (d.activeTrade.direction === 'BUY' ? 'pnl-pos' : 'pnl-neg');
  } else {
    activeEl.textContent = 'None';
    activeEl.className = 'stat-value';
  }

  renderTrade(d.activeTrade);
  renderClosedTrades(d.closedTrades || []);
  renderEntryLoop(d.entryLoop);
}

// ── Active trade card ─────────────────────────────────────────────────────────

function renderTrade(t) {
  const badge = document.getElementById('trade-badge');
  const empty = document.getElementById('trade-empty');
  const card  = document.getElementById('trade-card');

  if (!t) {
    badge.textContent = 'none'; badge.className = 'badge gray';
    empty.style.display = ''; card.style.display = 'none';
    return;
  }
  badge.textContent = `${t.direction} ${t.optionType}`;
  badge.className = 'badge ' + (t.direction === 'BUY' ? 'green' : 'red');
  empty.style.display = 'none'; card.style.display = '';

  const livePnl = (t.currentPremium - t.entryPremium) * t.lotSize;
  const pnlCls = livePnl > 0 ? 'pnl-pos' : livePnl < 0 ? 'pnl-neg' : '';
  const stageCls = t.slStage === 'Trail' ? 'pnl-pos' : t.slStage === 'Breakeven' ? '' : '';

  const cells = [
    ['Strike', t.strike + ' ' + t.optionType],
    ['Expiry', fmtDT(t.expiry)],
    ['Entry Index', fmt2(t.entryIndexPrice)],
    ['Current Index', fmt2(t.currentIndexPrice)],
    ['Target', fmt2(t.target)],
    ['Stop Loss', fmt2(t.currentSl)],
    ['SL Stage', t.slStage],
    ['Confidence', t.confidence != null ? t.confidence + '%' : '—'],
    ['Entry Premium', '₹' + fmt2(t.entryPremium)],
    ['Current Premium', '₹' + fmt2(t.currentPremium)],
    ['IV Used', t.currentIv != null ? (t.currentIv * 100).toFixed(1) + '%' : '—'],
    ['Live P&L', (livePnl >= 0 ? '+' : '') + '₹' + fmt2(livePnl)],
  ];
  card.innerHTML = cells.map(([lbl, val], i) => {
    const cls = lbl === 'SL Stage' ? stageCls : (lbl === 'Live P&L' ? pnlCls : '');
    return `<div class="trade-cell"><span class="lbl">${escHtml(lbl)}</span><span class="val ${cls}">${val}</span></div>`;
  }).join('');
}

// ── Closed trades ──────────────────────────────────────────────────────────────

function renderClosedTrades(trades) {
  document.getElementById('closed-count').textContent = trades.length;
  const tbody = document.getElementById('closed-tbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">No trades yet today</td></tr>';
    return;
  }
  const html = trades.slice().reverse().map(t => {
    const pnlCls = t.pnl > 0 ? 'pnl-pos' : t.pnl < 0 ? 'pnl-neg' : '';
    const ocCls  = t.status === 'CLOSED' ? '' : '';
    return `<tr>
      <td data-label="Dir">${escHtml(t.direction)}</td>
      <td data-label="Option">${t.strike} ${escHtml(t.optionType)}</td>
      <td data-label="Entry Idx">${fmt2(t.entryIndexPrice)}</td>
      <td data-label="Exit Idx">${fmt2(t.exitIndexPrice)}</td>
      <td data-label="Entry Prem">${fmt2(t.entryPremium)}</td>
      <td data-label="Exit Prem">${fmt2(t.exitPremium)}</td>
      <td data-label="Outcome" class="${ocCls}">${escHtml(t.status || '')}</td>
      <td data-label="P&L ₹" class="${pnlCls}">${(t.pnl >= 0 ? '+' : '') + fmt2(t.pnl)}</td>
    </tr>`;
  }).join('');
  if (tbody._h !== html) { tbody._h = html; tbody.innerHTML = html; }
}

// ── Entry Loop Monitor ("why didn't it fire") ─────────────────────────────────

function renderEntryLoop(d) {
  document.getElementById('entry-time').textContent = d && d.time ? d.time.substring(11, 16) : '—';

  const tbody = document.getElementById('leader-tbody');
  const rows = (d && d.leaderRows) || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">Waiting for data…</td></tr>';
  } else {
    tbody.innerHTML = rows.map(r => {
      const dirCls = r.close != null && r.open != null
        ? (r.close > r.open ? 'pnl-pos' : r.close < r.open ? 'pnl-neg' : '') : '';
      return `<tr>
        <td data-label="Leader">${escHtml(r.stock)}</td>
        <td data-label="Open">${fmt2(r.open)}</td>
        <td data-label="Close" class="${dirCls}">${fmt2(r.close)}</td>
        <td data-label="Volume">${r.volume != null ? Number(r.volume).toLocaleString('en-IN') : '—'}</td>
        <td data-label="Surge">${r.surged ? '<span class="badge green">yes</span>' : '<span class="badge gray">no</span>'}</td>
      </tr>`;
    }).join('');
  }

  const gates = document.getElementById('gate-rows');
  if (!d) { gates.innerHTML = ''; return; }
  const rows2 = [
    ['Leader vote', `${d.leaderSignal} (${d.green} green / ${d.red} red)`],
    ['Sideways range', d.sidewaysRange != null ? fmt2(d.sidewaysRange) + ' pts' : '—'],
    ['Momentum', d.momentumOk ? `OK — ${escHtml(d.momentumReason || '')}` : `weak — ${escHtml(d.momentumReason || '')}`],
    ['Volume surge count', `${d.strongQty} leaders`],
    ['RSI', d.rsi != null ? Number(d.rsi).toFixed(1) : '—'],
    ['MACD', d.macdDir || '—'],
    ['EMA stack', d.emaBullish ? 'Bullish' : d.emaBearish ? 'Bearish' : 'Neutral'],
    ['BN score', `bull ${Number(d.bnBull || 0).toFixed(1)} / bear ${Number(d.bnBear || 0).toFixed(1)}`],
  ];
  gates.innerHTML = rows2.map(([lbl, val]) =>
    `<div class="gate-row"><span class="g-lbl">${lbl}</span><span class="g-val">${val}</span></div>`
  ).join('');

  const reasonEl = document.getElementById('no-trade-reason');
  if (d.noTradeReason) {
    reasonEl.textContent = d.noTradeReason;
    reasonEl.className = 'no-trade-reason';
  } else {
    reasonEl.textContent = 'All gates clear — ready to fire on the next qualifying bar.';
    reasonEl.className = 'no-trade-reason ready';
  }
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
  const slipEl    = document.getElementById('bt-slip');
  const slippage  = slipEl && slipEl.value !== '' ? parseFloat(slipEl.value) : null;

  if (!from_date || !to_date) { toast('Select both a from and to date.', 'warn'); return; }

  let overrides = null;
  const ovrRaw = (document.getElementById('bt-overrides')?.value || '').trim();
  if (ovrRaw) {
    try {
      overrides = JSON.parse(ovrRaw);
      if (typeof overrides !== 'object' || Array.isArray(overrides)) throw new Error('not an object');
    } catch (e) {
      toast('Overrides must be a JSON object, e.g. {"BN_TARGET_POINTS": 40}', 'err');
      return;
    }
  }

  setBtStatus('running…', 'yellow');
  setRunBtn(true);
  document.getElementById('bt-summary').innerHTML = '';
  document.getElementById('bt-viz').style.display = 'none';
  document.getElementById('bt-meta').style.display = 'none';
  document.getElementById('bt-trades').innerHTML =
    '<tr><td colspan="11" class="empty-cell">Running…</td></tr>';

  const body = { from_date, to_date };
  if (slippage !== null && !Number.isNaN(slippage)) body.slippage_bps = slippage;
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

let _activePollRun = null;
let _pollFails     = 0;
const _POLL_MAX_FAILS = 8;

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
      if (runId !== _activePollRun) return;
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
          '<tr><td colspan="11" class="empty-cell">—</td></tr>';
        toast('Backtest failed: ' + (run.error || 'unknown error'), 'err');
        return;
      }
      setBtStatus('done', 'green');
      setExportBtn(runId);
      renderBacktestSummary(run);
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
      fetch('/api/backtests').then(r => r.json()).then(renderBtHistory).catch(() => {});
    })
    .catch(e => {
      if (runId !== _activePollRun) return;
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

function renderBacktestMeta(run) {
  const el = document.getElementById('bt-meta');
  if (!el) return;
  const p = run.params || {};
  const tags = [`<span class="tag">Timeframe <b>5m (intraday)</b></span>`];
  if (p.slippage_bps != null) tags.push(`<span class="tag">Slippage <b>${p.slippage_bps} bps</b></span>`);

  const ovr = p.overrides || {};
  const keys = Object.keys(ovr);
  let strat;
  if (!keys.length) {
    strat = '<span class="strat">Strategy: <b>Default</b></span>';
  } else {
    const parts = keys.map(k => {
      let v = ovr[k];
      if (v === true) v = 'on'; else if (v === false) v = 'off';
      return `${escHtml(k)}=${escHtml(String(v))}`;
    });
    strat = `<span class="strat">Strategy: <b>Custom</b> — ${parts.join(' · ')}</span>`;
    tags.push(`<span class="tag">${keys.length} override${keys.length !== 1 ? 's' : ''}</span>`);
  }
  el.innerHTML = tags.join('') + strat;
  el.style.display = '';
}

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

function renderBacktestViz(s) {
  const viz = document.getElementById('bt-viz');
  const curve = Array.isArray(s.equity_curve) ? s.equity_curve : [];
  const trades = s.total_trades ?? 0;
  if (!trades) { viz.style.display = 'none'; return; }
  viz.style.display = '';

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
    tbody.innerHTML = '<tr><td colspan="11" class="empty-cell">No trades</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlCls  = Number(t.net_pnl) > 0 ? 'pnl-pos' : Number(t.net_pnl) < 0 ? 'pnl-neg' : '';
    const ocCls   = t.outcome === 'TARGET' ? 'oc-target' : t.outcome === 'STOP' ? 'oc-stop' : 'oc-eod';
    const entryT  = fmtDT(t.entry_time);
    const exitT   = fmtDT(t.exit_time);
    return `<tr>
      <td class="sym-col">${escHtml(t.direction || '')}</td>
      <td>${t.strike || ''} ${escHtml(t.option_type || '')}</td>
      <td>${fmt2(t.entry_price)}</td>
      <td class="time-col">${entryT}</td>
      <td>${fmt2(t.exit_price)}</td>
      <td class="time-col">${exitT}</td>
      <td class="${ocCls}">${escHtml(t.outcome || '')}</td>
      <td class="num-col">${fmt2(t.entry_premium)}</td>
      <td class="num-col">${fmt2(t.exit_premium)}</td>
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
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  try {
    const d = s.substring(0, 10);
    const t = s.substring(11, 16);
    const [yy, mm, dd] = d.split('-');
    return `${parseInt(dd,10)} ${MON[parseInt(mm,10)-1]} ${yy} ${t}`;
  } catch { return s; }
}

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
        '<tr><td colspan="11" class="empty-cell">Running…</td></tr>';
      startPolling(active.run_id);
      return;
    }
    const latest = Array.isArray(runs) && runs.find(r => r.status === 'done');
    if (latest) loadRun(latest.run_id);
  })
  .catch(() => { /* server not ready yet — form stays visible */ });

connect();
