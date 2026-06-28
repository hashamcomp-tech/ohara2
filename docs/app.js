/* ── Ohara · app.js — shared utilities ──────────────────────── */

const DATA = './data';

// ── Auth ─────────────────────────────────────────────────────
// Change this password to whatever you want
const OHARA_PASSWORD = 'ohara1234';

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
  // clicking same star clears rating
  const newRating = current === rating ? 0 : rating;
  saveRating(slug, newRating);
  const container = document.getElementById(`stars-${slug}`);
  if (container) container.innerHTML = starsHTML(slug, newRating);
}

// ── Fetch ─────────────────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

function params() {
  return new URLSearchParams(window.location.search);
}

async function loadIndex()          { return fetchJSON(`${DATA}/index.json`); }
async function loadNovelMeta(slug)  { return fetchJSON(`${DATA}/${slug}/meta.json`); }
async function loadChapter(slug, n) { return fetchJSON(`${DATA}/${slug}/chapters/${n}.json`); }

// ── Helpers ───────────────────────────────────────────────────
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
