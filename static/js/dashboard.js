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
      if (d.type === 'TICK_UPDATE') { handleTickUpdate(d.prices); return; }
      if (d.type === 'ALERT') { if (typeof handleServerAlert === 'function') handleServerAlert(d); return; }
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

// ── Live tick stream (100ms TICK_UPDATE) — feeds the Stock Candles panel's
// live cell flash, the local qty-audit IndexedDB log, and CSV file export.
// c.html did all three of these off its OWN second WebSocket connection to
// the market-data server; here they're all driven by the single existing
// /ws/dashboard connection instead (see the plan's "Scanner data" decision). ─

function handleTickUpdate(prices) {
  if (!prices) return;
  if (typeof applyStockTickPrices === 'function') applyStockTickPrices(prices);
  if (typeof recordTickForAudit === 'function') recordTickForAudit(prices);
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
  const synBadge = document.getElementById('bn-synthetic-badge');
  if (synBadge) synBadge.style.display = d.bnIndexSynthetic ? '' : 'none';

  const nfLtpEl = document.getElementById('stat-nfltp');
  if (nfLtpEl) nfLtpEl.textContent = d.nfLtp ? fmt2(d.nfLtp) : '—';
  const nfSynBadge = document.getElementById('nf-synthetic-badge');
  if (nfSynBadge) nfSynBadge.style.display = d.nfIndexSynthetic ? '' : 'none';

  renderTrade(d.activeTrade, TRADE_IDS_BN, d.entryLoop);
  renderTrade(d.activeTradeNf, TRADE_IDS_NF, d.entryLoopNf);
  renderAtmWatch(d.bnAtmWatch, TRADE_IDS_BN);
  renderAtmWatch(d.nfAtmWatch, TRADE_IDS_NF);
  renderClosedTrades(d.closedTrades || [], d.closedTradesNf || []);
  renderEntryLoop(d.entryLoop, d.liveLeaderRows, ENTRY_IDS_BN);
  renderEntryLoop(d.entryLoopNf, d.liveLeaderRowsNf, ENTRY_IDS_NF);
  if (typeof checkAllPriceAlerts === 'function') checkAllPriceAlerts(d.liveLeaderRows, d.liveLeaderRowsNf);

  // Real server-side funds/active-trade state — read by kiteForm.js so the
  // manual order form's Submit button and Avail. Funds field reflect the
  // SAME account the automated engine trades against (the manual form is no
  // longer a separate decorative simulation, see kiteForm.js).
  if (d.funds != null) window._lastFunds = d.funds;
  window._hasActiveTradeBn = !!d.activeTrade;
  window._hasActiveTradeNf = !!d.activeTradeNf;
  if (typeof renderGlobalSignal === 'function') {
    renderGlobalSignal(d.globalSignal);
    renderGlobalSignal(d.globalSignalNf, STOCK_IDS_NF);
  }
  if (typeof renderWeightedRedGreen === 'function') {
    renderWeightedRedGreen(d.weightedRedGreen);
    renderWeightedRedGreen(d.weightedRedGreenNf, STOCK_IDS_NF);
  }
  if (typeof renderBreakoutBanner === 'function') {
    renderBreakoutBanner(d.breakout);
    renderBreakoutBanner(d.breakoutNf, STOCK_IDS_NF);
  }
  if (typeof renderStockCandles === 'function') {
    renderStockCandles(d.stockCandles);
    renderStockCandles(d.stockCandlesNf, STOCK_IDS_NF);
  }
  if (typeof renderSrLevels === 'function') {
    renderSrLevels(d.srLevels);
    renderSrLevels(d.srLevelsNf, STOCK_IDS_NF);
  }

  checkTradeTransitionForScreenshot(d.activeTrade, 'bn');
  checkTradeTransitionForScreenshot(d.activeTradeNf, 'nf');
  updateLocalTradeLog(d.activeTrade, d.closedTrades || []);
  updateLocalTradeLog(d.activeTradeNf, d.closedTradesNf || []);
}

