'use strict';

var _positionsFilter = 'OPEN';
var _positionsData = [];

function setPositionFilter(f) {
  _positionsFilter = f;
  document.getElementById('filter-open').classList.toggle('active', f === 'OPEN');
  document.getElementById('filter-closed').classList.toggle('active', f === 'CLOSED');
  loadPositions();
}

function _positionRowHtml(p) {
  var priceLabel = p.status === 'OPEN' ? fmt2(p.ltp) : fmt2(p.exitPrice);
  var exitBtn = p.status === 'OPEN'
    ? '<button class="btn-mini danger" onclick="exitPosition(' + p.id + ')">Exit</button>'
    : '';
  return '<tr data-token="' + p.token + '" data-id="' + p.id + '" data-status="' + p.status + '">' +
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(p.symbol) + '</td>' +
    '<td data-label="Side">' + p.side + '</td>' +
    '<td data-label="Qty" class="num-col">' + p.qty + '</td>' +
    '<td data-label="Avg. Price" class="num-col">' + fmt2(p.avgPrice) + '</td>' +
    '<td data-label="LTP / Exit" class="num-col" data-field="price">' + priceLabel + '</td>' +
    '<td data-label="P&amp;L" class="num-col ' + pnlClass(p.pnl) + '" data-field="pnl">' + pnlSign(p.pnl) + '</td>' +
    '<td>' + exitBtn + '</td>' +
  '</tr>';
}

function _renderSummary() {
  var openCount = _positionsData.filter(function (p) { return p.status === 'OPEN'; }).length;
  var openPnl = _positionsData.filter(function (p) { return p.status === 'OPEN'; })
    .reduce(function (s, p) { return s + p.pnl; }, 0);
  var realizedPnl = _positionsData.filter(function (p) { return p.status === 'CLOSED'; })
    .reduce(function (s, p) { return s + p.pnl; }, 0);
  document.getElementById('sum-open-count').textContent = openCount;
  var openEl = document.getElementById('sum-open-pnl');
  openEl.textContent = (openPnl >= 0 ? '+₹' : '-₹') + fmt2(Math.abs(openPnl));
  openEl.className = 'stat-value ' + pnlClass(openPnl);
  document.getElementById('sum-open-pnl-card').className = 'stat-card ' + (openPnl > 0 ? 'is-pos' : (openPnl < 0 ? 'is-neg' : ''));
  var realEl = document.getElementById('sum-realized-pnl');
  realEl.textContent = (realizedPnl >= 0 ? '+₹' : '-₹') + fmt2(Math.abs(realizedPnl));
  realEl.className = 'stat-value ' + pnlClass(realizedPnl);
  document.getElementById('sum-realized-pnl-card').className = 'stat-card ' + (realizedPnl > 0 ? 'is-pos' : (realizedPnl < 0 ? 'is-neg' : ''));
}

async function loadPositions() {
  var tbody = document.getElementById('positions-tbody');
  try {
    _positionsData = await apiGet('/api/positions?status=' + _positionsFilter);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">Failed to load positions</td></tr>';
    return;
  }
  tbody.innerHTML = _positionsData.length
    ? _positionsData.map(_positionRowHtml).join('')
    : '<tr><td colspan="7" class="empty-cell">No ' + (_positionsFilter === 'OPEN' ? 'open' : 'closed') + ' positions</td></tr>';
  _renderSummary();
}

async function exitPosition(id) {
  try {
    await apiPost('/api/positions/' + id + '/exit');
    toast('Position closed', 'ok');
    loadPositions();
  } catch (e) {
    toast(e.message, 'err');
  }
}

window.addEventListener('market:tick', function (evt) {
  if (_positionsFilter !== 'OPEN') return;
  var prices = evt.detail;
  var touched = false;
  _positionsData.forEach(function (p) {
    if (p.status !== 'OPEN' || !Object.prototype.hasOwnProperty.call(prices, p.token)) return;
    p.ltp = prices[p.token];
    p.pnl = p.side === 'BUY' ? (p.ltp - p.avgPrice) * p.qty : (p.avgPrice - p.ltp) * p.qty;
    touched = true;
    var row = document.querySelector('#positions-tbody tr[data-id="' + p.id + '"]');
    if (row) {
      var priceEl = row.querySelector('[data-field="price"]');
      var pnlEl = row.querySelector('[data-field="pnl"]');
      if (priceEl) priceEl.textContent = fmt2(p.ltp);
      if (pnlEl) { pnlEl.textContent = pnlSign(p.pnl); pnlEl.className = 'num-col ' + pnlClass(p.pnl); }
    }
  });
  if (touched) _renderSummary();
});

window.addEventListener('account:update', loadPositions);
document.addEventListener('DOMContentLoaded', loadPositions);
