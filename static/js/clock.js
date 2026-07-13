'use strict';

// ── Analog IST clock — port of c.html's renderFace/nowInIST/updateClock ─────
// Pure client-side, no data dependency at all (matches the source exactly).

(function () {
  function renderFace() {
    const face = document.getElementById('analog-face');
    if (!face) return;
    face.querySelectorAll('.analog-tick, .analog-num').forEach(n => n.remove());

    const small = face.clientWidth < 80;
    const fontSize = small ? 7 : 11;
    const inset = small ? 10 : 18;

    for (let i = 0; i < 60; i++) {
      // Small header clock: only draw the 12 hour ticks, skip minute ticks/numbers.
      if (small && i % 5 !== 0) continue;
      const tick = document.createElement('div');
      tick.className = 'analog-tick' + (i % 5 === 0 ? ' hour' : '');
      tick.style.transform = `translate(-50%,0) rotate(${i * 6}deg)`;
      face.appendChild(tick);

      if (i % 5 === 0 && !small) {
        const num = document.createElement('div');
        num.className = 'analog-num';
        num.style.position = 'absolute';
        num.style.fontSize = fontSize + 'px';
        num.style.color = 'var(--txt-2)';
        const hour = i === 0 ? 12 : i / 5;
        num.textContent = hour;
        const angle = (i * 6) * (Math.PI / 180);
        const radius = (face.clientWidth / 2) - inset;
        const cx = face.clientWidth / 2 + Math.round(Math.sin(angle) * radius);
        const cy = face.clientHeight / 2 - Math.round(Math.cos(angle) * radius);
        num.style.left = cx + 'px';
        num.style.top = cy + 'px';
        num.style.transform = 'translate(-50%,-50%)';
        face.appendChild(num);
      }
    }

    ['hour', 'minute', 'second'].forEach(kind => {
      const hand = document.createElement('div');
      hand.className = `analog-hand ${kind}`;
      hand.id = `analog-hand-${kind}`;
      face.appendChild(hand);
    });
    const cap = document.createElement('div');
    cap.className = 'analog-cap';
    face.appendChild(cap);
  }

  function nowInIST() {
    const nowLocal = new Date();
    const utc = nowLocal.getTime() + (nowLocal.getTimezoneOffset() * 60000);
    const istOffset = 5.5 * 60 * 60 * 1000;
    return new Date(utc + istOffset);
  }

  function updateClock() {
    const hourHand   = document.getElementById('analog-hand-hour');
    const minuteHand = document.getElementById('analog-hand-minute');
    const secondHand = document.getElementById('analog-hand-second');
    if (!hourHand || !minuteHand || !secondHand) return;

    const now = nowInIST();
    const hours = now.getHours(), minutes = now.getMinutes(), seconds = now.getSeconds();
    const ms = now.getMilliseconds();

    const secAngle  = (seconds + ms / 1000) * 6;
    const minAngle  = (minutes + seconds / 60) * 6;
    const hourAngle = ((hours % 12) + minutes / 60 + seconds / 3600) * 30;

    hourHand.style.transform   = `translate(-50%,-100%) rotate(${hourAngle}deg)`;
    minuteHand.style.transform = `translate(-50%,-100%) rotate(${minAngle}deg)`;
    secondHand.style.transform = `translate(-50%,-100%) rotate(${secAngle}deg)`;
  }

  renderFace();
  updateClock();
  setInterval(updateClock, 100);
  window.addEventListener('resize', renderFace);
})();
