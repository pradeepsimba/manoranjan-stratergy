'use strict';

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

// ── Main render ──────────────────────────────────────────────────────────────

function render(d) {
  document.getElementById('clock').textContent = d.clock || '—';
  setStatus('ws',  d.wsStatus  || '—', d.wsStatus === 'WS Connected' ? 'green' : 'red');
  setStatus('api', d.apiStatus || '—', d.apiStatus === 'API OK'       ? 'green' : 'red');

  document.querySelectorAll('.interval-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.iv === d.interval);
  });

  document.getElementById('stat-funds').textContent = '₹' + fmt(d.funds);

  renderSignal(d);
  renderActiveTrade(d.activeTrade);

  const pending = document.getElementById('pending-signal');
  if (d.pendingSignal) {
    pending.style.display = 'block';
    document.getElementById('pending-text').textContent =
      `${d.pendingSignal.type} — ${d.pendingSignal.reason}`;
  } else {
    pending.style.display = 'none';
  }

  renderIndicators(d.bnIndicators);
  renderEntryChecks(d.entryDiag);
  renderStocks(d.stocks || [], d.candleCounts || []);
  renderTrades(d.trades || []);
  renderSR(d.srLevels || []);
}

// ── Signal banner ─────────────────────────────────────────────────────────────

function renderSignal(d) {
  const banner = document.getElementById('signal-banner');
  const badge  = document.getElementById('global-signal');
  const ind = d.bnIndicators;
  if (!ind) return;
  if (ind.bullish) {
    banner.className = 'signal-banner signal-bull';
    banner.textContent = `▲ BULL GATE OPEN  (Bull=${ind.bull.toFixed(1)} Bear=${ind.bear.toFixed(1)})`;
    badge.textContent = 'BULL'; badge.className = 'badge green';
  } else if (ind.bearish) {
    banner.className = 'signal-banner signal-bear';
    banner.textContent = `▼ BEAR GATE OPEN  (Bull=${ind.bull.toFixed(1)} Bear=${ind.bear.toFixed(1)})`;
    badge.textContent = 'BEAR'; badge.className = 'badge red';
  } else {
    banner.className = 'signal-banner signal-neutral';
    banner.textContent = `— NEUTRAL  (Bull=${ind.bull?.toFixed(1) || 0} Bear=${ind.bear?.toFixed(1) || 0})`;
    badge.textContent = 'NEUTRAL'; badge.className = 'badge gray';
  }
}

// ── Active trade ─────────────────────────────────────────────────────────────

function renderActiveTrade(at) {
  const panel = document.getElementById('active-trade-panel');
  const statTrade = document.getElementById('stat-trade');
  const statPnl   = document.getElementById('stat-pnl');

  if (!at) {
    panel.style.display = 'none';
    statTrade.textContent = '—'; statTrade.style.color = '#8b949e';
    statPnl.textContent   = '—'; statPnl.style.color   = '#8b949e';
    return;
  }
  panel.style.display = 'block';
  const color = at.pnl >= 0 ? '#3fb950' : '#f85149';
  statTrade.textContent = at.type; statTrade.style.color = at.type === 'BUY' ? '#3fb950' : '#f85149';
  statPnl.textContent   = at.pnl.toFixed(2); statPnl.style.color = color;

  document.getElementById('at-type').textContent  = at.type;
  document.getElementById('at-type').style.color  = at.type === 'BUY' ? '#3fb950' : '#f85149';
  document.getElementById('at-entry').textContent = at.entry.toFixed(2);
  document.getElementById('at-ltp').textContent   = at.ltp.toFixed(2);
  document.getElementById('at-sl').textContent    = at.currentSL.toFixed(2);
  const pnlEl = document.getElementById('at-pnl');
  pnlEl.textContent = at.pnl.toFixed(2); pnlEl.style.color = color;
  const pnlRsEl = document.getElementById('at-pnlrs');
  pnlRsEl.textContent = '₹' + at.pnlRs.toFixed(2); pnlRsEl.style.color = color;
  document.getElementById('at-lots').textContent = at.numLots + ' lot' + (at.numLots > 1 ? 's' : '');
  document.getElementById('at-conf').textContent = at.confidence;
}

// ── BN Indicators ────────────────────────────────────────────────────────────

