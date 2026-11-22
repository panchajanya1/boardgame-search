"""
Board Game Search Engine for India
Run: python app.py
Open: http://localhost:5000  (or http://YOUR_PC_IP:5000 on iPhone)
"""

import copy
import threading
import json
import re
import time
import socket
import logging
from urllib.parse import urljoin
from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 12

BGFINDER_CACHE = None
BGFINDER_LOADED_AT = 0.0
BGFINDER_TTL = 6 * 3600  # prices/stock change; re-download a few times a day
BGFINDER_LOCK = threading.Lock()

SEARCH_CACHE = {}
CACHE_TTL = 300


# ──────────────────────────────────────────────
# SCRAPER HELPERS
# ──────────────────────────────────────────────

def clean_price(text):
    """Extract numeric price string from messy text."""
    if not text:
        return None
    text = text.strip()
    # Remove currency symbols and spaces, keep digits and . ,
    price = re.sub(r"[^\d.,]", "", text)
    # Remove trailing commas/dots
    price = price.strip(".,")
    if not price:
        return None
    try:
        # Normalize Indian number format: 1,299 → 1299
        val = float(price.replace(",", ""))
        if val < 1 or val > 500000:  # sanity filter
            return None
        return f"₹{val:,.0f}"
    except ValueError:
        return None

def normalize_title(title):
    # Keep text in brackets: "(Expansion)" / "(5-6 Players)" is a different
    # product, and dropping it made those collide with the base game.
    title = (title or "").lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


def matches_words(query, text):
    """Every query word must start a word in the text, so "catan" matches
    "Catan: Seafarers" but not "Yucatan Card Sleeves"."""
    tokens = normalize_title(text).split()
    return all(any(t.startswith(w) for t in tokens) for w in normalize_title(query).split())


def phrase_match(query, text):
    """Whole query appears in the title, starting at a word boundary."""
    q = normalize_title(query)
    return bool(q) and re.search(r"(?:^| )" + re.escape(q), normalize_title(text)) is not None


def absolute_url(url, base):
    """Make a store URL absolute (handles "/products/x" and "//cdn/..."),
    and refuse anything that isn't http(s), e.g. javascript: links."""
    if not url or not isinstance(url, str):
        return None
    url = urljoin(base + "/", url.strip())
    return url if url.startswith(("http://", "https://")) else None


def parse_price_number(value):
    """Shopify/BGFinder numeric price -> display string; 0/missing -> None."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return f"₹{val:,.0f}"


class ScraperError(Exception):
    """Raised when a store can't be searched, so the UI can show why."""


BLOCK_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "<title>Just a moment...</title>",
    "Attention Required! | Cloudflare",
)


