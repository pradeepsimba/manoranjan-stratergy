'use strict';

// ── Scanner: read-only candlestick pattern screens (scanner.html only) ─────────
// Calls GET /api/scanner/run, which evaluates every registered scanner
// (app/services/scanner.py) against the current 5m candle buffer for the
// whole tradable universe. Nothing here places or affects an order — same
// read-only role as the Console reports.

async function runScan() {
  var btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    var res = await apiGet('/api/scanner/run');
    _renderScanner('red',   res.first_3_red_candles   || []);
    _renderScanner('green', res.first_3_green_candles || []);
    _renderMeta(res.meta);
    toast('Scan complete', 'ok');
  } catch (e) {
    toast('Scan failed', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Scan';
  }
}

function _renderMeta(meta) {
  var el = document.getElementById('scan-meta');
  if (!meta) { el.textContent = ''; return; }
  el.textContent = 'Scanned ' + meta.scanned + ' of ' + meta.totalInstruments + ' instruments' +
    (meta.skippedInsufficientCandles ? ' · ' + meta.skippedInsufficientCandles + " skipped (today's 3rd candle hasn't closed yet)" : '');
}

function _renderScanner(key, rows) {
  document.getElementById('count-' + key).textContent = rows.length ? rows.length + ' matches' : '';
  var tbody = document.getElementById(key + '-tbody');
  tbody.innerHTML = rows.length ? rows.map(_scanRowHtml).join('') :
    '<tr><td colspan="3" class="empty-cell">' + emptyStateHtml('No matches', 'Nothing fits this pattern right now', 'search') + '</td></tr>';
}

function _scanRowHtml(r) {
  return '<tr class="scan-row" onclick="_openInTerminal(\'' + r.token + '\')">' +
    '<td data-label="Symbol" class="card-title sym-col"><span class="sym-cell">' + symAvatarHtml(r.name) + '<span class="sym-name">' + escHtml(r.name) + '</span></span></td>' +
    '<td data-label="LTP" class="num-col">' + fmt2(r.ltp) + '</td>' +
    '<td data-label="Matched Candle">' + fmtDT(r.matchedAt) + '</td>' +
  '</tr>';
}

function _openInTerminal(token) {
  location.href = '/?token=' + encodeURIComponent(token);
}

document.addEventListener('DOMContentLoaded', runScan);
