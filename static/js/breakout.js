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
                      srBody: 'sr-table-body', indexKey: 'BANKNIFTY',
                      barsSelect: 'stock-bars-select', ocToggle: 'show-oc-toggle' };
const STOCK_IDS_NF = { signal: 'global-signal-badge-nf', banner: 'breakout-banner-nf',
                      head: 'stock-table-head-nf', body: 'stock-table-body-nf',
                      srBody: 'sr-table-body-nf', indexKey: 'NIFTY50',
                      barsSelect: 'stock-bars-select-nf', ocToggle: 'show-oc-toggle-nf' };

// Whether each candle cell also prints its raw open/close (see
// _candleCellHtml) — off by default, shared across both panels like
// _stockBarsCount, persisted the same way.
let _showOC = localStorage.getItem('showCandleOC') === '1';

function setShowOC(checked) {
  _showOC = !!checked;
  localStorage.setItem('showCandleOC', _showOC ? '1' : '0');
  [STOCK_IDS_BN.ocToggle, STOCK_IDS_NF.ocToggle].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.checked = _showOC;
  });
  if (_lastStockCandlesBn) renderStockCandles(_lastStockCandlesBn, STOCK_IDS_BN);
  if (_lastStockCandlesNf) renderStockCandles(_lastStockCandlesNf, STOCK_IDS_NF);
}

// How many of each stock's most-recent bars to actually render as columns
// (the server sends up to 15 — see scheduler.py's _STOCK_TABLE_BARS — this
// just trims the client-side view). Shared across both panels, persisted
// like the theme/instrument-filter choices; the "Show all rows" button
// (toggleTableExpand) is a separate, orthogonal control — that one clamps
// visible STOCK ROWS (vertical), this clamps visible BAR COLUMNS (horizontal).
let _stockBarsCount = parseInt(localStorage.getItem('stockCandleBars'), 10) || 4;
let _lastStockCandlesBn = null;
let _lastStockCandlesNf = null;

