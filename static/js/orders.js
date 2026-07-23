'use strict';

var _ordersFilter = 'ALL';
var _todayOnly = false;
var _ordersData = [];

function setOrderFilter(f) {
  _ordersFilter = f;
  document.getElementById('filter-all').classList.toggle('active', f === 'ALL');
  document.getElementById('filter-pending').classList.toggle('active', f === 'PENDING');
  document.getElementById('filter-complete').classList.toggle('active', f === 'COMPLETE');
  renderOrders();
}

// Independent of the status tabs above (a "Today Only" toggle, not a 4th tab)
// so it composes with whichever status filter is active — e.g. "today's
// pending orders" — rather than forcing a choice between date and status.
function toggleTodayFilter() {
  _todayOnly = !_todayOnly;
  document.getElementById('filter-today').classList.toggle('active', _todayOnly);
  renderOrders();
}

function _isToday(iso) {
  if (!iso) return false;
  var d = new Date(iso);
  return !isNaN(d.getTime()) && d.toDateString() === new Date().toDateString();
}

function _orderRowHtml(o) {
  var price = o.status === 'COMPLETE' ? fmt2(o.filledPrice)
    : (o.orderType === 'LIMIT' ? fmt2(o.limitPrice) + ' (limit)' : '—');
  var cancelBtn = o.status === 'PENDING'
    ? '<button class="btn-mini danger" onclick="cancelOrder(' + o.id + ')">Cancel</button>'
    : '';
  var statusCell = '<span class="badge status-' + o.status + '">' + o.status + '</span>' +
    (o.status === 'REJECTED' && o.rejectReason ? '<div class="muted-text" style="margin-top:2px">' + escHtml(o.rejectReason) + '</div>' : '');
  return '<tr>' +
    '<td data-label="Time">' + fmtDT(o.createdAt) + '</td>' +
    '<td data-label="Symbol" class="card-title sym-col"><span class="sym-cell">' + symAvatarHtml(o.symbol) + '<span class="sym-name">' + escHtml(o.symbol) + '</span></span></td>' +
    '<td data-label="Side">' + sidePillHtml(o.side) + '</td>' +
    '<td data-label="Qty" class="num-col">' + o.qty + '</td>' +
    '<td data-label="Type">' + o.orderType + '</td>' +
    '<td data-label="Product">' + o.product + '</td>' +
    '<td data-label="Price" class="num-col">' + price + '</td>' +
    '<td data-label="Status">' + statusCell + '</td>' +
    '<td>' + cancelBtn + '</td>' +
  '</tr>';
}

function renderOrders() {
  var tbody = document.getElementById('orders-tbody');
  var rows = _ordersFilter === 'ALL' ? _ordersData : _ordersData.filter(function (o) { return o.status === _ordersFilter; });
  if (_todayOnly) rows = rows.filter(function (o) { return _isToday(o.createdAt); });
  tbody.innerHTML = rows.length
    ? rows.map(_orderRowHtml).join('')
    : '<tr><td colspan="9" class="empty-cell">' + emptyStateHtml(_todayOnly ? 'No orders today' : 'No orders', 'Orders you place from the Terminal will appear here', 'orders') + '</td></tr>';
}

async function loadOrders() {
  try {
    _ordersData = await apiGet('/api/orders');
  } catch (e) {
    document.getElementById('orders-tbody').innerHTML = '<tr><td colspan="9" class="empty-cell">Failed to load orders</td></tr>';
    return;
  }
  renderOrders();
}

async function cancelOrder(id) {
  try {
    await apiDelete('/api/orders/' + id);
    toast('Order cancelled', 'ok');
    loadOrders();
  } catch (e) {
    toast(e.message, 'err');
  }
}

window.addEventListener('account:update', loadOrders);
document.addEventListener('DOMContentLoaded', loadOrders);
