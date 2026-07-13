'use strict';

// ── Stock Candles panel — port of c.html's renderTable/updateGlobalSignal/
// detectBreakouts/displaySupportResistance. Entirely separate from the BN
// options strategy; purely informational. Server computes the breakout/
// global-signal/S-R math (app/engine/bn_breakout.py) and pushes it in every
// STATE_UPDATE — this file only renders it, plus applies live TICK_UPDATE
// price deltas onto the most-recent column for a live "flash" feel.

let _lastStockOrder = [];
let _lastOpenByStock = {};   // {name: open price of the newest/forming bar} — for live tick point-move

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

// Port of c.html's formatToTwoDecimals — TRUNCATES (floor), not rounds, and
// always 2 decimals. For negatives this floors away from zero (e.g. -5.001
// becomes "-5.01", not "-5.00") — an exact, deliberate quirk of the source.
function _floorToTwo(num) {
  if (typeof num !== 'number' || isNaN(num)) return 'N/A';
  return (Math.floor(num * 100) / 100).toFixed(2);
}

// Port of c.html's getClassAndContent — diff = close - open. No sign at all
// is shown (neither "+" nor "-") — direction is conveyed by cell color only;
// "N/A" when data is missing.
function _candleCellHtml(bar) {
  if (!bar || !bar.close || !bar.open) return '<span class="candle-cell neutral">N/A</span>';
  const diff = bar.close - bar.open;
  const cls = diff > 0 ? 'positive' : diff < 0 ? 'negative' : 'neutral';
  const content = _floorToTwo(Math.abs(diff));
  const time = bar.startTime ? bar.startTime.substring(11, 16) : '';
  const title = `${time} Open:${bar.open.toFixed(2)} Close:${bar.close.toFixed(2)}`;
  return `<span class="candle-cell ${cls}" title="${title}">${content}</span>`;
}

// Port of c.html's renderTable header labels: leftmost (newest) column is
// "Latest", then "Previous", "PrevPrev", then "Prev{rawIndex}" for anything
// further back — rawIndex counted from the OLDEST end of the window, exactly
// as c.html's `i` loop variable does (a source quirk, kept as-is).
function _colLabel(posFromNewest, n) {
  const i = n - 1 - posFromNewest;
  if (i === n - 1) return 'Latest';
  if (i === n - 2) return 'Previous';
  if (i === n - 3) return 'PrevPrev';
  return `Prev${i}`;
}

function renderStockCandles(stockCandles) {
  const head = document.getElementById('stock-table-head');
  const body = document.getElementById('stock-table-body');
  if (!head || !body || !stockCandles) return;

  const names = Object.keys(stockCandles);
  _lastStockOrder = names;
  _lastOpenByStock = {};
  names.forEach(n => {
    const bars = stockCandles[n] || [];
    if (bars.length) _lastOpenByStock[n] = bars[bars.length - 1].open;
  });
  const maxBars = Math.max(0, ...names.map(n => (stockCandles[n] || []).length));

  // Column header time subtext = that column's actual bar time, taken from
  // BANKNIFTY specifically — matches c.html's renderTable, which only ever
  // updates the header time span from the BankNifty index row (stock_symbol
  // "26009"), on the theory that every instrument shares the same 5m bars.
  const refBars = (stockCandles['BANKNIFTY'] || []).slice().reverse();

  let headHtml = '<th>Stock</th>';
  for (let i = 0; i < maxBars; i++) {
    const label = _colLabel(i, maxBars);
    const t = refBars[i] && refBars[i].startTime ? refBars[i].startTime.substring(11, 16) : '00';
    headHtml += `<th>${label} (o-c)<br><span class="muted-text">${t}</span></th>`;
  }
  headHtml += '<th>BuyQtyPending</th><th>SellQtyPending</th>';
  head.innerHTML = headHtml;

  if (!names.length) {
    body.innerHTML = '<tr><td class="empty-cell">Waiting for data…</td></tr>';
    return;
  }

  body.innerHTML = names.map(name => {
    const bars = (stockCandles[name] || []).slice().reverse();   // newest first, like c.html
    let row = `<tr data-stock="${name}"><td class="card-title">${name}</td>`;
    for (let i = 0; i < maxBars; i++) row += `<td data-col="${i}">${_candleCellHtml(bars[i])}</td>`;
    // c.html always shows N/A here — this feed carries no buy/sell pending-qty field.
    row += '<td class="neutral">N/A</td><td class="neutral">N/A</td>';
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
    const open = _lastOpenByStock[name];
    if (price === undefined || !open) continue;
    const row = document.querySelector(`#stock-table-body tr[data-stock="${CSS.escape(name)}"]`);
    if (!row) continue;
    const cell = row.querySelector('td[data-col="0"] .candle-cell');
    if (!cell) continue;
    const diff = price - open;
    cell.textContent = _floorToTwo(Math.abs(diff));
    cell.className = 'candle-cell ' + (diff > 0 ? 'positive' : diff < 0 ? 'negative' : 'neutral');
  }
}
