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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup
from ebooklib import epub

# ─────────────────────────── config ───────────────────────────
BASE_URL          = "https://freewebnovel.com"
DEFAULT_LISTING   = f"{BASE_URL}/sort/latest-novel"
CHAPTERS_PER_VOL  = 5000
CHAPTER_WORKERS   = 8       # parallel chapter fetches per novel
CHAPTER_DELAY     = 5       # seconds between chapter requests (be polite)
PAGE_DELAY        = 2       # seconds between listing page fetches
NOVEL_DELAY       = 3       # seconds between novels
PROGRESS_FILE     = "scraped_novels.txt"   # tracks completed novels for --resume
FAILED_FILE       = "failed_chapters.txt"  # tracks chapters that failed after all retries
RETRY_ATTEMPTS    = 3                      # how many times to retry a failed chapter
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
) -> None:
    """
    Merge-write docs/data/<slug>/meta.json.

    `chapters` is a list of (num, title) tuples for the chapters being added.
    Existing chapters not in this batch are preserved.
    """
    meta_path = os.path.join(SITE_DIR, "data", slug, "meta.json")
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)

    # Load existing meta if present
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        meta = {"slug": slug, "title": title, "chapters": []}

    # Always refresh top-level fields
    meta["slug"]        = slug
    meta["title"]       = title
    meta["lastUpdated"] = date.today().isoformat()
    if cover_url:
        meta["cover"] = cover_url
    if source_url:
        meta["source"] = source_url

    # Merge in new chapters
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
    else:
        index["novels"].append({
            "slug":          slug,
            "title":         title,
            "cover":         cover_url,
            "totalChapters": total_chapters,
            "lastUpdated":   date.today().isoformat(),
        })

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


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
    """
    try:
        res = requests.get(novel_url, headers=HEADERS, timeout=20)
        res.raise_for_status()
    except Exception as e:
        print(f"  [warn] Could not fetch novel page: {e}")
        return {"total": None, "cover": "", "title": ""}

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

    return {"total": total, "cover": cover, "title": title}


# ───────────────────────── chapter scraping ────────────────────
def _fetch_chapter_once(i: int, url: str) -> tuple[int, str, str]:
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    time.sleep(CHAPTER_DELAY)
    soup = BeautifulSoup(res.text, "html.parser")
    title_tag  = soup.select_one("h2")
    title_text = title_tag.get_text(strip=True) if title_tag else f"Chapter {i}"
    content_div = soup.select_one("div.txt")
    if not content_div:
        return i, title_text, ""
    for tag in content_div.select("script, style, ins, iframe, h1, h2, h3"):
        tag.decompose()
    text    = content_div.get_text(separator="\n")
    cleaned = clean_text(text)
    if not cleaned:
        raise ValueError("Content div found but cleaned text is empty")
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


def retry_failed_chapters(export_site: bool = True) -> None:
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
        novel_url  = f"{BASE_URL}/novel/{slug}"
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

    clear_failed_log()
    if still_failed:
        for slug, num, url, reason in still_failed:
            log_failed_chapter(slug, num, url, reason)
        print(f"\n{len(still_failed)} chapter(s) still failing — logged to {FAILED_FILE}")
    else:
        print("\nAll previously failed chapters recovered successfully.")


# ──────────────────── local output inspection ─────────────────
def get_local_novel_state(slug: str) -> tuple[int, int]:
    novel_dir = os.path.join("output", slug)
    if not os.path.isdir(novel_dir):
        return 0, 1
    max_chapter = 0
    max_vol     = 0
    for fname in os.listdir(novel_dir):
        if not fname.endswith(".epub"):
            continue
        m = re.search(r"-vol(\d+)-ch\d+-(\d+)\.epub$", fname)
        if m:
            vol_num = int(m.group(1))
            end_ch  = int(m.group(2))
            max_chapter = max(max_chapter, end_ch)
            max_vol     = max(max_vol, vol_num)
    return max_chapter, max_vol + 1


def get_all_local_slugs() -> list[str]:
    output_dir = "output"
    if not os.path.isdir(output_dir):
        return []
    return [
        name for name in os.listdir(output_dir)
        if os.path.isdir(os.path.join(output_dir, name))
    ]


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
) -> bool:
    slug       = novel_url.rstrip("/").split("/")[-1]
    print(f"\n{'─'*60}")
    print(f"Novel: {slug}")
    print(f"URL:   {novel_url}")

    # ── what we already have locally ────────────────────────────
    local_highest, next_vol_num = (0, 1) if force_full else get_local_novel_state(slug)
    if local_highest:
        print(f"  Local:  {local_highest} chapter(s) already on disk (next vol → Vol.{next_vol_num})")

    # ── fetch novel page info ────────────────────────────────────
    info       = get_novel_page_info(novel_url)
    total      = info["total"]
    cover_url  = info["cover"]
    novel_name = info["title"] or slug.replace("-", " ").title()

    if not total:
        print("  [warn] Could not determine chapter count — skipping.")
        return False
    print(f"  Title:  {novel_name}")
    print(f"  Cover:  {cover_url or '(none)'}")
    print(f"  Remote: {total} chapter(s) available")

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
        tasks = [(i, f"{novel_url}/chapter-{i}", slug) for i in range(start, end + 1)]
        results: dict[int, tuple[str, str]] = {}

        with ThreadPoolExecutor(max_workers=CHAPTER_WORKERS) as executor:
            futures = {executor.submit(scrape_chapter, t): t for t in tasks}
            for future in as_completed(futures):
                i, title, content, ok = future.result()
                results[i] = (title, content)
                if not ok:
                    total_failed += 1
                print(f"  ✓ Chapter {i}/{total}", end="\r", flush=True)
        print()

        chapters_data = [
            (i, *results.get(i, (f"Chapter {i}", ""))) for i in range(start, end + 1)
        ]

        # ── export JSON + static HTML per chapter ───────────────
        if export_site:
            for i, title, content in chapters_data:
                if content:
                    export_chapter_json(slug, i, title, content)
                    all_ch_tuples.append((i, title))
                    all_ch_data.append((i, title, content))
        else:
            all_ch_tuples.extend(
                (i, t) for i, t, c in chapters_data if c
            )

        # ── write epub ───────────────────────────────────────────
        if export_epub:
            make_epub(slug, novel_name, chapters_data, vol_num, start, end)

        time.sleep(2)

    # ── update site index & meta ─────────────────────────────────
    if export_site:
        print(f"  Updating Ohara site data…")
        upsert_novel_meta(slug, novel_name, all_ch_tuples,
                          cover_url=cover_url, source_url=novel_url)
        upsert_site_index(slug, novel_name, len(all_ch_tuples) + local_highest,
                          cover_url=cover_url)

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
    Stage this novel's site files and push to GitHub.
    Called after each novel when --auto-push is set.
    Returns True if push succeeded.
    """
    import subprocess

    data_path = os.path.join(SITE_DIR, "data", slug)
    read_path = os.path.join(SITE_DIR, "read", slug)
    index_path = os.path.join(SITE_DIR, "data", "index.json")

    print(f"\n  [git] Pushing {novel_name} to GitHub…")

    def run(cmd: list[str]) -> tuple[int, str]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()

    # Stage novel data + updated index
    for path in [data_path, read_path, index_path]:
        if os.path.exists(path):
            code, out = run(["git", "add", path])
            if code != 0:
                print(f"  [git] Warning: git add failed for {path}: {out}")

    # Check if there's anything to commit
    code, out = run(["git", "status", "--porcelain"])
    if not out.strip():
        print(f"  [git] Nothing new to commit for {novel_name} — already up to date.")
        return True

    # Commit
    msg = f"add/update: {novel_name}"
    code, out = run(["git", "commit", "-m", msg])
    if code != 0:
        print(f"  [git] Commit failed: {out}")
        return False
    print(f"  [git] Committed: {msg}")

    # Push
    code, out = run(["git", "push"])
    if code != 0:
        print(f"  [git] Push failed: {out}")
        print(f"  [git] Files are committed locally — run 'git push' manually later.")
        return False

    print(f"  [git] ✓ Pushed to GitHub successfully.")
    return True


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


