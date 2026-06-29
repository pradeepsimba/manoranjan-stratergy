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
    try { render(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };

  ws.onclose = ws.onerror = () => {
    setStatus('ws', 'Disconnected', 'red');
    reconnectTimer = setTimeout(connect, 3000);
  };
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
  renderGemini(d.geminiList || []);
  renderScans(d.scanResults || []);
}

function renderPositions(positions, openCount) {
  document.getElementById('pos-count').textContent = openCount;
  const tbody = document.getElementById('positions-tbody');
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-cell">No positions yet</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnlCls = p.livePnl > 0 ? 'pnl-pos' : p.livePnl < 0 ? 'pnl-neg' : '';
    const stCls  = p.status === 'OPEN' ? 'badge green' : 'badge gray';
    return `<tr class="${p.status === 'OPEN' ? 'bull-row' : ''}">
      <td>${p.symbol}</td>
      <td><span class="${stCls}">${p.status}</span></td>
      <td>${fmt2(p.entry)}</td>
      <td>${(p.entryTime || '').substring(11, 19)}</td>
      <td>${p.qty}</td>
      <td>${fmt2(p.sl)}</td>
      <td>${fmt2(p.target)}</td>
      <td>${fmt2(p.ltp)}</td>
      <td class="${pnlCls}">${(p.livePnl >= 0 ? '+' : '') + fmt2(p.livePnl)}</td>
    </tr>`;
  }).join('');
}

function renderGemini(list) {
  document.getElementById('gemini-count').textContent = list.length;
  const el = document.getElementById('gemini-list');
  if (!list.length) {
    el.innerHTML = '<span class="muted-text">Building at 09:00 IST…</span>';
    return;
  }
  el.innerHTML = list.map(s => `<span class="stock-chip">${s}</span>`).join('');
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
    tbody.innerHTML = '<tr><td colspan="3" class="empty-cell">Waiting for scans…</td></tr>';
    return;
  }
  tbody.innerHTML = scans.slice(-25).reverse().map(r => {
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
}

// ── Backtest ───────────────────────────────────────────────────────────────────

let btPoll = null;

function runBacktest() {
  const from_date = document.getElementById('bt-from').value;
  const to_date   = document.getElementById('bt-to').value;
  const capital   = parseFloat(document.getElementById('bt-capital').value) || 40000;

  if (!from_date || !to_date) { alert('Select both a from and to date.'); return; }
  if (capital < 1000) { alert('Capital must be at least ₹1,000.'); return; }

  setBtStatus('running…', 'yellow');
  setRunBtn(true);
  document.getElementById('bt-summary').innerHTML = '';
  document.getElementById('bt-trades').innerHTML =
    '<tr><td colspan="7" class="empty-cell">Running…</td></tr>';

  fetch('/api/backtest', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ from_date, to_date, capital }),
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
        document.getElementById('bt-summary').innerHTML =
          `<p class="pnl-neg" style="padding:8px 0;font-size:12px">${run.error || 'Backtest failed'}</p>`;
        document.getElementById('bt-trades').innerHTML =
          '<tr><td colspan="7" class="empty-cell">—</td></tr>';
        return;
      }
      setBtStatus('done', 'green');
      renderBacktestSummary(run.summary || {});
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
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
  const cells = [
    ['Trades',        s.total_trades ?? 0],
    ['Win rate',      ((s.win_rate ?? 0) * 100).toFixed(1) + '%'],
    ['Net P&L',       '₹' + fmt2(s.net_pnl ?? 0)],
    ['Profit factor', pf],
    ['Max DD',        '₹' + fmt2(s.max_drawdown ?? 0)],
    ['Avg R',         s.avg_r_multiple ?? 0],
    ['Costs',         '₹' + fmt2(s.total_costs ?? 0)],
    ['Days',          s.days_traded ?? 0],
  ];
  document.getElementById('bt-summary').innerHTML =
    '<div class="bt-grid">' +
    cells.map(([l, v]) =>
      `<div class="bt-cell"><div class="bt-cell-label">${l}</div><div class="bt-cell-val">${v}</div></div>`
    ).join('') +
    '</div>';
}

function renderBacktestTrades(trades) {
  const tbody = document.getElementById('bt-trades');
  if (!Array.isArray(trades) || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">No trades</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlCls = Number(t.net_pnl) > 0 ? 'pnl-pos' : Number(t.net_pnl) < 0 ? 'pnl-neg' : '';
    const ocCls  = t.outcome === 'TARGET' ? 'oc-target' : t.outcome === 'STOP' ? 'oc-stop' : 'oc-eod';
    return `<tr>
      <td>${t.symbol}</td>
      <td>${fmt2(t.entry_price)}</td>
      <td>${fmt2(t.exit_price)}</td>
      <td>${t.quantity}</td>
      <td class="${ocCls}">${t.outcome}</td>
      <td class="${pnlCls}">${fmt2(t.net_pnl)}</td>
      <td>${t.r_multiple}</td>
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

// ── Theme ──────────────────────────────────────────────────────────────────────

function toggleTheme() {
  const root    = document.documentElement;
  const current = root.getAttribute('data-theme') || 'dark';
  const next    = current === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── Init ───────────────────────────────────────────────────────────────────────

// On every page load, ask the backend if there is already a running backtest.
// If yes, hide the form and resume polling — no localStorage involved.
fetch('/api/backtests')
  .then(r => r.json())
  .then(runs => {
    const active = Array.isArray(runs) && runs.find(r => r.status === 'running');
    if (!active) return;
    setBtStatus('running…', 'yellow');
    setRunBtn(true);
    document.getElementById('bt-trades').innerHTML =
      '<tr><td colspan="7" class="empty-cell">Running…</td></tr>';
    startPolling(active.run_id);
  })
  .catch(() => { /* server not ready yet — form stays visible */ });

connect();
