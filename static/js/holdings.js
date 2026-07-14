'use strict';

var _holdingsData = [];

function _holdingRowHtml(h) {
  return '<tr data-token="' + h.token + '">' +
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(h.symbol) + '</td>' +
    '<td data-label="Qty" class="num-col">' + h.qty + '</td>' +
    '<td data-label="Avg. Cost" class="num-col">' + fmt2(h.avgPrice) + '</td>' +
    '<td data-label="LTP" class="num-col" data-field="ltp">' + fmt2(h.ltp) + '</td>' +
    '<td data-label="Current Value" class="num-col" data-field="value">' + fmt2(h.currentValue) + '</td>' +
    '<td data-label="P&amp;L" class="num-col ' + pnlClass(h.pnl) + '" data-field="pnl">' + pnlSign(h.pnl) + '</td>' +
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
    tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">Failed to load holdings</td></tr>';
    return;
  }
  tbody.innerHTML = _holdingsData.length
    ? _holdingsData.map(_holdingRowHtml).join('')
    : '<tr><td colspan="6" class="empty-cell">No holdings yet — buy something from the Terminal.</td></tr>';
  _renderSummary();
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