def update_all_local_novels(export_site: bool = True, export_epub: bool = True) -> None:
    slugs = get_all_local_slugs()
    if not slugs:
        print("No novels found in output/ — nothing to update.")
        return

    print(f"Checking {len(slugs)} local novel(s) for updates…\n")
    up_to_date = updated = errors = 0

    for idx, slug in enumerate(sorted(slugs), 1):
        novel_url = f"{BASE_URL}/novel/{slug}"
        print(f"[{idx}/{len(slugs)}] {slug}")
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
        success = scrape_novel(novel_url, export_site=export_site, export_epub=export_epub)
        if success:
            updated += 1
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


# ────────────────────────────── main ───────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape FreeWebNovel and export EPUBs + Ohara site JSON."
    )
    parser.add_argument("--novel", default=None, metavar="URL",
        help="Scrape a single novel directly, e.g. https://freewebnovel.com/novel/shadow-slave")
    parser.add_argument("--listing", default=DEFAULT_LISTING,
        help=f"Listing URL to crawl. Default: {DEFAULT_LISTING}")
    parser.add_argument("--pages", type=int, default=None, metavar="N",
        help="Only crawl the first N pages of the listing.")
    parser.add_argument("--resume", action="store_true",
        help=f"Skip novels already listed in {PROGRESS_FILE}.")
    parser.add_argument("--update", action="store_true",
        help="Check every novel already in output/ for new chapters.")
    parser.add_argument("--retry-failed", action="store_true",
        help=f"Re-attempt all chapters logged in {FAILED_FILE}.")
    parser.add_argument("--dry-run", action="store_true",
        help="Print discovered URLs without scraping.")
    parser.add_argument("--auto-push", action="store_true",
        help="After each novel, git commit and push to GitHub automatically.")
    parser.add_argument("--rebuild-html", action="store_true",
        help="Regenerate static HTML pages for all novels from existing JSON. No scraping.")
    parser.add_argument("--no-site", action="store_true",
        help=f"Skip JSON export to {SITE_DIR}/data/ (epub only).")
    parser.add_argument("--no-epub", action="store_true",
        help="Skip epub generation (site JSON only).")
    args = parser.parse_args()

    export_site = not args.no_site
    export_epub = not args.no_epub

    if args.rebuild_html:
        regenerate_static_html()
        return

    if args.update:
        update_all_local_novels(export_site=export_site, export_epub=export_epub)
        return

    if args.retry_failed:
        retry_failed_chapters(export_site=export_site)
        return

    # ── single novel shortcut ────────────────────────────────────
    if args.novel:
        url = args.novel.rstrip("/")
        if not url.startswith("http"):
            url = f"{BASE_URL}/novel/{url}"
        print(f"Scraping single novel: {url}")
        success = scrape_novel(url, export_site=export_site, export_epub=export_epub)
        if success:
            mark_done(url)
            slug = url.rstrip("/").split("/")[-1]
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
        success = scrape_novel(url, export_site=export_site, export_epub=export_epub)
        if success:
            mark_done(url)
            if args.auto_push and export_site:
                slug = url.rstrip("/").split("/")[-1]
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
