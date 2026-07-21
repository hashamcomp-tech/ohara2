/* ── Ohara · /api/progress — sync reading history via Vercel Blob ──────
   GET  /api/progress  → returns user's { progress, ratings } from Blob
   POST /api/progress  → merges incoming data with existing Blob, writes back

   Auth: Firebase ID token in Authorization: Bearer <token>
   Storage: Vercel Blob at history/<uid>.json (public, no random suffix)
*/

const { put, list } = require('@vercel/blob');
const admin = require('firebase-admin');

// ── Firebase Admin (singleton) ────────────────────────────────
if (!admin.apps.length) {
  const sa = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT || '{}');
  admin.initializeApp({ credential: admin.credential.cert(sa) });
}

// ── Helpers ───────────────────────────────────────────────────
const corsHeaders = {
  'Access-Control-Allow-Origin': 'https://hashamcomp-tech.github.io',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type',
};

async function verifyToken(req) {
  const hdr = req.headers.authorization || '';
  if (!hdr.startsWith('Bearer ')) throw new Error('Missing token');
  return admin.auth().verifyIdToken(hdr.slice(7));
}

async function readBlob(uid) {
  const { blobs } = await list({ prefix: `history/${uid}.json`, limit: 1 });
  const match = blobs.find(b => b.pathname === `history/${uid}.json`);
  if (!match) return { progress: {}, ratings: {} };
  const res = await fetch(match.url);
  if (!res.ok) return { progress: {}, ratings: {} };
  return res.json();
}

async function writeBlob(uid, data) {
  await put(`history/${uid}.json`, JSON.stringify(data), {
    access: 'public',
    contentType: 'application/json',
    addRandomSuffix: false,
  });
}

// ── Merge logic: latest timestamp wins per novel ─────────────
function mergeData(existing, incoming) {
  const progress = { ...(existing.progress || {}) };
  for (const [slug, entry] of Object.entries(incoming.progress || {})) {
    const cur = progress[slug];
    if (!cur || (entry.timestamp || 0) > (cur.timestamp || 0)) {
      progress[slug] = entry;
    }
  }

  // Ratings: incoming wins on conflict (most recently sent)
  const ratings = { ...(existing.ratings || {}), ...(incoming.ratings || {}) };
  // Remove zero-ratings so they don't clutter storage
  for (const k of Object.keys(ratings)) {
    if (!ratings[k]) delete ratings[k];
  }

  return { progress, ratings };
}

// ── Handler ──────────────────────────────────────────────────
module.exports = async function handler(req, res) {
  // CORS (same-origin in prod, useful for local dev)
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
  if (req.method === 'OPTIONS') return res.status(200).end();

  try {
    const decoded = await verifyToken(req);
    const uid = decoded.uid;

    if (req.method === 'GET') {
      const data = await readBlob(uid);
      return res.status(200).json(data);
    }

    if (req.method === 'POST') {
      const existing = await readBlob(uid);
      const merged = mergeData(existing, req.body || {});
      await writeBlob(uid, merged);
      return res.status(200).json({ ok: true });
    }

    return res.status(405).json({ error: 'Method not allowed' });
  } catch (e) {
    const status = e.code === 'auth/id-token-expired' ? 401 : 401;
    return res.status(status).json({ error: e.message || 'Unauthorized' });
  }
};
