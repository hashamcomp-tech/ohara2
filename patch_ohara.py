import re

with open('scrape_catalog.py', 'r') as f:
    content = f.read()

# 1. Imports
content = content.replace('import sys\n', 'import sys\nimport random\nimport itertools\nfrom concurrent.futures import ThreadPoolExecutor\nimport threading\n')

# 2. Proxy Globals and logic
proxy_code = """
BASE_URL = "https://freewebnovel.com"
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1"
]

_proxy_list: list[dict[str, str]] = []
_all_proxies: list[dict[str, str]] = []
_proxy_cycle = None
_proxy_lock = threading.Lock()

def _load_proxies(path: str) -> None:
    global _proxy_list, _all_proxies, _proxy_cycle
    raw_proxies = []
    try:
        with open(path) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"): continue
                if not line.startswith(("http://", "https://", "socks5://", "socks4://")):
                    line = f"http://{line}"
                raw_proxies.append({"http": line, "https": line})
    except Exception as e:
        print(f"  [error] Could not load proxy file {path}: {e}")
        return
        
    if not raw_proxies: return
        
    print(f"  Testing {len(raw_proxies)} proxies for connectivity to {BASE_URL}...")
    import requests
    working_proxies = []
    
    def _check_proxy(p: dict[str, str]) -> dict[str, str] | None:
        try:
            res = requests.get(BASE_URL, proxies=p, timeout=10, headers={"User-Agent": random.choice(_UA_POOL)})
            if res.status_code in (200, 403, 404): return p
        except Exception: pass
        return None

    with ThreadPoolExecutor(max_workers=min(32, len(raw_proxies))) as executor:
        for result in executor.map(_check_proxy, raw_proxies):
            if result: working_proxies.append(result)

    if not working_proxies:
        print(f"  [warn] All {len(raw_proxies)} proxies failed the startup test. Will blindly cycle through all of them anyway to protect your IP.")
        working_proxies = list(raw_proxies)

    _all_proxies = list(raw_proxies)
    _proxy_list = list(working_proxies)
    _proxy_cycle = itertools.cycle(_proxy_list)
    print(f"  Loaded {len(_proxy_list)} proxy/proxies from {path}")

def _next_proxy() -> dict[str, str] | None:
    if _proxy_cycle is None: return None
    with _proxy_lock: return next(_proxy_cycle)

def _remove_proxy(proxy: dict[str, str]) -> None:
    global _proxy_list, _proxy_cycle
    with _proxy_lock:
        if proxy in _proxy_list:
            _proxy_list.remove(proxy)
            if not _proxy_list:
                print(f"  [proxy dropped] Removed {proxy.get('http')}... Pool empty! Restarting proxy loop from backup to protect your IP.")
                _proxy_list = list(_all_proxies)
            else:
                print(f"  [proxy dropped] Removed {proxy.get('http')} from rotation. {len(_proxy_list)} remaining.")
            _proxy_cycle = itertools.cycle(_proxy_list)

def _get(url: str, *, timeout: int = 20, use_proxy: bool = True, max_retries: int = 5):
    import requests
    last_err = None
    for attempt in range(max_retries):
        headers = {**HEADERS, "User-Agent": random.choice(_UA_POOL)}
        proxy = _next_proxy() if use_proxy else None
        try:
            return requests.get(url, headers=headers, timeout=timeout, proxies=proxy)
        except requests.RequestException as e:
            last_err = e
            if proxy:
                print(f"  [proxy retry] Attempt {attempt + 1}/{max_retries} failed using {proxy.get('http')}: {type(e).__name__}")
                _remove_proxy(proxy)
            else: break
    if last_err: raise last_err
    raise requests.exceptions.RequestException(f"Failed to fetch {url}")

"""
content = content.replace('# ──────────────────────────── helpers ──────────────────────────', proxy_code + '\n# ──────────────────────────── helpers ──────────────────────────')

# Remove old User-Agent from HEADERS
content = re.sub(r'"User-Agent":[^,]+,', '', content)

