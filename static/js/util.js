'use strict';

// ── Shared page utilities ──────────────────────────────────────────────────────
// Included by every page BEFORE its page script (index/dashboard.js,
// settings/settings.js). Keep helpers that must behave identically across
// pages here — duplicating them per page lets a fix land on one page and
// miss the others.

// HTML-escape for interpolating untrusted text (stock symbols, setting values)
// into innerHTML templates.
function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
  const areaEl = document.createElementNS(NS, 'path');
  areaEl.setAttribute('class', 'area-fill'); areaEl.setAttribute('d', area);
  areaEl.style.fill = col;   // .style resolves var(); a presentation attr would not
  svg.appendChild(areaEl);
  const lineEl = document.createElementNS(NS, 'path');
  lineEl.setAttribute('class', 'line-path'); lineEl.setAttribute('d', d);
  lineEl.style.stroke = col;
  svg.appendChild(lineEl);

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
