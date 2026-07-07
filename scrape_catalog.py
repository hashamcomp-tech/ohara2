"""

FreeWebNovel — Catalog Scraper  +  Ohara site exporter
=======================================================
Scrapes every novel listed on a sort/genre/search page and turns each into:
  1. EPUB files  →  output/<slug>/
  2. JSON files  →  docs/data/<slug>/   (served by Ohara on GitHub Pages)

Supported listing URL types:
    Sort pages:   https://freewebnovel.com/sort/latest-novel
                  https://freewebnovel.com/sort/most-popular
                  https://freewebnovel.com/sort/completed-novel
                  https://freewebnovel.com/sort/latest-release
    Genre pages:  https://freewebnovel.com/genre/Action
                  https://freewebnovel.com/genre/Fantasy
                  https://freewebnovel.com/genre/Martial+Arts

Usage:
    python scrape_catalog.py
    python scrape_catalog.py --listing https://freewebnovel.com/sort/most-popular
    python scrape_catalog.py --listing https://freewebnovel.com/genre/Fantasy
    python scrape_catalog.py --pages 5          # only first 5 pages of the listing
    python scrape_catalog.py --resume           # skip novels already finished
    python scrape_catalog.py --dry-run          # just print discovered URLs, don't scrape
    python scrape_catalog.py --update           # check all output/ novels for new chapters
    python scrape_catalog.py --retry-failed     # retry chapters that previously errored
    python scrape_catalog.py --no-site          # skip JSON export (epub only)
    python scrape_catalog.py --no-epub          # skip epub, only export JSON for site
"""

import argparse
import json
import os
import re
import sys
import time
import html as html_lib

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; env vars must be set manually
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

# ─────────────────────────── config ───────────────────────────
BASE_URL          = "https://freewebnovel.com"
NOVELFIRE_URL     = "https://novelfire.net"
DEFAULT_LISTING   = f"{BASE_URL}/sort/latest-novel"
CHAPTERS_PER_VOL  = 5000
CHAPTER_WORKERS   = 8       # parallel chapter fetches per novel
CHAPTER_DELAY     = 5       # seconds between chapter requests (be polite)
PAGE_DELAY        = 2       # seconds between listing page fetches
NOVEL_DELAY       = 3       # seconds between novels
PROGRESS_FILE     = "scraped_novels.txt"   # tracks completed novels for --resume
FAILED_FILE       = "failed_chapters.txt"  # tracks chapters that failed after all retries
RETRY_ATTEMPTS    = 3                      # how many times to retry a failed chapter

# Genres to skip by default when scraping listings/updates.
# Add genres here (lowercase) to permanently exclude them without
# needing to type --exclude-genre every time.
# Example: DEFAULT_EXCLUDED_GENRES = {"harem", "smut", "adult"}
DEFAULT_EXCLUDED_GENRES: set[str] = set()
RETRY_BACKOFF     = [10, 30, 60]           # seconds to wait before each retry attempt

# Ohara site output — this folder becomes your GitHub Pages source
SITE_DIR          = "docs"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ──────────────────────────── helpers ──────────────────────────
def normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def clean_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    lines = [l for l in lines if "freewebnovel" not in normalize(l)]
    lines = [l for l in lines if "webnovel" not in normalize(l)]
    lines = [l for l in lines if not (len(l) < 30 and ".com" in normalize(l))]
    merged = []
    for line in lines:
        if not line:
            merged.append("")
            continue
        if (
            merged
            and merged[-1]
            and not merged[-1].endswith((".", "!", "?", ":", '"', "'"))
            and line[0].islower()
        ):
            merged[-1] += " " + line
        else:
            merged.append(line)
    result, prev_blank = [], False
    for line in merged:
        if not line:
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return "\n\n".join(l for l in result if l)


# ─────────────────────────── site JSON export ──────────────────
def _site_chapter_path(slug: str, num: int) -> str:
    return os.path.join(SITE_DIR, "data", slug, "chapters", f"{num}.json")


def export_chapter_json(slug: str, num: int, title: str, content: str) -> None:
    """Write a single chapter to docs/data/<slug>/chapters/<num>.json"""
    path = _site_chapter_path(slug, num)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"num": num, "title": title, "content": content}, f,
                  ensure_ascii=False, separators=(",", ":"))