// ── Active trade card ─────────────────────────────────────────────────────────
// renderTrade/renderEntryLoop take an `ids` set so the identical BankNifty
// rendering logic can also target the Nifty 50 panel's own DOM nodes,
// instead of duplicating each function body.

const TRADE_IDS_BN = { badge: 'trade-badge', empty: 'trade-empty', card: 'trade-card', atmWatch: 'atm-watch' };
const TRADE_IDS_NF = { badge: 'trade-badge-nf', empty: 'trade-empty-nf', card: 'trade-card-nf', atmWatch: 'atm-watch-nf' };

// Live ATM CE/PE watchlist (2026-09-18, explicit user decision) — the REAL
// market LTP for whatever the current at-the-money strike is, independent
// of whether a trade is open (see CLAUDE.md's "Live ATM CE/PE watchlist"
// note and market_data.py's set_bn_atm_watch/set_nf_atm_watch). Always
// visible, unlike the Black-Scholes quote in renderTrade()'s empty state
// below, which is a theoretical fallback shown only when no trade is open.
function renderAtmWatch(watch, ids) {
  ids = ids || TRADE_IDS_BN;
  const el = document.getElementById(ids.atmWatch);
  if (!el) return;
  if (!watch || watch.strike == null) { el.innerHTML = ''; return; }
  const ceVal = watch.ceLtp != null ? `₹${fmt2(watch.ceLtp)}` : '— (waiting for live tick)';
  const peVal = watch.peLtp != null ? `₹${fmt2(watch.peLtp)}` : '— (waiting for live tick)';
  el.innerHTML = `
    <div class="atm-watch-row"><span class="lbl">ATM ${watch.strike} CE — ${escHtml(watch.ceSymbol || '')}</span><span class="val">${ceVal}</span></div>
    <div class="atm-watch-row"><span class="lbl">ATM ${watch.strike} PE — ${escHtml(watch.peSymbol || '')}</span><span class="val">${peVal}</span></div>
  `;
}

function renderTrade(t, ids, diag) {
  ids = ids || TRADE_IDS_BN;
  const badge = document.getElementById(ids.badge);
  const empty = document.getElementById(ids.empty);
  const card  = document.getElementById(ids.card);
  if (!badge || !empty || !card) return;

  if (!t) {
    badge.textContent = 'none'; badge.className = 'badge gray';
    empty.style.display = ''; card.style.display = 'none';
    // Live ITM entry quote (2026-09-18, renamed from "ATM" 2026-09-23 — see
    // models.py's itm_strike NOTE) — shown even with no trade open, so
    // there's always a "what would this cost right now" reference for the
    // deep-ITM strike the strategy would actually trade. `diag` is the same
    // entryLoop/entryLoopNf diagnostic the (currently unused) Entry Loop
    // Monitor reads — itmCePremium/itmPePremium are computed every closed
    // bar regardless of gates. Still a theoretical Black-Scholes estimate,
    // not real option-chain data (see CLAUDE.md). Distinct from the always-
    // visible real ATM CE/PE watchlist above (renderAtmWatch) — that one
    // tracks the true at-the-money strike with a real market LTP; this one
    // is a synthetic preview of the strategy's own (ITM, offset) entry.
    if (diag && diag.itmStrike != null) {
      empty.innerHTML = `No active trade.<br><span class="muted-text">`
        + `Entry ITM ${diag.itmStrike} — CE ₹${fmt2(diag.itmCePremium)} / PE ₹${fmt2(diag.itmPePremium)}`
        + (diag.itmIv != null ? ` (IV ${(diag.itmIv * 100).toFixed(1)}%)` : '')
        + `</span>`;
    } else {
      empty.textContent = 'No active trade.';
    }
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
    ['Option Symbol', t.optionSymbol || '—'],
    ['Expiry', fmtDT(t.expiry)],
    ['Entry Index', fmt2(t.entryIndexPrice)],
    ['Current Index', fmt2(t.currentIndexPrice)],
    ['Target', fmt2(t.target)],
    ['Stop Loss', fmt2(t.currentSl)],
    ['SL Stage', t.slStage],
    ['Confidence', t.confidence != null ? t.confidence + '%' : '—'],
    ['Entry Premium', '₹' + fmt2(t.entryPremium)],
    // 'Current Premium', not 'Price Source' — the real-option-LTP feature
    // that used to let this switch between a theoretical Black-Scholes
    // mark and a real market LTP mid-trade was removed 2026-09-22 (it let
    // a real tick snap settlement past the scalp strategy's tight ~₹2-3
    // bracket — see CLAUDE.md's "Real-option-LTP paper trading" note for
    // the incident writeup). Every premium here is now always the
    // synthetic Black-Scholes mark, so a "Price Source" row showing a
    // permanently-constant value would just be dead weight.
    ['Current Premium', '₹' + fmt2(t.currentPremium)],
    ['IV Used', t.currentIv != null ? (t.currentIv * 100).toFixed(1) + '%' : '—'],
    ['Live P&L', (livePnl >= 0 ? '+' : '') + '₹' + fmt2(livePnl)],
  ];
  card.innerHTML = cells.map(([lbl, val], i) => {
    const cls = lbl === 'SL Stage' ? stageCls
      : lbl === 'Live P&L' ? pnlCls
      : '';
    return `<div class="trade-cell"><span class="lbl">${escHtml(lbl)}</span><span class="val ${cls}">${val}</span></div>`;
  }).join('');
}

