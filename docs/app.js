/* ── Ohara · app.js — shared utilities ────────────────────────────────── */

const DATA = './data';

// ── Vercel Blob cloud fallback ────────────────────────────────────
// When a local fetch fails (e.g. the file lives in Blob storage
// rather than the repo), the loaders below automatically retry the
// request against the Blob base URL stored in docs/data/config.json
// (written automatically by the scraper’s --cloud run).
let _cloudBase = null; // null = not yet fetched, '' = no cloud configured
async function _getCloudBase() {
  if (_cloudBase !== null) return _cloudBase;
  try {
    const res = await fetch('./data/config.json');
    if (!res.ok) { _cloudBase = ''; return ''; }
    const cfg = await res.json();
    _cloudBase = cfg.blobBase || '';
  } catch { _cloudBase = ''; }
  return _cloudBase;
}

async function _fetchWithCloudFallback(localPath, blobRelPath) {
  try {
    return await fetchJSON(localPath);
  } catch {
    const base = await _getCloudBase();
    if (!base) throw new Error(`Not found and no cloud configured: ${localPath}`);
    return fetchJSON(`${base}/${blobRelPath}`);
  }
}

// ── Auth ─────────────────────────────────────────────────────
function checkAuth() {
  if (localStorage.getItem('ohara-authed') !== '1') {
    window.location.replace('login.html');
  }
}

function logout() {
  localStorage.removeItem('ohara-authed');
  window.location.href = 'login.html';
}

// ── Progress ─────────────────────────────────────────────────
function saveProgress(slug, num, title) {
  const all = JSON.parse(localStorage.getItem('ohara-progress') || '{}');
  all[slug] = { chapter: num, title, timestamp: Date.now() };
  localStorage.setItem('ohara-progress', JSON.stringify(all));
}

function getProgress(slug) {
  const all = JSON.parse(localStorage.getItem('ohara-progress') || '{}');
  return all[slug] || null;
}

function getAllProgress() {
  return JSON.parse(localStorage.getItem('ohara-progress') || '{}');
}

// ── Ratings ──────────────────────────────────────────────────
function saveRating(slug, rating) {
  const all = JSON.parse(localStorage.getItem('ohara-ratings') || '{}');
  all[slug] = rating;
  localStorage.setItem('ohara-ratings', JSON.stringify(all));
}

function getRating(slug) {
  const all = JSON.parse(localStorage.getItem('ohara-ratings') || '{}');
  return all[slug] || 0;
}

function starsHTML(slug, currentRating) {
  return Array.from({ length: 5 }, (_, i) => {
    const n = i + 1;
    const filled = n <= currentRating;
    return `<span class="star ${filled ? 'filled' : ''}"
      onclick="rateNovel('${slug}', ${n})"
      title="${n} star${n > 1 ? 's' : ''}">&#9733;</span>`;
  }).join('');
}

function rateNovel(slug, rating) {
  const current = getRating(slug);
  const newRating = current === rating ? 0 : rating;
  saveRating(slug, newRating);
  const container = document.getElementById(`stars-${slug}`);
  if (container) container.innerHTML = starsHTML(slug, newRating);
}

// ── Network fetch ────────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

async function networkLoadIndex() {
  let localData = { novels: [] };
  let cloudData = { novels: [] };

  try { localData = await fetchJSON(`${DATA}/index.json`); } catch (e) {}

  const base = await _getCloudBase();
  if (base) {
    try { cloudData = await fetchJSON(`${base}/data/index.json`); } catch (e) {}
  }

  // Merge (cloud takes precedence if a novel exists in both)
  const merged = new Map();
  for (const n of localData.novels || []) merged.set(n.slug, n);
  for (const n of cloudData.novels || []) merged.set(n.slug, n);

  if (merged.size === 0) throw new Error("Could not load index from local or cloud.");

  return { novels: Array.from(merged.values()) };
}
async function networkLoadNovelMeta(slug)  { return _fetchWithCloudFallback(`${DATA}/${slug}/meta.json`, `data/${slug}/meta.json`); }
async function networkLoadChapter(slug, n) { return _fetchWithCloudFallback(`${DATA}/${slug}/chapters/${n}.json`, `data/${slug}/chapters/${n}.json`); }

// ── IndexedDB — offline storage ─────────────────────────────
const OFFLINE_DB_NAME    = 'ohara-offline';
const OFFLINE_DB_VERSION = 1;

function openOharaDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('novels')) {
        db.createObjectStore('novels', { keyPath: 'slug' });
      }
      if (!db.objectStoreNames.contains('chapters')) {
        const store = db.createObjectStore('chapters', { keyPath: 'key' });
        store.createIndex('bySlug', 'slug', { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror   = () => reject(req.error);
  });
}

function idbPut(db, storeName, value) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).put(value);
    tx.oncomplete = () => resolve();
    tx.onerror    = () => reject(tx.error);
  });
}

function idbGet(db, storeName, key) {
  return new Promise((resolve, reject) => {
    const tx  = db.transaction(storeName, 'readonly');
    const req = tx.objectStore(storeName).get(key);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror   = () => reject(req.error);
  });
}

function idbDelete(db, storeName, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    tx.objectStore(storeName).delete(key);
    tx.oncomplete = () => resolve();
    tx.onerror    = () => reject(tx.error);
  });
}

function idbGetAll(db, storeName) {
  return new Promise((resolve, reject) => {
    const tx  = db.transaction(storeName, 'readonly');
    const req = tx.objectStore(storeName).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror   = () => reject(req.error);
  });
}

function idbGetAllByIndex(db, storeName, indexName, value) {
  return new Promise((resolve, reject) => {
    const tx  = db.transaction(storeName, 'readonly');
    const idx = tx.objectStore(storeName).index(indexName);
    const req = idx.getAll(value);
    req.onsuccess = () => resolve(req.result || []);
    req.onerror   = () => reject(req.error);
  });
}

function idbDeleteAllByIndex(db, storeName, indexName, value) {
  return new Promise((resolve, reject) => {
    const tx  = db.transaction(storeName, 'readwrite');
    const idx = tx.objectStore(storeName).index(indexName);
    const req = idx.openCursor(IDBKeyRange.only(value));
    req.onsuccess = e => {
      const cursor = e.target.result;
      if (cursor) { cursor.delete(); cursor.continue(); }
    };
    tx.oncomplete = () => resolve();
    tx.onerror    = () => reject(tx.error);
  });
}

// ── Offline novel management ─────────────────────────────────
async function isNovelOffline(slug) {
  try {
    const db  = await openOharaDB();
    const rec = await idbGet(db, 'novels', slug);
    return !!rec;
  } catch (e) { return false; }
}

async function listOfflineSlugs() {
  try {
    const db  = await openOharaDB();
    const all = await idbGetAll(db, 'novels');
    return all.map(n => n.slug);
  } catch (e) { return []; }
}

async function listOfflineNovels() {
  try {
    const db  = await openOharaDB();
    const all = await idbGetAll(db, 'novels');
    return all.map(n => ({
      slug:          n.slug,
      title:         n.meta.title,
      cover:         n.meta.cover || '',
      tags:          n.meta.tags || [],
      totalChapters: n.meta.totalChapters || (n.meta.chapters || []).length,
      lastUpdated:   n.meta.lastUpdated || '',
    }));
  } catch (e) { return []; }
}

async function getOfflineNovelMeta(slug) {
  const db  = await openOharaDB();
  const rec = await idbGet(db, 'novels', slug);
  return rec ? rec.meta : null;
}

async function getOfflineChapter(slug, num) {
  const db  = await openOharaDB();
  const rec = await idbGet(db, 'chapters', `${slug}:${num}`);
  if (!rec) return null;
  return { num: rec.num, title: rec.title, content: rec.content };
}

async function getOfflineChapterCount(slug) {
  try {
    const db  = await openOharaDB();
    const chs = await idbGetAllByIndex(db, 'chapters', 'bySlug', slug);
    return chs.length;
  } catch (e) { return 0; }
}

async function removeNovelOffline(slug) {
  const db = await openOharaDB();
  await idbDelete(db, 'novels', slug);
  await idbDeleteAllByIndex(db, 'chapters', 'bySlug', slug);
}

/**
 * Silently re-sync every offline-saved novel in the background —
 * fetches fresh meta and downloads only chapters not already stored.
 * Safe to call often since saveNovelOffline() skips existing chapters.
 */
async function autoSyncOfflineLibrary(onNovelDone) {
  if (!navigator.onLine) return;
  const slugs = await listOfflineSlugs();
  for (const slug of slugs) {
    try {
      await saveNovelOffline(slug);
    } catch (e) { /* skip — will retry on next sync */ }
    if (onNovelDone) onNovelDone(slug);
  }
}

