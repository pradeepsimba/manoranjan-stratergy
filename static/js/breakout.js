'use strict';

// ── Stock Candles panel — port of c.html's renderTable/updateGlobalSignal/
// detectBreakouts/displaySupportResistance. Entirely separate from the BN
// options strategy; purely informational. Server computes the breakout/
// global-signal/S-R math (app/engine/bn_breakout.py) and pushes it in every
// STATE_UPDATE — this file only renders it, plus applies live TICK_UPDATE
// price deltas onto the most-recent column for a live "flash" feel.

let _lastStockOrder = [];

function renderGlobalSignal(gs) {
  const box = document.getElementById('global-signal-badge');
  if (!box) return;
  if (!gs) { box.innerHTML = 'GLOBAL SIGNAL: —'; return; }
  const pts = gs.points !== null && gs.points !== undefined
    ? ` (≈ ${gs.points > 0 ? '+' : ''}${gs.points} pts)` : '';
  box.innerHTML = `
    GLOBAL SIGNAL: <b style="color:${gs.color}">${gs.signal}</b>
    <small>Count: <span style="color:${gs.countColor}">${gs.countSignal}</span> |
    Weighted: ${gs.weightedPct > 0 ? '+' : ''}${gs.weightedPct}%${pts}</small>
  `;
}

function renderBreakoutBanner(b) {
  const box = document.getElementById('breakout-banner');
  if (!box) return;
  if (!b || !b.type) { box.innerHTML = '<b>No Breakout Detected</b>'; return; }
  if (!b.valid) { box.innerHTML = '<b>No Valid Breakout</b> (insufficient contributions)'; return; }
  const dirText = b.direction === 'bullish' ? 'Bullish' : 'Bearish';
  const level = b.level !== null && b.level !== undefined ? Number(b.level).toFixed(2) : 'N/A';
  const contribText = (b.contributors || [])
    .filter(c => c.significant)
    .map(c => `${c.token} (${c.points > 0 ? '+' : ''}${c.points} pts, ${c.change}%)`)
    .join(', ');
  const typeLabel = b.type.charAt(0).toUpperCase() + b.type.slice(1);
  box.innerHTML = `
    <b>${typeLabel} Breakout:</b> ${dirText} (${level})
    <br><small>Contributors: ${contribText || '—'}</small>
  `;
}

function _candleCellHtml(bar) {
  if (!bar || !bar.open || !bar.close) return '<span class="candle-cell neutral">—</span>';
  const cls = bar.close > bar.open ? 'positive' : (bar.close < bar.open ? 'negative' : 'neutral');
  return `<span class="candle-cell ${cls}">${bar.close.toFixed(1)}</span>`;
}

function renderStockCandles(stockCandles) {
  const head = document.getElementById('stock-table-head');
  const body = document.getElementById('stock-table-body');
  if (!head || !body || !stockCandles) return;

  const names = Object.keys(stockCandles);
  _lastStockOrder = names;
  const maxBars = Math.max(0, ...names.map(n => (stockCandles[n] || []).length));

  let headHtml = '<th>Stock</th>';
  for (let i = 0; i < maxBars; i++) headHtml += `<th>C${i}</th>`;
  head.innerHTML = headHtml;

  if (!names.length) {
    body.innerHTML = '<tr><td class="empty-cell">Waiting for data…</td></tr>';
    return;
  }

  body.innerHTML = names.map(name => {
    const bars = (stockCandles[name] || []).slice().reverse();   // newest first, like c.html
    let row = `<tr data-stock="${name}"><td class="card-title">${name}</td>`;
    for (let i = 0; i < maxBars; i++) row += `<td data-col="${i}">${_candleCellHtml(bars[i])}</td>`;
    return row + '</tr>';
  }).join('');
}

function renderSrLevels(srLevels) {
  const body = document.getElementById('sr-table-body');
  if (!body) return;
  const names = Object.keys(srLevels || {});
  if (!names.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty-cell">Waiting for data…</td></tr>';
    return;
  }
  const fmt = arr => (arr || []).map(v => Number(v).toFixed(2)).join(', ') || '-';
  body.innerHTML = names.map(name => {
    const d5  = (srLevels[name] || {}).m5  || {};
    const d15 = (srLevels[name] || {}).m15 || {};
    return `<tr>
      <td class="card-title">${name}</td>
      <td>${fmt(d5.supports)}</td><td>${fmt(d5.resistances)}</td>
      <td>${fmt(d15.supports)}</td><td>${fmt(d15.resistances)}</td>
    </tr>`;
  }).join('');
}

// Live cell flash off TICK_UPDATE (currently-forming bar's LTP only —
// column 0 in the table, since that's always the most recent bar).
function applyStockTickPrices(prices) {
  if (!prices) return;
  for (const name of _lastStockOrder) {
    const price = prices[name];
    if (price === undefined) continue;
    const row = document.querySelector(`#stock-table-body tr[data-stock="${CSS.escape(name)}"]`);
    if (!row) continue;
    const cell = row.querySelector('td[data-col="0"] .candle-cell');
    if (cell) cell.textContent = Number(price).toFixed(1);
  }
}
