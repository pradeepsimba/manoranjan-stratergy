'use strict';

// Minimal service worker — exists ONLY to satisfy PWA installability
// (Chromium-based browsers' "Install app" prompt looks for a registered SW
// with a fetch handler). Deliberately does NOT cache anything: this is a
// live trading dashboard where every request (WebSocket handshake, /api/*,
// the dashboard.css/*.js assets whose own freshness is already governed by
// the app's `?v=N` cache-busting query strings — see CLAUDE.md's frontend
// conventions) must always reflect the real current server state. A
// caching service worker on top of that would risk silently serving stale
// data/logic in exactly the app this repo warns hardest against that for.
// Every fetch is passed straight through to the network, untouched.

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
