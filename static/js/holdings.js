'use strict';

var _holdingsData = [];

function _triggerLabel(h) {
  var parts = [];
  if (h.targetPrice != null) parts.push('T ' + fmt2(h.targetPrice) + (h.targetQty != null ? ' ×' + h.targetQty : ''));
  if (h.stopLossPrice != null) parts.push('SL ' + fmt2(h.stopLossPrice) + (h.stopLossQty != null ? ' ×' + h.stopLossQty : ''));
  return parts.length ? parts.join(' / ') : '—';
}

function _holdingRowHtml(h) {
  return '<tr data-token="' + h.token + '">' +
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(h.symbol) + '</td>' +
    '<td data-label="Qty" class="num-col">' + h.qty + '</td>' +
    '<td data-label="Avg. Cost" class="num-col">' + fmt2(h.avgPrice) + '</td>' +
    '<td data-label="LTP" class="num-col" data-field="ltp">' + fmt2(h.ltp) + '</td>' +
    '<td data-label="Current Value" class="num-col" data-field="value">' + fmt2(h.currentValue) + '</td>' +
    '<td data-label="P&amp;L" class="num-col ' + pnlClass(h.pnl) + '" data-field="pnl">' + pnlSign(h.pnl) + '</td>' +
    '<td data-label="Target / SL" data-field="trigger">' + _triggerLabel(h) + '</td>' +
    '<td>' +
      '<button class="btn-mini" onclick="openTriggerModal(\'' + h.token + '\')">TP/SL</button> ' +
      '<button class="btn-mini danger" onclick="openSellModal(\'' + h.token + '\',' + h.qty + ')">Sell</button>' +
    '</td>' +
  '</tr>';
}

function _renderSummary() {
  var invested = _holdingsData.reduce(function (s, h) { return s + h.investedValue; }, 0);
  var current  = _holdingsData.reduce(function (s, h) { return s + h.currentValue; }, 0);
  var pnl = current - invested;
  document.getElementById('sum-invested').textContent = '₹' + fmt2(invested);
  document.getElementById('sum-current').textContent = '₹' + fmt2(current);
  var pnlEl = document.getElementById('sum-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+₹' : '-₹') + fmt2(Math.abs(pnl));
  pnlEl.className = 'stat-value ' + pnlClass(pnl);
  var card = document.getElementById('sum-pnl-card');
  card.className = 'stat-card ' + (pnl > 0 ? 'is-pos' : (pnl < 0 ? 'is-neg' : ''));
}

async function loadHoldings() {
  var tbody = document.getElementById('holdings-tbody');
  try {
    _holdingsData = await apiGet('/api/holdings');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">Failed to load holdings</td></tr>';
    return;
  }
  tbody.innerHTML = _holdingsData.length
    ? _holdingsData.map(_holdingRowHtml).join('')
    : '<tr><td colspan="8" class="empty-cell">No holdings yet — buy something from the Terminal.</td></tr>';
  _renderSummary();
}

// ── Target / Stop-Loss modal ─────────────────────────────────────────────────
var _triggerToken = null;

function openTriggerModal(token) {
  var h = _holdingsData.filter(function (x) { return x.token === token; })[0];
  if (!h) return;
  _triggerToken = token;
  document.getElementById('trigger-target').value = h.targetPrice != null ? h.targetPrice : '';
  document.getElementById('trigger-sl').value = h.stopLossPrice != null ? h.stopLossPrice : '';
  var tq = document.getElementById('trigger-target-qty');
  var sq = document.getElementById('trigger-sl-qty');
  tq.value = h.targetQty != null ? h.targetQty : '';
  sq.value = h.stopLossQty != null ? h.stopLossQty : '';
  tq.max = sq.max = h.qty;
  document.getElementById('trigger-modal').classList.remove('hidden');
}

function closeTriggerModal() {
  document.getElementById('trigger-modal').classList.add('hidden');
  _triggerToken = null;
}

async function _submitTriggers(target, sl, targetQty, slQty) {
  try {
    await apiPost('/api/holdings/' + _triggerToken + '/triggers', {
      target_price: target, stop_loss_price: sl,
      target_qty: targetQty, stop_loss_qty: slQty,
    });
    toast('Target/SL updated', 'ok');
    closeTriggerModal();
    loadHoldings();
  } catch (e) {
    toast(e.message, 'err');
  }
}

function saveTriggers() {
  var t  = document.getElementById('trigger-target').value;
  var s  = document.getElementById('trigger-sl').value;
  var tq = document.getElementById('trigger-target-qty').value;
  var sq = document.getElementById('trigger-sl-qty').value;
  _submitTriggers(
    t === '' ? null : Number(t), s === '' ? null : Number(s),
    tq === '' ? null : Number(tq), sq === '' ? null : Number(sq)
  );
}

function clearTriggers() {
  _submitTriggers(null, null, null, null);
}

// ── Sell (qty-bounded) modal ─────────────────────────────────────────────────
var _sellToken = null;

function openSellModal(token, openQty) {
  _sellToken = token;
  var input = document.getElementById('sell-qty');
  input.value = openQty;
  input.max = openQty;
  document.getElementById('sell-modal').classList.remove('hidden');
}

function closeSellModal() {
  document.getElementById('sell-modal').classList.add('hidden');
  _sellToken = null;
}

async function confirmSell() {
  var qty = Number(document.getElementById('sell-qty').value);
  if (!qty || qty <= 0) { toast('Enter a valid quantity', 'err'); return; }
  try {
    await apiPost('/api/holdings/' + _sellToken + '/sell', { qty: qty });
    toast('Holding sold', 'ok');
    closeSellModal();
    loadHoldings();
  } catch (e) {
    toast(e.message, 'err');
  }
}

window.addEventListener('market:tick', function (evt) {
  var prices = evt.detail;
  var touched = false;
  _holdingsData.forEach(function (h) {
    if (!Object.prototype.hasOwnProperty.call(prices, h.token)) return;
    h.ltp = prices[h.token];
    h.currentValue = h.ltp * h.qty;
    h.pnl = (h.ltp - h.avgPrice) * h.qty;
    touched = true;
    var row = document.querySelector('#holdings-tbody tr[data-token="' + h.token + '"]');
    if (row) {
      var ltpEl = row.querySelector('[data-field="ltp"]');
      var valEl = row.querySelector('[data-field="value"]');
      var pnlEl = row.querySelector('[data-field="pnl"]');
      if (ltpEl) ltpEl.textContent = fmt2(h.ltp);
      if (valEl) valEl.textContent = fmt2(h.currentValue);
      if (pnlEl) { pnlEl.textContent = pnlSign(h.pnl); pnlEl.className = 'num-col ' + pnlClass(h.pnl); }
    }
  });
  if (touched) _renderSummary();
});

window.addEventListener('account:update', loadHoldings);
document.addEventListener('DOMContentLoaded', loadHoldings);
