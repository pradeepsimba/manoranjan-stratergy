'use strict';

// ── Stock Candles panel — port of c.html's renderTable/updateGlobalSignal/
// detectBreakouts/displaySupportResistance. Entirely separate from the BN
// options strategy; purely informational. Server computes the breakout/
// global-signal/S-R math (app/engine/bn_breakout.py) and pushes it in every
// STATE_UPDATE — this file only renders it, plus applies live TICK_UPDATE
// price deltas onto the most-recent column for a live "flash" feel.

// _lastStockOrder/_lastOpenByStock accumulate across BOTH the BankNifty and
// Nifty 50 panels (renderStockCandles is called once per instrument) so
// applyStockTickPrices' live-flash works for whichever table(s) a ticking
// stock actually appears in — the 11 stocks BN and NF share render in both.
let _lastStockOrder = [];
let _lastOpenByStock = {};   // {name: open price of the newest/forming bar} — for live tick point-move

const STOCK_IDS_BN = { signal: 'global-signal-badge', banner: 'breakout-banner',
                      head: 'stock-table-head', body: 'stock-table-body',
                      srBody: 'sr-table-body', indexKey: 'BANKNIFTY' };
const STOCK_IDS_NF = { signal: 'global-signal-badge-nf', banner: 'breakout-banner-nf',
                      head: 'stock-table-head-nf', body: 'stock-table-body-nf',
                      srBody: 'sr-table-body-nf', indexKey: 'NIFTY50' };

function renderGlobalSignal(gs, ids) {
  ids = ids || STOCK_IDS_BN;
  const box = document.getElementById(ids.signal);
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

function renderBreakoutBanner(b, ids) {
  ids = ids || STOCK_IDS_BN;
  const box = document.getElementById(ids.banner);
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

function renderStockCandles(stockCandles, ids) {
  ids = ids || STOCK_IDS_BN;
  const head = document.getElementById(ids.head);
  const body = document.getElementById(ids.body);
  if (!head || !body || !stockCandles) return;

  const names = Object.keys(stockCandles);
  // Merge (not replace) — this fn is called once per instrument, and a
  // stock shared between BN and NF should stay tracked either way.
  _lastStockOrder = Array.from(new Set(_lastStockOrder.concat(names)));
  // Shared with qtyAudit.js (window-level, no module system here) — the
  // vendor's current protocol embeds a real per-trade quantity on live
  // ticks (parsed server-side into Candle.last_qty); the Big Trades panel
  // uses each stock's latest bar's last_qty as the real "quantity" figure.
  window._lastVolumeByStock = window._lastVolumeByStock || {};
  window._lastQtyByStock = window._lastQtyByStock || {};
  names.forEach(n => {
    const bars = stockCandles[n] || [];
    if (bars.length) {
      _lastOpenByStock[n] = bars[bars.length - 1].open;
      window._lastVolumeByStock[n] = bars[bars.length - 1].volume;
      window._lastQtyByStock[n] = bars[bars.length - 1].lastQty;
    }
  });
  const maxBars = Math.max(0, ...names.map(n => (stockCandles[n] || []).length));

  // Column header time subtext = that column's actual bar time, taken from
  // this panel's own index row (BANKNIFTY or NIFTY50) — matches c.html's
  // renderTable, which only ever updates the header time span from the
  // index row, on the theory that every instrument shares the same 5m bars.
  const refBars = (stockCandles[ids.indexKey] || []).slice().reverse();

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

function renderSrLevels(srLevels, ids) {
  ids = ids || STOCK_IDS_BN;
  const body = document.getElementById(ids.srBody);
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
    // Not scoped to one table's id — a stock shared between the BN and NF
    // panels has a row in both, and both should flash.
    const rows = document.querySelectorAll(`tr[data-stock="${CSS.escape(name)}"]`);
    if (!rows.length) continue;
    const diff = price - open;
    rows.forEach(row => {
      const cell = row.querySelector('td[data-col="0"] .candle-cell');
      if (!cell) return;
      cell.textContent = _floorToTwo(Math.abs(diff));
      cell.className = 'candle-cell ' + (diff > 0 ? 'positive' : diff < 0 ? 'negative' : 'neutral');
    });
  }
}
