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
                      barsSelect: 'stock-bars-select', ocToggle: 'show-oc-toggle',
                      weightedRG: 'weighted-red-green',
                      datePicker: 'stock-date-picker', dateLiveBtn: 'stock-date-live-btn',
                      dateStatus: 'stock-date-status', panel: 'bn' };
const STOCK_IDS_NF = { signal: 'global-signal-badge-nf', banner: 'breakout-banner-nf',
                      head: 'stock-table-head-nf', body: 'stock-table-body-nf',
                      srBody: 'sr-table-body-nf', indexKey: 'NIFTY 50',
                      barsSelect: 'stock-bars-select-nf', ocToggle: 'show-oc-toggle-nf',
                      weightedRG: 'weighted-red-green-nf',
                      datePicker: 'stock-date-picker-nf', dateLiveBtn: 'stock-date-live-btn-nf',
                      dateStatus: 'stock-date-status-nf', panel: 'nf' };

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
  _rerenderCurrent(STOCK_IDS_BN);
  _rerenderCurrent(STOCK_IDS_NF);
}

// How many of each stock's most-recent bars to actually render as columns
// (the server sends up to 50 — see scheduler.py's _STOCK_TABLE_BARS — this
// just trims the client-side view). Shared across both panels, persisted
// like the theme/instrument-filter choices; the "Show all rows" button
// (toggleTableExpand) is a separate, orthogonal control — that one clamps
// visible STOCK ROWS (vertical), this clamps visible BAR COLUMNS (horizontal).
let _stockBarsCount = parseInt(localStorage.getItem('stockCandleBars'), 10) || 4;
let _lastStockCandlesBn = null;
let _lastStockCandlesNf = null;

function setStockBarsCount(val) {
  const n = Math.max(1, Math.min(50, parseInt(val, 10) || 4));
  _stockBarsCount = n;
  localStorage.setItem('stockCandleBars', String(n));
  [STOCK_IDS_BN.barsSelect, STOCK_IDS_NF.barsSelect].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = String(n);
  });
  _rerenderCurrent(STOCK_IDS_BN);
  _rerenderCurrent(STOCK_IDS_NF);
}

// ── Date-picker historical view ─────────────────────────────────────────
// Lets a Stock Candles panel show one specific past calendar day instead of
// the live rolling buffer (explicit user request, 2026-09-16). Fetches
// GET /api/stock-candles/{bn|nf}?for_date=YYYY-MM-DD (app/api/dashboard.py)
// — individual stocks come from the vendor's real historical archive, the
// index row from this app's own self-recorded bn_index_bars/nf_index_bars
// table. While a panel is date-filtered, dashboard.js's render() skips
// feeding it live STATE_UPDATE data (see isDateFilterActive) so the
// historical view isn't silently overwritten a second later; clicking
// "Live" clears the filter and repaints from whatever live data is already
// cached (may be up to ~1s stale until the next STATE_UPDATE corrects it).
const _dateFilterActive = { bn: null, nf: null };      // null | "YYYY-MM-DD"
let _lastHistoricalCandlesBn = null;
let _lastHistoricalCandlesNf = null;

function isDateFilterActive(panel) {
  return !!_dateFilterActive[panel];
}

// Whichever data is CURRENTLY on screen for a panel — live or historical —
// so the O/C toggle and bar-count selector re-render the right one instead
// of always snapping back to live.
function _rerenderCurrent(ids) {
  const isNf = ids === STOCK_IDS_NF;
  const active = isNf ? _dateFilterActive.nf : _dateFilterActive.bn;
  const data = active
    ? (isNf ? _lastHistoricalCandlesNf : _lastHistoricalCandlesBn)
    : (isNf ? _lastStockCandlesNf : _lastStockCandlesBn);
  if (data) renderStockCandles(data, ids, !active);
}

function loadStockCandlesForDate(dateStr, ids) {
  ids = ids || STOCK_IDS_BN;
  if (!dateStr) return;
  const panel = ids.panel;
  const statusEl = document.getElementById(ids.dateStatus);
  if (statusEl) statusEl.textContent = `Loading ${dateStr}…`;

  fetch(`/api/stock-candles/${panel}?for_date=${encodeURIComponent(dateStr)}`)
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) {
        if (statusEl) statusEl.textContent = `Error: ${body.detail || 'request failed'}`;
        return;
      }
      _dateFilterActive[panel] = dateStr;
      if (panel === 'nf') _lastHistoricalCandlesNf = body.stockCandles || {};
      else _lastHistoricalCandlesBn = body.stockCandles || {};
      const liveBtn = document.getElementById(ids.dateLiveBtn);
      if (liveBtn) liveBtn.style.display = '';
      if (statusEl) {
        const count = Object.keys(body.stockCandles || {}).length;
        statusEl.textContent = count ? `Showing ${dateStr}` : `No data for ${dateStr}`;
      }
      renderStockCandles(body.stockCandles || {}, ids, false);
    })
    .catch(e => { if (statusEl) statusEl.textContent = `Error: ${e.message}`; });
}

