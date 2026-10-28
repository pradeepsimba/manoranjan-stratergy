'use strict';

// P&L Report page (2026-10-20) — date-picker + instrument filter, trades
// shown with the brokerage/STT/txn/GST/SEBI cost model applied on top of
// each trade's real (fee-free) recorded pnl. See /api/pnl-report.

(function () {
  const dateInput = document.getElementById('pnl-date');
  const today = new Date();
  // Default to yesterday — "today" is usually still live/incomplete while
  // the session is running, and this page is for reviewing a closed day.
  const yday = new Date(today);
  yday.setDate(yday.getDate() - 1);
  // Local calendar date, not toISOString() (UTC): between 00:00 and 05:30 IST the UTC date is
  // still the previous day, which preselected a report date two days back.
  dateInput.value = yday.getFullYear() + '-' +
    String(yday.getMonth() + 1).padStart(2, '0') + '-' +
    String(yday.getDate()).padStart(2, '0');
})();

function fmtMoney(n) {
  const sign = n < 0 ? '-' : '';
  return sign + '₹' + Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pnlClass(n) {
  return n > 0 ? 'pnl-pos' : (n < 0 ? 'pnl-neg' : '');
}

async function runPnlReport() {
  const dateVal = document.getElementById('pnl-date').value;
  const instrument = document.getElementById('pnl-instrument').value;
  if (!dateVal) {
    toast('Pick a date first', 'warn');
    return;
  }

  const btn = document.getElementById('pnl-submit');
  btn.disabled = true;
  btn.textContent = 'Loading…';

  try {
    const resp = await fetch('/api/pnl-report?report_date=' + encodeURIComponent(dateVal) +
                              '&instrument=' + encodeURIComponent(instrument));
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    renderPnlReport(data);
  } catch (e) {
    toast('Report failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Submit';
  }
}

function renderPnlReport(data) {
  const summaryEl = document.getElementById('pnl-summary');
  const tradesPanel = document.getElementById('pnl-trades-panel');
  const emptyEl = document.getElementById('pnl-empty');
  const body = document.getElementById('pnl-trades-body');

  if (!data.trades.length) {
    summaryEl.style.display = 'none';
    tradesPanel.style.display = 'none';
    emptyEl.style.display = '';
    return;
  }
  emptyEl.style.display = 'none';
  summaryEl.style.display = '';
  tradesPanel.style.display = '';

  const s = data.summary;
  document.getElementById('pnl-stat-trades').textContent = s.trades;
  document.getElementById('pnl-stat-gross').textContent = fmtMoney(s.grossPnl);
  document.getElementById('pnl-stat-gross').className = 'stat-value ' + pnlClass(s.grossPnl);
  document.getElementById('pnl-stat-fees').textContent = fmtMoney(-s.fees);
  document.getElementById('pnl-stat-net').textContent = fmtMoney(s.netPnl);
  document.getElementById('pnl-stat-net').className = 'stat-value ' + pnlClass(s.netPnl);
  document.getElementById('pnl-stat-winrate').textContent =
    s.winRateNet == null ? '—' : (s.winRateNet * 100).toFixed(0) + '%';

  body.innerHTML = data.trades.map(function (t) {
    return '<tr>' +
      '<td class="card-title">' + escHtml(t.time) + '</td>' +
      '<td data-label="Instrument">' + escHtml(t.instrument) + '</td>' +
      '<td data-label="Dir">' + escHtml(t.direction) + '</td>' +
      '<td data-label="Type">' + escHtml(t.optionType) + '</td>' +
      '<td data-label="Strike">' + escHtml(String(t.strike)) + '</td>' +
      '<td data-label="Entry">' + t.entryPremium.toFixed(2) + '</td>' +
      '<td data-label="Exit">' + t.exitPremium.toFixed(2) + '</td>' +
      '<td data-label="Qty">' + t.quantity + '</td>' +
      '<td data-label="Outcome">' + escHtml(t.outcome || '') + '</td>' +
      '<td data-label="Gross" class="' + pnlClass(t.grossPnl) + '">' + fmtMoney(t.grossPnl) + '</td>' +
      '<td data-label="Fees">' + fmtMoney(-t.fees) + '</td>' +
      '<td data-label="Net" class="' + pnlClass(t.netPnl) + '">' + fmtMoney(t.netPnl) + '</td>' +
      '</tr>';
  }).join('');
}
