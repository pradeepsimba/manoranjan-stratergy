'use strict';

// ── Watchlist panel + chart (index.html only) ──────────────────────────────────
var _watchlistData = [];   // [{token, name, ltp, change, changePct, dayOpen}]
var _selectedToken = null;

async function loadWatchlist() {
  try {
    _watchlistData = await apiGet('/api/instruments');
    _watchlistData.forEach(function (r) {
      // Reconstruct the day-open reference so future ticks can recompute
      // change/changePct locally instead of re-fetching on every tick.
      r.dayOpen = r.ltp > 0 ? r.ltp - r.change : 0;
    });
  } catch (e) {
    document.getElementById('watchlist-list').innerHTML =
      '<div class="muted-text" style="padding:20px;text-align:center">Failed to load instruments</div>';
    return;
  }
  renderWatchlist();
  if (_watchlistData.length && !_selectedToken) selectInstrument(_watchlistData[0].token);
}

function renderWatchlist() {
  var q = (document.getElementById('wl-search').value || '').toLowerCase();
  var list = document.getElementById('watchlist-list');
  var rows = _watchlistData.filter(function (r) { return r.name.toLowerCase().indexOf(q) !== -1; });
  if (!rows.length) {
    list.innerHTML = '<div class="muted-text" style="padding:20px;text-align:center">No matches</div>';
    return;
  }
  list.innerHTML = rows.map(_watchlistRowHtml).join('');
}

function _watchlistRowHtml(r) {
  var chgCls = r.change > 0 ? 'pnl-pos' : (r.change < 0 ? 'pnl-neg' : '');
  var activeCls = r.token === _selectedToken ? ' active' : '';
  return '<div class="watchlist-row' + activeCls + '" data-token="' + r.token + '" onclick="selectInstrument(\'' + r.token + '\')">' +
    '<div class="wl-left">' +
      '<span class="wl-dot ' + chgCls + '" data-field="dot"></span>' +
      '<div><div class="wl-name">' + escHtml(r.name) + '</div><div class="wl-token">#' + escHtml(r.token) + '</div></div>' +
    '</div>' +
    '<div class="wl-right">' +
      '<div class="wl-ltp" data-field="ltp">' + fmt2(r.ltp) + '</div>' +
      '<div class="wl-chg" data-field="chg"><span class="chg-chip ' + chgCls + '">' + pnlSign(r.change) + ' (' + r.changePct.toFixed(2) + '%)</span></div>' +
    '</div>' +
  '</div>';
}

function filterWatchlist() { renderWatchlist(); }

function selectInstrument(token) {
  _selectedToken = token;
  renderWatchlist();
  var inst = _watchlistData.find(function (r) { return r.token === token; });
  if (!inst) return;
  updateSymbolHeader(inst);
  loadChart(token);
  if (typeof onInstrumentSelected === 'function') onInstrumentSelected(inst);
}

function updateSymbolHeader(inst) {
  document.getElementById('sym-name').textContent = inst.name;
  document.getElementById('sym-token').textContent = '#' + inst.token;
  document.getElementById('sym-price').textContent = fmt2(inst.ltp);
  var chEl = document.getElementById('sym-change');
  var chgCls = inst.change > 0 ? 'pnl-pos' : (inst.change < 0 ? 'pnl-neg' : '');
  chEl.innerHTML = '<span class="chg-chip ' + chgCls + '">' + pnlSign(inst.change) + ' (' + inst.changePct.toFixed(2) + '%)</span>';
}

async function loadChart(token) {
  var container = document.getElementById('price-chart');
  var ohlcRow = document.getElementById('ohlc-row');
  container.classList.add('is-loading');
  container.innerHTML = '<div class="skeleton skeleton-row w-80"></div><div class="skeleton skeleton-row w-60"></div>';
  if (ohlcRow) ohlcRow.innerHTML = '';
  try {
    var candles = await apiGet('/api/instruments/' + token + '/candles?limit=100');
    if (!candles.length) {
      container.innerHTML = '<div class="muted-text" style="padding:24px;text-align:center">No chart data yet</div>';
      return;
    }
    var points = candles.map(function (c) { return [c.startTime, c.close]; });
    lineChart(container, points, { fmt: function (v) { return '₹' + fmt2(v); } });
    if (ohlcRow) ohlcRow.innerHTML = _ohlcRowHtml(candles);
  } catch (e) {
    container.innerHTML = '<div class="muted-text" style="padding:24px;text-align:center">Failed to load chart</div>';
  } finally {
    requestAnimationFrame(function () { container.classList.remove('is-loading'); });
  }
}

function _ohlcRowHtml(candles) {
  var open = candles[0].open;
  var high = Math.max.apply(null, candles.map(function (c) { return c.high; }));
  var low = Math.min.apply(null, candles.map(function (c) { return c.low; }));
  var vol = candles.reduce(function (s, c) { return s + (c.volume || 0); }, 0);
  var items = [['Open', fmt2(open)], ['High', fmt2(high)], ['Low', fmt2(low)], ['Volume', fmtInt(vol)]];
  return items.map(function (it) {
    return '<div class="ohlc-item"><span class="ohlc-lbl">' + it[0] + '</span><span class="ohlc-val">' + it[1] + '</span></div>';
  }).join('');
}

// Live LTP patch on tick — patches just the changed cells, no full re-render
// (the same "patch one cell by lookup, skip the full re-render" technique
// the old breakout.js used for its stock-candle table).
window.addEventListener('market:tick', function (evt) {
  var prices = evt.detail;
  var touchedSelected = false;
  _watchlistData.forEach(function (r) {
    if (!Object.prototype.hasOwnProperty.call(prices, r.token)) return;
    r.ltp = prices[r.token];
    if (r.dayOpen > 0) {
      r.change = r.ltp - r.dayOpen;
      r.changePct = r.change / r.dayOpen * 100;
    }
    var row = document.querySelector('.watchlist-row[data-token="' + r.token + '"]');
    if (row) {
      var ltpEl = row.querySelector('[data-field="ltp"]');
      var chgEl = row.querySelector('[data-field="chg"]');
      var dotEl = row.querySelector('[data-field="dot"]');
      var chgCls = r.change > 0 ? 'pnl-pos' : (r.change < 0 ? 'pnl-neg' : '');
      if (ltpEl) ltpEl.textContent = fmt2(r.ltp);
      if (chgEl) {
        chgEl.innerHTML = '<span class="chg-chip ' + chgCls + '">' + pnlSign(r.change) + ' (' + r.changePct.toFixed(2) + '%)</span>';
      }
      if (dotEl) dotEl.className = 'wl-dot ' + chgCls;
    }
    if (r.token === _selectedToken) {
      updateSymbolHeader(r);
      touchedSelected = true;
    }
  });
  if (touchedSelected && typeof onPricesUpdated === 'function') onPricesUpdated();
});

document.addEventListener('DOMContentLoaded', loadWatchlist);