function clearStockDateFilter(ids) {
  ids = ids || STOCK_IDS_BN;
  const panel = ids.panel;
  _dateFilterActive[panel] = null;
  const liveBtn = document.getElementById(ids.dateLiveBtn);
  if (liveBtn) liveBtn.style.display = 'none';
  const statusEl = document.getElementById(ids.dateStatus);
  if (statusEl) statusEl.textContent = '';
  const picker = document.getElementById(ids.datePicker);
  if (picker) picker.value = '';
  _rerenderCurrent(ids);
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

// User-supplied weightage badge: on the LATEST bar, how much of the index
// weight (restricted to stocks with a confirmed, user-verified weight — see
// cfg.BN_INDEX_WEIGHTS_CONFIRMED/NF_INDEX_WEIGHTS_CONFIRMED) is currently
// red vs green. `total` is the sum of ONLY those confirmed stocks' weights
// (not the full instrument list, and not normalized to 100) — an explicit
// user decision so this reads as "how much of the weight I trust is red
// right now", not a % of the whole index.
function renderWeightedRedGreen(wrg, ids) {
  ids = ids || STOCK_IDS_BN;
  const box = document.getElementById(ids.weightedRG);
  if (!box) return;
  if (!wrg || !wrg.total) { box.hidden = true; return; }
  box.hidden = false;
  const neutral = Math.max(0, wrg.total - wrg.red - wrg.green);
  const neutralText = neutral > 0.005 ? ` <span class="muted-text">(${neutral.toFixed(2)} unchanged)</span>` : '';
  box.innerHTML = `
    <span class="muted-text">Weightage (confirmed stocks):</span>
    <span class="pnl-neg">🔴 ${wrg.red.toFixed(2)}/${wrg.total.toFixed(2)}</span>
    <span class="pnl-pos">🟢 ${wrg.green.toFixed(2)}/${wrg.total.toFixed(2)}</span>${neutralText}
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
function _candleCellHtml(bar, showOC, alertPts) {
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
  // Tick mark: this bar's own |close-open| move has crossed the stock's
  // configured move-alert threshold (same points figure alerts.js's
  // checkPriceAlerts uses for the consensus check) — only ever passed in for
  // the Latest column, so this reads as "the current candle just qualified".
  const tick = alertPts != null && Math.abs(diff) >= alertPts
    ? `<span class="alert-tick" title="Crossed its ${alertPts} pt move-alert threshold">✓</span>`
    : '';
  return `<span class="candle-cell ${cls}" title="${title}">${tick}${content}${ocLine}</span>`;
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

// "YYYY-MM-DD" -> "16 Sep", for the day-boundary column label in
// renderStockCandles' header row.
function _fmtColDate(iso) {
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const [, m, d] = iso.split('-');
  return `${parseInt(d, 10)} ${MON[parseInt(m, 10) - 1]}`;
}

function renderStockCandles(stockCandles, ids, cacheAsLive) {
  ids = ids || STOCK_IDS_BN;
  const head = document.getElementById(ids.head);
  const body = document.getElementById(ids.body);
  if (!head || !body || !stockCandles) return;

  // cacheAsLive === false (passed by the date-picker path) keeps historical
  // data out of the live cache, so clicking "Live" doesn't show a past day.
  if (cacheAsLive !== false) {
    if (ids === STOCK_IDS_NF) _lastStockCandlesNf = stockCandles;
    else _lastStockCandlesBn = stockCandles;
    // A live call (STATE_UPDATE) while this panel is showing a picked date:
    // keep the cache fresh but don't repaint over the historical view.
    if (isDateFilterActive(ids.panel)) return;
  }

  const names = Object.keys(stockCandles);
  // Merge (not replace) — this fn is called once per instrument, and a
  // stock shared between BN and NF should stay tracked either way.
  _lastStockOrder = Array.from(new Set(_lastStockOrder.concat(names)));
  // Shared with qtyAudit.js (window-level, no module system here) — the
  // vendor's current protocol embeds a real per-trade quantity on live
  // ticks (parsed server-side into Candle.last_qty); recordTickForAudit
  // uses each stock's latest bar's last_qty as the real "quantity" figure.
  window._lastQtyByStock = window._lastQtyByStock || {};
  names.forEach(n => {
    const bars = stockCandles[n] || [];
    if (bars.length) {
      _lastOpenByStock[n] = bars[bars.length - 1].open;
      window._lastQtyByStock[n] = bars[bars.length - 1].lastQty;
    }
  });
  const fullBars = Math.max(0, ...names.map(n => (stockCandles[n] || []).length));
  // Clamped to the "Last N bars" selector — the server sends up to 15 (see
  // scheduler.py's _STOCK_TABLE_BARS), this is purely a client-side view trim.
  // A date-picker historical render (cacheAsLive === false) shows the full
  // day unclamped — the bars-count selector only applies to the live view.
  const maxBars = cacheAsLive === false ? fullBars : Math.min(fullBars, _stockBarsCount);

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

  // Day-wise grouping (2026-09-16, explicit user decision): columns run
  // newest-first left-to-right, so a "Last N bars" window wide enough to
  // reach back past today's open (N > ~75, or early in the session before
  // today has accumulated that many bars yet) mixes in yesterday's/earlier
  // bars with nothing distinguishing them. Mark the FIRST column of each
  // new (older) calendar day — computed once here from the shared index-row
  // times (refBars), reused for both the header (date label) and every
  // stock row's cell in that same column (border only, via .day-boundary),
  // so day boundaries stay visible without changing what data is shown.
  const dayBoundaryAt = new Array(maxBars).fill(false);
  {
    let prevColDate = null;
    for (let i = 0; i < maxBars; i++) {
      const barTime = refBars[i] && refBars[i].startTime;
      const curDate = barTime ? barTime.substring(0, 10) : null;
      if (i > 0 && curDate && prevColDate && curDate !== prevColDate) dayBoundaryAt[i] = true;
      if (curDate) prevColDate = curDate;
    }
  }

  // "Stock (N)" should count actual stocks only — the index row (BANKNIFTY/
  // NIFTY 50, one of `names`, rendered as its own row below) isn't a stock.
  const stockCount = names.filter(n => n !== ids.indexKey).length;
  let headHtml = `<th>Stock (${stockCount})</th>`;
  for (let i = 0; i < maxBars; i++) {
    const label = _colLabel(i, maxBars);
    const barTime = refBars[i] && refBars[i].startTime;
    const t = barTime ? barTime.substring(11, 16) : '00';
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
    const dayCls = dayBoundaryAt[i] ? ' day-boundary' : '';
    const dateLine = dayBoundaryAt[i] ? `<br><span class="muted-text day-date">${escHtml(_fmtColDate(barTime.substring(0, 10)))}</span>` : '';
    headHtml += `<th${gapTitle} class="${dayCls.trim()}">${label} (o-c)${gapTitle ? ' ⚠' : ''}<br><span class="muted-text">${t}</span>${dateLine}<br>` +
      `<span class="tally-g">G:${tally[i].g}</span> <span class="tally-r">R:${tally[i].r}</span></th>`;
  }
  headHtml += '<th>BuyQtyPending</th><th>SellQtyPending</th>';
  head.innerHTML = headHtml;

  if (!names.length) {
    body.innerHTML = '<tr><td class="empty-cell">Waiting for data…</td></tr>';
    return;
  }

  // Move-alert threshold per stock, keyed off the SAME settings alerts.js
  // reads (BN_PRICE_ALERT_KEY/NF_PRICE_ALERT_KEY + _alertPtsByKey) — only
  // defined for the leader stocks the Settings page has a threshold for; a
  // stock with none configured just never gets a tick mark.
  const keyByStock = ids === STOCK_IDS_NF
    ? (typeof NF_PRICE_ALERT_KEY !== 'undefined' ? NF_PRICE_ALERT_KEY : {})
    : (typeof BN_PRICE_ALERT_KEY !== 'undefined' ? BN_PRICE_ALERT_KEY : {});
  const alertPtsByKey = typeof _alertPtsByKey !== 'undefined' ? _alertPtsByKey : {};

  body.innerHTML = names.map(name => {
    const bars = reversedByName[name];   // newest first, like c.html
    const alertPts = alertPtsByKey[keyByStock[name]];
    let row = `<tr data-stock="${name}"><td class="card-title">${name}</td>`;
    for (let i = 0; i < maxBars; i++) {
      // Tick mark only ever on the Latest (i===0) column — "the current
      // candle just crossed its threshold", not every historical bar shown.
      row += `<td data-col="${i}"${dayBoundaryAt[i] ? ' class="day-boundary"' : ''}>${_candleCellHtml(bars[i], _showOC, i === 0 ? alertPts : null)}</td>`;
    }
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
  const alertPtsByKey = typeof _alertPtsByKey !== 'undefined' ? _alertPtsByKey : {};
  const bnKeyByStock = typeof BN_PRICE_ALERT_KEY !== 'undefined' ? BN_PRICE_ALERT_KEY : {};
  const nfKeyByStock = typeof NF_PRICE_ALERT_KEY !== 'undefined' ? NF_PRICE_ALERT_KEY : {};
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
      // Which panel this row belongs to decides which threshold applies —
      // BN and NF can configure a different points value for the same stock.
      const isNf = !!row.closest(`#${STOCK_IDS_NF.body}`);
      if (isDateFilterActive(isNf ? 'nf' : 'bn')) return;   // showing a picked date — don't overwrite with live ticks
      const alertPts = alertPtsByKey[(isNf ? nfKeyByStock : bnKeyByStock)[name]];
      const crossed = alertPts != null && Math.abs(diff) >= alertPts;
      const tick = crossed
        ? `<span class="alert-tick" title="Crossed its ${alertPts} pt move-alert threshold">✓</span>` : '';
      cell.innerHTML = tick + _floorToTwo(Math.abs(diff));
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
