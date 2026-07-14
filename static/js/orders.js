'use strict';

var _ordersFilter = 'ALL';
var _ordersData = [];

function setOrderFilter(f) {
  _ordersFilter = f;
  document.getElementById('filter-all').classList.toggle('active', f === 'ALL');
  document.getElementById('filter-pending').classList.toggle('active', f === 'PENDING');
  document.getElementById('filter-complete').classList.toggle('active', f === 'COMPLETE');
  renderOrders();
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
    '<td data-label="Symbol" class="card-title sym-col">' + escHtml(o.symbol) + '</td>' +
    '<td data-label="Side">' + o.side + '</td>' +
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
  tbody.innerHTML = rows.length
    ? rows.map(_orderRowHtml).join('')
    : '<tr><td colspan="9" class="empty-cell">No orders</td></tr>';
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
