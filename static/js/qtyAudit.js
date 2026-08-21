'use strict';

// ── Big Trades / qty audit ──────────────────────────────────────────────
// The visible table renders directly from `stockCandles` (server-side bar
// volume + a per-bar "surged" flag — the SAME data + threshold the Entry
// Loop Monitor's VOLUME/SURGE columns use, computed once in scheduler.py)
// via renderBigTradesFromCandles, so the two panels can never disagree.
//
// IndexedDB tick logging below (initStockDB/addStockRecord/
// auditStockQtyStorage/pruneOldStockRecords, a c.html/c1.html port) is kept
// only for the "DB Check" diagnostic button — an independent, browser-local
// record of raw ticks for debugging — it no longer feeds the visible table.

const QTY_AUDIT_LEADER_STOCKS = [
  'HDFC BANK', 'ICICI BANK', 'AXIS BANK',
  'STATE BANK OF INDIA', 'KOTAK BANK', 'INDUSIND BANK',
];
// Nifty 50's 12 leaders (app/config.py's NF_LEADER_STOCKS keys) — the Big
// Trades panel's Nifty 50 counterpart. Kept as a separate constant (not a
// rename of QTY_AUDIT_LEADER_STOCKS above) since dashboard.js's CSV/file
// export still only ever meant to cover BankNifty, matching c.html.
const QTY_AUDIT_LEADER_STOCKS_NF = [
  'HDFC BANK', 'RELIANCE INDUSTRIES', 'ICICI BANK', 'INFOSYS',
  'BHARTI AIRTEL', 'ITC', 'HCL TECHNOLOGIES', 'LARSEN & TOUBRO',
  'KOTAK BANK', 'AXIS BANK', 'STATE BANK OF INDIA', 'HINDUSTAN UNILEVER',
];
// c.html's getIntervalMinutes()/getQtyMultiplier() read a UI interval
// selector this app doesn't have (it's fixed 5m throughout) — hardcoded here.
const QTY_AUDIT_INTERVAL_MIN = 5;

const BIGTRADE_IDS_BN = {
  thead: 'bigtrade-thead', tbody: 'bigtrade-tbody',
  status: 'bigtrade-status', audit: 'bigtrade-audit',
  stocks: QTY_AUDIT_LEADER_STOCKS,
};
const BIGTRADE_IDS_NF = {
  thead: 'bigtrade-thead-nf', tbody: 'bigtrade-tbody-nf',
  status: 'bigtrade-status-nf', audit: 'bigtrade-audit-nf',
  stocks: QTY_AUDIT_LEADER_STOCKS_NF,
};

let _qtyDb = null;

function setBigTradeStatus(message, isFallback, ids) {
  ids = ids || BIGTRADE_IDS_BN;
  const status = document.getElementById(ids.status);
  if (!status) return;
  status.textContent = message;
  status.style.color = isFallback ? 'var(--warn)' : 'var(--accent-2)';
}

function setBigTradeAudit(message, ids) {
  ids = ids || BIGTRADE_IDS_BN;
  const audit = document.getElementById(ids.audit);
  if (audit) audit.innerHTML = message;
}

function initStockDB() {
  const req = indexedDB.open('StockDB', 1);
  req.onupgradeneeded = (e) => {
    const db = e.target.result;
    if (!db.objectStoreNames.contains('stocks')) {
      const store = db.createObjectStore('stocks', { keyPath: 'id', autoIncrement: true });
      store.createIndex('stockname', 'stockname', { unique: false });
      store.createIndex('time', 'time', { unique: false });
    }
  };
  req.onsuccess = (e) => {
    _qtyDb = e.target.result;
    const auditBoth = () => { auditStockQtyStorage(200, BIGTRADE_IDS_BN); auditStockQtyStorage(200, BIGTRADE_IDS_NF); };
    auditBoth();
    setInterval(auditBoth, 15000);
    // The table itself no longer reads from here, but the store still
    // grows unboundedly from recordTickForAudit — keep reclaiming space.
    setInterval(pruneOldStockRecords, 60000);
  };
  req.onerror = () => setBigTradeStatus('DB error', true);
}