function renderIndicators(ind) {
  if (!ind) return;
  const rsiEl = document.getElementById('ind-rsi');
  rsiEl.textContent = ind.rsi != null ? ind.rsi.toFixed(1) : '—';
  rsiEl.className = 'indicator-value ' + (ind.rsi > 58 ? 'bull-color' : ind.rsi < 42 ? 'bear-color' : 'neutral-color');

  const macdEl = document.getElementById('ind-macd');
  macdEl.textContent = ind.macdDir || '—';
  macdEl.className = 'indicator-value ' + (ind.macdDir === 'BUY' || ind.macdDir === 'CROSS↑' ? 'bull-color' :
                                            ind.macdDir === 'SELL' || ind.macdDir === 'CROSS↓' ? 'bear-color' : 'neutral-color');

  const emaEl = document.getElementById('ind-ema');
  const ema = ind.ema || ind.emaStack;
  if (ema) {
    emaEl.textContent = ema.bullish ? `EMA20=${ema.ema20} > EMA50=${ema.ema50} ▲` :
                        ema.bearish ? `EMA20=${ema.ema20} < EMA50=${ema.ema50} ▼` :
                        `EMA20=${ema.ema20} EMA50=${ema.ema50}`;
    emaEl.className = 'indicator-value ' + (ema.bullish ? 'bull-color' : ema.bearish ? 'bear-color' : 'neutral-color');
  }

  document.getElementById('ind-bull').textContent = (ind.bull || 0).toFixed(1);
  document.getElementById('ind-bear').textContent = (ind.bear || 0).toFixed(1);

  const gate = document.getElementById('gate-badge');
  if (ind.bullish) { gate.textContent = 'BULL GATE'; gate.className = 'badge green'; }
  else if (ind.bearish) { gate.textContent = 'BEAR GATE'; gate.className = 'badge red'; }
  else { gate.textContent = 'GATE CLOSED'; gate.className = 'badge gray'; }
}

// ── Entry Loop Monitor — exact match of c.html renderEntryLoopTable() ────────

