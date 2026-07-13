'use strict';

// ── Big Trades / qty audit — port of c.html's initStockDB/addStockRecord/
// auditStockQtyStorage/loadLast10ByIntervalQty/renderLast10Table. Entirely
// browser-local (IndexedDB), fed by this app's existing TICK_UPDATE stream
// (not a second connection to the market-data server, per plan). Note: this
// app's ticks carry only {stockname, ltp} — no per-tick qty field exists in
// this WS protocol (see the qty-gate fidelity note in bn_entry_exit.py), so
// the "qty" column here is always 0 — expected, not a bug.

const QTY_AUDIT_LEADER_STOCKS = [
  'HDFC BANK', 'ICICI BANK', 'AXIS BANK',
  'STATE BANK OF INDIA', 'KOTAK BANK', 'INDUSIND BANK',
];
const QTY_AUDIT_BUCKET_MS = 5 * 60 * 1000;   // matches this app's fixed 5m candle granularity

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
    loadLast10ByIntervalQty();
    auditStockQtyStorage();
    setInterval(loadLast10ByIntervalQty, 3000);
    setInterval(auditStockQtyStorage, 15000);
  };
  req.onerror = () => {
    const el = document.getElementById('bigtrade-audit');
    if (el) el.textContent = 'IndexedDB unavailable in this browser.';
  };
}

function addStockRecord(data) {
  if (!_qtyDb) return;
  const tx = _qtyDb.transaction('stocks', 'readwrite');
  tx.objectStore('stocks').add(data);
}

// Called from dashboard.js on every TICK_UPDATE — logs only the 6 leader
// stocks, matching c.html's scope exactly.
function recordTickForAudit(prices) {
  if (!prices) return;
  const now = new Date().toISOString();
  QTY_AUDIT_LEADER_STOCKS.forEach(name => {
    if (prices[name] === undefined) return;
    addStockRecord({ stockname: name, time: now, ltp: prices[name], qty: 0 });
  });
}

function auditStockQtyStorage(limit = 200) {
  const el = document.getElementById('bigtrade-audit');
  if (!_qtyDb || !el) return;
  const tx = _qtyDb.transaction('stocks', 'readonly');
  const store = tx.objectStore('stocks');
  const rows = [];
  store.openCursor(null, 'prev').onsuccess = (ev) => {
    const cursor = ev.target.result;
    if (cursor && rows.length < limit) {
      rows.push(cursor.value);
      cursor.continue();
    } else {
      if (!rows.length) { el.textContent = 'DB OK — no ticks recorded yet.'; return; }
      const newest = new Date(rows[0].time);
      const staleMs = Date.now() - newest.getTime();
      const stale = staleMs > 60000 ? ` (stale ${Math.round(staleMs / 1000)}s)` : '';
      el.textContent = `DB OK — ${rows.length} sample rows, newest @ ${newest.toLocaleTimeString()}${stale}`;
    }
  };
  store.openCursor(null, 'prev').onerror = () => { el.textContent = 'DB check failed.'; };
}

function loadLast10ByIntervalQty() {
  const body = document.getElementById('bigtrade-tbody');
  if (!_qtyDb || !body) return;
  const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);

  const tx = _qtyDb.transaction('stocks', 'readonly');
  const store = tx.objectStore('stocks');
  const buckets = {};   // `${stock}|${bucketStart}` -> sum

  store.openCursor().onsuccess = (ev) => {
    const cursor = ev.target.result;
    if (cursor) {
      const row = cursor.value;
      const t = new Date(row.time);
      if (t >= todayStart) {
        const bucketStart = Math.floor(t.getTime() / QTY_AUDIT_BUCKET_MS) * QTY_AUDIT_BUCKET_MS;
        const key = `${row.stockname}|${bucketStart}`;
        buckets[key] = (buckets[key] || 0) + (row.qty || 0) + 1;   // +1 per tick as a volume-of-activity proxy
      }
      cursor.continue();
    } else {
      renderLast10Table(buckets);
    }
  };
}

function renderLast10Table(buckets) {
  const body = document.getElementById('bigtrade-tbody');
  if (!body) return;
  const entries = Object.entries(buckets)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10);
  if (!entries.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty-cell">Waiting for ticks…</td></tr>';
    return;
  }
  const avg = entries.reduce((a, [, v]) => a + v, 0) / entries.length;
  body.innerHTML = entries.map(([key, sum]) => {
    const [stock, bucketStart] = key.split('|');
    const label = new Date(Number(bucketStart)).toLocaleTimeString();
    const spike = sum > avg * 1.5;
    return `<tr class="${spike ? 'qty-spike' : ''}"><td>${stock}</td><td>${label}</td><td>${sum}</td></tr>`;
  }).join('');
}

initStockDB();
