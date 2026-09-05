/* ── Ohara · sw.js — app shell cache for offline page loads ──
   This only caches the static HTML/CSS/JS shell so pages can open
   with zero network connection. Chapter/novel data is handled
   separately via IndexedDB in app.js (see saveNovelOffline).
*/

const CACHE_NAME = 'ohara-shell-v4';

const SHELL_FILES = [
  'index.html',
  'novel.html',
  'chapter.html',
  'reader.html',
  'login.html',
  'style.css',
  'app.js',
  'firebase-config.js',
];

// Firebase SDK scripts loaded from Google CDN — must be cached so the
// app shell can boot offline and Firebase Auth can restore its persisted
// session from IndexedDB without needing a network roundtrip.
const CDN_FILES = [
  'https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js',
  'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth-compat.js',
  'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore-compat.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll([...SHELL_FILES, ...CDN_FILES]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // ── CDN files (Firebase SDK) — cache-first ──────────────────
  // Serve from cache immediately; update cache in the background if online.
  if (CDN_FILES.includes(url.href)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const networkFetch = fetch(req)
          .then((res) => {
            if (res.ok) {
              caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
            }
            return res;
          })
          .catch(() => cached);
        return cached || networkFetch;
      })
    );
    return;
  }

  // ── Local shell files — stale-while-revalidate ──────────────
  if (url.origin !== location.origin) return;

  const filename = url.pathname.split('/').pop() || 'index.html';
  if (!SHELL_FILES.includes(filename)) return; // let data requests pass through normally

  // ignoreSearch: true — chapter.html?slug=...&ch=2 must still match
  // the cached 'chapter.html' entry, which has no query string.
  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then((cached) => {
      const networkFetch = fetch(req)
        .then((res) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
          return res;
        })
        .catch(() => cached || new Response(
          '<!DOCTYPE html><html><body style="background:#111010;color:#e9e4da;' +
          'font-family:Georgia,serif;text-align:center;padding:80px 24px">' +
          '<h1>You\u2019re offline</h1>' +
          '<p>This page hasn\u2019t been cached yet. Connect once so Ohara can save it for offline use.</p>' +
          '</body></html>',
          { status: 200, headers: { 'Content-Type': 'text/html' } }
        ));
      // Always resolve to a real Response — never undefined, which Safari rejects.
      return cached || networkFetch;
    })
  );
});
