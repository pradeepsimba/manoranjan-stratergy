'use strict';

// ── Kite-style manual order form — wired to the REAL paper-trading engine
// (explicit user decision, 2026-09-01, reversing this form's original
// decorative-only c.html-parity behavior). Submit/Exit call /api/manual-order
// and /api/manual-exit, which place/close the SAME st.active_trade the
// automated strategy uses (app/services/bn_trade.py's place_manual_order/
// force_close) — a manual trading-desk override, not a second trade slot.
// Pricing (ATM strike/expiry/Black-Scholes premium) is computed server-side
// from the real BankNifty spot + realized-vol estimate, same as an algo
// fill — there's no client-side pricing preview to keep in sync anymore.
//
// Lot size is always cfg.BN_LOT_SIZE (30) — this engine has no
// position-sizing concept (see CLAUDE.md), so the Qty field is fixed/
// display-only, not sent to the server.
//
// Order Type stays MARKET-only in effect: every fill in this engine is a
// synthetic Black-Scholes mark at the moment of the click, so there's no
// real order book for a LIMIT price to rest on.

let _kiteType = 'BUY';
let _kiteSubmitting = false;

function kiteSetType(type) {
  _kiteType = type;
  document.getElementById('kite-buy').classList.toggle('active', type === 'BUY');
  document.getElementById('kite-sell').classList.toggle('active', type === 'SELL');
}

function kiteSetOrdType() { /* cosmetic only — see file header; server always fills at the BS mark */ }

function kiteUpdateFundsDisplay() {
  const el = document.getElementById('kite-funds');
  if (!el) return;
  el.value = window._lastFunds != null ? `₹${Number(window._lastFunds).toFixed(2)}` : '—';
}

function _setKiteStatus(text) {
  const el = document.getElementById('kite-status');
  if (el) el.textContent = text;
}

function _kiteSetSubmitting(v) {
  _kiteSubmitting = v;
  const submitBtn = document.querySelector('.kite-actions .btn-run');
  if (submitBtn) submitBtn.disabled = v || !!window._hasActiveTradeBn;
}

function kiteSubmitOrder() {
  if (_kiteSubmitting) return;
  if (window._hasActiveTradeBn) { _setKiteStatus('A trade is already active — exit it before placing a new one.'); return; }
  _kiteSetSubmitting(true);
  _setKiteStatus('Submitting…');
  fetch('/api/manual-order', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction: _kiteType }),
  })
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) { _setKiteStatus(`Order rejected: ${body.detail || 'unknown error'}`); return; }
      _setKiteStatus(
        `${body.direction} ${body.strike}${body.optionType} @ premium ₹${Number(body.entryPremium).toFixed(2)} ` +
        `(index ${Number(body.entryIndexPrice).toFixed(2)}) — order ${body.orderId}`
      );
    })
    .catch(e => _setKiteStatus(`Order failed: ${e.message}`))
    .finally(() => _kiteSetSubmitting(false));
}

function manualExit() {
  if (_kiteSubmitting) return;
  _kiteSetSubmitting(true);
  _setKiteStatus('Exiting…');
  fetch('/api/manual-exit', { method: 'POST' })
    .then(r => r.json().then(body => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) { _setKiteStatus(`Exit failed: ${body.detail || 'unknown error'}`); return; }
      const pnl = Number(body.pnl);
      _setKiteStatus(`Exited @ premium ₹${Number(body.exitPremium).toFixed(2)} — P&L ₹${pnl.toFixed(2)}`);
    })
    .catch(e => _setKiteStatus(`Exit failed: ${e.message}`))
    .finally(() => _kiteSetSubmitting(false));
}

setInterval(() => {
  kiteUpdateFundsDisplay();
  const submitBtn = document.querySelector('.kite-actions .btn-run');
  if (submitBtn && !_kiteSubmitting) submitBtn.disabled = !!window._hasActiveTradeBn;
}, 1000);
kiteUpdateFundsDisplay();
