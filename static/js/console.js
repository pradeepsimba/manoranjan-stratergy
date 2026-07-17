'use strict';

// ── Console: reports, tradebook & account journal (console.html only) ──────────
// Read-only views over the same account data the trading pages show, derived
// server-side from orders + positions (see app/api/trading.py). Nothing here
// places or mutates anything — it's a reporting surface.

var _tradebook   = [];
var _tradeFilter = 'ALL';
var _journal     = [];
var _loaded      = { overview: false, tradebook: false, journal: false };
var _journalView   = 'list';
var _calMonth      = null;   // Date (first-of-month currently shown in the calendar)
var _calSelectedDay = null;  // 'YYYY-MM-DD', or null if none picked

// ── Tab switching (lazy-load each view on first visit) ──────────────────────────
function setConsoleTab(tab) {
  ['overview', 'tradebook', 'journal'].forEach(function (t) {
    document.getElementById('view-' + t).style.display = t === tab ? '' : 'none';
    document.getElementById('tab-' + t).classList.toggle('active', t === tab);
  });
  if (!_loaded[tab]) {
    _loaded[tab] = true;
    if (tab === 'overview')  loadOverview();
    if (tab === 'tradebook') loadTradebook();
    if (tab === 'journal')   loadJournal();
  }
}

// ── Overview ────────────────────────────────────────────────────────────────
async function loadOverview() {
  try {
    var s = await apiGet('/api/console/summary');
    _setSignedStat('ov-realized', s.realizedPnl, '₹');
    document.getElementById('ov-realized-sub').textContent =
      s.closedTrades + ' closed · ' + s.wins + 'W / ' + s.losses + 'L';
    _setSignedStat('ov-unrealized', s.unrealizedPnl, '₹');
    document.getElementById('ov-turnover').textContent = '₹' + fmt2(s.turnover);
    document.getElementById('ov-turnover-sub').textContent =
      'Buy ₹' + fmtCompact(s.buyValue) + ' · Sell ₹' + fmtCompact(s.sellValue);
    document.getElementById('ov-trades').textContent = fmtInt(s.totalTrades);
    document.getElementById('ov-trades-sub').textContent =
      s.pending + ' pending · ' + s.rejected + ' rejected';
    document.getElementById('ov-winrate').textContent = s.winRate.toFixed(1) + '%';
    document.getElementById('ov-winrate-sub').textContent = s.closedTrades + ' closed trades';
  } catch (e) { toast('Failed to load summary', 'err'); }

  try {
    var p = await apiGet('/api/console/pnl');
    _renderPnlTable(p.realized || []);
    _renderOverviewHoldings(p.holdings || []);
  } catch (e) { /* leave placeholders */ }
}

function _renderPnlTable(rows) {
  var tbody = document.getElementById('pnl-tbody');
  document.getElementById('pnl-count').textContent = rows.length ? rows.length + ' instruments' : '';
  tbody.innerHTML = rows.length ? rows.map(function (r) {
    return '<tr>' +
      '<td data-label="Symbol" class="card-title sym-col">' + escHtml(r.symbol) + '</td>' +
      '<td data-label="Trades" class="num-col">' + r.trades + '</td>' +
      '<td data-label="Realized P&L" class="num-col ' + pnlClass(r.realizedPnl) + '">₹' + pnlSign(r.realizedPnl) + '</td>' +
    '</tr>';
  }).join('') : '<tr><td colspan="3" class="empty-cell">No closed positions yet</td></tr>';
}

function _renderOverviewHoldings(rows) {
  var tbody = document.getElementById('ov-holdings-tbody');
  tbody.innerHTML = rows.length ? rows.map(function (h) {
    return '<tr>' +
      '<td data-label="Symbol" class="card-title sym-col">' + escHtml(h.symbol) + '</td>' +
      '<td data-label="Qty" class="num-col">' + h.qty + '</td>' +
      '<td data-label="Avg" class="num-col">' + fmt2(h.avgPrice) + '</td>' +
      '<td data-label="LTP" class="num-col">' + fmt2(h.ltp) + '</td>' +
      '<td data-label="P&L" class="num-col ' + pnlClass(h.pnl) + '">₹' + pnlSign(h.pnl) + '</td>' +
    '</tr>';
  }).join('') : '<tr><td colspan="5" class="empty-cell">No holdings</td></tr>';
}