def get_page(url, timeout=TIMEOUT):
    """Fetch a page and return its HTML. Raises ScraperError on any failure
    (network error, HTTP error, bot-protection page) instead of returning
    None, so a blocked store isn't silently reported as "no results"."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.Timeout:
        raise ScraperError("Store took too long to respond")
    except requests.RequestException as e:
        raise ScraperError(f"Could not reach store ({type(e).__name__})")

    if r.status_code in (403, 429, 503) and any(m in r.text for m in BLOCK_MARKERS):
        raise ScraperError(f"Store blocked the request (HTTP {r.status_code}, bot protection)")
    if r.status_code >= 400:
        raise ScraperError(f"Store returned HTTP {r.status_code}")
    if any(m in r.text for m in BLOCK_MARKERS):
        raise ScraperError("Store returned a bot-protection page")
    return r.text


# ──────────────────────────────────────────────
# SITE SCRAPERS
# ──────────────────────────────────────────────


def scrape_shopify_json(site_name, site_id, base_url, query):
    results = []
    url = (
        f"{base_url}/search/suggest.json"
        f"?q={requests.utils.quote(query)}"
        f"&resources[type]=product"
        f"&resources[limit]=20"
    )

    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.Timeout:
        raise ScraperError("Store took too long to respond")
    except requests.RequestException as e:
        raise ScraperError(f"Could not reach store ({type(e).__name__})")
    if r.status_code >= 400:
        raise ScraperError(f"Store returned HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise ScraperError("Store returned an unexpected (non-JSON) response")

    products = (
        data.get("resources", {})
            .get("results", {})
            .get("products", [])
    )

    for p in products:
        title = p.get("title")
        if not title:
            continue

        price = parse_price_number(p.get("price"))

        featured = p.get("featured_image")
        if isinstance(featured, dict):
            image = featured.get("url")
        elif isinstance(featured, str):
            image = featured
        else:
            image = p.get("image")

        available = p.get("available")
        results.append({
            "site": site_name,
            "site_id": site_id,
            "title": title,
            "price": price,
            "image": absolute_url(image, base_url),
            "url": absolute_url(p.get("url"), base_url) or base_url,
            "in_stock": available if isinstance(available, bool) else None,
        })

    return results

def scrape_tma(query):
    return scrape_shopify_json(
        "TMA Enterprises",
        "tma",
        "https://tmaenterprises.co",
        query,
    )

def scrape_satyam(query):
    return scrape_shopify_json(
        "Satyam Stationers",
        "satyam",
        "https://www.satyamstationers.com",
        query,
    )


def load_bgfinder():
    """BGFinder ships its whole catalogue in one JSON blob; download it once
    and refresh it every BGFINDER_TTL. If a refresh fails, keep serving the
    previous copy rather than failing every search."""
    global BGFINDER_CACHE, BGFINDER_LOADED_AT

    with BGFINDER_LOCK:
        fresh = (time.time() - BGFINDER_LOADED_AT) < BGFINDER_TTL
        if BGFINDER_CACHE is not None and fresh:
            return BGFINDER_CACHE
        try:
            log.info("Loading BGFinder dataset...")
            html = get_page("https://bgfindertool.com/")

            soup = BeautifulSoup(html, "html.parser")
            data_tag = soup.find("script", {"id": "products-data"})
            if not data_tag or not data_tag.string:
                raise ScraperError("BGFinder page layout changed (products-data not found)")
            try:
                data = json.loads(data_tag.string)
            except ValueError:
                raise ScraperError("BGFinder product data could not be parsed")
            if not isinstance(data, list):
                raise ScraperError("BGFinder product data could not be parsed")
        except ScraperError:
            if BGFINDER_CACHE is not None:
                log.warning("BGFinder refresh failed; using the previous copy")
                BGFINDER_LOADED_AT = time.time() - BGFINDER_TTL + 600  # retry in 10 min
                return BGFINDER_CACHE
            raise

        BGFINDER_CACHE = [p for p in data if isinstance(p, dict)]
        BGFINDER_LOADED_AT = time.time()
        log.info(f"BGFinder cache loaded: {len(BGFINDER_CACHE)} products")
        return BGFINDER_CACHE


def bgfinder_title(p):
    """Listing names are cut off ("Adventure Games: Gloom ..."); fall back to
    the full BGG name when that happens."""
    name = (p.get("name") or "").strip()
    primary = (p.get("NamePrimary") or "").strip()
    if primary and (not name or name.endswith("...") or name.endswith("…")):
        return primary
    return name or primary


def scrape_bgfinder(query):
    products = load_bgfinder()

    results = []

    for p in products:
        title = bgfinder_title(p)
        if not title:
            continue
        if not matches_words(query, f"{p.get('name') or ''} {p.get('NamePrimary') or ''}"):
            continue

        status = (p.get("status") or "").strip()
        results.append({
            "site": f"BGFinder → {p.get('store') or 'Unknown'}",
            "site_id": "bgfinder",
            "title": title,
            "price": p.get("priceDisplay") or parse_price_number(p.get("priceValue")),
            "image": absolute_url(p.get("Thumbnail") or p.get("Image"), "https://bgfindertool.com"),
            "url": absolute_url(p.get("url"), "https://bgfindertool.com"),
            "in_stock": None if not status else status.lower() in ("in stock", "preorder"),
            "stock_note": status if status.lower() == "preorder" else None,
            "bgg_rating": p.get("bggRating"),
            "bgg_rank": p.get("bggRank"),
            "players_min": p.get("playersMin"),
            "players_max": p.get("playersMax"),
            "playtime_min": p.get("playtimeMin"),
            "playtime_max": p.get("playtimeMax"),
        })

    def rank(x):
        try:
            return float(x.get("bgg_rank"))
        except (TypeError, ValueError):
            return 999999.0

    results.sort(key=rank)
    return results[:50]

def scrape_jaiman(query):
    return scrape_shopify_json(
        "Jaiman Toys",
        "jaiman",
        "https://jaimantoys.com",
        query,
    )


# ── WooCommerce helpers (Shop Befikar) ──

BEFIKAR_BASE = "https://shopbefikar.com"

def woo_price(price_el):
    """Current price from a WooCommerce `.price` element.

    The element can hold a sale (<del>old</del><ins>new</ins>), a range
    (₹500 – ₹800) or screen-reader text repeating the prices, so taking its
    whole text glues several numbers together. Read individual `.amount`
    nodes instead: the sale price if present, else the first (lowest) amount
    that isn't struck through."""
    if price_el is None:
        return None

    sale = price_el.select_one("ins .amount")
    if sale:
        return clean_price(sale.get_text())

    for amount in price_el.select(".amount"):
        if amount.find_parent("del"):
            continue
        return clean_price(amount.get_text())

    # No .amount markup at all (custom theme): only trust plain single prices.
    for sr in price_el.select(".screen-reader-text"):
        sr.decompose()
    text = price_el.get_text(" ", strip=True)
    if len(re.findall(r"\d[\d,]*(?:\.\d+)?", text)) == 1:
        return clean_price(text)
    return None


