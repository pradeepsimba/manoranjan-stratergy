'use strict';

// ── Qty audit (background tick logging only) ────────────────────────────
// 2026-09-22: the "Big Trades" visible table and its "DB Check" diagnostic
// button were removed from index.html in an earlier, unrelated dashboard-
// simplification pass — this file's DOM-writing functions
// (setBigTradeStatus/setBigTradeAudit/auditStockQtyStorage/
// renderBigTradesFromCandles) and their BIGTRADE_IDS_BN/NF/
// QTY_AUDIT_INTERVAL_MIN config were confirmed dead (zero matching element
// ids anywhere in the HTML) and removed. What remains is genuinely still
// live: recordTickForAudit logs every tick into IndexedDB (a c.html/c1.html
// port), independent of any current UI, and pruneOldStockRecords keeps that
// store from growing unbounded — kept in case this data is read/exported by
// something else later.

const QTY_AUDIT_LEADER_STOCKS = [
  'HDFC BANK', 'ICICI BANK', 'AXIS BANK',
  'STATE BANK OF INDIA', 'KOTAK BANK', 'INDUSIND BANK',
];
// Nifty 50's 12 leaders (app/config.py's NF_LEADER_STOCKS keys) — folded
// into QTY_AUDIT_ALL_LEADER_STOCKS below so recordTickForAudit logs both.
const QTY_AUDIT_LEADER_STOCKS_NF = [
  'HDFC BANK', 'RELIANCE INDUSTRIES', 'ICICI BANK', 'INFOSYS',
  'BHARTI AIRTEL', 'ITC', 'HCL TECHNOLOGIES', 'LARSEN & TOUBRO',
  'KOTAK BANK', 'AXIS BANK', 'STATE BANK OF INDIA', 'HINDUSTAN UNILEVER',
];

let _qtyDb = null;

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
    // The store still grows unboundedly from recordTickForAudit — keep reclaiming space.
    setInterval(pruneOldStockRecords, 60000);
  };
  req.onerror = () => console.error('qty-audit: StockDB open failed');
}

function addStockRecord(data) {
  if (!_qtyDb) return;
  const tx = _qtyDb.transaction('stocks', 'readwrite');
  const req = tx.objectStore('stocks').add(data);
  req.onerror = (e) => {
    console.error('addStockRecord FAILED:', e.target.error?.name, e.target.error?.message, data);
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
    // console.log removed (found in review, 2026-09-23) — this fired once
    // per leader stock on every ~100ms TICK_UPDATE (up to ~170 calls/sec)
    // for a debug trace with no current UI consumer, left over from before
    // this feature's visible panel was removed; addStockRecord below is the
    // actual audit write and is unaffected.
    addStockRecord({ stockname: name, time: now, ltp: prices[name], qty });
  });
}

initStockDB();