// ── Closed trades ──────────────────────────────────────────────────────────────
// Cached so setInstrumentFilter can re-filter immediately on toggle, without
// waiting for the next STATE_UPDATE tick.
let _lastClosedBn = [];
let _lastClosedNf = [];

function renderClosedTrades(bnTrades, nfTrades) {
  _lastClosedBn = bnTrades;
  _lastClosedNf = nfTrades || [];

  // "Today's Trades" panel was removed from the page — no-op once its DOM is gone.
  const countEl = document.getElementById('closed-count');
  const tbody = document.getElementById('closed-tbody');
  if (!countEl || !tbody) return;

  const instrFilter = window._instrFilter || 'both';
  const merged = _lastClosedBn.map(t => ({ ...t, _instr: 'BN' }))
    .concat(_lastClosedNf.map(t => ({ ...t, _instr: 'NF' })))
    .filter(t => instrFilter === 'both' || t._instr.toLowerCase() === instrFilter)
    .sort((a, b) => (a.exitTime || '').localeCompare(b.exitTime || ''));

  countEl.textContent = merged.length;
  if (!merged.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="empty-cell">No trades yet today</td></tr>';
    return;
  }
  const html = merged.slice().reverse().map(t => {
    const pnlCls = t.pnl > 0 ? 'pnl-pos' : t.pnl < 0 ? 'pnl-neg' : '';
    const ocCls  = t.status === 'CLOSED' ? '' : '';
    return `<tr>
      <td data-label="Instr"><span class="badge ${t._instr === 'NF' ? 'blue' : 'gray'}">${t._instr}</span></td>
      <td data-label="Dir">${escHtml(t.direction)}</td>
      <td data-label="Option">${t.strike} ${escHtml(t.optionType)}</td>
      <td data-label="Entry Time" class="time-col">${fmtDTS(t.entryTime)}</td>
      <td data-label="Exit Time" class="time-col">${fmtDTS(t.exitTime)}</td>
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

const ENTRY_IDS_BN = {
  time: 'entry-time', leaderTbody: 'leader-tbody', summary: 'entry-summary',
  gates: 'gate-rows', noTradeReason: 'no-trade-reason',
};
const ENTRY_IDS_NF = {
  time: 'entry-time-nf', leaderTbody: 'leader-tbody-nf', summary: 'entry-summary-nf',
  gates: 'gate-rows-nf', noTradeReason: 'no-trade-reason-nf',
};

function renderEntryLoop(d, liveLeaderRows, ids) {
  ids = ids || ENTRY_IDS_BN;
  const timeEl = document.getElementById(ids.time);
  if (!timeEl) return;
  timeEl.textContent = d && d.time ? d.time.substring(11, 16) : '—';

  const tbody = document.getElementById(ids.leaderTbody);
  // Live (per-second, current forming bar) rows take priority — falls back
  // to the frozen last-evaluated-bar snapshot only if the live feed hasn't
  // populated yet (e.g. right at WAIT_ZONE before any candle has arrived).
  const rows = (liveLeaderRows && liveLeaderRows.length ? liveLeaderRows : null) || (d && d.leaderRows) || [];
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

  const gates = document.getElementById(ids.gates);
  if (!d) {
    const summary = document.getElementById(ids.summary);
    if (summary) { summary.textContent = '—'; summary.className = 'entry-summary'; }
    if (gates) gates.innerHTML = '';
    return;
  }

  const req = d.sameDirectionRequired || 0;
  const sigOk = d.leaderSignal === 'BUY' || d.leaderSignal === 'SELL';
  const macdOk = !!d.macdDir;
  const emaOk = !!(d.emaBullish || d.emaBearish);
  const gateOk = !!(d.bnBullish || d.bnBearish);

  // Exact 14-condition list c.html's renderEntryLoopTable sums for its
  // "N/14 passed" summary (allOk = [marketOk, timeOk, noTradeOk, cooldownOk,
  // sidewaysOk, momOk, sigOk, dirOk, sqOk, ccOk, macdMet, emaMet, gateOk,
  // noRepeatOk]) — the first 3 and #10/#14 are structurally guaranteed by
  // this app's architecture (evaluate_entry is only ever invoked during the
  // ACTIVE phase, with no active trade, on a just-closed bar it hasn't seen
  // before — see scheduler.py's _tick_entries), so they can't actually fail
  // here the way c.html's own 3-second poll loop could observe them failing,
  // but they're still real conditions and shown for exact 1:1 fidelity.
  const marketOk = !!d.marketOpen;
  const timeOk = true;   // ACTIVE phase IS the 09:30-15:00 window — same guarantee
  const noTradeOk = !!d.noActiveTrade;
  const ccOk = !!d.candleCloseOk;
  const noRepeatOk = true;   // last_evaluated_bar dedup — same guarantee

  const gateList = [
    marketOk, timeOk, noTradeOk, d.cooldownOk, d.sidewaysOk, d.momentumOk,
    sigOk, d.dirCountOk, d.qtySurgeOk, ccOk, macdOk, emaOk, gateOk, noRepeatOk,
  ];
  const passed = gateList.filter(Boolean).length;

  // Derived from the SAME gateList the "N/14 passed" count uses (which
  // mixes the live noTradeOk with the rest of the frozen last-bar snapshot)
  // — not the backend's own d.entryReady, which is frozen at the moment of
  // that bar's evaluation and goes stale the instant a trade opens from it
  // (noActiveTrade flips live, but a banner reading d.entryReady wouldn't).
  const summary = document.getElementById(ids.summary);
  if (summary) {
    if (passed === gateList.length) {
      summary.textContent = '✔ ENTRY READY';
      summary.className = 'entry-summary ready';
    } else {
      summary.textContent = `✘ BLOCKED (${passed}/${gateList.length} passed)`;
      summary.className = 'entry-summary blocked';
    }
  }

  // [label, value, ok] — ok is true/false for an actual gate, null for
  // informational-only rows (matches c.html's "Display only" rows, which
  // show a value but no ✔/✘ — e.g. RSI, Leader Patterns, ATM strike/premium
  // — and are NOT part of the 14-condition count above).
  const rows2 = [
    ['Market open', marketOk ? 'Open' : 'Closed', marketOk],
    ['Time window', '09:30–15:00', timeOk],
    ['No active trade', noTradeOk ? 'Clear' : 'Trade open', noTradeOk],
    ['Cooldown', d.cooldownOk ? 'Clear' : `${Math.ceil((d.cooldownMs || 0) / 1000)}s remaining`, d.cooldownOk],
    ['Sideways range', (d.sidewaysRange != null ? fmt2(d.sidewaysRange) + ' pts' : '—'), d.sidewaysOk],
    ['Momentum', escHtml(d.momentumReason || (d.momentumOk ? 'OK' : 'weak')), d.momentumOk],
    ['Leader vote', `${d.leaderSignal} (${d.green} green / ${d.red} red)`, sigOk],
    ['Dir count', `G:${d.green} R:${d.red} (need ≥${req})`, d.dirCountOk],
    ['Strong qty', `${d.strongQty}/${d.leaderRows ? d.leaderRows.length : req} above threshold (need ≥${req})`, d.qtySurgeOk],
    ['Candle closed', ccOk ? 'Closed' : 'Forming', ccOk],
    ['RSI (14)', d.rsi != null ? Number(d.rsi).toFixed(1) : '—', null],
    ['MACD', `${d.macdDir || '—'}${d.macdVal != null ? ' (' + Number(d.macdVal).toFixed(2) + ')' : ''}`, macdOk],
    ['EMA stack', d.emaBullish ? 'Bullish' : d.emaBearish ? 'Bearish' : 'Neutral', emaOk],
    ['BN gate', `${d.bnBullish ? 'Bullish' : d.bnBearish ? 'Bearish' : 'Neutral'} (bull ${Number(d.bnBull || 0).toFixed(1)} / bear ${Number(d.bnBear || 0).toFixed(1)})`, gateOk],
    ['No candle repeat', noRepeatOk ? 'New candle' : 'Already traded', noRepeatOk],
    ['Entry ITM strike/premium', d.itmStrike != null
      ? `${d.itmStrike} @ ₹${fmt2(d.itmPremium)} (IV ${d.itmIv != null ? (d.itmIv * 100).toFixed(1) + '%' : '—'})`
      : '—', null],
  ];
  if (gates) gates.innerHTML = rows2.map(([lbl, val, ok]) => {
    const indicator = ok === null ? '<span class="g-ok na">—</span>'
      : ok ? '<span class="g-ok pass">✔</span>' : '<span class="g-ok fail">✘</span>';
    return `<div class="gate-row"><span class="g-lbl">${lbl}</span><span class="g-val">${val}</span>${indicator}</div>`;
  }).join('');

  const reasonEl = document.getElementById(ids.noTradeReason);
  if (!reasonEl) return;
  if (d.noTradeReason) {
    reasonEl.textContent = d.noTradeReason;
    reasonEl.className = 'no-trade-reason';
  } else {
    reasonEl.textContent = 'All gates clear — ready to fire on the next qualifying bar.';
    reasonEl.className = 'no-trade-reason ready';
  }
}

// ── Instrument view filter (Both / BankNifty / Nifty 50) ─────────────────────
// Purely a display filter — both engines keep trading in the background
// regardless of which view is selected; this just shows/hides the
// [data-instr] stat cards and panels. Persisted like the theme choice.

function setInstrumentFilter(which) {
  window._instrFilter = which;
  localStorage.setItem('instrFilter', which);
  document.querySelectorAll('[data-instr]').forEach(el => {
    el.style.display = (which === 'both' || el.dataset.instr === which) ? '' : 'none';
  });
  ['both', 'bn', 'nf'].forEach(w => {
    const btn = document.getElementById('instr-btn-' + w);
    if (btn) btn.classList.toggle('active', w === which);
  });
  // "Today's Trades" isn't a [data-instr] panel (it stays visible in every
  // view) — its ROWS filter by instrument instead, so re-apply immediately
  // from the cached data rather than waiting for the next STATE_UPDATE tick.
  renderClosedTrades(_lastClosedBn, _lastClosedNf);
}

setInstrumentFilter(localStorage.getItem('instrFilter') || 'both');

// Drops the .tbl-wrap max-height clamp (see dashboard.css) so a long table
// (Nifty 50's 32+1 rows in particular) can render fully instead of scrolling
// inside a small fixed box. wrapIds may be one id or an array of ids so a
// single button can expand a panel's stock-candle table + S-R table together.
function toggleTableExpand(btn, wrapIds) {
  const ids = Array.isArray(wrapIds) ? wrapIds : [wrapIds];
  const wraps = ids.map(id => document.getElementById(id)).filter(Boolean);
  if (!wraps.length) return;
  const expand = !wraps[0].classList.contains('expanded');
  wraps.forEach(w => w.classList.toggle('expanded', expand));
  btn.textContent = expand ? 'Show fewer rows' : 'Show all rows';
}

// Pops a panel out of its half-width grid column to cover the page (see
// .panel.fullview in dashboard.css) — real horizontal room for a wide table
// instead of just a scrollbar. Esc, or clicking the button again, closes it.
let _fullviewPanel = null;

function toggleFullView(btn) {
  const panel = btn.closest('.panel');
  if (!panel) return;
  const opening = panel !== _fullviewPanel;
  if (_fullviewPanel) {
    _fullviewPanel.classList.remove('fullview');
    document.body.classList.remove('fullview-active');
    _fullviewPanel = null;
  }
  if (opening) {
    panel.classList.add('fullview');
    document.body.classList.add('fullview-active');
    _fullviewPanel = panel;
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _fullviewPanel) {
    _fullviewPanel.classList.remove('fullview');
    document.body.classList.remove('fullview-active');
    _fullviewPanel = null;
  }
});

// ── Collapsible sections (c.html's toggleSection) ─────────────────────────────

function toggleSection(btn) {
  const targetId = btn.getAttribute('data-target');
  const body = document.getElementById(targetId);
  if (!body) return;
  const collapsed = body.style.display === 'none';
  body.style.display = collapsed ? '' : 'none';
  btn.textContent = collapsed ? '▼' : '▲';
}

// ── Local Trade Log (browser-local IndexedDB) ─────────────────────────────────
// Port of c.html's initTradeDB/saveTrade/updateDashboard, but sourced from the
// REAL activeTrade/closedTrades transitions in STATE_UPDATE (per the plan's
// "Trade history" decision) rather than a second, independent decision path.

// setTradeLogFilter/clearTradeLog/renderTradeLog removed 2026-09-22 — the
// #tradelog-tbody/#tl-filter-today/#tl-filter-all elements they targeted
// don't exist anywhere in the HTML and nothing calls setTradeLogFilter/
// clearTradeLog; renderTradeLog's body was an unconditional no-op past its
// `!tbody` guard. The IndexedDB persistence below (initTradesDB/
// _saveLocalTrade/updateLocalTradeLog) is untouched — it's a real,
// independent browser-local record of every server trade, kept even
// though there's currently no visible table reading it back.

let _tradesDb = null;

function initTradesDB() {
  // version 2: keyPath 'localId' (not autoIncrement) so re-logging the same
  // server trade after a page reload is an idempotent overwrite (put), not a
  // duplicate row — STATE_UPDATE always re-delivers the current
  // activeTrade/closedTrades on every reconnect, with no in-memory way to
  // remember "already logged" across a refresh. Bumped from v1 (autoIncrement
  // 'id') since onupgradeneeded only fires on a version change, never on a
  // schema change alone.
  const req = indexedDB.open('TradesDB', 2);
  req.onupgradeneeded = (e) => {
    const db = e.target.result;
    if (db.objectStoreNames.contains('trades')) db.deleteObjectStore('trades');
    const store = db.createObjectStore('trades', { keyPath: 'localId' });
    store.createIndex('time', 'time', { unique: false });
  };
  req.onsuccess = (e) => { _tradesDb = e.target.result; };
  req.onerror = () => console.error('TradesDB unavailable');
}

const _writtenTradeLogIds = new Set();   // skip redundant put()s for a trade already logged THIS session

function _saveLocalTrade(obj) {
  if (!_tradesDb || _writtenTradeLogIds.has(obj.localId)) return;
  // Mark "logged" only once the write actually succeeds — this used to be
  // set eagerly before put() was known to succeed, with no onerror handler
  // (unlike qtyAudit.js's addStockRecord), so a failed write (quota, a
  // stale connection) would silently and permanently drop that trade from
  // the log for the rest of the session (found in review).
  const tx = _tradesDb.transaction('trades', 'readwrite');
  const req = tx.objectStore('trades').put(obj);   // put (not add) — localId makes re-logging idempotent
  req.onsuccess = () => _writtenTradeLogIds.add(obj.localId);
  req.onerror = () => console.error('Local Trade Log write failed:', obj.localId, req.error);
}

function updateLocalTradeLog(activeTrade, closedTrades) {
  if (activeTrade && activeTrade.orderId) {
    _saveLocalTrade({
      localId: `${activeTrade.orderId}_ENTRY`,
      type: `${activeTrade.direction}_ENTRY`, price: activeTrade.entryIndexPrice,
      time: activeTrade.entryTime || new Date().toISOString(),
      confidence: activeTrade.confidence, pnl: null,
    });
  }

  closedTrades.forEach(t => {
    if (!t.orderId) return;
    _saveLocalTrade({
      localId: `${t.orderId}_EXIT`,
      type: `${t.direction}_EXIT`, price: t.exitIndexPrice,
      time: t.exitTime || new Date().toISOString(),
      confidence: t.confidence, pnl: t.pnl,
    });
  });
}

// ── Auto-screenshot on entry (port of c.html's takeTradeScreenshot) ──────────

// Per-instrument transition state — was a single shared flag until 2026-09-23
// (found in review), which meant this was only ever called for BankNifty and
// Nifty 50 entries never triggered a screenshot at all (NF manual/algo
// trading was generalized to a first-class instrument 2026-09-18, but this
// function was never updated alongside it). Keyed 'bn'/'nf' so each
// instrument's own open/closed transition is tracked independently.
const _hadActiveTrade = { bn: false, nf: false };

function checkTradeTransitionForScreenshot(activeTrade, instr) {
  const hasNow = !!activeTrade;
  if (hasNow && !_hadActiveTrade[instr] && typeof html2canvas === 'function') {
    const label = `${activeTrade.direction}_ENTRY_${activeTrade.entryIndexPrice}`;
    html2canvas(document.body).then(canvas => {
      const a = document.createElement('a');
      const ts = new Date().toISOString().replace(/[:.]/g, '-');
      a.download = `${instr.toUpperCase()}_${label}_${ts}.png`;
      a.href = canvas.toDataURL('image/png');
      a.click();
    }).catch(() => {});
  }
  _hadActiveTrade[instr] = hasNow;
}

// (chooseOutputFile/appendTickToFile CSV/file-export feature removed
// 2026-09-22 — the #fileexport-status element and its trigger button don't
// exist anywhere in the HTML; appendTickToFile was still called on every
// tick from handleTickUpdate but was permanently a no-op since
// _writableStream could only ever be set by the unreachable
// chooseOutputFile. Confirmed dead, not just unused.)

// (openConditionModal/closeConditionModal removed 2026-09-22 — the
// #condition-modal/#condition-modal-body elements they targeted don't
// exist anywhere in the HTML and no button calls them; confirmed dead.)

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

// Entry/exit on "Today's Trades" can legitimately be seconds apart (the
// tick-wise exit check runs every ~100ms on live index price, not on 5m bar
// boundaries) — fmtDT's minute-only granularity makes two distinct moments
// look identical, so this table specifically needs seconds to verify them.
function fmtDTS(s) {
  if (!s) return '—';
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  try {
    const d = s.substring(0, 10);
    const t = s.substring(11, 19);
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

// The Backtest panel's HTML was removed from index.html (dashboard
// simplification), so the page-load "resume a running backtest / load the
// last result" fetch that used to live here was deleted too — it called
// renderBtHistory/setBtStatus/etc., which unconditionally touch bt-history/
// bt-status/bt-trades and would throw a TypeError on every page load now
// that those elements don't exist. The /api/backtest* endpoints and
// runBacktest()/pollBacktest()/loadRun() etc. below are all still there and
// still work if ever called directly — there's just no button left to call
// them from.

initTradesDB();
connect();