def woo_image(container):
    """Real image URL, skipping lazy-load placeholders (data: URIs)."""
    for img in container.select("img"):
        candidates = [
            img.get("data-lazy-src"),
            img.get("data-src"),
            img.get("data-original"),
        ]
        for attr in ("data-lazy-srcset", "data-srcset", "srcset"):
            srcset = img.get(attr)
            if srcset:
                candidates.append(srcset.split(",")[0].strip().split(" ")[0])
        candidates.append(img.get("src"))

        for c in candidates:
            if c and not c.startswith("data:"):
                return urljoin(BEFIKAR_BASE, c)
    return None


def woo_link(card):
    link = card.select_one("a.woocommerce-LoopProduct-link[href], a.woocommerce-loop-product__link[href]")
    if link:
        return link["href"]
    for a in card.select("a[href]"):
        href = a["href"]
        if "add-to-cart" not in href and not href.startswith("#"):
            return href
    return None


def woo_single_product(soup, fallback_url):
    """WooCommerce redirects a search with exactly one hit straight to that
    product's page. Parse the product itself, not the 'Related products'."""
    title_el = soup.select_one(".product_title, .summary h1, h1.entry-title")
    if not title_el:
        return []

    summary = soup.select_one(".summary, .entry-summary") or soup
    url_el = soup.select_one('link[rel="canonical"], meta[property="og:url"]')
    url = (url_el.get("href") or url_el.get("content")) if url_el else None
    gallery = soup.select_one(".woocommerce-product-gallery, .images")

    return [{
        "site": "Shop Befikar",
        "site_id": "befikar",
        "title": title_el.get_text(strip=True),
        "price": woo_price(summary.select_one(".price")),
        "image": woo_image(gallery) if gallery else None,
        "url": absolute_url(url, BEFIKAR_BASE) or fallback_url,
        "in_stock": woo_in_stock(
            summary.select_one(".stock"),
            (soup.select_one(".type-product") or soup).get("class"),
        ),
    }]


