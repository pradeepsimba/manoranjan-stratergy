'use strict';

// ── Order ticket + this page's mini order/position tables (index.html only) ───
var _side      = 'BUY';
var _product   = 'CNC';
var _orderType = 'MARKET';

function setSide(s) {
  _side = s;
  document.getElementById('side-buy').classList.toggle('active', s === 'BUY');
  document.getElementById('side-sell').classList.toggle('active', s === 'SELL');
  updateTicketSummary();
}

function setProduct(p) {
  _product = p;
  document.getElementById('prod-cnc').classList.toggle('active', p === 'CNC');
  document.getElementById('prod-mis').classList.toggle('active', p === 'MIS');
}

function setOrderType(t) {
  _orderType = t;
  document.getElementById('type-market').classList.toggle('active', t === 'MARKET');
  document.getElementById('type-limit').classList.toggle('active', t === 'LIMIT');
  document.getElementById('limit-price-field').style.display = t === 'LIMIT' ? 'block' : 'none';
  updateTicketSummary();
}

function onInstrumentSelected(inst) {
  document.getElementById('ticket-symbol').value = inst.name + ' (#' + inst.token + ')';
  document.getElementById('ticket-submit').disabled = false;
  var priceEl = document.getElementById('ticket-price');
  if (!priceEl.value) priceEl.value = inst.ltp ? inst.ltp.toFixed(2) : '';
  updateTicketSummary();
}

function onPricesUpdated() { updateTicketSummary(); }

function updateTicketSummary() {
  var summary = document.getElementById('ticket-summary');
  var btn = document.getElementById('ticket-submit');
  var inst = _watchlistData.find(function (r) { return r.token === _selectedToken; });
  if (!inst) {
    summary.textContent = 'Select an instrument to see estimated order value.';
    return;
  }
  var qty = parseInt(document.getElementById('ticket-qty').value, 10) || 0;
  var price = _orderType === 'LIMIT'
    ? (parseFloat(document.getElementById('ticket-price').value) || 0)
    : inst.ltp;
  var value = qty * price;
  summary.innerHTML = 'Est. order value: <b>₹' + fmt2(value) + '</b> &middot; ' + qty + ' &times; ₹' + fmt2(price);
  btn.textContent = _side + ' ' + inst.name;
}

async function submitOrder(evt) {
  evt.preventDefault();
  var statusEl = document.getElementById('ticket-status');
  statusEl.textContent = '';
  if (!_selectedToken) return false;

  var qty = parseInt(document.getElementById('ticket-qty').value, 10);
  if (!qty || qty <= 0) {
    statusEl.textContent = 'Enter a valid quantity.';
    statusEl.className = 'ticket-status pnl-neg';
    return false;
  }
  var limitPrice = null;
  if (_orderType === 'LIMIT') {
    limitPrice = parseFloat(document.getElementById('ticket-price').value);
    if (!limitPrice || limitPrice <= 0) {
      statusEl.textContent = 'Enter a valid limit price.';
      statusEl.className = 'ticket-status pnl-neg';
      return false;
    }
  }

  var btn = document.getElementById('ticket-submit');
  btn.disabled = true;
  try {
    var res = await apiPost('/api/orders', {
      token: _selectedToken, side: _side, order_type: _orderType, product: _product,
      qty: qty, limit_price: limitPrice,
    });
    var order = res.order;
    if (order.status === 'REJECTED') {
      statusEl.textContent = 'Rejected: ' + order.rejectReason;
      statusEl.className = 'ticket-status pnl-neg';
      toast('Order rejected: ' + order.rejectReason, 'err');
    } else if (order.status === 'COMPLETE') {
      statusEl.textContent = 'Filled at ₹' + fmt2(order.filledPrice) + '.';
      statusEl.className = 'ticket-status pnl-pos';
      toast(_side + ' order filled — ' + qty + ' × ' + order.symbol, 'ok');
    } else {
      statusEl.textContent = 'Order placed — resting as ' + order.status + '.';
      statusEl.className = 'ticket-status';
      toast('Limit order placed', 'ok');
    }
    loadRecentOrders();
    loadQuickPositions();
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'ticket-status pnl-neg';
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
  return false;
}

function _orderRowHtml(o) {
  var typeLabel = o.orderType + (o.orderType === 'LIMIT' && o.limitPrice ? ' @ ' + fmt2(o.limitPrice) : '');
  return '<tr>' +
    '<td data-label="Time">' + fmtDT(o.createdAt) + '</td>' +
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(o.symbol) + '</td>' +
    '<td data-label="Side">' + o.side + '</td>' +
    '<td data-label="Qty" class="num-col">' + o.qty + '</td>' +
    '<td data-label="Type">' + escHtml(typeLabel) + '</td>' +
    '<td data-label="Status"><span class="badge status-' + o.status + '">' + o.status + '</span></td>' +
  '</tr>';
}

async function loadRecentOrders() {
  var tbody = document.getElementById('recent-orders-tbody');
  try {
    var orders = await apiGet('/api/orders');
    tbody.innerHTML = orders.length
      ? orders.slice(0, 8).map(_orderRowHtml).join('')
      : '<tr><td colspan="6" class="empty-cell">No orders yet</td></tr>';
  } catch (e) { /* leave previous content on transient failure */ }
}

function _positionRowHtml(p) {
  return '<tr>' +
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(p.symbol) + '</td>' +
    '<td data-label="Side">' + p.side + '</td>' +
    '<td data-label="Qty" class="num-col">' + p.qty + '</td>' +
    '<td data-label="P&amp;L" class="num-col ' + pnlClass(p.pnl) + '">' + pnlSign(p.pnl) + '</td>' +
  '</tr>';
}

async function loadQuickPositions() {
  var tbody = document.getElementById('quick-positions-tbody');
  try {
    var positions = await apiGet('/api/positions?status=OPEN');
    tbody.innerHTML = positions.length
      ? positions.map(_positionRowHtml).join('')
      : '<tr><td colspan="4" class="empty-cell">No open positions</td></tr>';
  } catch (e) { /* leave previous content on transient failure */ }
}

window.addEventListener('account:update', function () {
  loadRecentOrders();
  loadQuickPositions();
});

document.addEventListener('DOMContentLoaded', function () {
  loadRecentOrders();
  loadQuickPositions();
});
