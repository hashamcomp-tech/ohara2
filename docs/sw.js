/* ── Ohara · sw.js — app shell cache for offline page loads ──
   This only caches the static HTML/CSS/JS shell so pages can open
   with zero network connection. Chapter/novel data is handled
   separately via IndexedDB in app.js (see saveNovelOffline).
*/

const CACHE_NAME = 'ohara-shell-v1';

const SHELL_FILES = [
  'index.html',
  'novel.html',
  'chapter.html',
  'reader.html',
  'login.html',
  'style.css',
  'app.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_FILES))
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
  if (url.origin !== location.origin) return;

  const filename = url.pathname.split('/').pop() || 'index.html';
  if (!SHELL_FILES.includes(filename)) return; // let data requests pass through normally

  // Stale-while-revalidate: serve from cache instantly, refresh in background
  event.respondWith(
    caches.match(req).then((cached) => {
      const networkFetch = fetch(req)
        .then((res) => {
          caches.open(CACHE_NAME).then((cache) => cache.put(req, res.clone()));
          return res;
        })
        .catch(() => cached);
      return cached || networkFetch;
    })
  );
});