# 3. Replace requests.get with _get
content = re.sub(r'requests\.get\(([^,]+), headers=HEADERS, timeout=([^)]+)\)', r'_get(\1, timeout=\2)', content)

# 4. Argparse Defaults Change
content = content.replace(
    'parser.add_argument("--auto-push", action="store_true",',
    'parser.add_argument("--no-auto-push", action="store_false", dest="auto_push",'
)
content = content.replace(
    'parser.add_argument("--no-epub", action="store_true",',
    'parser.add_argument("--epub", action="store_false", dest="no_epub",'
)

# 5. Add new flags
flags_code = """    parser.add_argument("--epub", action="store_false", dest="no_epub",
        help=(
            "Generate EPUB files in output/."
            "Off by default to save space on overnight runs."
        ))
    parser.add_argument("--fast", action="store_true", help="Speed mode: reduces delays, workers=16.")
    parser.add_argument("--workers", type=int, default=8, metavar="N", help="Parallel chapter threads per novel.")
    parser.add_argument("--delay", type=float, default=5, metavar="SECS", help="Delay after each chapter request.")
    parser.add_argument("--proxy-file", metavar="FILE", help="Path to proxy list for rotation.")"""

content = content.replace(
    'parser.add_argument("--no-auto-push", action="store_false", dest="auto_push",\n        help=(\n            "After each novel, automatically:\\n"\n            "  1. git add the novel\'s data files (docs/data/<slug>/, index.json),\\n"\n            "     plus docs/read/<slug>/ too if --html was also passed\\n"\n            "  2. git commit and git push to GitHub\\n"\n            "Requires git to be configured with push access."\n        ))',
    'parser.add_argument("--no-auto-push", action="store_false", dest="auto_push", help="Skip automatically committing and pushing to GitHub (auto-push is ON by default).")'
)

# We need to correctly replace the --no-epub old argument block with our new flags block
old_no_epub = """    parser.add_argument("--no-epub", action="store_true",
        help=(
            "Skip EPUB generation. Only exports JSON for the site.\\n"
            "Recommended for overnight runs to save disk space."
        ))"""
new_no_epub = """    parser.add_argument("--epub", action="store_false", dest="no_epub", help="Generate EPUB files in output/. (EPUB generation is OFF by default)")"""

content = re.sub(r'    parser\.add_argument\("--epub".*?\)\)', new_no_epub, content, flags=re.DOTALL)
content = content.replace(new_no_epub, flags_code)

# 6. Apply settings
settings_code = """    args = parser.parse_args()

    # ── Apply speed / proxy settings ─────────────────────────────
    global CHAPTER_WORKERS, CHAPTER_DELAY, PAGE_DELAY, NOVEL_DELAY

    if args.fast:
        CHAPTER_DELAY   = 1
        PAGE_DELAY      = 0.5
        NOVEL_DELAY     = 1
        CHAPTER_WORKERS = 16
        print("⚡ Fast mode enabled  (delay=1s, page_delay=0.5s, novel_delay=1s, workers=16)")

    if args.workers != 8:
        CHAPTER_WORKERS = args.workers
    if args.delay != 5:
        CHAPTER_DELAY = args.delay

    if args.proxy_file:
        _load_proxies(args.proxy_file)

    if args.proxy_file or args.fast or args.workers != 8 or args.delay != 5:
        print(f"  Workers: {CHAPTER_WORKERS}  |  Chapter delay: {CHAPTER_DELAY}s  |  Page delay: {PAGE_DELAY}s  |  Novel delay: {NOVEL_DELAY}s")
"""
content = content.replace('    args = parser.parse_args()', settings_code)

# Add globals
content = content.replace('def main() -> None:', 'def main() -> None:\n    global CHAPTER_WORKERS, CHAPTER_DELAY, PAGE_DELAY, NOVEL_DELAY')

with open('scrape_catalog.py', 'w') as f:
    f.write(content)

