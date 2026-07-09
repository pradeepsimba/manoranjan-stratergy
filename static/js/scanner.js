'use strict';

let scanning = false;

async function triggerScan() {
  if (scanning) return;
  
  const btn = document.getElementById('btn-scan');
  const prevText = btn.innerHTML;
  btn.innerHTML = '<span class="loading-spinner"></span>Scanning...';
  btn.disabled = true;
  scanning = true;
  
  // Set summary cards to loading state
  document.getElementById('stat-scanned').textContent = '...';
  document.getElementById('stat-bullish').textContent = '...';
  document.getElementById('stat-bearish').textContent = '...';
  document.getElementById('stat-time').textContent = '...';

  try {
    const res = await fetch('/api/scanner/scan');
    if (!res.ok) {
      throw new Error(`Server returned ${res.status}: ${res.statusText}`);
    }
    const data = await res.json();
    renderResults(data);
    toast(`Scan completed successfully! Found ${data.length} setups.`, 'ok');
  } catch (err) {
    console.error('Scan failed:', err);
    toast(`Scan failed: ${err.message}`, 'err');
    
    // Reset indicators
    document.getElementById('stat-scanned').textContent = 'Error';
    document.getElementById('stat-bullish').textContent = 'Error';
    document.getElementById('stat-bearish').textContent = 'Error';
    document.getElementById('stat-time').textContent = 'Error';
  } finally {
    btn.innerHTML = prevText;
    btn.disabled = false;
    scanning = false;
  }
}

function renderResults(signals) {
  const bullishBody = document.getElementById('bullish-tbody');
  const bearishBody = document.getElementById('bearish-tbody');
  
  // Clear previous content
  bullishBody.innerHTML = '';
  bearishBody.innerHTML = '';
  
  const bullish = [];
  const bearish = [];
  
  // Separate into bullish and bearish
  signals.forEach(sig => {
    if (sig.type === 'bullish') {
      bullish.push(sig);
    } else if (sig.type === 'bearish') {
      bearish.push(sig);
    }
  });
  
  // Sort descending by Date, then alphabetically by Symbol
  const sortFn = (a, b) => {
    if (b.date !== a.date) {
      return b.date.localeCompare(a.date);
    }
    return a.symbol.localeCompare(b.symbol);
  };
  
  bullish.sort(sortFn);
  bearish.sort(sortFn);
  
  // Render Bullish setups
  if (bullish.length === 0) {
    bullishBody.innerHTML = '<tr><td colspan="6" class="empty-cell">No bullish setups found</td></tr>';
  } else {
    bullish.forEach(sig => {
      const tr = document.createElement('tr');
      // Highlight the row a bit
      tr.className = 'bull-row';
      tr.innerHTML = `
        <td class="col-sym">${escHtml(sig.symbol)}</td>
        <td class="muted">${escHtml(sig.date)}</td>
        <td>${sig.open.toFixed(2)}</td>
        <td class="pos-num" style="font-weight: 600;">${sig.high.toFixed(2)}</td>
        <td>${sig.adr_10.toFixed(2)}</td>
        <td style="color: var(--prime); font-weight: 600;">${sig.adr_high_10.toFixed(2)}</td>
      `;
      bullishBody.appendChild(tr);
    });
  }
  
  // Render Bearish setups
  if (bearish.length === 0) {
    bearishBody.innerHTML = '<tr><td colspan="6" class="empty-cell">No bearish setups found</td></tr>';
  } else {
    bearish.forEach(sig => {
      const tr = document.createElement('tr');
      // Style first column border like .bull-row but negative red
      tr.style.borderLeft = '2px solid var(--neg)';
      tr.innerHTML = `
        <td class="col-sym">${escHtml(sig.symbol)}</td>
        <td class="muted">${escHtml(sig.date)}</td>
        <td>${sig.open.toFixed(2)}</td>
        <td class="neg-num" style="font-weight: 600;">${sig.low.toFixed(2)}</td>
        <td>${sig.adr_10.toFixed(2)}</td>
        <td style="color: var(--accent-2); font-weight: 600;">${sig.adr_low_10.toFixed(2)}</td>
      `;
      bearishBody.appendChild(tr);
    });
  }
  
  // Update badges
  document.getElementById('bullish-count').textContent = bullish.length;
  document.getElementById('bearish-count').textContent = bearish.length;
  
  // Update stats strip
  // To count how many unique stocks we scanned, let's count from the watchlists or standard size
  // Let's call another API or use a reasonable proxy. Actually we can fetch active watchlist size
  // But let's just make a simple call to `/api/status` to get the count, or show it based on API.
  // Better: we can get the unique symbols present in the data by calling /api/status.
  fetch('/api/status')
    .then(r => r.json())
    .then(statusData => {
      document.getElementById('stat-scanned').textContent = statusData.watchlist || '—';
    })
    .catch(() => {
      document.getElementById('stat-scanned').textContent = '—';
    });
    
  document.getElementById('stat-bullish').textContent = bullish.length;
  document.getElementById('stat-bearish').textContent = bearish.length;
  
  const now = new Date();
  const timeStr = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  document.getElementById('stat-time').textContent = timeStr;
}

// Run scan automatically on load
window.addEventListener('DOMContentLoaded', () => {
  triggerScan();
});
