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
  if (!_watchlistData.length) return;
  var wanted = new URLSearchParams(location.search).get('token');
  var initial = wanted && _watchlistData.some(function (r) { return r.token === wanted; })
    ? wanted : _watchlistData[0].token;
  if (!_selectedToken) selectInstrument(initial);
}

function renderWatchlist() {
  var q = (document.getElementById('wl-search').value || '').toLowerCase();
  var list = document.getElementById('watchlist-list');
  var rows = _watchlistData.filter(function (r) { return r.name.toLowerCase().indexOf(q) !== -1; });
  if (!rows.length) {
    list.innerHTML = '<div class="muted-text" style="padding:26px;text-align:center">' + emptyStateHtml('No matches', 'Try a different search term', 'search') + '</div>';
    return;
  }
  list.innerHTML = rows.map(_watchlistRowHtml).join('');
}

function _depthLineHtml(r) {
  if (r.bestBid == null || r.bestAsk == null) return '—';
  return '<span class="depth-bid">B ' + fmt2(r.bestBid) + '×' + r.bestBidQty + '</span>' +
    '&nbsp;&nbsp;<span class="depth-ask">A ' + fmt2(r.bestAsk) + '×' + r.bestAskQty + '</span>';
}

function _watchlistRowHtml(r) {
  var chgCls = r.change > 0 ? 'pnl-pos' : (r.change < 0 ? 'pnl-neg' : '');
  var activeCls = r.token === _selectedToken ? ' active' : '';
  return '<div class="watchlist-row' + activeCls + '" data-token="' + r.token + '" onclick="selectInstrument(\'' + r.token + '\')">' +
    '<div class="wl-left">' +
      symAvatarHtml(r.name) +
      '<div><div class="wl-name">' + escHtml(r.name) +
        (r.assetType === 'INDEX' ? '<span class="wl-index-tag">INDEX</span>' : '') +
        '</div><div class="wl-token">#' + escHtml(r.token) + '</div></div>' +
    '</div>' +
    '<div class="wl-right">' +
      '<div class="wl-ltp" data-field="ltp">' + fmt2(r.ltp) + '</div>' +
      '<div class="wl-chg" data-field="chg"><span class="chg-chip ' + chgCls + '">' + pnlSign(r.change) + ' (' + r.changePct.toFixed(2) + '%)</span></div>' +
      '<div class="wl-depth" data-field="depth">' + _depthLineHtml(r) + '</div>' +
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
  loadDepth(token);
  if (typeof onInstrumentSelected === 'function') onInstrumentSelected(inst);
}

// ── Market depth ("snap") panel for whichever instrument is selected ────────
// Surfaces every field the feed's "snap" string carries: 5-level bid/ask
// book, cumulative buy/sell qty, open interest (+ its day change %), and the
// upper/lower circuit price band. (The feed's separate "quote"/"ltp" fields
// carry no information beyond what's already in "snap" plus the scalar LTP
// this app already tracks, so there's nothing further to parse out of those.)

function _depthStatsHtml(d) {
  var items = [
    ['Buy Qty',  d.buyQty  != null ? fmtInt(d.buyQty)  : '—'],
    ['Sell Qty', d.sellQty != null ? fmtInt(d.sellQty) : '—'],
    ['OI',       d.oi != null ? fmtInt(d.oi) + (d.oiChangePct != null ? ' (' + pnlSign(d.oiChangePct) + '%)' : '') : '—'],
    ['Upper Circuit', d.upperCircuit != null ? fmt2(d.upperCircuit) : '—'],
    ['Lower Circuit', d.lowerCircuit != null ? fmt2(d.lowerCircuit) : '—'],
  ];
  return items.map(function (it) {
    return '<div class="ohlc-item"><span class="ohlc-lbl">' + it[0] + '</span><span class="ohlc-val">' + it[1] + '</span></div>';
  }).join('');
}

function _depthPanelHtml(d) {
  if (!d) return '<div class="depth-empty">No depth data yet</div>';
  var bids = d.bids || [];
  var asks = d.asks || [];
  var statsHtml = '<div class="depth-stats">' + _depthStatsHtml(d) + '</div>';
  if (!bids.length && !asks.length) {
    return statsHtml + '<div class="depth-empty">No order-book levels yet</div>';
  }
  var rows = Math.max(bids.length, asks.length);
  var bidHtml = '<div class="depth-col-hdr">Bids (price × qty)</div>';
  var askHtml = '<div class="depth-col-hdr">Asks (price × qty)</div>';
  for (var i = 0; i < rows; i++) {
    var b = bids[i], a = asks[i];
    bidHtml += '<div class="depth-row bid">' +
      (b ? '<span class="depth-price">' + fmt2(b.price) + '</span><span class="depth-qty">' + b.qty + '</span>' : '') +
    '</div>';
    askHtml += '<div class="depth-row ask">' +
      (a ? '<span class="depth-price">' + fmt2(a.price) + '</span><span class="depth-qty">' + a.qty + '</span>' : '') +
    '</div>';
  }
  return statsHtml + '<div class="depth-grid">' + bidHtml + askHtml + '</div>';
}

async function loadDepth(token) {
  var panel = document.getElementById('depth-panel');
  if (!panel) return;
  try {
    var d = await apiGet('/api/instruments/' + token + '/depth');
    if (token === _selectedToken) panel.innerHTML = _depthPanelHtml(d);
  } catch (e) {
    if (token === _selectedToken) panel.innerHTML = '<div class="depth-empty">Failed to load depth</div>';
  }
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
      var chgCls = r.change > 0 ? 'pnl-pos' : (r.change < 0 ? 'pnl-neg' : '');
      if (ltpEl) ltpEl.textContent = fmt2(r.ltp);
      if (chgEl) {
        chgEl.innerHTML = '<span class="chg-chip ' + chgCls + '">' + pnlSign(r.change) + ' (' + r.changePct.toFixed(2) + '%)</span>';
      }
    }
    if (r.token === _selectedToken) {
      updateSymbolHeader(r);
      touchedSelected = true;
    }
  });
  if (touchedSelected && typeof onPricesUpdated === 'function') onPricesUpdated();
});

// Depth delta carries the FULL book (not just top-of-book) for whichever
// tokens just ticked — cheap enough here since it's only ever a handful of
// symbols per 100ms cycle, not the whole watchlist (see scheduler.py).
window.addEventListener('market:depth', function (evt) {
  var depth = evt.detail || {};
  Object.keys(depth).forEach(function (token) {
    var d = depth[token];
    var r = _watchlistData.find(function (x) { return x.token === token; });
    if (r) {
      var bid = d.bids && d.bids[0], ask = d.asks && d.asks[0];
      r.bestBid = bid ? bid.price : null; r.bestBidQty = bid ? bid.qty : null;
      r.bestAsk = ask ? ask.price : null; r.bestAskQty = ask ? ask.qty : null;
      r.ltpQty = d.ltpQty;
      var cell = document.querySelector('.watchlist-row[data-token="' + token + '"] [data-field="depth"]');
      if (cell) cell.innerHTML = _depthLineHtml(r);
    }
    if (token === _selectedToken) {
      var panel = document.getElementById('depth-panel');
      if (panel) panel.innerHTML = _depthPanelHtml(d);
    }
  });
});

document.addEventListener('DOMContentLoaded', loadWatchlist);