/**
 * Download a novel for offline reading.
 * Fetches meta + every chapter not already stored locally.
 * Safe to call again later — it only fetches missing chapters,
 * so this doubles as the "update offline copy" function.
 * progressCb(done, total) is called as chapters download.
 */
async function saveNovelOffline(slug, progressCb) {
  const db   = await openOharaDB();
  const meta = await networkLoadNovelMeta(slug);

  // Fetch and store the cover image as a blob
  if (meta.cover) {
    try {
      const res  = await fetch(meta.cover);
      const blob = await res.blob();
      await idbPut(db, 'novels', { slug, meta, coverBlob: blob, savedAt: Date.now() });
    } catch (e) {
      // Cover fetch failed (network error, CORS, etc.) — store without it
      await idbPut(db, 'novels', { slug, meta, savedAt: Date.now() });
    }
  } else {
    await idbPut(db, 'novels', { slug, meta, savedAt: Date.now() });
  }

  const chapters = meta.chapters || [];
  const existing = await idbGetAllByIndex(db, 'chapters', 'bySlug', slug);
  const haveNums = new Set(existing.map(c => c.num));
  const toFetch  = chapters.filter(c => !haveNums.has(c.num));

  let done = chapters.length - toFetch.length;
  if (progressCb) progressCb(done, chapters.length);

  const BATCH = 6;
  for (let i = 0; i < toFetch.length; i += BATCH) {
    const batch = toFetch.slice(i, i + BATCH);
    await Promise.all(batch.map(async c => {
      try {
        const chData = await networkLoadChapter(slug, c.num);
        await idbPut(db, 'chapters', {
          key: `${slug}:${c.num}`, slug, num: c.num,
          title: chData.title, content: chData.content,
        });
      } catch (e) { /* skip — will retry on next save/update */ }
      done++;
      if (progressCb) progressCb(done, chapters.length);
    }));
  }

  return meta;
}

/**
 * Get the best available cover URL for a novel.
 * Online: returns the remote URL directly.
 * Offline: returns an object URL created from the stored blob,
 *          or the remote URL as fallback if no blob was saved.
 * Call URL.revokeObjectURL() on the returned URL when done if it starts with 'blob:'.
 */
async function getCoverURL(slug, remoteURL) {
  if (navigator.onLine) return remoteURL || '';
  try {
    const db  = await openOharaDB();
    const rec = await idbGet(db, 'novels', slug);
    if (rec && rec.coverBlob) {
      return URL.createObjectURL(rec.coverBlob);
    }
  } catch (e) { /* fall through */ }
  return remoteURL || '';
}

// ── Offline-aware loaders (drop-in replacements) ─────────────
// Every page already calls loadIndex / loadNovelMeta / loadChapter,
// so making these offline-aware gives offline support everywhere
// for free, with zero changes needed on the calling pages.
async function loadIndex() {
  try {
    return await networkLoadIndex();
  } catch (e) {
    const offline = await listOfflineNovels();
    return { novels: offline };
  }
}

async function loadNovelMeta(slug) {
  try {
    return await networkLoadNovelMeta(slug);
  } catch (e) {
    const offlineMeta = await getOfflineNovelMeta(slug);
    if (offlineMeta) return offlineMeta;
    throw e;
  }
}

async function loadChapter(slug, n) {
  try {
    return await networkLoadChapter(slug, n);
  } catch (e) {
    const offlineCh = await getOfflineChapter(slug, n);
    if (offlineCh) return offlineCh;
    throw e;
  }
}

// ── Helpers ───────────────────────────────────────────────────
function params() {
  return new URLSearchParams(window.location.search);
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function go(page, queryObj) {
  const q = new URLSearchParams(queryObj).toString();
  window.location.href = `${page}?${q}`;
}

function showState(id) {
  ['state-loading', 'state-error', 'state-empty', 'content'].forEach(s => {
    const el = document.getElementById(s);
    if (el) el.classList.toggle('hidden', s !== id);
  });
}

function renderContent(content) {
  if (!content) return '<p><em>No content available.</em></p>';
  return content
    .split(/\n\n+/)
    .filter(p => p.trim())
    .map(p => `<p>${esc(p.trim())}</p>`)
    .join('\n');
}

// ── Service worker — caches the app shell so pages can load ──
// even with zero network connection, not just the chapter data.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  });
}