function addStockRecord(data) {
  if (!_qtyDb) return;
  const tx = _qtyDb.transaction('stocks', 'readwrite');
  const req = tx.objectStore('stocks').add(data);
  req.onerror = (e) => {
    console.error('addStockRecord FAILED:', e.target.error?.name, e.target.error?.message, data);
    setBigTradeStatus(`DB write failed: ${e.target.error?.name || 'error'}`, true);
  };
  tx.onerror = (e) => {
    console.error('addStockRecord TRANSACTION FAILED:', e.target.error?.name, e.target.error?.message);
  };
}

// Port of c1.html's pruneOldStockRecords — keeps the store from growing
// unbounded (c1.html's own author hit 900MB+ from months of unpruned tick
// logging, which silently broke new writes). A cursor-by-cursor prune on a
// multi-hundred-MB backlog can itself take minutes and block other
// transactions on this store (exactly why Big Trades got stuck on "Loading
// today qty..." in c1.html's own debugging) — so an oversized store is
// wiped outright instead of pruned row by row.
function pruneOldStockRecords(daysToKeep = 2) {
  if (!_qtyDb) return;

  const tx = _qtyDb.transaction('stocks', 'readwrite');
  const store = tx.objectStore('stocks');
  const countReq = store.count();

  countReq.onsuccess = () => {
    const total = countReq.result;
    if (total > 20000) {
      const clearTx = _qtyDb.transaction('stocks', 'readwrite');
      clearTx.objectStore('stocks').clear();
      clearTx.oncomplete = () => console.log(`Cleared oversized stocks store (${total} rows) instead of a slow row-by-row prune.`);
      clearTx.onerror = (e) => console.error('Clear failed:', e.target.error?.name, e.target.error?.message);
      return;
    }

    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - daysToKeep);
    cutoff.setHours(0, 0, 0, 0);
    const cutoffStr = cutoff.toISOString();

    const pruneTx = _qtyDb.transaction('stocks', 'readwrite');
    const index = pruneTx.objectStore('stocks').index('time');
    const range = IDBKeyRange.upperBound(cutoffStr);

    let deleted = 0;
    index.openCursor(range).onsuccess = (ev) => {
      const cursor = ev.target.result;
      if (cursor) {
        cursor.delete();
        deleted++;
        cursor.continue();
      } else if (deleted > 0) {
        console.log(`Pruned ${deleted} stock record(s) older than ${daysToKeep} day(s).`);
      }
    };
    pruneTx.onerror = (e) => console.error('Prune failed:', e.target.error?.name, e.target.error?.message);
  };
  countReq.onerror = (e) => console.error('Count failed:', e.target.error?.name, e.target.error?.message);
}

// Called from dashboard.js on every TICK_UPDATE — logs the UNION of BN's 6
// and NF's 12 leader stocks (c.html only ever had the 6; the NF panel's own
// "DB Check" audit needs its 12 tracked in the same shared IndexedDB store).
const QTY_AUDIT_ALL_LEADER_STOCKS = Array.from(
  new Set(QTY_AUDIT_LEADER_STOCKS.concat(QTY_AUDIT_LEADER_STOCKS_NF))
);

function recordTickForAudit(prices) {
  if (!prices) return;
  const now = new Date().toISOString();
  const qtys = window._lastQtyByStock || {};
  QTY_AUDIT_ALL_LEADER_STOCKS.forEach(name => {
    if (prices[name] === undefined) return;
    const qty = qtys[name] !== undefined ? qtys[name] : 0;
    console.log(`[qty-audit] ${name} LTP=${prices[name]} qty=${qty}`);
    addStockRecord({ stockname: name, time: now, ltp: prices[name], qty });
  });
}