function setStockBarsCount(val) {
  const n = Math.max(1, Math.min(15, parseInt(val, 10) || 4));
  _stockBarsCount = n;
  localStorage.setItem('stockCandleBars', String(n));
  [STOCK_IDS_BN.barsSelect, STOCK_IDS_NF.barsSelect].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = String(n);
  });
  if (_lastStockCandlesBn) renderStockCandles(_lastStockCandlesBn, STOCK_IDS_BN);
  if (_lastStockCandlesNf) renderStockCandles(_lastStockCandlesNf, STOCK_IDS_NF);
}

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
// "N/A" when data is missing. `showOC` (the "Show O/C" toggle) additionally
// prints the raw open/close under the diff, for verifying a run of bars
// against the timestamps without hovering each cell one at a time.
function _candleCellHtml(bar, showOC) {
  if (!bar || !bar.close || !bar.open) return '<span class="candle-cell neutral">N/A</span>';
  const diff = bar.close - bar.open;
  const cls = diff > 0 ? 'positive' : diff < 0 ? 'negative' : 'neutral';
  const content = _floorToTwo(Math.abs(diff));
  const time = bar.startTime ? bar.startTime.substring(11, 16) : '';
  // The number shown/here is a raw POINT difference, not a %, and the
  // per-stock alert thresholds (Settings → BN/NF Alerts) are set in % —
  // spell out the % here so it's not mistaken for one at a glance (see
  // static/js/alerts.js's checkPriceAlerts, which uses this same % move).
  const movePct = Math.abs(diff) / bar.open * 100;
  const title = `${time} Open:${bar.open.toFixed(2)} Close:${bar.close.toFixed(2)} (${movePct.toFixed(3)}% move)`;
  const ocLine = showOC
    ? `<br><span class="candle-oc">O:${bar.open.toFixed(2)} C:${bar.close.toFixed(2)} (${movePct.toFixed(2)}%)</span>`
    : '';
  return `<span class="candle-cell ${cls}" title="${title}">${content}${ocLine}</span>`;
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

  if (ids === STOCK_IDS_NF) _lastStockCandlesNf = stockCandles;
  else _lastStockCandlesBn = stockCandles;

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
  const fullBars = Math.max(0, ...names.map(n => (stockCandles[n] || []).length));
  // Clamped to the "Last N bars" selector — the server sends up to 15 (see
  // scheduler.py's _STOCK_TABLE_BARS), this is purely a client-side view trim.
  const maxBars = Math.min(fullBars, _stockBarsCount);

  // Newest-first per stock, computed once and reused for both the header's
  // per-column green/red tally and the body rows below.
  const reversedByName = {};
  names.forEach(n => { reversedByName[n] = (stockCandles[n] || []).slice().reverse(); });

  // Per-column green/red count across every stock (index row included) —
  // "how many stocks closed up/down on THIS bar", shown under each time
  // header. Client-side only — doesn't need a new server payload field,
  // stockCandles already carries every bar this needs.
  const tally = [];
  for (let i = 0; i < maxBars; i++) {
    let g = 0, r = 0;
    names.forEach(name => {
      const bar = reversedByName[name][i];
      if (!bar || bar.open == null || bar.close == null) return;
      if (bar.close > bar.open) g++;
      else if (bar.close < bar.open) r++;
    });
    tally.push({ g, r });
  }

  // Column header time subtext = that column's actual bar time, taken from
  // this panel's own index row (BANKNIFTY or NIFTY50) — matches c.html's
  // renderTable, which only ever updates the header time span from the
  // index row, on the theory that every instrument shares the same 5m bars.
  const refBars = reversedByName[ids.indexKey] || [];

  let headHtml = `<th>Stock (${names.length})</th>`;
  for (let i = 0; i < maxBars; i++) {
    const label = _colLabel(i, maxBars);
    const t = refBars[i] && refBars[i].startTime ? refBars[i].startTime.substring(11, 16) : '00';
    // The synthetic index (see market_data.py's _update_synthetic_*_index)
    // advances reactively off live ticks with no backfill — a WS gap of any
    // length just leaves consecutive bars far apart in time with nothing to
    // signal it. Flag it here so a big jump reads as "known data gap", not
    // as a rendering bug.
    const prevBar = refBars[i + 1];
    let gapTitle = '';
    if (prevBar && prevBar.startTime && refBars[i] && refBars[i].startTime) {
      const gapMin = (new Date(refBars[i].startTime) - new Date(prevBar.startTime)) / 60000;
      if (gapMin > 10) gapTitle = ` title="Data gap: ${Math.round(gapMin)} min since the previous bar (likely a feed interruption)"`;
    }
    headHtml += `<th${gapTitle}>${label} (o-c)${gapTitle ? ' ⚠' : ''}<br><span class="muted-text">${t}</span><br>` +
      `<span class="tally-g">G:${tally[i].g}</span> <span class="tally-r">R:${tally[i].r}</span></th>`;
  }
  headHtml += '<th>BuyQtyPending</th><th>SellQtyPending</th>';
  head.innerHTML = headHtml;

  if (!names.length) {
    body.innerHTML = '<tr><td class="empty-cell">Waiting for data…</td></tr>';
    return;
  }

  body.innerHTML = names.map(name => {
    const bars = reversedByName[name];   // newest first, like c.html
    let row = `<tr data-stock="${name}"><td class="card-title">${name}</td>`;
    for (let i = 0; i < maxBars; i++) row += `<td data-col="${i}">${_candleCellHtml(bars[i], _showOC)}</td>`;
    // Latest tick's cumulative pending buy/sell qty (parsed server-side from
    // the feed's `snap` text — see market_data.py) — a live per-stock figure,
    // not per-column, so it always reads off bars[0] regardless of maxBars.
    const latest = bars[0];
    const buyQty = latest && latest.buyQty != null ? Number(latest.buyQty).toLocaleString('en-IN') : 'N/A';
    const sellQty = latest && latest.sellQty != null ? Number(latest.sellQty).toLocaleString('en-IN') : 'N/A';
    row += `<td class="neutral">${buyQty}</td><td class="neutral">${sellQty}</td>`;
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

// Sync both "Last N bars" selects, and both "Show O/C" checkboxes, to their
// persisted choices on page load.
[STOCK_IDS_BN.barsSelect, STOCK_IDS_NF.barsSelect].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.value = String(_stockBarsCount);
});
[STOCK_IDS_BN.ocToggle, STOCK_IDS_NF.ocToggle].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.checked = _showOC;
});
