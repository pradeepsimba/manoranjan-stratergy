'use strict';

// ── Kite manual-order form — DECORATIVE ONLY, exactly like c.html: local UI
// state + a client-side Black-Scholes preview, never touches the real
// paper-trading engine (per explicit user direction). Funds here are a
// separate localStorage-only simulation, distinct from the server's real
// `funds` shown in the stat strip / funds bar.

const KITE_LOT_SIZE = 30;
let _kiteType = 'BUY';
let _kiteOrdType = 'MARKET';
let _kitePosition = null;   // { type, strike, optType, entryPremium, qty }

function _kiteFunds() {
  const v = parseFloat(localStorage.getItem('kiteSimFunds'));
  return Number.isFinite(v) ? v : 100000;
}
function _setKiteFunds(v) { localStorage.setItem('kiteSimFunds', String(v)); }

function _kiteNormalCdf(x) {
  const a1=0.254829592, a2=-0.284496736, a3=1.421413741, a4=-1.453152027, a5=1.061405429, p=0.3275911;
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x) / Math.sqrt(2);
  const t = 1 / (1 + p * ax);
  const y = 1 - (((((a5*t+a4)*t)+a3)*t+a2)*t+a1)*t*Math.exp(-ax*ax);
  return 0.5 * (1 + sign * y);
}

function _kiteBlackScholes(S, K, T, r, sigma, type) {
  if (T <= 0) return { price: type === 'CE' ? Math.max(0, S - K) : Math.max(0, K - S) };
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT);
  const d2 = d1 - sigma * sqrtT;
  const price = type === 'CE'
    ? S * _kiteNormalCdf(d1) - K * Math.exp(-r * T) * _kiteNormalCdf(d2)
    : K * Math.exp(-r * T) * _kiteNormalCdf(-d2) - S * _kiteNormalCdf(-d1);
  return { price: Math.max(0, price) };
}

function _kiteAtmStrike(spot) { return Math.round(spot / 100) * 100; }

function _kiteNextExpiryYears() {
  const now = new Date();
  const day = now.getDay();
  let daysUntil = (4 - day + 7) % 7;
  if (daysUntil === 0 && now.getHours() * 60 + now.getMinutes() >= 930) daysUntil = 7;
  const expiry = new Date(now);
  expiry.setDate(now.getDate() + daysUntil);
  expiry.setHours(15, 30, 0, 0);
  return Math.max(0, (expiry - now) / (365 * 24 * 3600000));
}

function kiteSetType(type) {
  _kiteType = type;
  document.getElementById('kite-buy').classList.toggle('active', type === 'BUY');
  document.getElementById('kite-sell').classList.toggle('active', type === 'SELL');
}

function kiteSetOrdType(v) {
  _kiteOrdType = v;
  document.getElementById('kite-price').disabled = (v === 'MARKET');
}

function kiteUpdateFundsDisplay() {
  const el = document.getElementById('kite-funds');
  if (el) el.value = `₹${_kiteFunds().toFixed(2)}`;
}

function kiteSubmitOrder() {
  const spot = window._lastBnLtp;
  if (!spot) { _setKiteStatus('No live BankNifty price yet — cannot simulate.'); return; }
  const qty = Math.max(KITE_LOT_SIZE, parseInt(document.getElementById('kite-qty').value, 10) || KITE_LOT_SIZE);
  const strike = _kiteAtmStrike(spot);
  const optType = _kiteType === 'BUY' ? 'CE' : 'PE';
  const T = _kiteNextExpiryYears();
  const { price } = _kiteBlackScholes(spot, strike, T, 0.065, 0.28, optType);

  _kitePosition = { type: _kiteType, strike, optType, entryPremium: price, entrySpot: spot, qty };
  _setKiteStatus(
    `Simulated ${_kiteType} ${strike}${optType} @ premium ₹${price.toFixed(2)} × ${qty} ` +
    `(${_kiteOrdType}) — local only, does not affect the real paper-trading engine.`
  );
}

function manualExit() {
  if (!_kitePosition) { _setKiteStatus('No simulated position to exit.'); return; }
  const spot = window._lastBnLtp || _kitePosition.entrySpot;
  const T = _kiteNextExpiryYears();
  const { price } = _kiteBlackScholes(spot, _kitePosition.strike, T, 0.065, 0.28, _kitePosition.optType);
  const pnl = (price - _kitePosition.entryPremium) * _kitePosition.qty;
  _setKiteFunds(_kiteFunds() + pnl);
  _setKiteStatus(`Simulated exit @ premium ₹${price.toFixed(2)} — P&L ₹${pnl.toFixed(2)} (local only).`);
  _kitePosition = null;
  kiteUpdateFundsDisplay();
}

function _setKiteStatus(text) {
  const el = document.getElementById('kite-status');
  if (el) el.textContent = text;
}

kiteUpdateFundsDisplay();
