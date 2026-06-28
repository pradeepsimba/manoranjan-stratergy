'use strict';

// ── Live WebSocket ─────────────────────────────────────────────────────────────

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

// ── Render (matches scheduler._build_payload) ──────────────────────────────────

const PHASE_CLS = {
  pre_market: 'gray', wait_zone: 'yellow', active: 'green',
  cutoff: 'yellow', closed: 'gray',
};

function render(d) {
  document.getElementById('clock').textContent = d.clock || '—';

  setStatus('ws',  d.wsStatus  || '—', d.wsStatus  === 'WS Connected' ? 'green' : 'red');
  setStatus('api', d.apiStatus || '—', d.apiStatus === 'API OK'       ? 'green' : 'red');

  const phase = d.phase || '—';
  const pb = document.getElementById('phase-badge');
  pb.textContent = phase.replace('_', ' ').toUpperCase();
  pb.className = 'badge ' + (PHASE_CLS[phase] || 'gray');

  // Stat boxes
  document.getElementById('stat-nifty').textContent = d.niftyLtp ? fmt2(d.niftyLtp) : '—';

  const pnlEl = document.getElementById('stat-pnl');
  const pnl   = d.dailyPnl || 0;
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '₹' + fmt2(pnl);
  pnlEl.className   = 'value ' + (pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : '');

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
    tbody.innerHTML = '<tr><td colspan="9" style="color:#8b949e;text-align:center">No positions yet</td></tr>';
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
    el.innerHTML = '<span style="color:#8b949e">Built at 09:00…</span>';
    return;
  }
  el.innerHTML = list.map(s => `<span class="badge gray">${s}</span>`).join('');
}

function renderScans(scans) {
  const tbody = document.getElementById('scans-tbody');
  if (!scans.length) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:#8b949e;text-align:center">Waiting for scans…</td></tr>';
    return;
  }
  tbody.innerHTML = scans.slice(-25).reverse().map(r => {
    const passed = !!r.pass;
    const detail = passed
      ? (r.signal ? `@${fmt2(r.signal.ltp)} RSI ${fmt2(r.signal.rsi)} ADX ${fmt2(r.signal.adx)}` : 'signal')
      : (r.reason || '');
    return `<tr>
      <td>${r.symbol}</td>
      <td><span class="badge ${passed ? 'green' : 'gray'}">${passed ? 'SIGNAL' : 'skip'}</span></td>
      <td style="color:#8b949e">${detail}</td>
    </tr>`;
  }).join('');
}

// ── Backtest (POST /api/backtest → poll GET /api/backtest/{id}) ─────────────────

let btPoll = null;

function runBacktest() {
  const from_date = document.getElementById('bt-from').value;
  const to_date   = document.getElementById('bt-to').value;
  if (!from_date || !to_date) { alert('Pick a from and to date'); return; }

  setBtStatus('starting…', 'yellow');
  fetch('/api/backtest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ from_date, to_date }),
  })
    .then(r => r.json())
    .then(d => {
      if (!d.run_id) throw new Error(d.detail || 'no run_id');
      setBtStatus('running…', 'yellow');
      clearInterval(btPoll);
      btPoll = setInterval(() => pollBacktest(d.run_id), 1500);
    })
    .catch(e => setBtStatus('error: ' + e.message, 'red'));
}

function pollBacktest(runId) {
  fetch(`/api/backtest/${runId}`)
    .then(r => r.json())
    .then(run => {
      if (run.status === 'running') { setBtStatus('running…', 'yellow'); return; }
      clearInterval(btPoll);
      if (run.status === 'error') {
        setBtStatus('failed', 'red');
        document.getElementById('bt-summary').innerHTML =
          `<span class="pnl-neg">${run.error || 'backtest failed'}</span>`;
        return;
      }
      setBtStatus('done', 'green');
      renderBacktestSummary(run.summary || {});
      fetch(`/api/backtest/${runId}/trades`).then(r => r.json()).then(renderBacktestTrades);
    })
    .catch(e => { clearInterval(btPoll); setBtStatus('error: ' + e.message, 'red'); });
}

function renderBacktestSummary(s) {
  const pf = s.profit_factor != null ? s.profit_factor : '—';
  const cells = [
    ['Trades',       s.total_trades ?? 0],
    ['Win rate',     ((s.win_rate ?? 0) * 100).toFixed(1) + '%'],
    ['Net P&L',      '₹' + fmt2(s.net_pnl ?? 0)],
    ['Profit factor', pf],
    ['Max DD',       '₹' + fmt2(s.max_drawdown ?? 0)],
    ['Avg R',        s.avg_r_multiple ?? 0],
    ['Costs',        '₹' + fmt2(s.total_costs ?? 0)],
    ['Days',         s.days_traded ?? 0],
  ];
  document.getElementById('bt-summary').innerHTML =
    `<div class="trade-info" style="grid-template-columns:repeat(4,1fr)">` +
    cells.map(([l, v]) =>
      `<div class="stat-box"><div class="label">${l}</div><div class="value" style="font-size:13px">${v}</div></div>`
    ).join('') + `</div>`;
}

function renderBacktestTrades(trades) {
  const tbody = document.getElementById('bt-trades');
  if (!Array.isArray(trades) || !trades.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e;text-align:center">No trades</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const cls = Number(t.net_pnl) > 0 ? 'pnl-pos' : Number(t.net_pnl) < 0 ? 'pnl-neg' : '';
    return `<tr>
      <td>${t.symbol}</td>
      <td>${fmt2(t.entry_price)}</td>
      <td>${fmt2(t.exit_price)}</td>
      <td>${t.quantity}</td>
      <td>${t.outcome}</td>
      <td class="${cls}">${fmt2(t.net_pnl)}</td>
      <td>${t.r_multiple}</td>
    </tr>`;
  }).join('');
}

function setBtStatus(text, cls) {
  const el = document.getElementById('bt-status');
  el.textContent = text;
  el.className = 'badge ' + cls;
}

// ── Helpers ─────────────────────────────────────────────────────────────────────

function setStatus(which, text, cls) {
  const el = document.getElementById(which + '-status');
  if (el) { el.textContent = text; el.className = 'badge ' + cls; }
}

function fmt2(n) {
  if (n == null || n === '') return '—';
  return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── Start ────────────────────────────────────────────────────────────────────────

connect();
