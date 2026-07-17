'use strict';

// ── Big Trades / qty audit — literal port of c.html's initStockDB/
// addStockRecord/auditStockQtyStorage/loadLast10ByIntervalQty/
// renderLast10Table/getIntervalMinutes/getQtyMultiplier. Entirely
// browser-local (IndexedDB), fed by this app's existing TICK_UPDATE stream
// (not a second connection to the market-data server, per plan) instead of
// c.html's own WS. Note: this app's ticks carry only {stockname, ltp} — no
// per-tick qty field exists in this WS protocol (see the qty-gate fidelity
// note in bn_entry_exit.py), so every row's qty is always 0 — expected, not
// a bug. This means the BUY/SELL threshold-crossing highlight in
// renderLast10Table will never fire against real data here (qty never
// increases), which is the correct, honest behavior given the feed.

const QTY_AUDIT_LEADER_STOCKS = [
  'HDFC BANK', 'ICICI BANK', 'AXIS BANK',
  'STATE BANK OF INDIA', 'KOTAK BANK', 'INDUSIND BANK',
];
// c.html's getIntervalMinutes()/getQtyMultiplier() read a UI interval
// selector this app doesn't have (it's fixed 5m throughout) — hardcoded here.
const QTY_AUDIT_INTERVAL_MIN = 5;

let _qtyDb = null;

function setBigTradeStatus(message, isFallback) {
  const status = document.getElementById('bigtrade-status');
  if (!status) return;
  status.textContent = message;
  status.style.color = isFallback ? 'var(--warn)' : 'var(--accent-2)';
}

function setBigTradeAudit(message) {
  const audit = document.getElementById('bigtrade-audit');
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
    loadLast10ByIntervalQty();
    auditStockQtyStorage();
    setInterval(loadLast10ByIntervalQty, 3000);
    setInterval(auditStockQtyStorage, 15000);
  };
  req.onerror = () => setBigTradeStatus('DB error', true);
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
  const volumes = window._lastVolumeByStock || {};
  QTY_AUDIT_LEADER_STOCKS.forEach(name => {
    if (prices[name] === undefined) return;
    // Real per-tick trade quantity doesn't exist in this feed (TICK_UPDATE
    // only ever carries LTP) — the latest 5m bar's volume is the closest
    // available non-zero proxy, same substitution as the qty-surge gate.
    const qty = volumes[name] !== undefined ? volumes[name] : 0;
    console.log(`[qty-audit] ${name} LTP=${prices[name]} qty(bar volume)=${qty}`);
    addStockRecord({ stockname: name, time: now, ltp: prices[name], qty });
  });
}

// Port of c.html's auditStockQtyStorage — exact scan/tally/status logic.
function auditStockQtyStorage(limit = 200) {
  if (!_qtyDb) {
    setBigTradeStatus('Waiting for DB...');
    setBigTradeAudit('IndexedDB is not ready yet.');
    return;
  }

  const trackedStocks = new Set(QTY_AUDIT_LEADER_STOCKS);
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

    if (trackedTodayQtyRows > 0) setBigTradeStatus(`DB OK: ${trackedTodayQtyRows} qty rows today`);
    else if (trackedQtyRows > 0) setBigTradeStatus('DB has qty rows, but not today', true);
    else if (recentQtyRows > 0) setBigTradeStatus('Recent rows exist, tracked qty missing', true);
    else setBigTradeStatus('Recent rows have no qty', true);

    const sampleHtml = samples.length
      ? samples.map(s => `${s.stockname} | ${s.time} | Qty: ${s.qty}`).join('<br>')
      : 'No recent stock rows found in the last scan.';
    setBigTradeAudit(
      `Recent scan: ${scanned} rows | Qty rows: ${recentQtyRows} | Tracked qty: ${trackedQtyRows} | Tracked today qty: ${trackedTodayQtyRows}<br>${sampleHtml}`
    );
  };
  req.onerror = () => {
    setBigTradeStatus('DB read failed', true);
    setBigTradeAudit('Could not read IndexedDB cursor for StockDB.stocks.');
  };
}