function renderEntryChecks(diag) {
  const panel = document.getElementById('entryLoopPanel');
  if (!panel) return;
  if (!diag) {
    panel.innerHTML = '<div style="color:#8b949e;padding:8px;">Waiting for data…</div>';
    return;
  }

  const T = ok => ok
    ? '<span style="color:#39aa39;font-weight:bold;font-size:15px;">✔</span>'
    : '<span style="color:#c93535;font-weight:bold;font-size:15px;">✘</span>';

  const row = (label, required, current, ok) => `
    <tr style="border-bottom:1px solid #1e1e1e;">
      <td style="padding:5px 6px;color:#fff;font-size:12px;white-space:nowrap;">${label}</td>
      <td style="padding:5px 6px;color:#aaa;font-size:11px;">${required}</td>
      <td style="padding:5px 6px;color:#fff;font-size:12px;">${current}</td>
      <td style="padding:5px 6px;text-align:center;">${T(ok)}</td>
    </tr>`;

  // Pre-checks
  const marketOk   = !!diag.marketOpen;
  const timeOk     = !!diag.timeWindowOk;
  const noTradeOk  = !!diag.noActiveTrade;
  const cooldownOk = (diag.cooldownMs || 0) >= 60000;
  const cooldownVal = (diag.cooldownMs || 0) < 3600000
    ? `${Math.floor((diag.cooldownMs || 0) / 1000)}s ago`
    : 'Never exited';

  const sidewaysOk  = diag.sidewaysRange != null && diag.sidewaysRange >= 12;
  const sidewaysVal = diag.sidewaysRange != null ? `${Number(diag.sidewaysRange).toFixed(1)} pts` : '—';

  const mom    = diag.momentum || {};
  const momOk  = !!mom.ok;
  const momVal = mom.reason || '—';

  const sigOk    = diag.leaderSignal === 'BUY' || diag.leaderSignal === 'SELL';
  const sigColor = diag.leaderSignal === 'BUY' ? '#39aa39' : diag.leaderSignal === 'SELL' ? '#c93535' : '#888';
  const sigVal   = `<span style="color:${sigColor};font-weight:bold;">${diag.leaderSignal || '—'}</span> <span style="color:#666;font-size:10px;">(${diag.leaderReason || ''})</span>`;

  const dirCount = Math.max(diag.green || 0, diag.red || 0);
  const dirOk    = dirCount >= 3;
  const dirVal   = `<span style="color:#39aa39;">G:${diag.green || 0}</span> <span style="color:#c93535;">R:${diag.red || 0}</span> <span style="color:#888;">(need ≥3)</span>`;

  const sqOk  = (diag.strongQty || 0) >= 3;
  const sqVal = `${diag.strongQty || 0} / 6 above threshold <span style="color:#888;">(need ≥3)</span>`;

  const ccOk  = !!diag.candleCloseOk;
  const ccVal = ccOk ? 'Closed' : `Waiting → ${diag.candleCloseTime || '—'}`;

  const noRepeatOk = !diag.alreadyTradedCandle;

  // BN Indicators
  const ind = diag.bnInd || {};

  const rsiOk    = ind.rsi != null;
  const rsiColor = rsiOk ? (ind.rsi < 35 ? '#39aa39' : ind.rsi > 65 ? '#c93535' : '#aaa') : '#666';
  const rsiVal   = rsiOk
    ? `<span style="color:${rsiColor};font-weight:bold;">${Number(ind.rsi).toFixed(1)}</span>`
    : '—';

  const macdOk    = ind.macdDir && ind.macdDir !== '—';
  const macdColor = (ind.macdDir === 'BUY' || ind.macdDir === 'CROSS↑') ? '#39aa39'
                  : (ind.macdDir === 'SELL' || ind.macdDir === 'CROSS↓') ? '#c93535' : '#aaa';
  const macdValStr = macdOk
    ? `<span style="color:${macdColor};font-weight:bold;">${ind.macdDir}${ind.macdVal != null ? ` (${ind.macdVal})` : ''}</span>`
    : '—';

  const lp       = ind.leaderPat || {};
  const patBull  = lp.bullCount || 0, patBear = lp.bearCount || 0;
  const patColor = patBull >= 2 ? '#39aa39' : patBear >= 2 ? '#c93535' : '#888';
  const patNames = (lp.matches || []).map(m => `${m.stock}(${(m.pattern || '').split(' ')[0]})`).join(' ') || '—';
  const patVal   = (lp.matches || []).length
    ? `<span style="color:${patColor};font-weight:bold;">${patBull}▲ ${patBear}▼</span> <span style="color:#777;font-size:10px;">${patNames}</span>`
    : '<span style="color:#666;">—</span>';

  const ema      = ind.emaStack;
  const emaMet   = ema && (ema.bullish || ema.bearish);
  const emaColor = ema && ema.bullish ? '#39aa39' : ema && ema.bearish ? '#c93535' : '#888';
  const emaVal   = ema
    ? `<span style="color:${emaColor};font-weight:bold;">${ema.bullish ? '▲' : ema.bearish ? '▼' : '~'} ${ema.ema20}/${ema.ema50}</span>`
    : '<span style="color:#666;">—</span>';

  const isConflict = (ind.bull >= 2) && (ind.bear >= 2);
  const gateOk     = !!(ind.bullish || ind.bearish);
  const gateColor  = ind.bullish ? '#39aa39' : ind.bearish ? '#c93535' : isConflict ? '#f0b429' : '#888';
  const gateLabel  = ind.bullish ? 'BULLISH' : ind.bearish ? 'BEARISH' : isConflict ? '⚡ CONFLICT' : 'NEUTRAL';
  const gateVal    = `<span style="color:${gateColor};font-weight:bold;">${gateLabel}</span> <span style="color:#666;font-size:10px;">Bull:${ind.bull || 0} Bear:${ind.bear || 0}</span>`;

  // Summary bar
  const allOk = [marketOk, timeOk, noTradeOk, cooldownOk, sidewaysOk, momOk,
                 sigOk, dirOk, sqOk, ccOk, macdOk, emaMet, gateOk, noRepeatOk];
  const metCount   = allOk.filter(Boolean).length;
  const entryReady = allOk.every(Boolean);
  const isPending  = !entryReady && metCount >= allOk.length - 1 && !ccOk;
  const summaryBg  = entryReady ? 'rgba(0,180,0,0.15)' : isPending ? 'rgba(230,126,0,0.15)' : 'rgba(160,0,0,0.12)';
  const sumColor   = entryReady ? '#39aa39' : isPending ? '#e67e00' : '#c93535';
  const sumLabel   = entryReady ? '✔ ENTRY READY'
    : isPending ? '⏳ PRE-QUALIFIED — firing at candle close'
    : `✘ BLOCKED  (${metCount}/${allOk.length} passed)`;

  const bn = diag.bn || {};

  let html = `
  <div style="padding:7px 8px;background:${summaryBg};border-radius:5px;text-align:center;font-size:14px;font-weight:bold;color:${sumColor};margin-bottom:5px;">
    ${sumLabel}
  </div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
    <span style="font-size:12px;color:#aaa;">🕐 <b style="color:#fff;">${diag.time || ''}</b></span>
    ${bn.open != null ? `<span style="font-size:12px;color:#aaa;">BN O:<b style="color:#fff;">${bn.open}</b> C:<b style="color:#fff;">${bn.close}</b></span>` : ''}
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed;">
    <colgroup>
      <col style="width:26%"><col style="width:30%"><col style="width:36%"><col style="width:8%">
    </colgroup>
    <thead>
      <tr style="background:#181818;">
        <th style="padding:4px 6px;text-align:left;color:#888;font-weight:normal;font-size:11px;">Condition</th>
        <th style="padding:4px 6px;text-align:left;color:#888;font-weight:normal;font-size:11px;">Threshold</th>
        <th style="padding:4px 6px;text-align:left;color:#888;font-weight:normal;font-size:11px;">Live Value</th>
        <th style="padding:4px 6px;text-align:center;color:#888;font-weight:normal;font-size:11px;">✔</th>
      </tr>
    </thead>
    <tbody>
      <tr style="border-bottom:1px solid #1e1e1e;">
        <td style="padding:5px 6px;color:#fff;font-size:12px;white-space:nowrap;">Pre-checks</td>
        <td colspan="2" style="padding:5px 6px;font-size:12px;">
          <span style="${marketOk ? 'color:#39aa39' : 'color:#c93535'}">Market ${marketOk ? 'Open' : 'Closed'}</span>
          &nbsp;|&nbsp;
          <span style="${timeOk ? 'color:#39aa39' : 'color:#c93535'}">Window ${timeOk ? 'OK' : 'Out'}</span>
          &nbsp;|&nbsp;
          <span style="${noTradeOk ? 'color:#39aa39' : 'color:#c93535'}">${noTradeOk ? 'No Trade' : 'Active Trade'}</span>
          &nbsp;|&nbsp;
          <span style="${cooldownOk ? 'color:#39aa39' : 'color:#c93535'}">CD ${cooldownOk ? cooldownVal : 'Active'}</span>
        </td>
        <td style="padding:4px 6px;text-align:center;">${T(marketOk && timeOk && noTradeOk && cooldownOk)}</td>
      </tr>
      ${row('Sideways Filter', 'Range ≥ 12 pts (last 5)', sidewaysVal, sidewaysOk)}
      ${row('Momentum', 'Strong move required', momVal, momOk)}
      ${row('Leader Signal', 'BUY or SELL', sigVal, sigOk)}
      ${row('Dir Count', '≥ 3 same dir', dirVal, dirOk)}
      ${row('Strong Qty', '≥ 3 above threshold', sqVal, sqOk)}
      ${row('Candle Closed', 'Full candle complete', ccVal, ccOk)}
      ${row('No Candle Repeat', 'New candle only', diag.alreadyTradedCandle ? 'Already traded' : 'New candle', noRepeatOk)}
      <tr style="background:#111;">
        <td colspan="4" style="padding:2px 6px;color:#444;font-size:10px;letter-spacing:1px;">─── BN INDICATORS ───────────────────</td>
      </tr>
      <tr style="border-bottom:1px solid #1e1e1e;">
        <td style="padding:5px 6px;color:#888;font-size:12px;white-space:nowrap;">RSI (14)</td>
        <td style="padding:5px 6px;color:#666;font-size:11px;">Display only</td>
        <td style="padding:5px 6px;font-size:12px;">${rsiVal}</td>
        <td style="padding:5px 6px;text-align:center;color:#555;font-size:11px;">—</td>
      </tr>
      ${row('MACD (12,26,9)', 'Direction signal present', macdValStr, macdOk)}
      <tr style="border-bottom:1px solid #1e1e1e;">
        <td style="padding:5px 6px;color:#888;font-size:12px;white-space:nowrap;">Leader Patterns</td>
        <td style="padding:5px 6px;color:#666;font-size:11px;">Display only</td>
        <td style="padding:5px 6px;font-size:12px;">${patVal}</td>
        <td style="padding:5px 6px;text-align:center;color:#555;font-size:11px;">—</td>
      </tr>
      ${row('EMA Stack (20/50)', 'Price > EMA20 > EMA50', emaVal, emaMet)}
      ${row('BN Gate', 'Score ≥ 2, lead > 0.9', gateVal, gateOk)}
    </tbody>
  </table>`;

  // Leader stocks sub-table (matching c.html)
  const ldrStocks = diag.stocks || [];
  if (ldrStocks.length > 0) {
    html += `
    <div style="margin-top:5px;">
      <div style="font-size:11px;color:#888;margin-bottom:3px;letter-spacing:1px;">─── LEADER STOCKS ───────────────────────────────</div>
      <table style="width:100%;border-collapse:collapse;font-size:12px;">
        <thead>
          <tr style="background:#1a1a1a;color:#888;">
            <th style="padding:4px 5px;text-align:left;">Stock</th>
            <th style="text-align:center;">Open</th>
            <th style="text-align:center;">Close</th>
            <th style="text-align:center;">Dir</th>
            <th style="text-align:center;">Threshold</th>
            <th style="text-align:center;">Qty ✔</th>
          </tr>
        </thead>
        <tbody>`;
    ldrStocks.forEach(({ stock, candle, qty, threshold }) => {
      if (!candle) {
        html += `<tr><td style="padding:3px 4px;color:#aaa;">${stock}</td><td colspan="5" style="color:#555;font-size:10px;">No candle</td></tr>`;
        return;
      }
      const diff     = candle.close - candle.open;
      const dir      = diff > 0 ? '▲' : diff < 0 ? '▼' : '—';
      const dirColor = diff > 0 ? '#39aa39' : diff < 0 ? '#c93535' : '#888';
      const qtyOk   = qty >= threshold;
      html += `
      <tr style="border-bottom:1px solid #1a1a1a;">
        <td style="padding:4px 5px;color:#fff;">${stock}</td>
        <td style="text-align:center;color:#fff;">${candle.open}</td>
        <td style="text-align:center;color:#fff;">${candle.close}</td>
        <td style="text-align:center;color:${dirColor};font-weight:bold;font-size:13px;">${dir}</td>
        <td style="text-align:center;color:#aaa;">${threshold}</td>
        <td style="text-align:center;font-weight:bold;color:${qtyOk ? '#39aa39' : '#c93535'}">${qty} ${qtyOk ? '✔' : '✘'}</td>
      </tr>`;
    });
    html += `</tbody></table></div>`;
  }

  panel.innerHTML = html;
}

