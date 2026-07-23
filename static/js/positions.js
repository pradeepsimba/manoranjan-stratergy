'use strict';

var _positionsFilter = 'OPEN';
var _positionsData = [];
var _allPositionsData = [];

function setPositionFilter(f) {
  _positionsFilter = f;
  document.getElementById('filter-open').classList.toggle('active', f === 'OPEN');
  document.getElementById('filter-closed').classList.toggle('active', f === 'CLOSED');
  loadPositions();
}

function _isToday(iso) {
  if (!iso) return false;
  var d = new Date(iso);
  return !isNaN(d.getTime()) && d.toDateString() === new Date().toDateString();
}

function _triggerLabel(p) {
  var parts = [];
  if (p.targetPrice != null) parts.push('T ' + fmt2(p.targetPrice) + (p.targetQty != null ? ' ×' + p.targetQty : ''));
  if (p.stopLossPrice != null) parts.push('SL ' + fmt2(p.stopLossPrice) + (p.stopLossQty != null ? ' ×' + p.stopLossQty : ''));
  return parts.length ? parts.join(' / ') : '—';
}

function _positionRowHtml(p) {
  var priceLabel = p.status === 'OPEN' ? fmt2(p.ltp) : fmt2(p.exitPrice);
  var actions = p.status === 'OPEN'
    ? '<button class="btn-mini" onclick="openTriggerModal(' + p.id + ')">TP/SL</button> ' +
      '<button class="btn-mini danger" onclick="openExitModal(' + p.id + ',' + p.qty + ')">Exit</button>'
    : '';
  return '<tr data-token="' + p.token + '" data-id="' + p.id + '" data-status="' + p.status + '">' +
    '<td data-label="Symbol" class="card-title sym-col"><span class="sym-cell">' + symAvatarHtml(p.symbol) + '<span class="sym-name">' + escHtml(p.symbol) + '</span></span></td>' +
    '<td data-label="Side">' + sidePillHtml(p.side) + '</td>' +
    '<td data-label="Qty" class="num-col">' + p.qty + '</td>' +
    '<td data-label="Avg. Price" class="num-col">' + fmt2(p.avgPrice) + '</td>' +
    '<td data-label="LTP / Exit" class="num-col" data-field="price">' + priceLabel + '</td>' +
    '<td data-label="P&amp;L" class="num-col ' + pnlClass(p.pnl) + '" data-field="pnl">' + pnlSign(p.pnl) + '</td>' +
    '<td data-label="Margin Used" class="num-col">' + fmt2(p.marginUsed) + '</td>' +
    '<td data-label="Target / SL" data-field="trigger">' + _triggerLabel(p) + '</td>' +
    '<td>' + actions + '</td>' +
  '</tr>';
}

function _renderSummary() {
  var openCount = _allPositionsData.filter(function (p) { return p.status === 'OPEN'; }).length;
  var openPnl = _allPositionsData.filter(function (p) { return p.status === 'OPEN'; })
    .reduce(function (s, p) { return s + p.pnl; }, 0);
  // Booked P&L from TODAY only — includes a still-OPEN position that only had a
  // partial exit today (via lastExitAt), not just fully CLOSED ones (via closedAt).
  var todayRows = _allPositionsData.filter(function (p) {
    var ts = p.status === 'CLOSED' ? p.closedAt : p.lastExitAt;
    return _isToday(ts);
  });
  var realizedPnl = todayRows.reduce(function (s, p) { return s + (p.realizedPnl || 0); }, 0);
  var wins = todayRows.filter(function (p) { return (p.realizedPnl || 0) > 0; }).length;
  var losses = todayRows.filter(function (p) { return (p.realizedPnl || 0) < 0; }).length;
  var winRate = todayRows.length ? (wins / todayRows.length * 100) : 0;
  document.getElementById('sum-open-count').textContent = openCount;
  var openEl = document.getElementById('sum-open-pnl');
  openEl.textContent = (openPnl >= 0 ? '+₹' : '-₹') + fmt2(Math.abs(openPnl));
  openEl.className = 'stat-value ' + pnlClass(openPnl);
  document.getElementById('sum-open-pnl-card').className = 'stat-card ' + (openPnl > 0 ? 'is-pos' : (openPnl < 0 ? 'is-neg' : ''));
  var realEl = document.getElementById('sum-realized-pnl');
  realEl.textContent = (realizedPnl >= 0 ? '+₹' : '-₹') + fmt2(Math.abs(realizedPnl));
  realEl.className = 'stat-value ' + pnlClass(realizedPnl);
  document.getElementById('sum-realized-pnl-card').className = 'stat-card ' + (realizedPnl > 0 ? 'is-pos' : (realizedPnl < 0 ? 'is-neg' : ''));
  document.getElementById('sum-winrate').textContent = todayRows.length ? winRate.toFixed(1) + '%' : '—';
  document.getElementById('sum-winrate-sub').textContent =
    todayRows.length ? todayRows.length + ' closed · ' + wins + 'W / ' + losses + 'L' : 'No trades closed today';
  document.getElementById('close-all-btn').style.display =
    (_positionsFilter === 'OPEN' && openCount > 0) ? '' : 'none';
}