// ── Tradebook ─────────────────────────────────────────────────────────────────
async function loadTradebook() {
  try {
    _tradebook = await apiGet('/api/console/tradebook');
  } catch (e) {
    document.getElementById('tradebook-tbody').innerHTML =
      '<tr><td colspan="8" class="empty-cell">Failed to load tradebook</td></tr>';
    return;
  }
  renderTradebook();
}

function setTradeFilter(f) {
  _tradeFilter = f;
  ['ALL', 'BUY', 'SELL'].forEach(function (x) {
    document.getElementById('tbf-' + x.toLowerCase()).classList.toggle('active', x === f);
  });
  renderTradebook();
}

function _filteredTrades() {
  var q = (document.getElementById('tb-search').value || '').toLowerCase();
  return _tradebook.filter(function (t) {
    if (_tradeFilter !== 'ALL' && t.side !== _tradeFilter) return false;
    if (q && t.symbol.toLowerCase().indexOf(q) === -1) return false;
    return true;
  });
}

function renderTradebook() {
  var tbody = document.getElementById('tradebook-tbody');
  var rows = _filteredTrades();
  tbody.innerHTML = rows.length ? rows.map(function (t) {
    var sideCls = t.side === 'BUY' ? 'side-buy-txt' : 'side-sell-txt';
    return '<tr>' +
      '<td data-label="Time">' + fmtDT(t.filledAt) + '</td>' +
      '<td data-label="Symbol" class="card-title sym-col">' + escHtml(t.symbol) + '</td>' +
      '<td data-label="Side"><span class="' + sideCls + '">' + t.side + '</span></td>' +
      '<td data-label="Product">' + t.product + '</td>' +
      '<td data-label="Type">' + t.orderType + '</td>' +
      '<td data-label="Qty" class="num-col">' + t.qty + '</td>' +
      '<td data-label="Price" class="num-col">' + fmt2(t.price) + '</td>' +
      '<td data-label="Value" class="num-col">₹' + fmt2(t.value) + '</td>' +
    '</tr>';
  }).join('') : '<tr><td colspan="8" class="empty-cell">No executed trades</td></tr>';
}

function exportTradebook() {
  var rows = _filteredTrades();
  if (!rows.length) { toast('Nothing to export', 'warn'); return; }
  var header = ['Time', 'Symbol', 'Token', 'Side', 'Product', 'Type', 'Qty', 'Price', 'Value'];
  var lines = [header.join(',')];
  rows.forEach(function (t) {
    lines.push([
      _csv(fmtDT(t.filledAt)), _csv(t.symbol), _csv(t.token), t.side, t.product,
      t.orderType, t.qty, t.price, t.value,
    ].join(','));
  });
  _downloadCsv(lines.join('\n'), 'alto-tradebook.csv');
  toast('Tradebook exported', 'ok');
}

// ── Journal ─────────────────────────────────────────────────────────────────
async function loadJournal() {
  var wrap = document.getElementById('journal-wrap');
  try {
    var res = await apiGet('/api/journal');
    _journal = res.entries || [];
  } catch (e) {
    wrap.innerHTML = '<div class="empty-cell">Failed to load journal</div>';
    return;
  }
  wrap.innerHTML = _journal.length
    ? _journalHtml(_journal)
    : '<div class="empty-cell">No account activity yet</div>';
  if (_journalView === 'calendar') renderCalendar();
}

function setJournalView(view) {
  _journalView = view;
  document.getElementById('jview-list').classList.toggle('active', view === 'list');
  document.getElementById('jview-calendar').classList.toggle('active', view === 'calendar');
  document.getElementById('journal-wrap').style.display = view === 'list' ? '' : 'none';
  document.getElementById('journal-calendar-wrap').style.display = view === 'calendar' ? '' : 'none';
  if (view === 'calendar') renderCalendar();
}

// ── Journal calendar (client-side grouping of the same /api/journal data) ───

function _pad2(n) { return n < 10 ? '0' + n : '' + n; }

function _dateKey(s) {
  var d = new Date(s);
  if (isNaN(d.getTime())) return null;
  return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate());
}

function _journalByDay() {
  var map = {};
  _journal.forEach(function (e) {
    var key = _dateKey(e.at);
    if (!key) return;
    if (!map[key]) map[key] = { cash: 0, count: 0, entries: [] };
    map[key].cash += e.cashFlow;
    map[key].count += 1;
    map[key].entries.push(e);
  });
  return map;
}

