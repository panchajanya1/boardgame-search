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
    title = title.lower()
    title = re.sub(r"\(.*?\)", "", title)
    title = re.sub(r"[^a-z0-9 ]", "", title)
    return " ".join(title.split())


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

        try:
            price = f"₹{float(p.get('price', 0)):,.0f}"
        except (TypeError, ValueError):
            price = None

        featured = p.get("featured_image")
        if isinstance(featured, dict):
            image = featured.get("url")
        elif isinstance(featured, str):
            image = featured
        else:
            image = p.get("image")

        product_url = p.get("url", "")
        results.append({
            "site": site_name,
            "site_id": site_id,
            "title": title,
            "price": price,
            "image": image,
            "url": (
                product_url
                if product_url.startswith("http")
                else base_url + product_url
            ),
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


def scrape_bgfinder(query):
    global BGFINDER_CACHE

    if BGFINDER_CACHE is None:
        log.info("Loading BGFinder dataset...")
        html = get_page("https://bgfindertool.com/")

        soup = BeautifulSoup(html, "html.parser")
        data_tag = soup.find("script", {"id": "products-data"})
        if not data_tag or not data_tag.string:
            raise ScraperError("BGFinder page layout changed (products-data not found)")

        try:
            BGFINDER_CACHE = json.loads(data_tag.string)
        except ValueError:
            raise ScraperError("BGFinder product data could not be parsed")
        log.info(f"BGFinder cache loaded: {len(BGFINDER_CACHE)} products")

    results = []
    words = query.lower().split()

    for p in BGFINDER_CACHE:
        name = p.get("name", "")
        if not all(word in name.lower() for word in words):
            continue

        results.append({
            "site": f"BGFinder → {p.get('store', 'Unknown')}",
            "site_id": "bgfinder",
            "title": name,
            "price": p.get("priceDisplay"),
            "image": None,
            "url": p.get("url"),
            "bgg_rating": p.get("bggRating"),
            "bgg_rank": p.get("bggRank"),
            "players_min": p.get("playersMin"),
            "players_max": p.get("playersMax"),
            "playtime_min": p.get("playtimeMin"),
            "playtime_max": p.get("playtimeMax"),
        })

    results.sort(key=lambda x: x.get("bgg_rank") or 999999)
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
        "url": url or fallback_url,
    }]


def scrape_befikar(query):
    """Shop Befikar — WooCommerce shop"""
    url = f"{BEFIKAR_BASE}/?s={requests.utils.quote(query)}&post_type=product"
    html = get_page(url)
    soup = BeautifulSoup(html, "html.parser")

    body = soup.body
    body_classes = body.get("class", []) if body else []
    if "single-product" in body_classes:
        return woo_single_product(soup, url)

    cards = [
        c for c in soup.select("ul.products li.product, li.product")
        if not c.find_parent(class_=["related", "upsells", "cross-sells", "up-sells"])
    ]

    results = []
    for card in cards:
        title_el = (
            card.select_one(".woocommerce-loop-product__title")
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
            "url": urljoin(BEFIKAR_BASE, woo_link(card) or url),
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


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    sites_param = request.args.get("sites", "")
    enabled = [s.strip() for s in sites_param.split(",") if s.strip()] if sites_param else None

    if not query:
        return jsonify({"error": "No query provided"}), 400

    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400

    start = time.time()
    results = search_all(query, enabled)
    total_time = round(time.time() - start, 2)

    # Flatten all results for a combined view
    
    all_items = []
    seen = set()

    for site_id, data in results.items():
        for item in data["items"]:

            key = (
                normalize_title(
                    item.get("title", "")
                ),
                item.get("site", "")
            )
            
                    
           

            if key in seen:
                continue

            seen.add(key)
            all_items.append(item)

    def price_value(item):
        price = item.get("price")

        if not price:
            return 999999999

        try:
            return float(price.replace("₹", "").replace(",", ""))
        except:
            return 999999999

    query_lower = query.lower()

    all_items.sort(
        key=lambda x: (
            query_lower not in x["title"].lower(),
            price_value(x)
        )
    )

    best_prices = {}

    for item in all_items:

        title_key = normalize_title(
            item.get("title", "")
        )

        price = price_value(item)

        if price >= 999999999:
            continue

        if title_key not in best_prices:
            best_prices[title_key] = price
        else:
            best_prices[title_key] = min(
                best_prices[title_key],
                price
            )

    for item in all_items:

        title_key = normalize_title(
            item.get("title", "")
        )

        item["is_best_price"] = (
            price_value(item)
            == best_prices.get(title_key)
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