async function loadPositions() {
  var tbody = document.getElementById('positions-tbody');
  try {
    // Fetch the full open+closed set (unfiltered) so the summary tiles above the
    // tabs always reflect the whole account, then slice client-side for the table.
    _allPositionsData = await apiGet('/api/positions');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-cell">Failed to load positions</td></tr>';
    return;
  }
  _positionsData = _positionsFilter === 'OPEN'
    ? _allPositionsData.filter(function (p) { return p.status === 'OPEN'; })
    : _allPositionsData.filter(function (p) { return p.status === 'CLOSED' && _isToday(p.closedAt); });
  tbody.innerHTML = _positionsData.length
    ? _positionsData.map(_positionRowHtml).join('')
    : '<tr><td colspan="9" class="empty-cell">' + (_positionsFilter === 'OPEN'
        ? emptyStateHtml('No open positions', 'Place an MIS order from the Terminal', 'chart')
        : emptyStateHtml('No positions closed today', 'Positions you exit today will appear here', 'chart')) + '</td></tr>';
  _renderSummary();
}

// ── Target / Stop-Loss modal ─────────────────────────────────────────────────
var _triggerPositionId = null;

function openTriggerModal(id) {
  var p = _positionsData.filter(function (x) { return x.id === id; })[0];
  if (!p) return;
  _triggerPositionId = id;
  document.getElementById('trigger-target').value = p.targetPrice != null ? p.targetPrice : '';
  document.getElementById('trigger-sl').value = p.stopLossPrice != null ? p.stopLossPrice : '';
  var tq = document.getElementById('trigger-target-qty');
  var sq = document.getElementById('trigger-sl-qty');
  tq.value = p.targetQty != null ? p.targetQty : '';
  sq.value = p.stopLossQty != null ? p.stopLossQty : '';
  tq.max = sq.max = p.qty;
  document.getElementById('trigger-modal').classList.remove('hidden');
}

function closeTriggerModal() {
  document.getElementById('trigger-modal').classList.add('hidden');
  _triggerPositionId = null;
}

async function _submitTriggers(target, sl, targetQty, slQty) {
  try {
    await apiPost('/api/positions/' + _triggerPositionId + '/triggers', {
      target_price: target, stop_loss_price: sl,
      target_qty: targetQty, stop_loss_qty: slQty,
    });
    toast('Target/SL updated', 'ok');
    closeTriggerModal();
    loadPositions();
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

// ── Exit (qty-bounded) modal ─────────────────────────────────────────────────
var _exitPositionId = null;

function openExitModal(id, openQty) {
  var p = _allPositionsData.filter(function (x) { return x.id === id; })[0];
  _exitPositionId = id;
  var input = document.getElementById('exit-qty');
  input.value = openQty;
  input.max = openQty;
  document.getElementById('exit-modal-symbol').textContent = p
    ? p.symbol + ' · ' + p.side + ' ' + p.qty
    : '';
  document.getElementById('exit-modal').classList.remove('hidden');
}

function closeExitModal() {
  document.getElementById('exit-modal').classList.add('hidden');
  _exitPositionId = null;
}

async function confirmExit() {
  var qty = Number(document.getElementById('exit-qty').value);
  if (!qty || qty <= 0) { toast('Enter a valid quantity', 'err'); return; }
  try {
    await apiPost('/api/positions/' + _exitPositionId + '/exit', { qty: qty });
    toast('Position exited', 'ok');
    closeExitModal();
    loadPositions();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ── Close All (bulk) modal ───────────────────────────────────────────────────

function openCloseAllModal() {
  var open = _positionsData.filter(function (p) { return p.status === 'OPEN'; });
  var pnl = open.reduce(function (s, p) { return s + p.pnl; }, 0);
  document.getElementById('close-all-summary').innerHTML =
    'Close all <b>' + open.length + '</b> open position' + (open.length === 1 ? '' : 's') +
    ' at the current market price? Combined open P&amp;L: ' +
    '<span class="' + pnlClass(pnl) + '">' + pnlSign(pnl) + '</span>.';
  document.getElementById('close-all-modal').classList.remove('hidden');
}

function closeCloseAllModal() {
  document.getElementById('close-all-modal').classList.add('hidden');
}

async function confirmCloseAll() {
  try {
    var res = await apiPost('/api/positions/close-all');
    toast('Closed ' + res.closed + ' position' + (res.closed === 1 ? '' : 's'), 'ok');
    closeCloseAllModal();
    loadPositions();
  } catch (e) {
    toast(e.message, 'err');
  }
}

window.addEventListener('market:tick', function (evt) {
  if (_positionsFilter !== 'OPEN') return;
  var prices = evt.detail;
  var touched = false;
  _allPositionsData.forEach(function (p) {
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
