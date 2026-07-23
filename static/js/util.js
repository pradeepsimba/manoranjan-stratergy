'use strict';

// ── Shared page utilities ──────────────────────────────────────────────────────
// Included by every page BEFORE its page script. Keep helpers that must
// behave identically across pages here — duplicating them per page lets a
// fix land on one page and miss the others.

// ── JSON fetch wrapper — throws Error(message) on any non-2xx response,
// using the server's {detail} body when present (FastAPI's HTTPException
// shape) so callers can just try/catch and toast the message. ─────────────────
async function apiFetch(url, opts) {
  const res = await fetch(url, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, opts || {}));
  let body = null;
  try { body = await res.json(); } catch (e) { /* no/invalid JSON body */ }
  if (!res.ok) {
    const msg = (body && (body.detail || body.message)) || (res.status + ' ' + res.statusText);
    throw new Error(msg);
  }
  return body;
}

function apiGet(url) { return apiFetch(url); }
function apiPost(url, data) { return apiFetch(url, { method: 'POST', body: JSON.stringify(data || {}) }); }
function apiDelete(url) { return apiFetch(url, { method: 'DELETE' }); }

// ── WebSocket connect-with-reconnect — every page's live channel(s) use this
// same shape (3s backoff, JSON-parsed messages dispatched to onMessage). ────────
function connectWS(path, onMessage, onStatus) {
  let ws = null;
  let reconnectTimer = null;
  let closedByCaller = false;

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(proto + '://' + location.host + path);
    ws.onopen = function () {
      clearTimeout(reconnectTimer);
      if (onStatus) onStatus('Connected');
    };
    ws.onmessage = function (e) {
      try { onMessage(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    ws.onclose = ws.onerror = function () {
      if (onStatus) onStatus('Disconnected');
      if (!closedByCaller) {
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 3000);
      }
    };
  }
  connect();
  return {
    close: function () { closedByCaller = true; clearTimeout(reconnectTimer); if (ws) ws.close(); },
  };
}

// ── Number/date formatters shared across every table/panel ─────────────────────
function fmt2(n) {
  return (Number(n) || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtInt(n) { return (Number(n) || 0).toLocaleString('en-IN'); }
function fmtDT(s) {
  if (!s) return '—';
  const d = new Date(s);
  if (isNaN(d.getTime())) return escHtml(s);
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}
function pnlClass(v) { return v > 0 ? 'pnl-pos' : (v < 0 ? 'pnl-neg' : ''); }
function pnlSign(v) { return (v > 0 ? '+' : '') + fmt2(v); }

// HTML-escape for interpolating untrusted text (stock symbols, setting values)
// into innerHTML templates.
function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Shared visual components (v2 design system) ────────────────────────────────
// Used by every page's row templates so symbols, sides and empty tables look
// identical everywhere. CSS lives in dashboard.css (.sym-avatar/.side-pill/
// .empty-state).

// Deterministic hue from the symbol name — same symbol always gets the same
// avatar color, across pages and reloads, with no stored mapping.
function symHue(name) {
  var h = 0, s = String(name);
  for (var i = 0; i < s.length; i++) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

function symInitials(name) {
  var words = String(name).trim().split(/\s+/);
  var init = (words[0] ? words[0][0] : '') + (words[1] ? words[1][0] : '');
  return init.toUpperCase();
}

function symAvatarHtml(name) {
  return '<span class="sym-avatar" style="--av-h:' + symHue(name) + '">' + escHtml(symInitials(name)) + '</span>';
}

function sidePillHtml(side) {
  return '<span class="side-pill ' + (side === 'BUY' ? 'buy' : 'sell') + '">' + escHtml(side) + '</span>';
}

var _EMPTY_ICONS = {
  chart:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19V10M9 19V5M14 19V13M19 19V8"/></svg>',
  orders:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 3.5h6v3H9z"/><path d="M9 11.5h6M9 15.5h4"/></svg>',
  holdings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="12" rx="2"/><path d="M9 8V6.5A2.5 2.5 0 0 1 11.5 4h1A2.5 2.5 0 0 1 15 6.5V8"/></svg>',
  search:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>',
  inbox:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
};

// emptyStateHtml('No orders', 'Place one from the Terminal', 'orders')
// → icon + title + hint block, meant to sit inside an .empty-cell td/div.
function emptyStateHtml(title, hint, icon) {
  return '<div class="empty-state">' + (_EMPTY_ICONS[icon] || _EMPTY_ICONS.inbox) +
    '<div class="empty-state-title">' + escHtml(title) + '</div>' +
    (hint ? '<div class="empty-state-hint">' + escHtml(hint) + '</div>' : '') +
  '</div>';
}

// Light/dark theme flip — the boot snippet in each page's <head> applies the
// persisted choice before first paint; this toggles and persists it.
function toggleTheme() {
  const root    = document.documentElement;
  const current = root.getAttribute('data-theme') || 'dark';
  const next    = current === 'light' ? 'dark' : 'light';
  root.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── Toasts ─────────────────────────────────────────────────────────────────────
// Non-blocking replacement for alert(). type: 'ok' | 'err' | 'warn' | '' (info).
function toast(msg, type, ms) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    host.className = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.innerHTML = '<span class="toast-msg"></span><button class="toast-x" aria-label="Dismiss">×</button>';
  el.querySelector('.toast-msg').textContent = msg;
  host.appendChild(el);
  requestAnimationFrame(function () { el.classList.add('in'); });

  let timer = null;
  const kill = function () {
    clearTimeout(timer);
    el.classList.add('out');
    el.addEventListener('transitionend', function () { el.remove(); }, { once: true });
    setTimeout(function () { if (el.parentNode) el.remove(); }, 400);
  };
  el.querySelector('.toast-x').addEventListener('click', kill);
  timer = setTimeout(kill, ms || (type === 'err' ? 6000 : 3500));
  return el;
}

// ── SVG line chart (single series, e.g. equity curve) ──────────────────────────
// points: [[label, value], ...]. Renders an area+line with gridlines, a zero
// baseline (when the range crosses zero) and a hover crosshair + tooltip.
// fmt(value) formats the tooltip figure. Returns nothing; paints into `container`.
var _chartGradSeq = 0;

function lineChart(container, points, opts) {
  opts = opts || {};
  const fmt = opts.fmt || function (v) { return String(v); };
  const W = 640, H = opts.height || 170, PL = 46, PR = 12, PT = 12, PB = 20;
  container.innerHTML = '';
  if (!points || points.length === 0) {
    container.innerHTML = '<div class="muted-text" style="padding:24px;text-align:center">No data</div>';
    return;
  }
  const vals = points.map(function (p) { return p[1]; });
  let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const n = points.length;
  const x = function (i) { return PL + (n <= 1 ? 0 : (i / (n - 1)) * (W - PL - PR)); };
  const y = function (v) { return PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB); };

  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'chart');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.style.height = H + 'px';

  // horizontal gridlines + y labels (4 ticks)
  for (let t = 0; t <= 3; t++) {
    const v = lo + (hi - lo) * (t / 3);
    const gy = y(v);
    const gl = document.createElementNS(NS, 'line');
    gl.setAttribute('class', 'grid-line');
    gl.setAttribute('x1', PL); gl.setAttribute('x2', W - PR);
    gl.setAttribute('y1', gy); gl.setAttribute('y2', gy);
    svg.appendChild(gl);
    const lb = document.createElementNS(NS, 'text');
    lb.setAttribute('class', 'axis-lbl');
    lb.setAttribute('x', PL - 6); lb.setAttribute('y', gy + 3);
    lb.setAttribute('text-anchor', 'end');
    lb.textContent = opts.yfmt ? opts.yfmt(v) : Math.round(v);
    svg.appendChild(lb);
  }
  // zero baseline when the series crosses zero
  if (lo < 0 && hi > 0) {
    const zl = document.createElementNS(NS, 'line');
    zl.setAttribute('class', 'zero-line');
    zl.setAttribute('x1', PL); zl.setAttribute('x2', W - PR);
    zl.setAttribute('y1', y(0)); zl.setAttribute('y2', y(0));
    svg.appendChild(zl);
  }

  // area + line — colored by net direction (last vs first)
  const up = vals[n - 1] >= vals[0];
  const col = opts.color || (up ? 'var(--pos)' : 'var(--neg)');
  let d = '', area = '';
  points.forEach(function (p, i) {
    const px = x(i), py = y(p[1]);
    d += (i === 0 ? 'M' : 'L') + px.toFixed(1) + ' ' + py.toFixed(1) + ' ';
  });
  area = d + 'L' + x(n - 1).toFixed(1) + ' ' + (H - PB) + ' L' + x(0).toFixed(1) + ' ' + (H - PB) + ' Z';

  // Top-to-bottom fade instead of a flat tint — reads as depth, not a flat swatch.
  const gradId = 'chartGrad' + (++_chartGradSeq);
  const defs = document.createElementNS(NS, 'defs');
  const grad = document.createElementNS(NS, 'linearGradient');
  grad.setAttribute('id', gradId);
  grad.setAttribute('x1', '0'); grad.setAttribute('y1', '0');
  grad.setAttribute('x2', '0'); grad.setAttribute('y2', '1');
  const stop1 = document.createElementNS(NS, 'stop');
  stop1.setAttribute('offset', '0%'); stop1.style.stopColor = col; stop1.style.stopOpacity = '0.32';
  const stop2 = document.createElementNS(NS, 'stop');
  stop2.setAttribute('offset', '100%'); stop2.style.stopColor = col; stop2.style.stopOpacity = '0';
  grad.appendChild(stop1); grad.appendChild(stop2);
  defs.appendChild(grad);
  svg.appendChild(defs);

  const areaEl = document.createElementNS(NS, 'path');
  areaEl.setAttribute('class', 'area-fill'); areaEl.setAttribute('d', area);
  areaEl.style.fill = 'url(#' + gradId + ')';
  areaEl.style.opacity = '1';
  svg.appendChild(areaEl);
  const lineEl = document.createElementNS(NS, 'path');
  lineEl.setAttribute('class', 'line-path'); lineEl.setAttribute('d', d);
  lineEl.style.stroke = col;
  svg.appendChild(lineEl);

  // Persistent last-price marker — dashed line to the right edge + a dot,
  // the classic "current level" readout on a live-price terminal chart.
  const lastX = x(n - 1), lastY = y(vals[n - 1]);
  const lastLine = document.createElementNS(NS, 'line');
  lastLine.setAttribute('class', 'chart-last-line');
  lastLine.setAttribute('x1', PL); lastLine.setAttribute('x2', W - PR);
  lastLine.setAttribute('y1', lastY); lastLine.setAttribute('y2', lastY);
  lastLine.style.stroke = col;
  svg.appendChild(lastLine);
  const lastDot = document.createElementNS(NS, 'circle');
  lastDot.setAttribute('class', 'chart-last-dot');
  lastDot.setAttribute('cx', lastX); lastDot.setAttribute('cy', lastY); lastDot.setAttribute('r', 3.5);
  lastDot.style.fill = col;
  svg.appendChild(lastDot);

  // crosshair + hover dot
  const cross = document.createElementNS(NS, 'line');
  cross.setAttribute('class', 'chart-cross');
  cross.setAttribute('y1', PT); cross.setAttribute('y2', H - PB);
  svg.appendChild(cross);
  const dot = document.createElementNS(NS, 'circle');
  dot.setAttribute('class', 'chart-dot'); dot.setAttribute('r', 4);
  svg.appendChild(dot);

  wrap.appendChild(svg);
  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  wrap.appendChild(tip);
  container.appendChild(wrap);

  svg.addEventListener('mousemove', function (e) {
    const r = svg.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;      // viewBox scales to width
    let i = Math.round(frac * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const px = x(i), py = y(points[i][1]);
    cross.setAttribute('x1', px); cross.setAttribute('x2', px);
    cross.style.opacity = 1;
    dot.setAttribute('cx', px); dot.setAttribute('cy', py);
    dot.style.fill = col; dot.style.opacity = 1;
    tip.style.left = (px / W * 100) + '%';
    tip.style.top  = (py / H * 100) + '%';
    tip.style.opacity = 1;
    tip.innerHTML = '<div>' + fmt(points[i][1]) + '</div>' +
                    (points[i][0] ? '<div class="tip-sub">' + escHtml(points[i][0]) + '</div>' : '');
  });
  svg.addEventListener('mouseleave', function () {
    cross.style.opacity = 0; dot.style.opacity = 0; tip.style.opacity = 0;
  });
}

// ── Outcome breakdown bars (status-colored, always labelled) ───────────────────
// rows: [{label, value, kind}] where kind ∈ 'win'|'loss'|'flat'.
function outcomeBars(container, rows) {
  const total = rows.reduce(function (s, r) { return s + r.value; }, 0) || 1;
  container.innerHTML = rows.map(function (r) {
    const pct = (r.value / total * 100).toFixed(1);
    return '<div class="ob-row">' +
      '<span class="ob-lbl">' + escHtml(r.label) + '</span>' +
      '<span class="ob-track"><span class="ob-fill ' + r.kind + '" style="width:' + pct + '%"></span></span>' +
      '<span class="ob-val">' + r.value + '</span>' +
    '</div>';
  }).join('');
}
