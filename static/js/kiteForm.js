'use strict';

// ── Kite-style manual order form — wired to the REAL paper-trading engine
// (explicit user decision, 2026-09-01, reversing this form's original
// decorative-only c.html-parity behavior). Submit/Exit call /api/manual-order
// (+/-nf) and /api/manual-exit(-nf), which place/close the SAME
// st.active_trade/active_trade_nf the automated strategy uses
// (app/services/bn_trade.py/nf_trade.py's place_manual_order/force_close) —
// a manual trading-desk override, not a second trade slot. Pricing (ATM
// strike/expiry/Black-Scholes premium) is computed server-side from the
// real BankNifty/Nifty 50 spot + realized-vol estimate, same as an algo
// fill — there's no client-side pricing preview to keep in sync anymore.
//
// Generalized to both instruments 2026-09-18 (explicit user decision, NF
// manual trading previously didn't exist) using the same ids-object pattern
// as STOCK_IDS_BN/STOCK_IDS_NF etc. elsewhere in this app, rather than a
// second copy of this whole file.
//
// Lot size is always cfg.BN_LOT_SIZE/NF_LOT_SIZE — this engine has no
// position-sizing concept (see CLAUDE.md), so the Qty field is fixed/
// display-only, not sent to the server.
//
// Order Type stays MARKET-only in effect: every fill in this engine is a
// synthetic (or, once a real tick arrives, real-LTP) mark at/after the
// moment of the click, so there's no real order book for a LIMIT price to
// rest on.

const KITE_IDS_BN = {
  buy: 'kite-buy', sell: 'kite-sell', funds: 'kite-funds', status: 'kite-status',
  submitBtn: 'kite-submit-btn', hasActiveFlag: '_hasActiveTradeBn',
  orderUrl: '/api/manual-order', exitUrl: '/api/manual-exit', label: 'BankNifty',
};
const KITE_IDS_NF = {
  buy: 'kite-buy-nf', sell: 'kite-sell-nf', funds: 'kite-funds-nf', status: 'kite-status-nf',
  submitBtn: 'kite-submit-btn-nf', hasActiveFlag: '_hasActiveTradeNf',
  orderUrl: '/api/manual-order-nf', exitUrl: '/api/manual-exit-nf', label: 'Nifty 50',
};

const _kiteType = { bn: 'BUY', nf: 'BUY' };
const _kiteSubmitting = { bn: false, nf: false };

function _kiteKey(ids) { return ids === KITE_IDS_NF ? 'nf' : 'bn'; }

function kiteSetType(type, ids) {
  ids = ids || KITE_IDS_BN;
  _kiteType[_kiteKey(ids)] = type;
  const buyEl = document.getElementById(ids.buy);
  const sellEl = document.getElementById(ids.sell);
  if (buyEl) buyEl.classList.toggle('active', type === 'BUY');
  if (sellEl) sellEl.classList.toggle('active', type === 'SELL');
}

function kiteSetOrdType() { /* cosmetic only — see file header; server always fills at the current mark */ }

function kiteUpdateFundsDisplay(ids) {
  ids = ids || KITE_IDS_BN;
  const el = document.getElementById(ids.funds);
  if (!el) return;
  el.value = window._lastFunds != null ? `₹${Number(window._lastFunds).toFixed(2)}` : '—';
}

function _setKiteStatus(ids, text) {
  const el = document.getElementById(ids.status);
  if (el) el.textContent = text;
}

function _kiteSetSubmitting(ids, v) {
  const key = _kiteKey(ids);
  _kiteSubmitting[key] = v;
  const submitBtn = document.getElementById(ids.submitBtn);
  if (submitBtn) submitBtn.disabled = v || !!window[ids.hasActiveFlag];
}

function kiteSubmitOrder(ids) {
  ids = ids || KITE_IDS_BN;
  const key = _kiteKey(ids);
  if (_kiteSubmitting[key]) return;
  if (window[ids.hasActiveFlag]) {
    _setKiteStatus(ids, 'A trade is already active — exit it before placing a new one.');
    return;
  }
  _kiteSetSubmitting(ids, true);
  _setKiteStatus(ids, 'Submitting…');
  fetch(ids.orderUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: _kiteType[key] }),
  })
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) { _setKiteStatus(ids, `Order rejected: ${body.detail || 'unknown error'}`); return; }
      _setKiteStatus(ids,
        `${body.direction} ${body.strike}${body.optionType} @ premium ₹${Number(body.entryPremium).toFixed(2)} ` +
        `(index ${Number(body.entryIndexPrice).toFixed(2)}) — order ${body.orderId}`
      );
    })
    .catch(e => _setKiteStatus(ids, `Order failed: ${e.message}`))
    .finally(() => _kiteSetSubmitting(ids, false));
}

function manualExit(ids) {
  ids = ids || KITE_IDS_BN;
  const key = _kiteKey(ids);
  if (_kiteSubmitting[key]) return;
  _kiteSetSubmitting(ids, true);
  _setKiteStatus(ids, 'Exiting…');
  fetch(ids.exitUrl, { method: 'POST' })
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) { _setKiteStatus(ids, `Exit failed: ${body.detail || 'unknown error'}`); return; }
      const pnl = Number(body.pnl);
      _setKiteStatus(ids, `Exited @ premium ₹${Number(body.exitPremium).toFixed(2)} — P&L ₹${pnl.toFixed(2)}`);
    })
    .catch(e => _setKiteStatus(ids, `Exit failed: ${e.message}`))
    .finally(() => _kiteSetSubmitting(ids, false));
}

setInterval(() => {
  [KITE_IDS_BN, KITE_IDS_NF].forEach(ids => {
    kiteUpdateFundsDisplay(ids);
    const key = _kiteKey(ids);
    const submitBtn = document.getElementById(ids.submitBtn);
    if (submitBtn && !_kiteSubmitting[key]) submitBtn.disabled = !!window[ids.hasActiveFlag];
  });
}, 1000);
kiteUpdateFundsDisplay(KITE_IDS_BN);
kiteUpdateFundsDisplay(KITE_IDS_NF);