function calShiftMonth(delta) {
  _calMonth.setMonth(_calMonth.getMonth() + delta);
  _calSelectedDay = null;
  renderCalendar();
}

function renderCalendar() {
  if (!_journal.length) {
    document.getElementById('cal-month-label').textContent = '';
    document.getElementById('cal-grid').innerHTML = '<div class="empty-cell" style="grid-column:1/-1">No account activity yet</div>';
    document.getElementById('cal-day-detail').innerHTML = '';
    return;
  }
  if (!_calMonth) {
    var latest = _journal.length ? new Date(_journal[0].at) : new Date();
    _calMonth = isNaN(latest.getTime()) ? new Date() : new Date(latest.getFullYear(), latest.getMonth(), 1);
  }
  var byDay = _journalByDay();
  var y = _calMonth.getFullYear(), m = _calMonth.getMonth();
  document.getElementById('cal-month-label').textContent =
    _calMonth.toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });

  var firstDow     = new Date(y, m, 1).getDay();   // 0=Sun
  var daysInMonth  = new Date(y, m + 1, 0).getDate();
  var todayKey     = _dateKey(new Date().toISOString());
  var dows         = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  var html = dows.map(function (d) { return '<div class="cal-dow">' + d + '</div>'; }).join('');
  for (var i = 0; i < firstDow; i++) html += '<div class="cal-cell empty"></div>';

  for (var day = 1; day <= daysInMonth; day++) {
    var key = y + '-' + _pad2(m + 1) + '-' + _pad2(day);
    var info = byDay[key];
    var classes = 'cal-cell';
    if (info) classes += ' has-data';
    if (key === todayKey) classes += ' today';
    if (key === _calSelectedDay) classes += ' selected';

    var inner = '<div class="cal-day-num">' + day + '</div>';
    if (info) {
      var cashCls = info.cash > 0 ? 'pnl-pos' : (info.cash < 0 ? 'pnl-neg' : 'muted-text');
      var cashTxt = info.cash === 0 ? '—' : (info.cash > 0 ? '+' : '−') + '₹' + fmt2(Math.abs(info.cash));
      inner += '<div class="cal-day-cash ' + cashCls + '">' + cashTxt + '</div>';
      inner += '<div class="cal-day-count">' + info.count + ' trade' + (info.count === 1 ? '' : 's') + '</div>';
    }
    html += '<div class="' + classes + '"' + (info ? ' onclick="selectCalDay(\'' + key + '\')"' : '') + '>' + inner + '</div>';
  }

  document.getElementById('cal-grid').innerHTML = html;
  _renderCalDayDetail(byDay);
}

function selectCalDay(key) {
  _calSelectedDay = _calSelectedDay === key ? null : key;
  renderCalendar();
}

function _renderCalDayDetail(byDay) {
  var wrap = document.getElementById('cal-day-detail');
  var info = _calSelectedDay ? byDay[_calSelectedDay] : null;
  if (!info) { wrap.innerHTML = ''; return; }
  var label = new Date(_calSelectedDay + 'T00:00:00').toLocaleDateString('en-IN',
    { weekday: 'long', day: '2-digit', month: 'short', year: 'numeric' });
  wrap.innerHTML = '<div class="journal-day" style="position:static">' + escHtml(label) + '</div>' +
    info.entries.map(_journalRowHtml).join('');
}

function _journalHtml(entries) {
  var byDay = _journalByDay();
  var out = '';
  var lastDay = null;
  entries.forEach(function (e) {
    var day = _dayLabel(e.at);
    if (day !== lastDay) {
      out += '<div class="journal-day">' + escHtml(day) + _journalDayTotalHtml(byDay[_dateKey(e.at)]) + '</div>';
      lastDay = day;
    }
    out += _journalRowHtml(e);
  });
  return out;
}

// Same net-cash-flow figure the calendar view shows per day (NOT realized
// P&L — a pure-buy day nets negative here without being a "loss").
function _journalDayTotalHtml(info) {
  if (!info) return '';
  var cls = info.cash > 0 ? 'pnl-pos' : (info.cash < 0 ? 'pnl-neg' : 'muted-text');
  var txt = info.cash === 0 ? '—' : (info.cash > 0 ? '+' : '−') + '₹' + fmt2(Math.abs(info.cash));
  return '<span class="journal-day-total ' + cls + '">' + txt + '</span>';
}