// Port of c.html's loadLast10ByIntervalQty — today-only, bucketed by
// QTY_AUDIT_INTERVAL_MIN, summed qty + latest ltp per bucket per stock.
function loadLast10ByIntervalQty() {
  if (!_qtyDb) { setBigTradeStatus('Waiting for DB...'); return; }

  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  const tx = _qtyDb.transaction('stocks', 'readonly');
  const store = tx.objectStore('stocks');
  const data = {};
  let sawTodayRows = false;

  setBigTradeStatus('Loading today qty...');

  store.openCursor(null, 'prev').onsuccess = (ev) => {
    const cursor = ev.target.result;
    if (cursor) {
      const r = cursor.value;
      const parsedTime = r.time ? new Date(r.time) : null;
      const isToday = parsedTime ? parsedTime >= todayStart : false;

      if (r.stockname && parsedTime && isToday && r.qty !== null && r.qty !== '' && !isNaN(r.qty)) {
        sawTodayRows = true;
        const d = new Date(parsedTime);
        const min = d.getMinutes();
        const bucketMin = Math.floor(min / QTY_AUDIT_INTERVAL_MIN) * QTY_AUDIT_INTERVAL_MIN;
        d.setMinutes(bucketMin, 0, 0);
        const bucketTime = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;

        if (!data[r.stockname]) data[r.stockname] = {};
        if (!data[r.stockname][bucketTime]) data[r.stockname][bucketTime] = { qty: 0, ltp: Number(r.ltp) };
        data[r.stockname][bucketTime].qty += Number(r.qty);
        data[r.stockname][bucketTime].ltp = Number(r.ltp);
      }

      if (sawTodayRows && parsedTime && !isToday) { renderLast10Table(data); return; }
      cursor.continue();
    } else {
      renderLast10Table(data);
    }
  };
}

let latestMinuteQty = {};

// Port of c.html's renderLast10Table — one column per stock, 10 rows by
// recency (per-stock, independently), BUY/SELL threshold-crossing highlight.
function renderLast10Table(data) {
  const thead = document.getElementById('bigtrade-thead');
  const tbody = document.getElementById('bigtrade-tbody');
  if (!thead || !tbody) return;

  // Always all 6 leader stocks as columns (not filtered to "has data yet")
  // — stays visible/stable immediately rather than growing column-by-column
  // as ticks trickle in; stocks with no data yet just render "–" cells.
  const stocks = QTY_AUDIT_LEADER_STOCKS;
  const haveAnyData = stocks.some(s => data[s]);

  if (!haveAnyData) {
    setBigTradeStatus('No qty data yet', true);
  }

  const maxRows = 10;
  const STOCK_THRESHOLDS = {
    'STATE BANK OF INDIA': 1000, 'KOTAK BANK': 1000, 'INDUSIND BANK': 1000,
    'AXIS BANK': 1000, 'ICICI BANK': 1000, 'HDFC BANK': 1000,
  };

  thead.innerHTML = '<tr>' + stocks.map(s => `<th>${s}</th>`).join('') + '</tr>';

  const stockRows = {};
  stocks.forEach(stock => {
    stockRows[stock] = Object.entries(data[stock] || {})
      .map(([time, obj]) => ({ time, qty: obj.qty, ltp: obj.ltp }))
      .sort((a, b) => b.time.localeCompare(a.time))
      .slice(0, 10);
  });

  let html = '';
  for (let i = 0; i < maxRows; i++) {
    html += '<tr>';
    stocks.forEach(stock => {
      const row = stockRows[stock][i];
      if (!row) { html += '<td>–</td>'; return; }

      const threshold = Number(STOCK_THRESHOLDS[stock] || 999999);
      const currRow = stockRows[stock][i];
      const prevRow = stockRows[stock][i + 1];
      if (i === 0) latestMinuteQty[stock] = row.qty;

      let signalColor = '';
      if (currRow && prevRow) {
        const currQty = Number(currRow.qty), prevQty = Number(prevRow.qty);
        const currPrice = Number(currRow.ltp), prevPrice = Number(prevRow.ltp);
        if (currQty > prevQty && currQty >= threshold && currPrice > prevPrice) signalColor = 'var(--pos)';
        else if (currQty > prevQty && currQty >= threshold && currPrice < prevPrice) signalColor = 'var(--neg)';
      }

      html += `<td style="text-align:center;background-color:${signalColor || 'transparent'};font-weight:bold;">
        ${row.time}<br><span style="color:var(--txt)">(${row.qty})</span>
      </td>`;
    });
    html += '</tr>';
  }
  tbody.innerHTML = html;
  if (haveAnyData) {
    setBigTradeStatus(`Live (${stocks.filter(s => data[s]).length}/${stocks.length} stocks)`);
  }
}

initStockDB();
