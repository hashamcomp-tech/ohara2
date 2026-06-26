/* ── Ohara · app.js — shared data utilities ─────────────────── */

const DATA = './data';

/** Fetch JSON, throws on failure */
async function fetchJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

/** Parse query string */
function params() {
  return new URLSearchParams(window.location.search);
}

/** Load docs/data/index.json */
async function loadIndex() {
  return fetchJSON(`${DATA}/index.json`);
}

/** Load docs/data/<slug>/meta.json */
async function loadNovelMeta(slug) {
  return fetchJSON(`${DATA}/${slug}/meta.json`);
}

/** Load docs/data/<slug>/chapters/<n>.json */
async function loadChapter(slug, num) {
  return fetchJSON(`${DATA}/${slug}/chapters/${num}.json`);
}

/** Render a cover <img> or fallback emoji */
function coverHTML(novel, cls = '') {
  if (novel.cover) {
    return `<img src="${novel.cover}" alt="${esc(novel.title)} cover" loading="lazy" onerror="this.parentElement.innerHTML='<span class=cover-fallback>📖</span>'">`;
  }
  return `<span class="cover-fallback">📖</span>`;
}

/** HTML-escape */
function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Nav to a page with query params */
function go(page, queryObj) {
  const q = new URLSearchParams(queryObj).toString();
  window.location.href = `${page}?${q}`;
}

/** Show a state element, hide others */
function showState(id) {
  ['state-loading', 'state-error', 'state-empty', 'content'].forEach(s => {
    const el = document.getElementById(s);
    if (el) el.classList.toggle('hidden', s !== id);
  });
}

/** Render paragraphs from chapter content string */
function renderContent(content) {
  if (!content) return '<p><em>No content available.</em></p>';
  return content
    .split(/\n\n+/)
    .filter(p => p.trim())
    .map(p => `<p>${esc(p.trim())}</p>`)
    .join('\n');
}