def woo_in_stock(stock_el=None, body_classes=None):
    """True/False from WooCommerce's instock/outofstock markers, None if unknown."""
    classes = set(body_classes or [])
    if stock_el is not None:
        classes |= set(stock_el.get("class", []))
    if classes & {"outofstock", "out-of-stock"}:
        return False
    if classes & {"instock", "in-stock", "onbackorder", "available-on-backorder"}:
        return True
    return None


def scrape_befikar(query):
    """Shop Befikar — WooCommerce shop"""
    url = f"{BEFIKAR_BASE}/?s={requests.utils.quote(query)}&post_type=product"
    html = get_page(url)
    soup = BeautifulSoup(html, "html.parser")

    body = soup.body
    body_classes = body.get("class", []) if body else []
    if "single-product" in body_classes:
        return woo_single_product(soup, url)

    # Classic themes use <ul class="products"><li class="product">; Shop
    # Befikar's Woodmart theme uses <div class="products"><div class="product">.
    cards = [
        c for c in soup.select(".products .product, li.product")
        if not c.find_parent(class_=["related", "upsells", "cross-sells", "up-sells"])
    ]

    results = []
    for card in cards:
        title_el = (
            card.select_one(".woocommerce-loop-product__title")
            or card.select_one(".wd-entities-title")
            or card.select_one(".product-title, .product-name")
            or card.select_one("h2, h3")
        )
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            continue

        results.append({
            "site": "Shop Befikar",
            "site_id": "befikar",
            "title": title,
            "price": woo_price(card.select_one(".price")),
            "image": woo_image(card),
            "url": absolute_url(woo_link(card), BEFIKAR_BASE) or url,
            "in_stock": woo_in_stock(card.select_one(".stock"), card.get("class")),
        })
        if len(results) >= 20:
            break

    return results


# ──────────────────────────────────────────────
# PARALLEL SEARCH
# ──────────────────────────────────────────────

SCRAPERS = {
    "tma": scrape_tma,
    "satyam": scrape_satyam,
    "jaiman": scrape_jaiman,
    "befikar": scrape_befikar,
    "bgfinder": scrape_bgfinder,
}

CACHE_LOCK = threading.Lock()


def search_all(query, enabled_sites=None):
    if enabled_sites is None:
        enabled_sites = list(SCRAPERS.keys())
    enabled_sites = [s for s in enabled_sites if s in SCRAPERS]

    cache_key = (query.lower(), tuple(sorted(enabled_sites)))
    with CACHE_LOCK:
        cached = SEARCH_CACHE.get(cache_key)
    if cached and (time.time() - cached["time"]) < CACHE_TTL:
        return copy.deepcopy(cached["data"])

    results_map = {}
    lock = threading.Lock()

    def run_scraper(site_id, scraper_fn):
        start = time.time()
        try:
            items = scraper_fn(query)
            entry = {"items": items, "elapsed": round(time.time() - start, 2), "error": None}
            log.info(f"{site_id}: {len(items)} results in {entry['elapsed']}s")
        except ScraperError as e:
            log.warning(f"{site_id}: {e}")
            entry = {"items": [], "elapsed": round(time.time() - start, 2), "error": str(e)}
        except Exception as e:
            log.exception(f"{site_id} scraper crashed")
            entry = {"items": [], "elapsed": round(time.time() - start, 2),
                     "error": f"Scraper error: {type(e).__name__}: {e}"}
        with lock:
            results_map[site_id] = entry

    threads = []
    for site_id in enabled_sites:
        t = threading.Thread(target=run_scraper, args=(site_id, SCRAPERS[site_id]), daemon=True)
        threads.append(t)
        t.start()

    # One shared deadline for all stores (not TIMEOUT+2 per join).
    deadline = time.time() + TIMEOUT + 2
    for t in threads:
        t.join(timeout=max(0, deadline - time.time()))

    # Snapshot under the lock: threads that are still running keep writing into
    # results_map, so the response (and cache) must not share that dict.
    with lock:
        snapshot = copy.deepcopy(results_map)

    for site_id in enabled_sites:
        if site_id not in snapshot:
            snapshot[site_id] = {"items": [], "elapsed": TIMEOUT + 2,
                                 "error": "Store took too long to respond"}

    # Only cache complete, error-free searches; otherwise a slow or flaky store
    # would stay missing for the whole cache TTL.
    if all(v["error"] is None for v in snapshot.values()):
        with CACHE_LOCK:
            if len(SEARCH_CACHE) > 500:
                SEARCH_CACHE.clear()
            SEARCH_CACHE[cache_key] = {"time": time.time(), "data": copy.deepcopy(snapshot)}

    return snapshot


# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


NO_PRICE = float("inf")


def price_value(item):
    price = item.get("price")
    if not price:
        return NO_PRICE
    try:
        return float(re.sub(r"[^\d.]", "", price))
    except ValueError:
        return NO_PRICE


def dedupe_key(item):
    """Same listing returned twice by a store. Keyed on the product URL
    (minus tracking query strings like ?_pos=1&_sid=...), falling back to the
    title, so different editions with similar names are kept apart."""
    url = (item.get("url") or "").split("?")[0].split("#")[0].rstrip("/")
    return (item.get("site", ""), url or normalize_title(item.get("title", "")))


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    sites_param = request.args.get("sites", "")
    enabled = [s.strip() for s in sites_param.split(",") if s.strip()] if sites_param else None

    if not query:
        return jsonify({"error": "No query provided"}), 400

    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400

    if enabled is not None and not any(s in SCRAPERS for s in enabled):
        return jsonify({"error": "No valid stores selected"}), 400

    start = time.time()
    results = search_all(query, enabled)
    total_time = round(time.time() - start, 2)

    # Flatten all results for a combined view
    all_items = []
    seen = set()
    for data in results.values():
        for item in data["items"]:
            key = dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            all_items.append(item)

    # Relevance: titles containing the query first, then in-stock before
    # sold-out, then cheapest.
    all_items.sort(key=lambda x: (
        not phrase_match(query, x.get("title", "")),
        x.get("in_stock") is False,
        price_value(x),
    ))

    # "Best price" only means something when the same game is offered by more
    # than one store; sold-out listings can't be the best price.
    offers = {}
    for item in all_items:
        price = price_value(item)
        if price == NO_PRICE or item.get("in_stock") is False:
            continue
        offers.setdefault(normalize_title(item.get("title", "")), []).append((price, item.get("site")))

    for item in all_items:
        group = offers.get(normalize_title(item.get("title", "")), [])
        item["is_best_price"] = (
            len({site for _, site in group}) > 1
            and item.get("in_stock") is not False
            and price_value(item) == min(p for p, _ in group)
        )

    return jsonify({
        "query": query,
        "total_results": len(all_items),
        "total_time": total_time,
        "by_site": results,
        "all_items": all_items,
    })


@app.route("/api/sites")
def api_sites():
    return jsonify([
        {"id": "tma", "name": "TMA Enterprises", "url": "https://tmaenterprises.co/", "color": "#1a6b4a"},
        {"id": "satyam", "name": "Satyam Stationers", "url": "https://www.satyamstationers.com/", "color": "#1e4fa3"},
        {"id": "jaiman", "name": "Jaiman Toys", "url": "https://jaimantoys.com/", "color": "#a33b1e"},
        {"id": "befikar", "name": "Shop Befikar", "url": "https://shopbefikar.com/", "color": "#6a1ea3"},
        {"id": "bgfinder", "name": "BG Finder Tool", "url": "https://bgfindertool.com/", "color": "#a37a1e"},
    ])


# ──────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = get_local_ip()
    print("\n" + "="*55)
    print("  🎲  Board Game Search Engine — India")
    print("="*55)
    print(f"\n  ✅  Open on this PC:     http://localhost:5000")
    print(f"  📱  Open on iPhone:      http://{ip}:5000")
    print(f"\n  (Make sure your iPhone is on the same Wi-Fi network)")
    print("\n  Press Ctrl+C to stop the server")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