// ── Stock candles — 3-candle diff table matching screenshot ──────────────────

function renderStocks(stocks, candleCounts) {
  const thead = document.getElementById('stocks-thead');
  const tbody = document.getElementById('stocks-tbody');
  if (!thead || !tbody) return;

  const colLabels = ['Latest', 'Previous', 'PrevPrev'];

  // Build dynamic header (2 rows: label+time, then g/r/n counts)
  const cc = candleCounts.length ? candleCounts : colLabels.map(l => ({ label: l, time: '', green: 0, red: 0, neutral: 0 }));
  thead.innerHTML = `
    <tr>
      <th rowspan="2" style="vertical-align:middle;text-align:left;color:#4caf50;font-size:12px;padding:5px 8px;">Stock Name</th>
      ${cc.map(c => `<th style="text-align:center;color:#c9d1d9;padding:4px 6px;font-size:11px;">
        ${c.label} (o-c)<br><span style="color:#8b949e;font-size:10px;">(${c.time || '—'})</span>
      </th>`).join('')}
      <th rowspan="2" style="vertical-align:middle;text-align:center;color:#c9d1d9;font-size:11px;padding:5px 6px;">BuyQtyPending</th>
      <th rowspan="2" style="vertical-align:middle;text-align:center;color:#c9d1d9;font-size:11px;padding:5px 6px;">SellQtyPending</th>
    </tr>
    <tr>
      ${cc.map(c => `<th style="text-align:center;color:#8b949e;font-weight:normal;font-size:10px;padding:2px 6px;">
        <span style="color:#3fb950;">g:${c.green}</span> <span style="color:#f85149;">r:${c.red}</span> <span style="color:#8b949e;">n:${c.neutral}</span>
      </th>`).join('')}
    </tr>`;

  // Build rows
  tbody.innerHTML = stocks.map(s => {
    const c3 = s.c3 || [];
    const cells = c3.map(c => {
      const bg  = c.diff > 0 ? '#1a5c2a' : c.diff < 0 ? '#5c1a1a' : '#21262d';
      const clr = c.diff > 0 ? '#3fb950' : c.diff < 0 ? '#f85149' : '#8b949e';
      const txt = c.diff > 0 ? '+' + c.diff.toFixed(2) : c.diff.toFixed(2);
      return `<td style="text-align:center;background:${bg};color:${clr};font-weight:bold;padding:5px 6px;">${txt}</td>`;
    }).join('');
    // Pad missing candle cells
    const pad = Array(Math.max(0, 3 - c3.length)).fill('<td style="text-align:center;color:#555;">—</td>').join('');
    const buyColor  = s.buyQty  > 0 ? '#3fb950' : '#8b949e';
    const sellColor = s.sellQty > 0 ? '#f85149' : '#8b949e';
    return `<tr>
      <td style="color:#c9d1d9;white-space:nowrap;padding:5px 8px;">${s.name} <span style="color:#444;font-size:10px;">(${s.symbol})</span></td>
      ${cells}${pad}
      <td style="text-align:center;color:${buyColor};padding:5px 6px;">${fmtLargeQty(s.buyQty)}</td>
      <td style="text-align:center;color:${sellColor};padding:5px 6px;">${fmtLargeQty(s.sellQty)}</td>
    </tr>`;
  }).join('');
}

