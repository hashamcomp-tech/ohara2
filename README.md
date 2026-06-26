# 📚 Ohara — Personal Novel Library

A self-hosted static web library for your scraped novels.  
Scrape → export JSON → push to GitHub → read anywhere.

---

## Repo structure

```
ohara/
├── docs/                  ← GitHub Pages source (deploy this folder)
│   ├── index.html         ← Novel grid / homepage
│   ├── novel.html         ← Novel detail + chapter list
│   ├── chapter.html       ← Chapter reader
│   ├── style.css
│   ├── app.js
│   └── data/
│       ├── index.json     ← Master novel list (auto-generated)
│       └── <slug>/
│           ├── meta.json  ← Novel metadata + chapter index
│           └── chapters/
│               ├── 1.json
│               ├── 2.json
│               └── …
├── output/                ← EPUB files (not committed, gitignored)
├── scrape_catalog.py      ← The scraper
├── scraped_novels.txt     ← Resume tracking (auto-created)
└── failed_chapters.txt    ← Failed chapter log (auto-created)
```

---

## 1 · Install dependencies

```bash
pip install requests beautifulsoup4 ebooklib
```

---

## 2 · Scrape novels

The scraper writes EPUBs to `output/` **and** JSON to `docs/data/` automatically.

```bash
# Scrape the default listing (latest novels)
python scrape_catalog.py

# Scrape a specific genre or sort page
python scrape_catalog.py --listing https://freewebnovel.com/genre/Fantasy
python scrape_catalog.py --listing https://freewebnovel.com/sort/most-popular

# Only first 3 pages of a listing
python scrape_catalog.py --pages 3

# Skip novels already completed in previous runs
python scrape_catalog.py --resume

# Check every novel already in output/ for new chapters
python scrape_catalog.py --update

# Retry any chapters that failed
python scrape_catalog.py --retry-failed

# Only export JSON (no epub) — useful for site-only updates
python scrape_catalog.py --no-epub

# Only export epub (skip site JSON)
python scrape_catalog.py --no-site
```

---

## 3 · Deploy to GitHub Pages

### First time

```bash
git init
git add docs/ scrape_catalog.py README.md
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
python scrape_catalog.py --update

# Push the updated data files
git add docs/data/
git commit -m "update novels $(date +%Y-%m-%d)"
git push
```

GitHub Pages rebuilds automatically within ~30 seconds.

---

## 4 · Gitignore

Create a `.gitignore` so you don't accidentally commit the large epub files:

```
output/
scraped_novels.txt
failed_chapters.txt
__pycache__/
*.pyc
```

---

## 5 · Site features

| Feature | Detail |
|---------|--------|
| Novel grid | Cover image, chapter count, last updated |
| Live search | Filter novels by name instantly |
| Chapter list | Paginated list (100 per page) |
| Previous / Next | Navigate between chapters |
| Merge Next 10 | Load and append the next 10 chapters inline without leaving the page |
| Font size | A− / A+ controls saved to localStorage |
| Dark theme | Easy on the eyes for long reading sessions |

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
