# 📚 Ohara — Personal Novel Library

A self-hosted static web library for your scraped novels.  
Scrape → export JSON/HTML → push to GitHub/Vercel Blob → read anywhere.

---

## Repo structure

```text
ohara/
├── docs/                  ← GitHub Pages source (deploy this folder)
│   ├── index.html         ← Novel grid / homepage
│   ├── novel.html         ← Novel detail + chapter list
│   ├── chapter.html       ← Chapter reader
│   ├── style.css
│   ├── app.js
│   ├── read/              ← Static HTML exported chapters
│   └── data/
│       ├── index.json     ← Master novel list (auto-generated)
│       └── <slug>/
│           ├── meta.json  ← Novel metadata + chapter index
│           └── chapters/
│               ├── 1.json
│               ├── 2.json
│               └── …
├── output/                ← EPUB files (not committed, gitignored)
├── ohara.py               ← The scraper (aka scrape_catalog.py)
├── scraped_novels.txt     ← Resume tracking (auto-created)
└── failed_chapters.txt    ← Failed chapter log (auto-created)
```

---

## 1 · Install dependencies

```bash
pip install requests beautifulsoup4 ebooklib python-dotenv
```

---

## 2 · Scrape novels

The scraper (`ohara.py`) can write EPUBs to `output/`, JSON to `docs/data/`, and static HTML to `docs/read/`. It can also upload directly to Vercel Blob using the `--cloud` flags.

### Basic Scraping

```bash
# Scrape the default listing (latest novels)
python ohara.py

# Scrape a specific single novel
python ohara.py --novel shadow-slave

# Scrape a specific genre or sort page
python ohara.py --listing https://freewebnovel.com/genre/Fantasy

# Only first 3 pages of a listing
python ohara.py --pages 3

# Skip novels already completed in previous runs
python ohara.py --resume

# Exclude specific genres from being scraped
python ohara.py --exclude-genre Yaoi Yuri "Shounen Ai"
```

### Maintenance and Updates

```bash
# Check every local novel for new chapters
python ohara.py --update

# Run continuously, checking for updates every 60 minutes
python ohara.py --watch --interval 60

# Automatically commit and push to GitHub after updates finish
python ohara.py --update --auto-push

# Retry any chapters that failed
python ohara.py --retry-failed

# Fetch tags for existing novels
python ohara.py --fetch-tags

# Print a summary of the local and cloud library
python ohara.py --status
```

### Output Options

```bash
# Only export JSON (no epub) — useful for site-only updates
python ohara.py --no-epub

# Only export epub (skip site JSON)
python ohara.py --no-site

# Generate static HTML files for SEO and non-JS clients
python ohara.py --html

# Rebuild all static HTML files for existing novels
python ohara.py --rebuild-html
```

### Cloud Storage (Vercel Blob)

Requires `BLOB_READ_WRITE_TOKEN` in your `.env` file.

```bash
# Upload scraped data to Vercel Blob alongside local files
python ohara.py --cloud

# Upload directly to Vercel Blob ONLY (no local files written)
python ohara.py --cloud-only

# Upload your existing local library to Vercel Blob
python ohara.py --upload-existing
```

### Deletion

```bash
# Delete a specific novel locally (and optionally in the cloud)
python ohara.py --delete-novel shadow-slave

# Delete all novels of specific genres (use --dry-run to preview)
python ohara.py --delete-genre Yaoi Yuri --dry-run
```

### Speed & Proxy Tuning

The scraper supports proxy rotation, User-Agent rotation, and adjustable concurrency to speed up scraping and avoid IP-based rate limiting.

```bash
# Fast mode — 5x speed boost (reduces delays, increases workers to 16)
python ohara.py --novel URL --fast

# Fast mode + proxy rotation (recommended for heavy scraping)
python ohara.py --novel URL --fast --proxy-file proxies.txt

# Custom worker count and delay
python ohara.py --novel URL --workers 24 --delay 1

# Full speed bulk update with proxies
python ohara.py --update --fast --proxy-file proxies.txt --no-epub --auto-push
```

| Flag | Default | Description |
|------|---------|-------------|
| `--fast` | off | Speed preset: delay=1s, page_delay=0.5s, novel_delay=1s, workers=16 |
| `--workers N` | 8 | Parallel chapter-download threads per novel |
| `--delay SECS` | 5 | Seconds to wait after each chapter request (per-thread) |
| `--proxy-file FILE` | none | Path to a proxy list file for round-robin rotation |

> **Note:** `--workers` and `--delay` override `--fast` values when used together.

**Proxy file format** (`proxies.txt`):
```text
# One proxy per line — lines starting with '#' are ignored
http://123.45.67.89:8080
socks5://proxy.example.com:1080
host:port
http://user:pass@proxy.example.com:3128
```

> **Warning:** Using `--fast` without proxies may trigger anti-bot blocks (429 errors). Combine with `--proxy-file` for best results.

---

## 3 · Deploy to GitHub Pages

### First time

```bash
git init
git add docs/ ohara.py README.md
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

Then in GitHub → **Settings → Pages**:
- Source: `Deploy from a branch`
- Branch: `main`
- Folder: `/docs`
- Save

Your site will be live at `https://YOUR_USERNAME.github.io/YOUR_REPO/`

### Updating with new chapters

```bash
# Scrape new content
python ohara.py --update

# Push the updated data files
git add docs/data/ docs/read/
git commit -m "update novels $(date +%Y-%m-%d)"
git push
```
Or simply use `python ohara.py --update --auto-push` to do this automatically!

---

## 4 · Gitignore

Create a `.gitignore` so you don't accidentally commit the large epub files or sensitive environment variables:

```text
output/
scraped_novels.txt
failed_chapters.txt
__pycache__/
*.pyc
.env
```

---

## 5 · Site features

| Feature | Detail |
|---------|--------|
| Novel grid | Cover image, chapter count, last updated |
| Live search | Filter novels by name instantly |
| Content Filter| Password-protected filter to lock/unlock specific novel visibility |
| Chapter list | Paginated list (100 per page) |
| Previous / Next | Navigate between chapters |
| Merge Next 10 | Load and append the next 10 chapters inline without leaving the page |
| Font size | A− / A+ controls saved to localStorage |
| Dark theme | Easy on the eyes for long reading sessions |

> **Note on Content Filter:** The filter is locked by default and requires a password to unlock. To change the password, search for `prompt("Enter password to unlock:")` in `docs/index.html` and change the string it compares against.

---

## Data format reference

### `docs/data/index.json`
```json
{
  "novels": [
    {
      "slug": "the-beginning-after-the-end",
      "title": "The Beginning After the End",
      "cover": "https://…/cover.jpg",
      "totalChapters": 450,
      "lastUpdated": "2025-01-15"
    }
  ]
}
```

### `docs/data/<slug>/meta.json`
```json
{
  "slug": "the-beginning-after-the-end",
  "title": "The Beginning After the End",
  "cover": "https://…/cover.jpg",
  "source": "https://freewebnovel.com/novel/the-beginning-after-the-end",
  "totalChapters": 450,
  "lastUpdated": "2025-01-15",
  "tags": ["Action", "Adventure"],
  "chapters": [
    { "num": 1, "title": "Chapter 1: Prologue" },
    { "num": 2, "title": "Chapter 2: A New World" }
  ]
}
```

### `docs/data/<slug>/chapters/<n>.json`
```json
{
  "num": 1,
  "title": "Chapter 1: Prologue",
  "content": "Paragraph one.\n\nParagraph two.\n\nParagraph three."
}
```