function fmtLargeQty(n) {
  if (!n) return '0';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

// ── Support & Resistance ──────────────────────────────────────────────────────

function renderSR(srLevels) {
  const tbody = document.getElementById('sr-tbody');
  if (!tbody) return;
  if (!srLevels || srLevels.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:#8b949e;text-align:center;">Loading S/R levels…</td></tr>';
    return;
  }
  tbody.innerHTML = srLevels.map(s => `
    <tr>
      <td>${s.name}</td>
      <td style="color:#3fb950">${(s.s5sup  || []).map(v => v.toFixed(2)).join(', ') || '—'}</td>
      <td style="color:#f85149">${(s.s5res  || []).map(v => v.toFixed(2)).join(', ') || '—'}</td>
      <td style="color:#3fb950">${(s.s15sup || []).map(v => v.toFixed(2)).join(', ') || '—'}</td>
      <td style="color:#f85149">${(s.s15res || []).map(v => v.toFixed(2)).join(', ') || '—'}</td>
    </tr>
  `).join('');
}

// ── Trades table ─────────────────────────────────────────────────────────────

function renderTrades(trades) {
  const tbody = document.getElementById('trades-tbody');
  tbody.innerHTML = trades.map((t, i) => {
    const isBuy  = t.type.startsWith('BUY');
    const isExit = t.type.includes('EXIT');
    const pnlCls = t.pnl > 0 ? 'pnl-pos' : t.pnl < 0 ? 'pnl-neg' : '';
    return `<tr>
      <td>${i + 1}</td>
      <td style="color:${isBuy ? '#3fb950' : '#f85149'};font-weight:bold">${t.type}</td>
      <td>${t.price.toFixed(2)}</td>
      <td>${(t.time || '').substring(11, 19)}</td>
      <td class="${pnlCls}">${isExit ? (t.pnl > 0 ? '+' : '') + t.pnl.toFixed(2) : '—'}</td>
      <td style="color:#8b949e">${t.confidence || ''}</td>
    </tr>`;
  }).join('');
  const scroll = tbody.closest('.tbl-scroll');
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

// ── Controls ─────────────────────────────────────────────────────────────────

function setIntervalFeed(iv) {
  document.querySelectorAll('.interval-tab').forEach(t => t.classList.toggle('active', t.dataset.iv === iv));
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: 'SET_INTERVAL', interval: iv }));
  fetch('/api/interval', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ interval: iv }) });
}

window.setInterval = setIntervalFeed;

function manualEntry(type) {
  const price = parseFloat(document.getElementById('manual-price').value);
  if (!price) { alert('Enter a price first'); return; }
  fetch('/api/entry', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ type, price }) })
    .then(r => r.json()).then(d => { if (d.error) alert(d.error); });
}

function manualExit() {
  fetch('/api/exit', { method: 'POST' })
    .then(r => r.json()).then(d => { if (d.error) alert(d.error); });
}

function clearTrades() {
  if (!confirm('Clear all trades?')) return;
  fetch('/api/trades', { method: 'DELETE' });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function setStatus(which, text, cls) {
  const el = document.getElementById(which + '-status');
  if (el) { el.textContent = text; el.className = 'badge ' + cls; }
}

function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

function fmtQty(n) {
  if (!n) return '—';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return n.toFixed(0);
}

// ── Start ────────────────────────────────────────────────────────────────────

connect();