def export_chapter_html(
    slug: str,
    num: int,
    title: str,
    content: str,
    prev_num: int | None,
    next_num: int | None,
    novel_title: str,
) -> None:
    """
    Write a static HTML chapter page to docs/read/<slug>/<num>.html.
    Works with Safari Reader. Also includes a Merge Next 10 button
    that fetches sibling HTML files and appends their content inline.
    """
    html_dir = os.path.join(SITE_DIR, "read", slug)
    os.makedirs(html_dir, exist_ok=True)

    paragraphs = "\n".join(
        f"    <p>{html_lib.escape(p.strip())}</p>"
        for p in content.split("\n\n") if p.strip()
    )

    root      = "../../"  # docs/read/<slug>/ → docs/
    prev_link = (
        f'<a href="{prev_num}.html" rel="prev">&#8592; Ch {prev_num}</a>'
        if prev_num is not None else '<span></span>'
    )
    next_link = (
        f'<a href="{next_num}.html" rel="next">Ch {next_num} &#8594;</a>'
        if next_num is not None else '<span></span>'
    )
    merge_disabled = 'disabled' if next_num is None else ''
    merge_label    = 'All chapters loaded' if next_num is None else 'Merge Next 10'
    next_num_js    = next_num if next_num is not None else 'null'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html_lib.escape(title)} — {html_lib.escape(novel_title)}</title>
  <style>
    :root {{ --ink:#1a1a1a; --dim:#666; --bg:#fff; --gold:#8a6a1a; --border:#ddd; }}
    @media (prefers-color-scheme:dark) {{
      :root {{ --ink:#e9e4da; --dim:#9a9286; --bg:#111010; --gold:#c9a84c; --border:#2a2825; }}
    }}
    *{{ box-sizing:border-box; margin:0; padding:0; }}
    body{{ background:var(--bg); color:var(--ink); font-family:Georgia,'Times New Roman',serif;
          font-size:1.125rem; line-height:1.85; padding:40px 24px 80px;
          max-width:680px; margin:0 auto; }}
    header{{ margin-bottom:32px; padding-bottom:20px; border-bottom:1px solid var(--border); }}
    header a{{ color:var(--gold); text-decoration:none; font-size:.85rem; }}
    h1{{ font-size:1.35rem; font-weight:700; margin-top:10px; line-height:1.25; }}
    article p{{ margin-bottom:1.4em; text-align:justify; hyphens:auto; }}
    .ch-divider{{ text-align:center; padding:28px 0 16px; color:var(--dim);
                  font-size:.8rem; letter-spacing:.1em; text-transform:uppercase; }}
    .ch-divider::before,.ch-divider::after{{ content:''; display:inline-block;
      width:40px; height:1px; background:var(--border); vertical-align:middle; margin:0 12px; }}
    .ch-title{{ font-size:1.1rem; font-weight:700; color:var(--gold); margin-bottom:16px; }}
    nav{{ display:grid; grid-template-columns:1fr auto 1fr; gap:10px; align-items:center;
          margin-top:48px; padding-top:20px; border-top:1px solid var(--border); font-size:.88rem; }}
    nav a{{ color:var(--gold); text-decoration:none; }}
    nav a:hover{{ text-decoration:underline; }}
    nav .nav-right{{ text-align:right; }}
    nav .nav-mid{{ text-align:center; }}
    .btn-merge{{ background:rgba(138,106,26,.12); border:1px solid var(--gold);
                 color:var(--gold); padding:9px 16px; border-radius:6px; cursor:pointer;
                 font-family:Georgia,serif; font-size:.85rem; font-weight:600;
                 transition:background .15s; white-space:nowrap; }}
    .btn-merge:hover:not(:disabled){{ background:rgba(138,106,26,.25); }}
    .btn-merge:disabled{{ opacity:.35; cursor:not-allowed; }}
    #merge-status{{ text-align:center; padding:12px; font-size:.82rem;
                    color:var(--dim); display:none; }}
  </style>
</head>
<body>
<header>
  <a href="{root}novel.html?slug={slug}">&larr; {html_lib.escape(novel_title)}</a>
  <h1 id="page-title">{html_lib.escape(title)}</h1>
</header>

<div id="chapters">
  <article id="ch-{num}">
{paragraphs}
  </article>
</div>

<div id="merge-status"></div>

<nav>
  <span class="nav-left">{prev_link}</span>
  <span class="nav-mid">
    <button class="btn-merge" id="btn-merge" {merge_disabled}>{merge_label}</button>
  </span>
  <span class="nav-right">{next_link}</span>
</nav>

<script>
  // Chapters available after this one — discovered from meta.json
  var slug       = "{slug}";
  var currentNum = {num};
  var nextNum    = {next_num_js};
  var maxLoaded  = currentNum;
  var allNums    = null;   // loaded lazily from meta.json
  var merging    = false;
  var root       = "{root}";

  async function getNums() {{
    if (allNums) return allNums;
    var res = await fetch(root + "data/" + slug + "/meta.json");
    var meta = await res.json();
    allNums = meta.chapters.map(function(c){{ return c.num; }});
    return allNums;
  }}

  document.getElementById("btn-merge").addEventListener("click", async function() {{
    if (merging) return;
    merging = true;
    var btn    = document.getElementById("btn-merge");
    var status = document.getElementById("merge-status");
    btn.textContent = "Loading\u2026";
    btn.disabled = true;

    var nums   = await getNums();
    var idx    = nums.indexOf(maxLoaded);
    var toLoad = nums.slice(idx + 1, idx + 11);
    if (!toLoad.length) {{ merging = false; return; }}

    status.style.display = "block";
    status.textContent = "Fetching chapters " + toLoad[0] + "\u2013" + toLoad[toLoad.length-1] + "\u2026";

    var container = document.getElementById("chapters");
    var firstNew  = null;

    for (var i = 0; i < toLoad.length; i++) {{
      var n = toLoad[i];
      try {{
        var res  = await fetch(n + ".html");
        var html = await res.text();
        var doc  = new DOMParser().parseFromString(html, "text/html");
        var art  = doc.querySelector("article");
        var h1   = doc.querySelector("h1");
        if (!art) continue;

        // Divider
        var divider = document.createElement("div");
        divider.className = "ch-divider";
        divider.textContent = "Chapter " + n;
        container.appendChild(divider);

        // Chapter title
        if (h1) {{
          var chTitle = document.createElement("div");
          chTitle.className = "ch-title";
          chTitle.textContent = h1.textContent;
          container.appendChild(chTitle);
        }}

        // Content
        var section = document.createElement("article");
        section.id = "ch-" + n;
        section.innerHTML = art.innerHTML;
        container.appendChild(section);

        if (!firstNew) firstNew = divider;
        maxLoaded = n;
      }} catch(e) {{
        console.warn("Failed to load chapter " + n, e);
      }}
    }}

    // Update nav next link
    var nextIdx = nums.indexOf(maxLoaded) + 1;
    var newNext = nextIdx < nums.length ? nums[nextIdx] : null;
    var navRight = document.querySelector(".nav-right");
    if (newNext) {{
      navRight.innerHTML = '<a href="' + newNext + '.html" rel="next">Ch ' + newNext + ' &#8594;</a>';
    }} else {{
      navRight.innerHTML = '<span></span>';
    }}

    status.style.display = "none";
    btn.textContent = newNext ? "\u2295 Merge Next 10" : "\u2713 All loaded";
    btn.disabled = !newNext;
    merging = false;

    if (firstNew) firstNew.scrollIntoView({{ behavior:"smooth", block:"start" }});
  }});
</script>
</body>
</html>"""

    path = os.path.join(html_dir, f"{num}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def upsert_novel_meta(
    slug: str,
    title: str,
    chapters: list[tuple[int, str]],
    cover_url: str = "",
    source_url: str = "",
    tags: list[str] | None = None,
) -> None:
    """
    Merge-write docs/data/<slug>/meta.json.

    `chapters` is a list of (num, title) tuples for the chapters being added.
    Existing chapters not in this batch are preserved.
    """
    meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)

    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        meta = {"slug": slug, "title": title, "chapters": []}

    meta["slug"]        = slug
    meta["title"]       = title
    meta["lastUpdated"] = date.today().isoformat()
    if cover_url:
        meta["cover"] = cover_url
    if source_url:
        meta["source"] = source_url
    if tags:
        meta["tags"] = tags

    existing = {c["num"]: c for c in meta.get("chapters", [])}
    for num, ch_title in chapters:
        existing[num] = {"num": num, "title": ch_title}

    meta["chapters"]      = sorted(existing.values(), key=lambda c: c["num"])
    meta["totalChapters"] = len(meta["chapters"])

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def upsert_site_index(
    slug: str,
    title: str,
    total_chapters: int,
    cover_url: str = "",
    tags: list[str] | None = None,
) -> None:
    """Update docs/data/index.json with this novel's summary entry."""
    index_path = os.path.join(SITE_DIR, "data", "index.json")
    os.makedirs(os.path.dirname(index_path), exist_ok=True)

    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = {"novels": []}

    entry = next((n for n in index["novels"] if n["slug"] == slug), None)
    if entry:
        entry["totalChapters"] = total_chapters
        entry["lastUpdated"]   = date.today().isoformat()
        if cover_url:
            entry["cover"] = cover_url
        if tags:
            entry["tags"] = tags
    else:
        index["novels"].append({
            "slug":          slug,
            "title":         title,
            "cover":         cover_url,
            "tags":          tags or [],
            "totalChapters": total_chapters,
            "lastUpdated":   date.today().isoformat(),
        })

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


# ──────────────────── Vercel Blob cloud upload ────────────────────
#
# Activated via --cloud (local + Blob) or --cloud-only (Blob only).
# Requires: BLOB_READ_WRITE_TOKEN in .env or environment.
#   pip install python-dotenv   (optional, for .env file support)
# ─────────────────────────────────────────────────────────────────
BLOB_TOKEN      = os.getenv("BLOB_READ_WRITE_TOKEN", "")
BLOB_UPLOAD_URL = "https://blob.vercel-storage.com"
BLOB_API_VER    = "7"

_blob_base_url: str | None = None   # cached after first successful upload or config read


def _load_blob_base_url() -> str:
    """
    Discover the public Blob store base URL using three strategies (in order):
      1. Cached in memory
      2. docs/data/config.json on disk  (written by a prior --cloud run)
      3. Derived from the BLOB_READ_WRITE_TOKEN structure
    """
    global _blob_base_url
    if _blob_base_url is not None:
        return _blob_base_url

    config_path = os.path.join(SITE_DIR, "data", "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            base = cfg.get("blobBase", "")
            if base:
                _blob_base_url = base
                return _blob_base_url
        except Exception:
            pass

    if BLOB_TOKEN:
        m = re.match(r"vercel_blob_rw_([A-Za-z0-9]+)_", BLOB_TOKEN)
        if m:
            store_id = m.group(1).lower()
            _blob_base_url = f"https://{store_id}.public.blob.vercel-storage.com"
            return _blob_base_url

    _blob_base_url = ""
    return _blob_base_url


def _cache_blob_url_from_response(public_url: str) -> None:
    """Extract and cache the Blob base URL from an upload response URL."""
    global _blob_base_url
    if not public_url or _blob_base_url:
        return
    # URL looks like: https://xxxx.public.blob.vercel-storage.com/data/slug/...
    for marker in ["/data/", "/read/"]:
        idx = public_url.find(marker)
        if idx != -1:
            _blob_base_url = public_url[:idx]
            _save_blob_config()
            return
    # Generic fallback
    parts = public_url.rsplit("/", 1)
    if len(parts) == 2:
        _blob_base_url = parts[0]
        _save_blob_config()


def _save_blob_config() -> None:
    """Persist the Blob base URL to docs/data/config.json."""
    if not _blob_base_url:
        return
    config_path = os.path.join(SITE_DIR, "data", "config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    try:
        existing: dict = {}
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as f:
                existing = json.load(f)
        existing["blobBase"] = _blob_base_url
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [cloud] Warning: could not save config.json: {e}")


def _blob_headers(content_type: str = "application/json; charset=utf-8") -> dict:
    return {
        "Authorization":       f"Bearer {BLOB_TOKEN}",
        "x-api-version":       BLOB_API_VER,
        "content-type":        content_type,
        "x-add-random-suffix": "false",
    }


def cloud_upload(blob_path: str, data: bytes,
                 content_type: str = "application/json; charset=utf-8") -> str:
    """
    Upload raw bytes to Vercel Blob at the given path.
    Returns the public URL of the uploaded file.
    Raises RuntimeError if BLOB_READ_WRITE_TOKEN is not set.
    """
    if not BLOB_TOKEN:
        raise RuntimeError(
            "BLOB_READ_WRITE_TOKEN is not set.\n"
            "Create a .env file in the project root:\n"
            "  BLOB_READ_WRITE_TOKEN=vercel_blob_rw_...\n"
            "or export it as an environment variable."
        )
    url  = f"{BLOB_UPLOAD_URL}/{blob_path}"
    resp = requests.put(url, data=data, headers=_blob_headers(content_type), timeout=60)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Vercel Blob API error: {resp.status_code} - {resp.text}") from e
    result     = resp.json()
    public_url = result.get("url", "")
    _cache_blob_url_from_response(public_url)
    return public_url


def cloud_upload_json(blob_path: str, obj: dict) -> str:
    """Serialize a dict to compact JSON and upload to Vercel Blob."""
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return cloud_upload(blob_path, data)


def cloud_download_json(blob_path: str) -> dict | None:
    """
    Download a JSON file from Vercel Blob.
    Returns the parsed dict, or None if the file does not exist (404).
    """
    base = _load_blob_base_url()
    if not base:
        return None
    try:
        resp = requests.get(f"{base}/{blob_path}", timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def cloud_export_chapter_json(slug: str, num: int, title: str, content: str) -> None:
    """Upload a single chapter JSON to Vercel Blob."""
    blob_path = f"data/{slug}/chapters/{num}.json"
    try:
        cloud_upload_json(blob_path, {"num": num, "title": title, "content": content})
    except Exception as e:
        print(f"  [cloud] Warning: failed to upload chapter {num}: {e}")


def cloud_upsert_novel_meta(
    slug: str,
    title: str,
    chapters: list[tuple[int, str]],
    cover_url: str = "",
    source_url: str = "",
    tags: list[str] | None = None,
) -> None:
    """
    Merge-write data/<slug>/meta.json to Vercel Blob.
    Downloads the current Blob version first (if available) so existing
    chapters not in this batch are preserved — mirrors upsert_novel_meta().
    """
    blob_path = f"data/{slug}/meta.json"

    meta = cloud_download_json(blob_path)
    if meta is None:
        local_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
        if os.path.exists(local_path):
            with open(local_path, encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {"slug": slug, "title": title, "chapters": []}

    meta["slug"]        = slug
    meta["title"]       = title
    meta["lastUpdated"] = date.today().isoformat()
    if cover_url:
        meta["cover"] = cover_url
    if source_url:
        meta["source"] = source_url
    if tags:
        meta["tags"] = tags

    existing = {c["num"]: c for c in meta.get("chapters", [])}
    for num, ch_title in chapters:
        existing[num] = {"num": num, "title": ch_title}

    meta["chapters"]      = sorted(existing.values(), key=lambda c: c["num"])
    meta["totalChapters"] = len(meta["chapters"])

    try:
        cloud_upload_json(blob_path, meta)
    except Exception as e:
        print(f"  [cloud] Warning: failed to upload meta.json for {slug}: {e}")


def cloud_upsert_index(
    slug: str,
    title: str,
    total_chapters: int,
    cover_url: str = "",
    tags: list[str] | None = None,
) -> None:
    """
    Merge-write data/index.json to Vercel Blob.
    Same merge logic as upsert_site_index() but reads/writes from Blob.
    """
    blob_path = "data/index.json"

    index = cloud_download_json(blob_path)
    if index is None:
        local_path = os.path.join(SITE_DIR, "data", "index.json")
        if os.path.exists(local_path):
            with open(local_path, encoding="utf-8") as f:
                index = json.load(f)
        else:
            index = {"novels": []}

    entry = next((n for n in index["novels"] if n["slug"] == slug), None)
    if entry:
        entry["totalChapters"] = total_chapters
        entry["lastUpdated"]   = date.today().isoformat()
        if cover_url:
            entry["cover"] = cover_url
        if tags:
            entry["tags"] = tags
    else:
        index["novels"].append({
            "slug":          slug,
            "title":         title,
            "cover":         cover_url,
            "tags":          tags or [],
            "totalChapters": total_chapters,
            "lastUpdated":   date.today().isoformat(),
        })

    try:
        cloud_upload_json(blob_path, index)
    except Exception as e:
        print(f"  [cloud] Warning: failed to upload index.json: {e}")


def cloud_get_novel_state(slug: str) -> tuple[int, int]:
    """
    Like get_local_novel_state() but reads from Vercel Blob.
    Used in --cloud-only mode to find the highest chapter already uploaded.
    Returns (highest_chapter_num, next_vol_num).
    """
    meta = cloud_download_json(f"data/{slug}/meta.json")
    if meta:
        chapters = meta.get("chapters", [])
        if chapters:
            return max(c["num"] for c in chapters), 1
    return 0, 1


def cloud_get_all_slugs() -> list[str]:
    """
    Return all novel slugs from Blob's data/index.json.
    Used by --update in --cloud-only mode.
    """
    index = cloud_download_json("data/index.json")
    if index:
        return sorted(n["slug"] for n in index.get("novels", []))
    return []


def cloud_upload_existing() -> None:
    """
    Upload all existing local docs/data/ JSON files to Vercel Blob.
    Run once with --upload-existing to seed the Blob store from your
    current local library without re-scraping anything.
    """
    if not BLOB_TOKEN:
        print("Error: BLOB_READ_WRITE_TOKEN not set in .env — cannot upload.")
        return

    data_dir = os.path.join(SITE_DIR, "data")
    if not os.path.isdir(data_dir):
        print("No docs/data/ directory found. Run the scraper first.")
        return

    print("Uploading existing local data to Vercel Blob…\n")
    total_uploaded = 0

    # Upload index.json first
    index_path = os.path.join(data_dir, "index.json")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index_obj = json.load(f)
        url = cloud_upload_json("data/index.json", index_obj)
        print(f"  ✓ index.json  →  {url}")
        total_uploaded += 1

    # Upload each novel's meta + chapters
    slugs = sorted(
        d for d in os.listdir(data_dir)
        if os.path.isdir(os.path.join(data_dir, d))
    )
    for slug in slugs:
        slug_dir = os.path.join(data_dir, slug)

        meta_path = os.path.join(slug_dir, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            try:
                cloud_upload_json(f"data/{slug}/meta.json", meta)
                print(f"  [{slug}] meta.json uploaded")
                total_uploaded += 1
            except Exception as e:
                print(f"  [{slug}] meta.json upload failed: {e}")

        ch_dir = os.path.join(slug_dir, "chapters")
        if not os.path.isdir(ch_dir):
            continue
        ch_files = sorted(f for f in os.listdir(ch_dir) if f.endswith(".json"))
        uploaded_chs = 0
        for fname in ch_files:
            with open(os.path.join(ch_dir, fname), encoding="utf-8") as f:
                ch = json.load(f)
            num = ch.get("num") or int(fname.replace(".json", ""))
            try:
                cloud_upload_json(f"data/{slug}/chapters/{num}.json", ch)
                uploaded_chs  += 1
                total_uploaded += 1
                print(f"  [{slug}] {uploaded_chs}/{len(ch_files)} chapters", end="\r", flush=True)
            except Exception as e:
                print(f"\n  [{slug}] Chapter {num} upload failed: {e}")
        if ch_files:
            print(f"  [{slug}] ✓ {uploaded_chs}/{len(ch_files)} chapters uploaded")

    _save_blob_config()
    if _blob_base_url:
        print(f"\n  Blob base URL: {_blob_base_url}")
        print(f"  Saved to docs/data/config.json")
        print(f"  → git add docs/data/config.json && git commit -m 'add blob config' && git push")

    print(f"\nDone — {total_uploaded} file(s) uploaded to Vercel Blob.")


# ─────────────────────── site detection ────────────────────────
def detect_site(url: str) -> str:
    """Return 'novelfire' or 'freewebnovel' based on URL."""
    if "novelfire.net" in url:
        return "novelfire"
    return "freewebnovel"


def slug_from_url(url: str) -> str:
    """Extract the novel slug from any supported URL."""
    url = url.rstrip("/")
    for prefix in ("/book/", "/novel/"):
        if prefix in url:
            return url.split(prefix)[-1].split("/")[0]
    return url.split("/")[-1]


def url_for_slug(slug: str) -> str:
    """
    Return the canonical novel URL for a slug.
    Reads the 'source' field from meta.json if available
    so novelfire novels get the right base URL.
    Falls back to freewebnovel.
    """
    meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            src = meta.get("source", "")
            if src:
                return src
        except Exception:
            pass
    return f"{BASE_URL}/novel/{slug}"


# ──────────────────── NovelFire adapter ────────────────────────
def _nf_clean_text(text: str) -> str:
    """Clean scraped novelfire text, stripping watermarks and site notices."""
    JUNK_PHRASES = [
        "novelfire.net", "novel fire", "made with ♥ for novel lovers",
        "if you find any errors", "please let us know so we can fix",
        "non-standard content, ads redirect",
        "to continue serving our readers",
        "i have removed korean novels due to copyright",
        "thank you for your continued support",
        "still working hard to bring you more great novels",
        "tip: you can use left, right keyboard keys",
        "tap the middle of the screen to reveal",
        "share to your friends",
        "restore scroll position",
        "javascript:;",
    ]
    lines = [line.strip() for line in text.splitlines()]
    filtered = []
    for line in lines:
        lo = line.lower()
        if any(j in lo for j in JUNK_PHRASES):
            continue
        if not line:
            filtered.append("")
            continue
        if len(line) < 15 and not line.endswith((".", "!", "?", "…", '"', "'")):
            continue
        filtered.append(line)
    merged = []
    for line in filtered:
        if (merged and merged[-1]
                and not merged[-1].endswith((".", "!", "?", ":", '"', "'"))
                and line and line[0].islower()):
            merged[-1] += " " + line
        else:
            merged.append(line)
    result, prev_blank = [], False
    for line in merged:
        if not line:
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return "\n\n".join(l for l in result if l)


def nf_get_novel_page_info(novel_url: str) -> dict:
    try:
        res = requests.get(novel_url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print(f"  [warn] Could not fetch novel page: {e}")
        return {"total": None, "cover": "", "title": "", "tags": []}

    soup = BeautifulSoup(res.text, "html.parser")

    og_title = soup.find("meta", property="og:title")
    title = og_title["content"].replace(" - Novel Fire", "").strip() if og_title else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    og_image = soup.find("meta", property="og:image")
    cover = og_image["content"].strip() if og_image and og_image.get("content") else ""

    tags: list[str] = []
    seen_tags: set[str] = set()
    for a in soup.select("a[href*='/genre-']"):
        m = re.search(r"/genre-([^/]+)/", a.get("href", ""))
        if m:
            tag = m.group(1).replace("-", " ").title()
            if tag.lower() not in seen_tags and tag.lower() != "all":
                tags.append(tag)
                seen_tags.add(tag.lower())

    total = None
    for tag in soup.find_all(string=re.compile(r"\d+\s*Chapters?")):
        m = re.search(r"(\d+)", tag)
        if m:
            total = int(m.group(1))
            break
    if not total:
        max_ch = 0
        for a in soup.select("a[href*='/chapter-']"):
            m = re.search(r"/chapter-(\d+)", a.get("href", ""))
            if m:
                max_ch = max(max_ch, int(m.group(1)))
        total = max_ch if max_ch else None

    return {"total": total, "cover": cover, "title": title, "tags": tags}


def nf_fetch_chapter_once(i: int, url: str) -> tuple[int, str, str]:
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    time.sleep(CHAPTER_DELAY)
    soup = BeautifulSoup(res.text, "html.parser")

    # Title: h1 format is "[Novel Name](link) Chapter N - N: Title[ ... words ]"
    h1 = soup.find("h1")
    title_text = f"Chapter {i}"
    if h1:
        for a in h1.find_all("a"):
            a.decompose()
        raw = h1.get_text(strip=True)
        # Strip "[ ... words ]" word count badge
        raw = re.sub(r"\[\s*\.{3}\s*words?\s*\]", "", raw, flags=re.IGNORECASE).strip()
        raw = raw.strip(" -")
        if raw:
            title_text = raw

    # Content: try dedicated div selectors first
    content_div = (
        soup.select_one("div.chapter-content")
        or soup.select_one("div#chapter-content")
        or soup.select_one("div.text-left")
        or soup.select_one("div#chapter-container")
    )
    if content_div:
        for tag in content_div.select("script,style,ins,iframe,h1,h2,h3,a,.ads,noscript"):
            tag.decompose()
        text = content_div.get_text(separator="\n")
    else:
        # Fallback: collect <p> tags longer than 40 chars
        paras = []
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if t and len(t) > 40:
                paras.append(t)
        text = "\n\n".join(paras)

    cleaned = _nf_clean_text(text)
    if not cleaned:
        raise ValueError("Chapter content empty after cleaning")
    return i, title_text, cleaned


def nf_get_listing_page_novels(page_url: str) -> list[str]:
    try:
        res = requests.get(page_url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print(f"  [warn] Could not fetch listing page {page_url}: {e}")
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    urls = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.match(r"^/book/[^/]+/?$", href) or re.match(
            r"^https://novelfire\.net/book/[^/]+/?$", href
        ):
            full = href if href.startswith("http") else NOVELFIRE_URL + href
            full = full.rstrip("/")
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


def nf_discover_all_novels(listing_base: str, max_pages: int | None = None) -> list[str]:
    all_urls: list[str] = []
    seen: set[str] = set()
    page = 1
    base = re.sub(r"\?page=\d+", "", listing_base).rstrip("/")
    print(f"Discovering novels from: {base}")
    while True:
        if max_pages and page > max_pages:
            print(f"  Reached page limit ({max_pages}).")
            break
        page_url = base if page == 1 else f"{base}?page={page}"
        print(f"  Fetching listing page {page}: {page_url}")
        novels = nf_get_listing_page_novels(page_url)
        new = [u for u in novels if u not in seen]
        if not new:
            print(f"  No new novels on page {page} — done discovering.")
            break
        seen.update(new)
        all_urls.extend(new)
        print(f"  Found {len(new)} novel(s) on page {page} ({len(all_urls)} total so far)")
        page += 1
        time.sleep(PAGE_DELAY)
    print(f"\nTotal novels discovered: {len(all_urls)}\n")
    return all_urls


# ─────────────────────── listing crawler ───────────────────────
def get_listing_page_novels(page_url: str) -> list[str]:
    """Return novel URLs found on one listing page."""
    try:
        res = requests.get(page_url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print(f"  [warn] Could not fetch listing page {page_url}: {e}")
        return []
    soup = BeautifulSoup(res.text, "html.parser")
    urls = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.match(r"^/novel/[^/]+/?$", href) or re.match(
            r"^https://freewebnovel\.com/novel/[^/]+/?$", href
        ):
            full = href if href.startswith("http") else BASE_URL + href
            full = full.rstrip("/")
            if full not in urls:
                urls.append(full)
    return urls


def discover_all_novels(listing_base: str, max_pages: int | None = None) -> list[str]:
    if detect_site(listing_base) == "novelfire":
        return nf_discover_all_novels(listing_base, max_pages)
    # ── freewebnovel ─────────────────────────────────────────────
    all_urls: list[str] = []
    seen: set[str] = set()
    page = 1
    print(f"Discovering novels from: {listing_base}")
    while True:
        if max_pages and page > max_pages:
            print(f"  Reached page limit ({max_pages}).")
            break
        page_url = f"{listing_base}/{page}/" if page > 1 else f"{listing_base}/"
        print(f"  Fetching listing page {page}: {page_url}")
        novels = get_listing_page_novels(page_url)
        new = [u for u in novels if u not in seen]
        if not new:
            print(f"  No new novels on page {page} — done discovering.")
            break
        seen.update(new)
        all_urls.extend(new)
        print(f"  Found {len(new)} novel(s) on page {page} ({len(all_urls)} total so far)")
        page += 1
        time.sleep(PAGE_DELAY)
    print(f"\nTotal novels discovered: {len(all_urls)}\n")
    return all_urls


# ─────────────────────── novel metadata ────────────────────────
def get_novel_page_info(novel_url: str) -> dict:
    """
    Fetch the novel landing page and extract:
      - total chapters (int or None)
      - cover URL (str or "")
      - title (str or "")
      - tags (list[str])
    Routes to the correct site adapter automatically.
    """
    if detect_site(novel_url) == "novelfire":
        return nf_get_novel_page_info(novel_url)
    try:
        res = requests.get(novel_url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print(f"  [warn] Could not fetch novel page: {e}")
        return {"total": None, "cover": "", "title": "", "tags": []}

    soup = BeautifulSoup(res.text, "html.parser")

    # Title
    og_title = soup.find("meta", property="og:title")
    title = og_title["content"].strip() if og_title and og_title.get("content") else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    # Cover image
    og_image = soup.find("meta", property="og:image")
    cover = og_image["content"].strip() if og_image and og_image.get("content") else ""

    # Tags — genre links on the novel page
    tags: list[str] = []
    seen_tags: set[str] = set()
    # Primary: og:novel:genre meta tag
    og_genre = soup.find("meta", property="og:novel:genre")
    if og_genre and og_genre.get("content"):
        for t in og_genre["content"].split(","):
            t = t.strip()
            if t and t.lower() not in seen_tags:
                tags.append(t)
                seen_tags.add(t.lower())
    # Fallback: genre/tag anchor links
    if not tags:
        for a in soup.select("a[href*='/genre/'], a[href*='/tag/']"):
            t = a.get_text(strip=True)
            if t and t.lower() not in seen_tags and len(t) < 40:
                tags.append(t)
                seen_tags.add(t.lower())

    # Total chapters
    total = None
    meta_ch = soup.find("meta", property="og:novel:lastest_chapter_url")
    if meta_ch:
        url = meta_ch.get("content", "")
        m = re.search(r"chapter-(\d+)", url)
        if m:
            total = int(m.group(1))
    if not total:
        max_ch = 0
        for a in soup.select("a[href*='/chapter-']"):
            m = re.search(r"/chapter-(\d+)", a["href"])
            if m:
                max_ch = max(max_ch, int(m.group(1)))
        total = max_ch if max_ch else None

    return {"total": total, "cover": cover, "title": title, "tags": tags}


# ───────────────────────── chapter scraping ────────────────────
def _fetch_chapter_once(i: int, url: str) -> tuple[int, str, str]:
    if detect_site(url) == "novelfire":
        return nf_fetch_chapter_once(i, url)
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    time.sleep(CHAPTER_DELAY)
    soup = BeautifulSoup(res.text, "html.parser")

    # ── Title ──────────────────────────────────────────────────────
    # Primary: <h2>, which normally has the full descriptive title.
    bare_pattern = re.compile(r"^chapter\s*\d+\.?$", re.IGNORECASE)
    title_text = ""
    h2 = soup.select_one("h2")
    if h2:
        title_text = h2.get_text(strip=True)

    # Fall back to <title> if h2 is missing or is a bare "Chapter N"
    if not title_text or bare_pattern.match(title_text):
        title_tag = soup.find("title")
        if title_tag:
            page_title = title_tag.get_text(strip=True)
            m = re.search(r"(Chapter\s*\d+[^-|]*(?:-[^-|]+)?)", page_title, re.IGNORECASE)
            if m:
                candidate = _clean_fwn_title(m.group(1))
                if candidate and not bare_pattern.match(candidate):
                    title_text = candidate

    title_text = _clean_fwn_title(title_text) if title_text else f"Chapter {i}"

    # ── Content ────────────────────────────────────────────────────
    content_div = soup.select_one("div.txt")
    if not content_div:
        # Raise so scrape_chapter()'s retry loop fires and the failure
        # is logged to failed_chapters.txt — never silently return "".
        raise ValueError(
            f"Chapter {i}: content div (div.txt) not found — "
            "possible captcha, block, or markup change"
        )
    for tag in content_div.select("script, style, ins, iframe, h1, h2, h3"):
        tag.decompose()
    text    = content_div.get_text(separator="\n")
    cleaned = clean_text(text)
    if not cleaned:
        raise ValueError(f"Chapter {i}: content div found but cleaned text is empty")
    return i, title_text, cleaned


def scrape_chapter(args: tuple[int, str, str]) -> tuple[int, str, str, bool]:
    i, url, novel_slug = args
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            i, title, content = _fetch_chapter_once(i, url)
            if attempt > 1:
                print(f"  [retry ok] Chapter {i} succeeded on attempt {attempt}")
            return i, title, content, True
        except Exception as e:
            last_err = e
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
                print(f"  [retry {attempt}/{RETRY_ATTEMPTS}] Chapter {i} failed ({e}) — waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"  [failed] Chapter {i} gave up after {RETRY_ATTEMPTS} attempts: {last_err}")
    log_failed_chapter(novel_slug, i, url, str(last_err))
    return i, f"Chapter {i}", "", False


# ──────────────────── failed-chapter tracking ──────────────────
def log_failed_chapter(novel_slug: str, chapter_num: int, url: str, reason: str) -> None:
    with open(FAILED_FILE, "a") as f:
        f.write(f"{novel_slug}\t{chapter_num}\t{url}\t{reason}\n")


def load_failed_chapters() -> list[tuple[str, int, str, str]]:
    if not os.path.exists(FAILED_FILE):
        return []
    entries = []
    with open(FAILED_FILE) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                slug, num, url = parts[0], int(parts[1]), parts[2]
                reason = parts[3] if len(parts) > 3 else ""
                entries.append((slug, num, url, reason))
    return entries


def clear_failed_log() -> None:
    if os.path.exists(FAILED_FILE):
        os.remove(FAILED_FILE)


def retry_failed_chapters(export_site: bool = True, cloud: bool = False) -> None:
    failures = load_failed_chapters()
    if not failures:
        print("No failed chapters on record.")
        return

    print(f"\nRetrying {len(failures)} previously failed chapter(s)…\n")

    by_slug: dict[str, list[tuple[int, str]]] = {}
    for slug, num, url, _ in failures:
        by_slug.setdefault(slug, []).append((num, url))

    still_failed: list[tuple[str, int, str, str]] = []

    for slug, chapters in by_slug.items():
        novel_name = slug.replace("-", " ").title()
        novel_url  = url_for_slug(slug)
        print(f"Novel: {novel_name} — retrying {len(chapters)} chapter(s)")
        tasks = [(num, url, slug) for num, url in chapters]
        recovered: list[tuple[int, str, str]] = []

        with ThreadPoolExecutor(max_workers=CHAPTER_WORKERS) as executor:
            futures = {executor.submit(scrape_chapter, t): t for t in tasks}
            for future in as_completed(futures):
                num, title, content, ok = future.result()
                if ok and content:
                    recovered.append((num, title, content))
                    if export_site:
                        export_chapter_json(slug, num, title, content)
                    if cloud:
                        cloud_export_chapter_json(slug, num, title, content)
                    print(f"  ✓ Recovered chapter {num}")
                else:
                    orig = next((u for n, u in chapters if n == num), f"{novel_url}/chapter-{num}")
                    still_failed.append((slug, num, orig, "still failing after retry"))

        if recovered:
            recovered.sort(key=lambda x: x[0])
            start_ch = recovered[0][0]
            end_ch   = recovered[-1][0]
            # Write recovered epub
            book = epub.EpubBook()
            book.set_title(f"{novel_name} — Recovered Chapters")
            book.set_language("en")
            spine = ["nav"]
            ch_items = []
            for num, title, content in recovered:
                c = epub.EpubHtml(title=title, file_name=f"chapter_{num}.xhtml", lang="en")
                html_content = f"<h1>{html_lib.escape(title)}</h1>\n"
                for para in content.split("\n\n"):
                    if para.strip():
                        html_content += f"<p>{html_lib.escape(para.strip())}</p>\n"
                c.content = html_content
                book.add_item(c)
                ch_items.append(c)
                spine.append(c)
            book.toc = tuple(ch_items)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.spine = spine
            novel_dir = os.path.join("output", slug)
            os.makedirs(novel_dir, exist_ok=True)
            filepath = os.path.join(novel_dir, f"{slug}-recovered-ch{start_ch}-{end_ch}.epub")
            try:
                epub.write_epub(filepath, book)
                print(f"  Saved recovered epub: {filepath}")
            except Exception as e:
                print(f"  [error] Could not write recovered epub: {e}")

            # Update site meta with recovered chapters
            if export_site:
                upsert_novel_meta(slug, novel_name,
                                  [(n, t) for n, t, _ in recovered])
            if cloud:
                cloud_upsert_novel_meta(slug, novel_name,
                                        [(n, t) for n, t, _ in recovered])

    clear_failed_log()
    if still_failed:
        for slug, num, url, reason in still_failed:
            log_failed_chapter(slug, num, url, reason)
        print(f"\n{len(still_failed)} chapter(s) still failing — logged to {FAILED_FILE}")
    else:
        print("\nAll previously failed chapters recovered successfully.")


# ──────────────────── local output inspection ─────────────────
def get_local_novel_state(slug: str) -> tuple[int, int]:
    """
    Return (highest_chapter_on_disk, next_vol_num).
    Checks epub files in output/<slug>/ first, then falls back to
    docs/data/<slug>/meta.json so --no-epub runs are tracked correctly.
    """
    # Check epub output folder
    novel_dir   = os.path.join("output", slug)
    max_chapter = 0
    max_vol     = 0
    if os.path.isdir(novel_dir):
        for fname in os.listdir(novel_dir):
            if not fname.endswith(".epub"):
                continue
            m = re.search(r"-vol(\d+)-ch\d+-(\d+)\.epub$", fname)
            if m:
                vol_num = int(m.group(1))
                end_ch  = int(m.group(2))
                max_chapter = max(max_chapter, end_ch)
                max_vol     = max(max_vol, vol_num)

    # If no epubs found, check site meta.json (covers --no-epub runs)
    if max_chapter == 0:
        meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                chapters = meta.get("chapters", [])
                if chapters:
                    max_chapter = max(c["num"] for c in chapters)
            except Exception:
                pass

    return max_chapter, max_vol + 1


def get_all_local_slugs() -> list[str]:
    """Return every slug that has a folder under output/ OR docs/data/."""
    slugs: set[str] = set()
    for base in ["output", os.path.join(SITE_DIR, "data")]:
        if os.path.isdir(base):
            for name in os.listdir(base):
                if os.path.isdir(os.path.join(base, name)):
                    slugs.add(name)
    return sorted(slugs)


# ──────────────────────── epub builder ────────────────────────
def make_epub(
    slug: str,
    novel_name: str,
    chapters_data: list[tuple[int, str, str]],
    vol_num: int,
    start_ch: int,
    end_ch: int,
) -> None:
    book = epub.EpubBook()
    book.set_title(f"{novel_name} Vol.{vol_num}")
    book.set_language("en")
    chapters, spine = [], ["nav"]

    for i, title, content in chapters_data:
        if not content:
            continue
        c = epub.EpubHtml(title=title, file_name=f"chapter_{i}.xhtml", lang="en")
        html_content = f"<h1>{html_lib.escape(title)}</h1>\n"
        for para in content.split("\n\n"):
            if para.strip():
                html_content += f"<p>{html_lib.escape(para.strip())}</p>\n"
        c.content = html_content
        book.add_item(c)
        chapters.append(c)
        spine.append(c)

    if not chapters:
        print(f"  [skip] No content for Vol.{vol_num} of {novel_name}")
        return

    book.toc   = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    novel_dir = os.path.join("output", slug)
    os.makedirs(novel_dir, exist_ok=True)
    filepath = os.path.join(novel_dir, f"{slug}-vol{vol_num}-ch{start_ch}-{end_ch}.epub")
    try:
        epub.write_epub(filepath, book)
        print(f"  Saved epub: {filepath}")
    except Exception as e:
        print(f"  [error] Failed to write epub: {e}")


# ───────────────────────── novel scraper ───────────────────────
def scrape_novel(
    novel_url: str,
    force_full: bool = False,
    export_site: bool = True,
    export_epub: bool = True,
    cloud: bool = False,
    cloud_only: bool = False,
    excluded_genres: set[str] | None = None,
) -> bool:
    excluded_genres = excluded_genres or set()
    site = detect_site(novel_url)
    slug = slug_from_url(novel_url)
    print(f"\n{'─'*60}")
    print(f"Novel: {slug}  [{site}]")
    print(f"URL:   {novel_url}")

    # Chapter URL pattern differs by site
    def chapter_url(n: int) -> str:
        if site == "novelfire":
            return f"{NOVELFIRE_URL}/book/{slug}/chapter-{n}"
        return f"{BASE_URL}/novel/{slug}/chapter-{n}"

    # ── what we already have locally ────────────────────────────
    if force_full:
        local_highest, next_vol_num = 0, 1
    elif cloud_only:
        # Explicit cloud-only: read prior state from Blob, not local disk.
        # This avoids the --cloud --no-site ambiguity where export_site=False
        # for two different reasons (user chose cloud-only vs just skipping HTML).
        local_highest, next_vol_num = cloud_get_novel_state(slug)
    else:
        local_highest, next_vol_num = get_local_novel_state(slug)
    if local_highest:
        print(f"  Local:  {local_highest} chapter(s) already on disk (next vol → Vol.{next_vol_num})")

    # ── fetch novel page info ────────────────────────────────────
    info       = get_novel_page_info(novel_url)
    total      = info["total"]
    cover_url  = info["cover"]
    novel_name = info["title"] or slug.replace("-", " ").title()
    tags       = info.get("tags", [])

    if not total:
        print("  [warn] Could not determine chapter count — skipping.")
        return False
    print(f"  Title:  {novel_name}")
    print(f"  Tags:   {', '.join(tags) if tags else '(none)'}")
    print(f"  Cover:  {cover_url or '(none)'}")
    print(f"  Remote: {total} chapter(s) available")

    # ── genre filter ─────────────────────────────────────────────
    if excluded_genres:
        tag_lower = {t.lower() for t in tags}
        matched   = excluded_genres & tag_lower
        if matched:
            print(f"  [skip] Excluded genre(s): {', '.join(sorted(matched))}")
            return False

    # ── decide what to download ──────────────────────────────────
    first_needed = local_highest + 1
    if first_needed > total:
        print("  ✓ Already up to date — nothing to download.")
        return True

    new_count = total - local_highest
    if local_highest:
        print(f"  → {new_count} new chapter(s) to download (ch{first_needed}–{total})")
    else:
        print(f"  → Downloading all {total} chapter(s)")

    # ── build volume ranges ──────────────────────────────────────
    volumes = []
    vol_num = next_vol_num
    for start in range(first_needed, total + 1, CHAPTERS_PER_VOL):
        end = min(start + CHAPTERS_PER_VOL - 1, total)
        volumes.append((vol_num, start, end))
        vol_num += 1

    # ── scrape each volume ───────────────────────────────────────
    total_failed  = 0
    all_ch_tuples: list[tuple[int, str]] = []   # (num, title) for meta.json
    all_ch_data:   list[tuple[int, str, str]] = []  # (num, title, content) for HTML export

    for vol_num, start, end in volumes:
        print(f"\n  Volume {vol_num}: chapters {start}–{end}")
        tasks = [(i, chapter_url(i), slug) for i in range(start, end + 1)]
        results: dict[int, tuple[str, str]] = {}

        with ThreadPoolExecutor(max_workers=CHAPTER_WORKERS) as executor:
            futures = {executor.submit(scrape_chapter, t): t for t in tasks}
            for future in as_completed(futures):
                i, title, content, ok = future.result()
                results[i] = (title, content)
                if not ok:
                    total_failed += 1
                elif cloud_only and content:
                    # Upload to Blob immediately as each chapter finishes,
                    # overlapping upload I/O with threads still fetching the
                    # rest of the volume — instead of a sequential post-batch
                    # upload that blocks until every chapter is done first.
                    cloud_export_chapter_json(slug, i, title, content)
                print(f"  ✓ Chapter {i}/{total}", end="\r", flush=True)
        print()

        chapters_data = [
            (i, *results.get(i, (f"Chapter {i}", ""))) for i in range(start, end + 1)
        ]

        # ── export JSON + static HTML per chapter ───────────────
        for i, title, content in chapters_data:
            if not content:
                continue
            if export_site:
                export_chapter_json(slug, i, title, content)
            if cloud and not cloud_only:
                # --cloud (not --cloud-only): upload after confirming non-empty
                # content. cloud_only already uploaded in as_completed above.
                cloud_export_chapter_json(slug, i, title, content)
            all_ch_tuples.append((i, title))
            if export_site:
                all_ch_data.append((i, title, content))

        # ── write epub ───────────────────────────────────────────
        if export_epub:
            make_epub(slug, novel_name, chapters_data, vol_num, start, end)

        time.sleep(2)

    # ── update site index & meta ─────────────────────────────────
    if export_site:
        print(f"  Updating Ohara site data…")
        upsert_novel_meta(slug, novel_name, all_ch_tuples,
                          cover_url=cover_url, source_url=novel_url, tags=tags)
        upsert_site_index(slug, novel_name, len(all_ch_tuples) + local_highest,
                          cover_url=cover_url, tags=tags)
    if cloud:
        print(f"  Uploading to Vercel Blob…")
        cloud_upsert_novel_meta(slug, novel_name, all_ch_tuples,
                                cover_url=cover_url, source_url=novel_url, tags=tags)
        cloud_upsert_index(slug, novel_name, len(all_ch_tuples) + local_highest,
                           cover_url=cover_url, tags=tags)
        _save_blob_config()
        print(f"  ✓ Cloud upload complete  ({_blob_base_url})")

    if export_site:
        # ── generate static HTML pages (Safari Reader compatible) ─
        # We need the full chapter list from meta to resolve prev/next correctly
        meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
        with open(meta_path, encoding="utf-8") as f:
            full_meta = json.load(f)
        all_nums = [c["num"] for c in full_meta["chapters"]]

        print(f"  Writing static HTML pages…")
        for i, title, content in all_ch_data:
            idx      = all_nums.index(i) if i in all_nums else -1
            prev_num = all_nums[idx - 1] if idx > 0 else None
            next_num = all_nums[idx + 1] if 0 <= idx < len(all_nums) - 1 else None
            export_chapter_html(slug, i, title, content, prev_num, next_num, novel_name)

        print(f"  ✓ Site data written to {SITE_DIR}/data/{slug}/ and {SITE_DIR}/read/{slug}/")

    if total_failed:
        print(f"\n  ⚠ {total_failed} chapter(s) failed — logged to {FAILED_FILE}")
        print(f"    Run with --retry-failed to attempt re-downloading them.")
    return True


def git_push_novel(slug: str, novel_name: str) -> bool:
    """
    Stage docs/data/<slug>/ and index.json, commit and push to GitHub.
    Pulls remote changes first (rebase) so code updates from GitHub
    are always merged in before pushing novel data.
    Called after each novel when --auto-push is set.
    Returns True if push succeeded.
    """
    import subprocess

    # ── git stage + commit + push ────────────────────────────────
    data_path   = os.path.join(SITE_DIR, "data", slug)
    read_path   = os.path.join(SITE_DIR, "read", slug)
    index_path  = os.path.join(SITE_DIR, "data", "index.json")
    config_path = os.path.join(SITE_DIR, "data", "config.json")

    print(f"\n  [git] Pushing {novel_name} to GitHub…")

    def run(cmd: list[str]) -> tuple[int, str]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()

    for path in [data_path, read_path, index_path, config_path]:
        if os.path.exists(path):
            code, out = run(["git", "add", path])
            if code != 0:
                print(f"  [git] Warning: git add failed for {path}: {out}")

    code, out = run(["git", "status", "--porcelain"])
    if not out.strip():
        print(f"  [git] Nothing new to commit for {novel_name} — already up to date.")
        # Still pull in case there are remote code updates
        run(["git", "pull", "--rebase", "--autostash", "origin", "main"])
        return True

    msg = f"add/update: {novel_name}"
    code, out = run(["git", "commit", "-m", msg])
    if code != 0:
        print(f"  [git] Commit failed: {out}")
        return False
    print(f"  [git] Committed: {msg}")

    # ── Pull remote changes before pushing ───────────────────────
    # --autostash: temporarily stashes any unstaged changes so rebase
    #              doesn't abort, then restores them after.
    print(f"  [git] Pulling remote updates…")
    code, out = run(["git", "pull", "--rebase", "--autostash", "origin", "main"])
    if code != 0:
        print(f"  [git] Pull/rebase failed: {out}")
        print(f"  [git] Attempting push anyway…")
    else:
        if "Already up to date" in out or not out.strip():
            print(f"  [git] Already up to date.")
        else:
            print(f"  [git] Pulled and rebased: {out.splitlines()[0]}")

    code, out = run(["git", "push"])
    if code != 0:
        print(f"  [git] Push failed: {out}")
        print(f"  [git] Files are committed locally — run 'git push' manually later.")
        return False

    print(f"  [git] ✓ Pushed to GitHub successfully.")
    return True


def fetch_tags_for_all(auto_push: bool = False, cloud: bool = False) -> None:
    """
    Visit every novel's landing page, scrape tags, and update
    meta.json + index.json (local and/or Blob). No chapters are downloaded.
    """
    if cloud and not get_all_local_slugs():
        slugs = cloud_get_all_slugs()
    else:
        slugs = get_all_local_slugs()
    if not slugs:
        print("No novels found in output/, docs/data/, or Vercel Blob.")
        return

    print(f"Fetching tags for {len(slugs)} novel(s)…\n")

    for idx, slug in enumerate(sorted(slugs), 1):
        novel_url  = url_for_slug(slug)
        print(f"[{idx}/{len(slugs)}] {slug}")
        info = get_novel_page_info(novel_url)
        tags       = info.get("tags", [])
        cover_url  = info.get("cover", "")
        novel_name = info.get("title") or slug.replace("-", " ").title()

        if tags:
            print(f"  Tags: {', '.join(tags)}")
        else:
            print(f"  Tags: (none found)")

        # Update local meta.json with tags (preserve existing chapters)
        meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            meta["tags"] = tags
            if cover_url:
                meta["cover"] = cover_url
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

        # Update local index.json entry with tags
        index_path = os.path.join(SITE_DIR, "data", "index.json")
        if os.path.exists(index_path):
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
            entry = next((n for n in index["novels"] if n["slug"] == slug), None)
            if entry:
                entry["tags"] = tags
                if cover_url:
                    entry["cover"] = cover_url
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

        # Mirror tag updates to Vercel Blob when requested
        if cloud:
            chapters_for_meta: list[tuple[int, str]] = []
            # Try local meta first; fall back to Blob
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    local_meta = json.load(f)
                chapters_for_meta = [(c["num"], c["title"]) for c in local_meta.get("chapters", [])]
            cloud_upsert_novel_meta(slug, novel_name, chapters_for_meta,
                                    cover_url=cover_url, tags=tags)
            total_ch = len(chapters_for_meta)
            cloud_upsert_index(slug, novel_name, total_ch, cover_url=cover_url, tags=tags)
            print(f"  [cloud] ✓ Tags synced to Vercel Blob")

        if auto_push:
            git_push_novel(slug, novel_name)

        time.sleep(PAGE_DELAY)

    print(f"\nDone — tags updated for {len(slugs)} novel(s).")
    if not auto_push:
        print("Push with: git add docs/data/ && git commit -m 'add tags' && git push")


# ──────────────────── novel deletion (single + by genre) ────────
def remove_slug_from_progress(slug: str) -> None:
    """Remove any line referencing this slug from PROGRESS_FILE (scraped_novels.txt)."""
    if not os.path.exists(PROGRESS_FILE):
        return
    with open(PROGRESS_FILE) as f:
        lines = f.readlines()
    kept = [l for l in lines if slug_from_url(l.strip()) != slug]
    if len(kept) != len(lines):
        with open(PROGRESS_FILE, "w") as f:
            f.writelines(kept)


def remove_slug_from_failed(slug: str) -> None:
    """Remove any failed-chapter entries for this slug from FAILED_FILE."""
    if not os.path.exists(FAILED_FILE):
        return
    with open(FAILED_FILE) as f:
        lines = f.readlines()
    kept = [l for l in lines if not l.startswith(f"{slug}\t")]
    if len(kept) != len(lines):
        with open(FAILED_FILE, "w") as f:
            f.writelines(kept)


def delete_novel_files(slug: str) -> None:
    """
    Fully remove a novel from disk and from all tracking logs:
      - docs/data/<slug>/     (JSON data)
      - docs/read/<slug>/     (static HTML, if exists)
      - output/<slug>/        (epubs, if exists)
      - scraped_novels.txt    (removes this novel's progress entry)
      - failed_chapters.txt   (removes this novel's failed-chapter entries)
    Does NOT touch index.json — caller is responsible for that so batch
    deletes (like --delete-genre) only rewrite the index once at the end.
    After this, the novel is fully "forgotten" and can be freshly
    re-downloaded from scratch, or left deleted permanently.
    """
    import shutil

    for base in (os.path.join(SITE_DIR, "data", slug),
                 os.path.join(SITE_DIR, "read", slug),
                 os.path.join("output", slug)):
        if os.path.isdir(base):
            shutil.rmtree(base)
            print(f"  ✓ Removed {base}")

    remove_slug_from_progress(slug)
    remove_slug_from_failed(slug)
    print(f"  ✓ Cleared progress/failed-chapter logs for {slug}")


def cloud_delete_novel(slug: str) -> bool:
    """
    Remove a novel's chapters and meta from Vercel Blob by updating
    the Blob index.json to exclude the slug.
    Individual chapter blobs cannot be individually deleted without
    listing APIs, so we remove the novel from the index (making it
    invisible to the site) and overwrite meta.json with a tombstone.
    Returns True if the novel was found and removed from the index.
    """
    if not BLOB_TOKEN:
        print("  [cloud] Cannot delete from Blob: BLOB_READ_WRITE_TOKEN not set.")
        return False
    blob_index = cloud_download_json("data/index.json")
    if not blob_index:
        return False
    original_count = len(blob_index.get("novels", []))
    blob_index["novels"] = [n for n in blob_index.get("novels", []) if n["slug"] != slug]
    removed = len(blob_index["novels"]) < original_count
    if removed:
        try:
            cloud_upload_json("data/index.json", blob_index)
            # Overwrite meta with tombstone so loaders get a clean 404-like state
            cloud_upload_json(f"data/{slug}/meta.json",
                              {"slug": slug, "title": "", "chapters": [], "deleted": True})
            print(f"  [cloud] ✓ Removed {slug} from Blob index.")
        except Exception as e:
            print(f"  [cloud] Warning: could not update Blob index: {e}")
    return removed


def delete_novel(slug: str, auto_push: bool = False, cloud: bool = False) -> None:
    """Delete a single novel by slug — used by --delete-novel."""
    index_path = os.path.join(SITE_DIR, "data", "index.json")
    novel_name = slug

    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
        match = next((n for n in index.get("novels", []) if n["slug"] == slug), None)
        if match:
            novel_name = match.get("title", slug)
        index["novels"] = [n for n in index.get("novels", []) if n["slug"] != slug]
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    data_exists = os.path.isdir(os.path.join(SITE_DIR, "data", slug))
    has_local   = data_exists or os.path.isdir(os.path.join("output", slug))

    if cloud:
        found_in_blob = cloud_delete_novel(slug)
        if not has_local and not found_in_blob:
            print(f"No novel found locally or in Vercel Blob with slug '{slug}'.")
            return
    elif not has_local:
        print(f"No novel found locally with slug '{slug}'. "
              f"(If it's cloud-only, re-run with --cloud to also remove from Blob.)")
        return

    print(f"\nDeleting: {novel_name} ({slug})")
    if has_local:
        delete_novel_files(slug)
    print(f"\n✓ '{novel_name}' fully removed. You can download it again anytime with --novel.")

    if auto_push:
        import subprocess
        print("\n[git] Staging and pushing deletion…")

        def run(cmd):
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.returncode, (r.stdout + r.stderr).strip()

        run(["git", "add", "-A", os.path.join(SITE_DIR, "data")])
        run(["git", "add", "-A", os.path.join(SITE_DIR, "read")])
        run(["git", "add", "-A", "output"])

        code, out = run(["git", "status", "--porcelain"])
        if not out.strip():
            print("  [git] Nothing to commit.")
            return
        code, out = run(["git", "commit", "-m", f"delete novel: {novel_name}"])
        if code != 0:
            print(f"  Commit failed: {out}")
            return
        # Pull before push (matches git_push_novel behavior)
        code, out = run(["git", "pull", "--rebase", "--autostash", "origin", "main"])
        if code != 0:
            print(f"  Pull/rebase failed: {out}")
        code, out = run(["git", "push"])
        if code == 0:
            print(f"  ✓ Pushed to GitHub.")
        else:
            print(f"  Push failed: {out}\n  Files committed locally — run 'git push' manually.")


def delete_by_genre(genres: list[str], dry_run: bool = False, auto_push: bool = False) -> None:
    """
    Delete all novels whose tags contain any of the given genres.
    Removes:
      - docs/data/<slug>/   (JSON data)
      - docs/read/<slug>/   (static HTML, if exists)
      - output/<slug>/      (epubs, if exists)
      - progress/failed-chapter log entries for each deleted novel
    Also updates docs/data/index.json to remove the entries.
    Deleted novels are fully "forgotten" so they can be freely
    re-downloaded and re-deleted anytime.
    """
    # Normalise input genres for case-insensitive matching
    target = {g.strip().lower() for g in genres}
    print(f"\nLooking for novels tagged with: {', '.join(sorted(target))}\n")

    index_path = os.path.join(SITE_DIR, "data", "index.json")
    if not os.path.exists(index_path):
        print("No index.json found — nothing to do.")
        return

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    to_delete = []
    to_keep   = []

    for novel in index.get("novels", []):
        novel_tags = {t.strip().lower() for t in novel.get("tags", [])}
        if novel_tags & target:   # any overlap
            to_delete.append(novel)
        else:
            to_keep.append(novel)

    if not to_delete:
        print("No novels found with those tags.")
        return

    print(f"Found {len(to_delete)} novel(s) to delete:")
    for n in to_delete:
        matched = [t for t in n.get("tags", []) if t.lower() in target]
        print(f"  - {n['title']} ({n['slug']}) — matched tags: {', '.join(matched)}")

    if dry_run:
        print(f"\n[Dry run] Nothing deleted. Remove --dry-run to actually delete.")
        return

    # Confirm unless running non-interactively
    try:
        confirm = input(f"\nDelete these {len(to_delete)} novel(s)? [y/N] ").strip().lower()
    except EOFError:
        confirm = 'y'

    if confirm != 'y':
        print("Aborted.")
        return

    deleted = []
    for novel in to_delete:
        slug = novel["slug"]
        print(f"\nDeleting: {novel.get('title', slug)} ({slug})")
        delete_novel_files(slug)
        deleted.append(slug)

    # Update index.json once at the end
    index["novels"] = to_keep
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Removed {len(deleted)} novel(s) from index.json")

    if auto_push and deleted:
        import subprocess
        print("\n[git] Staging and pushing deletions…")

        def run(cmd):
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.returncode, (r.stdout + r.stderr).strip()

        run(["git", "add", "-A", os.path.join(SITE_DIR, "data")])
        run(["git", "add", "-A", os.path.join(SITE_DIR, "read")])
        run(["git", "add", "-A", "output"])
        run(["git", "add", index_path])

        msg = f"delete novels with genre(s): {', '.join(sorted(target))}"
        code, out = run(["git", "commit", "-m", msg])
        if code != 0:
            print(f"  Commit failed: {out}")
            return
        code, out = run(["git", "push"])
        if code == 0:
            print(f"  ✓ Pushed to GitHub.")
        else:
            print(f"  Push failed: {out}")


def regenerate_static_html() -> None:
    """
    Read every chapter JSON already in docs/data/ and write the
    corresponding static HTML to docs/read/ — no network requests needed.
    Use this after updating the scraper to add static HTML support,
    so existing novels get reader-compatible pages without re-scraping.
    """
    data_dir = os.path.join(SITE_DIR, "data")
    if not os.path.isdir(data_dir):
        print("No docs/data/ directory found. Run the scraper first.")
        return

    slugs = [
        d for d in os.listdir(data_dir)
        if os.path.isdir(os.path.join(data_dir, d)) and d != "."
    ]
    if not slugs:
        print("No novels found in docs/data/.")
        return

    print(f"Regenerating static HTML for {len(slugs)} novel(s)…\n")
    total_written = 0

    for slug in sorted(slugs):
        meta_path = os.path.join(data_dir, slug, "meta.json")
        if not os.path.exists(meta_path):
            print(f"  [{slug}] No meta.json — skipping.")
            continue

        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

        novel_name = meta.get("title", slug.replace("-", " ").title())
        chapters   = meta.get("chapters", [])
        all_nums   = [c["num"] for c in chapters]
        written    = 0

        print(f"  {novel_name} — {len(all_nums)} chapter(s)")

        for idx, ch_meta in enumerate(chapters):
            num       = ch_meta["num"]
            ch_path   = os.path.join(data_dir, slug, "chapters", f"{num}.json")
            if not os.path.exists(ch_path):
                continue
            with open(ch_path, encoding="utf-8") as f:
                ch = json.load(f)

            prev_num = all_nums[idx - 1] if idx > 0 else None
            next_num = all_nums[idx + 1] if idx < len(all_nums) - 1 else None
            export_chapter_html(slug, num, ch.get("title", f"Chapter {num}"),
                                ch.get("content", ""), prev_num, next_num, novel_name)
            written += 1
            print(f"    {written}/{len(all_nums)}", end="\r", flush=True)

        print(f"    ✓ {written} pages written to {SITE_DIR}/read/{slug}/")
        total_written += written

    print(f"\nDone — {total_written} static HTML page(s) written.")
    print(f"Push docs/read/ to GitHub to make them live.")


def update_all_local_novels(
    export_site: bool = True,
    export_epub: bool = True,
    auto_push: bool = False,
    cloud: bool = False,
    cloud_only: bool = False,
    excluded_genres: set[str] | None = None,
) -> None:
    excluded_genres = excluded_genres or set()
    if cloud_only:
        slugs = cloud_get_all_slugs()
        if not slugs:
            print("No novels found in Vercel Blob index — nothing to update.")
            return
    else:
        slugs = get_all_local_slugs()
        if not slugs:
            print("No novels found in output/ or docs/data/ — nothing to update.")
            return

    print(f"Checking {len(slugs)} local novel(s) for updates…\n")
    up_to_date = updated = errors = 0

    for idx, slug in enumerate(sorted(slugs), 1):
        novel_url  = url_for_slug(slug)
        print(f"[{idx}/{len(slugs)}] {slug}")
        if cloud_only:
            local_highest, _ = cloud_get_novel_state(slug)
        else:
            local_highest, _ = get_local_novel_state(slug)
        info  = get_novel_page_info(novel_url)
        total = info["total"]
        if not total:
            print("  [warn] Could not fetch chapter count — skipping.")
            errors += 1
            time.sleep(PAGE_DELAY)
            continue
        if local_highest >= total:
            print(f"  ✓ Up to date ({total} chapters)")
            up_to_date += 1
            time.sleep(PAGE_DELAY)
            continue
        print(f"  ↑ {total - local_highest} new chapter(s) (local: {local_highest}, remote: {total})")
        novel_name = info.get("title") or slug.replace("-", " ").title()
        success = scrape_novel(novel_url, export_site=export_site, export_epub=export_epub,
                               cloud=cloud, cloud_only=cloud_only,
                               excluded_genres=excluded_genres)
        if success:
            updated += 1
            if auto_push and export_site:
                git_push_novel(slug, novel_name)
        else:
            errors += 1
        time.sleep(NOVEL_DELAY)

    print(f"\n{'═'*60}")
    print(f"Update complete — {updated} updated, {up_to_date} already current, {errors} error(s).")


# ─────────────────── resume / progress tracking ────────────────
def load_progress() -> set[str]:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(novel_url: str) -> None:
    with open(PROGRESS_FILE, "a") as f:
        f.write(novel_url + "\n")


def _print_status(cloud: bool = False) -> None:
    """
    Print a summary of the local (and optionally Blob) library:
    each novel, its chapter count, last-updated date, and any
    pending failed chapters. Also shows the failed-chapter count.
    """
    index_path = os.path.join(SITE_DIR, "data", "index.json")
    novels: list[dict] = []

    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            novels = json.load(f).get("novels", [])

    if cloud and not novels:
        blob_index = cloud_download_json("data/index.json")
        if blob_index:
            novels = blob_index.get("novels", [])

    failed = load_failed_chapters()
    failed_by_slug: dict[str, int] = {}
    for slug, _, _, _ in failed:
        failed_by_slug[slug] = failed_by_slug.get(slug, 0) + 1

    if not novels:
        print("Library is empty — no novels found.")
        return

    novels_sorted = sorted(novels, key=lambda n: n.get("lastUpdated", ""), reverse=True)
    total_ch = sum(n.get("totalChapters", 0) for n in novels_sorted)

    print(f"\n{'═'*62}")
    print(f"  Ohara library status — {len(novels_sorted)} novel(s), "
          f"{total_ch:,} total chapters")
    print(f"{'═'*62}")
    for n in novels_sorted:
        slug    = n.get("slug", "?")
        title   = n.get("title", slug)[:45]
        chs     = n.get("totalChapters", 0)
        updated = n.get("lastUpdated", "—")
        fail    = failed_by_slug.get(slug, 0)
        fail_str = f"  ⚠ {fail} failed" if fail else ""
        print(f"  {title:<46} {chs:>5} ch   {updated}{fail_str}")
    print(f"{'─'*62}")
    if failed:
        print(f"  ⚠ {len(failed)} chapter(s) pending retry — run --retry-failed")
    else:
        print(f"  ✓ No failed chapters pending")
    print(f"{'═'*62}\n")



# ────────────────────────────── main ───────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ohara scraper — download novels from FreeWebNovel and publish them\n"
            "to your Ohara GitHub Pages site as JSON + static HTML.\n\n"
            "Common workflows:\n"
            "  Single novel:   --novel URL [--auto-push] [--no-epub]\n"
            "  Bulk scrape:    --listing URL --pages N [--auto-push] [--no-epub]\n"
            "  Update all:     --update [--auto-push] [--no-epub]\n"
            "  Auto-update:    --watch [--interval 60] [--auto-push] [--no-epub]\n"
            "  Rebuild HTML:   --rebuild-html\n"
            "  Retry failures: --retry-failed\n"
            "  Delete novel:   --delete-novel shadow-slave [--auto-push]\n"
            "  Delete genre:   --delete-genre harem smut [--dry-run] [--auto-push]\n"
            "  Skip genre:     --exclude-genre harem smut\n\n"
            "Running in the background with nohup:\n"
            "  # Single novel, background:\n"
            "  nohup python3 scrape_catalog.py --novel URL --no-epub --auto-push > output.log 2>&1 &\n\n"
            "  # Auto-update every hour, background:\n"
            "  nohup python3 scrape_catalog.py --watch --auto-push --no-epub > watch.log 2>&1 &\n\n"
            "  # Bulk batch in background (chain novels sequentially):\n"
            "  nohup bash -c '\n"
            "    python3 scrape_catalog.py --novel URL1 --no-epub --auto-push\n"
            "    python3 scrape_catalog.py --novel URL2 --no-epub --auto-push\n"
            "  ' > batch.log 2>&1 &\n\n"
            "Monitoring background jobs:\n"
            "  tail -f watch.log           # live log output\n"
            "  ps aux | grep scrape        # check if still running\n"
            "  pkill -f scrape_catalog.py  # stop all scraper processes"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--novel", default=None, metavar="URL",
        help=(
            "Scrape a single novel by URL and export it to the site.\n"
            "Example: --novel https://freewebnovel.com/novel/shadow-slave"
        ))
    parser.add_argument("--listing", default=DEFAULT_LISTING, metavar="URL",
        help=(
            "Listing page to crawl (sort or genre page). Paginates automatically.\n"
            "Examples: freewebnovel.com/sort/most-popular\n"
            "          freewebnovel.com/genre/Fantasy\n"
            f"Default: {DEFAULT_LISTING}"
        ))
    parser.add_argument("--pages", type=int, default=None, metavar="N",
        help="Only crawl the first N pages of the listing (useful for testing).")
    parser.add_argument("--resume", action="store_true",
        help=f"Skip novels already marked done in {PROGRESS_FILE}. Use after an interrupted run.")
    parser.add_argument("--update", action="store_true",
        help=(
            "Check every novel in output/ and docs/data/ against the site\n"
            "and download any new chapters. Combine with --auto-push to publish immediately."
        ))
    parser.add_argument("--watch", action="store_true",
        help=(
            "Run --update in a loop forever, sleeping --interval minutes between runs.\n"
            "Combine with --auto-push and --no-epub for a fully automated background updater.\n"
            "Example: --watch --auto-push --no-epub --interval 60\n"
            "Stop with Ctrl+C."
        ))
    parser.add_argument("--interval", type=int, default=60, metavar="MINUTES",
        help="Minutes to sleep between --watch update cycles. Default: 60.")
    parser.add_argument("--auto-push", action="store_true",
        help=(
            "After each novel, automatically:\n"
            "  1. Regenerate static HTML pages for Safari Reader (docs/read/<slug>/)\n"
            "  2. git add the novel's data and HTML files\n"
            "  3. git commit and git push to GitHub\n"
            "Requires git to be configured with push access."
        ))
    parser.add_argument("--fetch-tags", action="store_true",
        help="Fetch and update tags for all existing novels without re-scraping chapters.")
    parser.add_argument("--rebuild-html", action="store_true",
        help=(
            "Rebuild all static HTML reader pages from existing JSON in docs/data/.\n"
            "No network requests. Run this after updating the scraper to add new\n"
            "HTML features without re-scraping anything.\n"
            "Then: git add docs/read/ && git commit -m 'rebuild html' && git push"
        ))
    parser.add_argument("--retry-failed", action="store_true",
        help=f"Re-download all chapters logged as failed in {FAILED_FILE}.")
    parser.add_argument("--exclude-genre", nargs="+", metavar="GENRE", default=[],
        help=(
            "Skip any novel whose tags include any of these genres.\n"
            "Case-insensitive. Works with --novel, --listing, --update, --watch.\n"
            "Example: --exclude-genre harem smut adult\n"
            "To permanently exclude genres without typing this every time,\n"
            f"edit DEFAULT_EXCLUDED_GENRES at the top of {__file__}."
        ))
    parser.add_argument("--delete-novel", metavar="SLUG",
        help=(
            "Delete a single novel by its slug (the name in the URL).\n"
            "Removes JSON data, static HTML, epubs, and clears its\n"
            "progress/failed-chapter log entries so it can be freely\n"
            "re-downloaded or re-deleted anytime.\n"
            "Example: --delete-novel shadow-slave\n"
            "Add --auto-push to commit and push the deletion automatically."
        ))
    parser.add_argument("--delete-genre", nargs="+", metavar="GENRE",
        help=(
            "Delete all novels tagged with any of the given genres.\n"
            "Case-insensitive. Removes JSON data, static HTML, epubs,\n"
            "and clears progress/failed-chapter logs for each novel.\n"
            "Example: --delete-genre harem smut adult\n"
            "Add --dry-run to preview without deleting anything.\n"
            "Add --auto-push to commit and push the deletions automatically."
        ))
    parser.add_argument("--dry-run", action="store_true",
        help=(
            "Preview mode — nothing is deleted or downloaded.\n"
            "Works with: --delete-genre (show what would be deleted)\n"
            "            --delete-novel (show what would be removed)\n"
            "            --listing      (just print discovered URLs)"
        ))
    parser.add_argument("--status", action="store_true",
        help=(
            "Print a summary of your local library: each novel, chapter count,\n"
            "last-updated date, and any pending failed chapters.\n"
            "Add --cloud to also check Vercel Blob."
        ))
    parser.add_argument("--no-epub", action="store_true",
        help=(
            "Skip EPUB generation. Only exports JSON and static HTML for the site.\n"
            "Recommended for overnight runs to save disk space."
        ))
    parser.add_argument("--no-site", action="store_true",
        help=f"Skip site JSON and HTML export. Only generates EPUBs in output/.")
    parser.add_argument("--cloud", action="store_true",
        help=(
            "Upload scraped JSON to Vercel Blob storage IN ADDITION to writing local files.\n"
            "Requires BLOB_READ_WRITE_TOKEN in .env.\n"
            "Example: --novel URL --cloud --no-epub"
        ))
    parser.add_argument("--cloud-only", action="store_true",
        help=(
            "Upload scraped JSON to Vercel Blob storage ONLY — skip writing local docs/data/ files.\n"
            "Requires BLOB_READ_WRITE_TOKEN in .env.\n"
            "Ideal when running from a machine without the full repo checked out.\n"
            "Example: --novel URL --cloud-only --no-epub"
        ))
    parser.add_argument("--upload-existing", action="store_true",
        help=(
            "Upload all existing local docs/data/ JSON files to Vercel Blob.\n"
            "Run this once to seed the Blob store from your current library.\n"
            "No re-scraping — reads from disk only."
        ))
    args = parser.parse_args()

    export_site = not args.no_site
    export_epub = not args.no_epub

    cloud      = args.cloud or args.cloud_only
    cloud_only = args.cloud_only
    if cloud_only:
        export_site = False

    if cloud and not BLOB_TOKEN:
        print(
            "Error: --cloud / --cloud-only requires BLOB_READ_WRITE_TOKEN.\n"
            "Create a .env file in the project root and add:\n"
            "  BLOB_READ_WRITE_TOKEN=vercel_blob_rw_...\n"
            "then run:  pip install python-dotenv"
        )
        sys.exit(1)

    if cloud and BLOB_TOKEN and not re.match(r"^vercel_blob_rw_[A-Za-z0-9]+_[A-Za-z0-9]+$", BLOB_TOKEN):
        print(
            "Error: BLOB_READ_WRITE_TOKEN doesn't look like a valid Vercel Blob token.\n"
            "Expected format: vercel_blob_rw_<store_id>_<secret>\n"
            f"Got: {BLOB_TOKEN[:20]}{'...' if len(BLOB_TOKEN) > 20 else ''}\n"
            "Double-check the value copied from your Vercel Blob store settings."
        )
        sys.exit(1)

    # Merge CLI --exclude-genre with any permanently configured DEFAULT_EXCLUDED_GENRES
    excluded_genres = DEFAULT_EXCLUDED_GENRES | {g.lower() for g in args.exclude_genre}
    if excluded_genres:
        print(f"Genre filter active — skipping novels tagged: {', '.join(sorted(excluded_genres))}")

    if args.status:
        _print_status(cloud=cloud)
        return

    if args.delete_novel:
        if args.dry_run:
            # Preview what would be deleted without doing anything
            slug = args.delete_novel
            index_path = os.path.join(SITE_DIR, "data", "index.json")
            name = slug
            if os.path.exists(index_path):
                with open(index_path, encoding="utf-8") as fh:
                    idx = json.load(fh)
                m = next((n for n in idx.get("novels", []) if n["slug"] == slug), None)
                if m:
                    name = m.get("title", slug)
            local_data  = os.path.isdir(os.path.join(SITE_DIR, "data", slug))
            local_epub  = os.path.isdir(os.path.join("output", slug))
            blob_meta   = cloud_download_json(f"data/{slug}/meta.json") if cloud else None
            print(f"[Dry run] Would delete: {name} ({slug})")
            print(f"  Local data (docs/data/): {'yes' if local_data else 'no'}")
            print(f"  Local epubs (output/):   {'yes' if local_epub else 'no'}")
            if cloud:
                print(f"  Vercel Blob:             {'yes' if blob_meta and not blob_meta.get('deleted') else 'no'}")
            print("Remove --dry-run to actually delete.")
            return
        delete_novel(args.delete_novel, auto_push=args.auto_push, cloud=cloud)
        return

    if args.delete_genre:
        delete_by_genre(args.delete_genre, dry_run=args.dry_run, auto_push=args.auto_push)
        return

    if args.upload_existing:
        cloud_upload_existing()
        return

    if args.rebuild_html:
        regenerate_static_html()
        return

    if args.fetch_tags:
        fetch_tags_for_all(auto_push=args.auto_push, cloud=cloud)
        return

    # ── watch mode: loop forever, updating every --interval minutes ──
    if args.watch:
        interval_secs = args.interval * 60
        print(f"Watch mode: updating every {args.interval} minute(s). Press Ctrl+C to stop.\n")
        run = 0
        while True:
            run += 1
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{'═'*60}")
            print(f"[Watch] Run #{run} started at {now}")
            print(f"{'═'*60}")
            update_all_local_novels(export_site=export_site, export_epub=export_epub,
                                    auto_push=args.auto_push, cloud=cloud,
                                    cloud_only=cloud_only,
                                    excluded_genres=excluded_genres)
            next_time = time.strftime("%H:%M:%S", time.localtime(time.time() + interval_secs))
            print(f"\n[Watch] Next update at {next_time} — sleeping for {args.interval} minute(s)…")
            try:
                time.sleep(interval_secs)
            except KeyboardInterrupt:
                print("\n[Watch] Stopped.")
                return

    if args.update:
        update_all_local_novels(export_site=export_site, export_epub=export_epub,
                                auto_push=args.auto_push, cloud=cloud,
                                cloud_only=cloud_only,
                                excluded_genres=excluded_genres)
        return

    if args.retry_failed:
        retry_failed_chapters(export_site=export_site, cloud=cloud)
        return

    # ── single novel shortcut ────────────────────────────────────
    if args.novel:
        url = args.novel.rstrip("/")
        if not url.startswith("http"):
            url = f"{BASE_URL}/novel/{url}"
        print(f"Scraping single novel: {url}")
        success = scrape_novel(url, export_site=export_site, export_epub=export_epub,
                               cloud=cloud, cloud_only=cloud_only,
                               excluded_genres=excluded_genres)
        if success:
            mark_done(url)
            slug = slug_from_url(url)
            novel_name = slug.replace("-", " ").title()
            if args.auto_push and export_site:
                git_push_novel(slug, novel_name)
            else:
                print(f"-> Run: git add docs/ && git commit -m 'add novel' && git push")
        return

    novel_urls = discover_all_novels(args.listing, max_pages=args.pages)
    if not novel_urls:
        print("No novels found. Exiting.")
        sys.exit(0)

    if args.dry_run:
        print("── Dry run — discovered novels ──")
        for url in novel_urls:
            print(url)
        print(f"\nTotal: {len(novel_urls)}")
        return

    done    = load_progress() if args.resume else set()
    skipped = [u for u in novel_urls if u in done]
    queue   = [u for u in novel_urls if u not in done]

    if skipped:
        print(f"Resuming: skipping {len(skipped)} already-completed novel(s).")
    print(f"Novels to scrape: {len(queue)}\n")

    for idx, url in enumerate(queue, 1):
        print(f"\n[{idx}/{len(queue)}] Starting novel: {url}")
        success = scrape_novel(url, export_site=export_site, export_epub=export_epub,
                               cloud=cloud, cloud_only=cloud_only,
                               excluded_genres=excluded_genres)
        if success:
            mark_done(url)
            if args.auto_push and export_site:
                slug = slug_from_url(url)
                novel_name = slug.replace("-", " ").title()
                git_push_novel(slug, novel_name)
        time.sleep(NOVEL_DELAY)

    print(f"\n{'═'*60}")
    if export_epub:
        print(f"EPUBs saved in output/")
    if export_site:
        print(f"Site data saved in {SITE_DIR}/data/")
        print(f"→ Push {SITE_DIR}/ to GitHub and enable GitHub Pages from that folder.")


if __name__ == "__main__":
    main()