function _journalRowHtml(e) {
  var cashCls = e.cashFlow > 0 ? 'pnl-pos' : (e.cashFlow < 0 ? 'pnl-neg' : 'muted-text');
  var cashTxt = e.cashFlow === 0 ? '—' : (e.cashFlow > 0 ? '+' : '−') + '₹' + fmt2(Math.abs(e.cashFlow));
  var iconCls = _journalIconClass(e);
  return '<div class="jrow">' +
    '<div class="jrow-icon ' + iconCls.cls + '">' + iconCls.glyph + '</div>' +
    '<div class="jrow-main">' +
      '<div class="jrow-desc">' + _journalDesc(e) + '</div>' +
      '<div class="jrow-meta">' + fmtTime(e.at) + ' · ' + e.product +
        ' · <span class="badge status-' + e.status + '" style="padding:1px 7px">' + e.status + '</span></div>' +
    '</div>' +
    '<div class="jrow-cash ' + cashCls + '">' + cashTxt + '</div>' +
    '<div class="jrow-bal"><span class="jrow-bal-lbl">Bal</span>₹' + fmt2(e.balance) + '</div>' +
  '</div>';
}

function _journalDesc(e) {
  var verb = e.side === 'BUY' ? 'Bought' : 'Sold';
  if (e.status === 'REJECTED')  verb = 'Rejected ' + (e.side === 'BUY' ? 'buy' : 'sell');
  if (e.status === 'CANCELLED') verb = 'Cancelled ' + (e.side === 'BUY' ? 'buy' : 'sell');
  if (e.status === 'PENDING')   verb = 'Placed ' + (e.side === 'BUY' ? 'buy' : 'sell');
  var priceTxt = e.price != null ? ' @ ₹' + fmt2(e.price) : '';
  return '<b>' + escHtml(e.symbol) + '</b> · ' + verb + ' ' + fmtInt(e.qty) + priceTxt;
}

function _journalIconClass(e) {
  if (e.status === 'REJECTED')  return { cls: 'j-rej',  glyph: '✕' };
  if (e.status === 'CANCELLED') return { cls: 'j-cxl',  glyph: '⊘' };
  if (e.status === 'PENDING')   return { cls: 'j-pend', glyph: '⏳' };
  return e.side === 'BUY' ? { cls: 'j-buy', glyph: '↓' } : { cls: 'j-sell', glyph: '↑' };
}

// ── Small helpers local to this page ────────────────────────────────────────
function _setSignedStat(id, v, prefix) {
  var el = document.getElementById(id);
  el.textContent = (v >= 0 ? '' : '−') + prefix + fmt2(Math.abs(v));
  el.className = 'stat-value ' + pnlClass(v);
}

function fmtCompact(n) {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1e7) return (n / 1e7).toFixed(2) + 'Cr';
  if (Math.abs(n) >= 1e5) return (n / 1e5).toFixed(2) + 'L';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return fmt2(n);
}

function fmtTime(s) {
  if (!s) return '—';
  var d = new Date(s);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
}

function _dayLabel(s) {
  if (!s) return 'Unknown date';
  var d = new Date(s);
  if (isNaN(d.getTime())) return 'Unknown date';
  var today = new Date();
  var yest = new Date(); yest.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === yest.toDateString()) return 'Yesterday';
  return d.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
}

function _csv(s) {
  s = String(s == null ? '' : s);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function _downloadCsv(text, filename) {
  var blob = new Blob([text], { type: 'text/csv;charset=utf-8;' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
}

// Refresh whatever tab is active when this user's account changes (a fill,
// cancel, square-off) so the console never goes stale behind a live trade.
window.addEventListener('account:update', function () {
  _loaded = { overview: false, tradebook: false, journal: false };
  var active = document.querySelector('.console-tabs a.active');
  var tab = active ? active.id.replace('tab-', '') : 'overview';
  _loaded[tab] = true;
  if (tab === 'overview')  loadOverview();
  if (tab === 'tradebook') loadTradebook();
  if (tab === 'journal')   loadJournal();
});

document.addEventListener('DOMContentLoaded', function () {
  _loaded.overview = true;
  loadOverview();
});