// Port of c.html's auditStockQtyStorage — exact scan/tally/status logic.
function auditStockQtyStorage(limit = 200, ids) {
  ids = ids || BIGTRADE_IDS_BN;
  if (!_qtyDb) {
    setBigTradeStatus('Waiting for DB...', false, ids);
    setBigTradeAudit('IndexedDB is not ready yet.', ids);
    return;
  }

  const trackedStocks = new Set(ids.stocks);
  const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);

  const tx = _qtyDb.transaction('stocks', 'readonly');
  const store = tx.objectStore('stocks');

  let scanned = 0, recentQtyRows = 0, trackedQtyRows = 0, trackedTodayQtyRows = 0;
  const samples = [];

  const req = store.openCursor(null, 'prev');
  req.onsuccess = (event) => {
    const cursor = event.target.result;
    if (cursor && scanned < limit) {
      const row = cursor.value || {};
      const time = row.time;
      const hasQty = row.qty !== null && row.qty !== undefined && row.qty !== '' && !isNaN(row.qty);
      const isTracked = trackedStocks.has(row.stockname);
      const isToday = time ? new Date(time) >= todayStart : false;

      scanned++;
      if (hasQty) recentQtyRows++;
      if (isTracked && hasQty) trackedQtyRows++;
      if (isTracked && isToday && hasQty) trackedTodayQtyRows++;

      if (samples.length < 5 && (isTracked || hasQty)) {
        samples.push({ stockname: row.stockname || 'Unknown', time: time || 'No time', qty: hasQty ? row.qty : 'missing' });
      }

      cursor.continue();
      return;
    }

    if (trackedTodayQtyRows > 0) setBigTradeStatus(`DB OK: ${trackedTodayQtyRows} qty rows today`, false, ids);
    else if (trackedQtyRows > 0) setBigTradeStatus('DB has qty rows, but not today', true, ids);
    else if (recentQtyRows > 0) setBigTradeStatus('Recent rows exist, tracked qty missing', true, ids);
    else setBigTradeStatus('Recent rows have no qty', true, ids);

    const sampleHtml = samples.length
      ? samples.map(s => `${s.stockname} | ${s.time} | Qty: ${s.qty}`).join('<br>')
      : 'No recent stock rows found in the last scan.';
    setBigTradeAudit(
      `Recent scan: ${scanned} rows | Qty rows: ${recentQtyRows} | Tracked qty: ${trackedQtyRows} | Tracked today qty: ${trackedTodayQtyRows}<br>${sampleHtml}`,
      ids
    );
  };
  req.onerror = () => {
    setBigTradeStatus('DB read failed', true, ids);
    setBigTradeAudit('Could not read IndexedDB cursor for StockDB.stocks.', ids);
  };
}

// Renders the Big Trades table straight from stockCandles (server bar
// volume + per-bar "surged" flag, computed in scheduler.py using the exact
// same *_QTY_THRESHOLD_* setting the Entry Loop Monitor's SURGE column
// checks) — one column per leader stock, up to 10 most-recent bars per
// stock, newest first. Called from dashboard.js's render() on every
// STATE_UPDATE (1/sec), same cadence the rest of the dashboard uses.
function renderBigTradesFromCandles(stockCandles, ids) {
  ids = ids || BIGTRADE_IDS_BN;
  const thead = document.getElementById(ids.thead);
  const tbody = document.getElementById(ids.tbody);
  if (!thead || !tbody || !stockCandles) return;

  const stocks = ids.stocks;
  const haveAnyData = stocks.some(s => (stockCandles[s] || []).length);
  if (!haveAnyData) setBigTradeStatus('No qty data yet', true, ids);

  thead.innerHTML = '<tr>' + stocks.map(s => `<th>${s}</th>`).join('') + '</tr>';

  const maxRows = 10;
  const stockRows = {};
  stocks.forEach(stock => {
    stockRows[stock] = (stockCandles[stock] || []).slice().reverse().slice(0, maxRows);
  });

  let html = '';
  for (let i = 0; i < maxRows; i++) {
    html += '<tr>';
    stocks.forEach(stock => {
      const bar = stockRows[stock][i];
      if (!bar) { html += '<td>–</td>'; return; }

      const time = bar.startTime ? bar.startTime.substring(11, 16) : '';
      let signalColor = '';
      if (bar.surged && bar.close != null && bar.open != null) {
        signalColor = bar.close > bar.open ? 'var(--pos)' : bar.close < bar.open ? 'var(--neg)' : '';
      }

      html += `<td style="text-align:center;background-color:${signalColor || 'transparent'};font-weight:bold;">
        ${time}<br><span style="color:var(--txt)">(${Number(bar.volume || 0).toLocaleString('en-IN')})</span>
      </td>`;
    });
    html += '</tr>';
  }
  tbody.innerHTML = html;
  if (haveAnyData) {
    setBigTradeStatus(`Live (${stocks.filter(s => (stockCandles[s] || []).length).length}/${stocks.length} stocks)`, false, ids);
  }
}

initStockDB();
