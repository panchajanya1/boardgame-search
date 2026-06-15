"""
Board Game Search Engine for India
Run: python app.py
Open: http://localhost:5000  (or http://YOUR_PC_IP:5000 on iPhone)
"""

import threading
import json
import re
import time
import socket
import logging
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


def get_page(url, timeout=TIMEOUT):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


# ──────────────────────────────────────────────
# SITE SCRAPERS
# ──────────────────────────────────────────────



def scrape_shopify_json(site_name, site_id, base_url, query):
    results = []

    try:
        url = (
                f"{base_url}/search/suggest.json"
                f"?q={requests.utils.quote(query)}"
                f"&resources[type]=product"
                f"&resources[limit]=20"
                )

       

        r = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
        )

        r.raise_for_status()

        data = r.json()

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
            except Exception:
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
                )
            })

            
            

        return results

    except Exception as e:
        log.warning(f"{site_name}: {e}")
        return []

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

    results = []

    try:

        if BGFINDER_CACHE is None:

            log.info("Loading BGFinder dataset...")

            html = get_page("https://bgfindertool.com/")
            
            print("BG1 HTML:", html is not None)
            print("BG1 HTML length:", len(html) if html else 0)

            if not html:
                return results

            soup = BeautifulSoup(html, "html.parser")

            data_tag = soup.find(
                "script",
                {"id": "products-data"}
            )

            print("BG2 products-data found:", data_tag is not None)

            if not data_tag:
                log.warning("BGFinder: products-data not found")
                return results

            BGFINDER_CACHE = json.loads(data_tag.string)

            print("BG3 cache size:", len(BGFINDER_CACHE))

            log.info(
                f"BGFinder cache loaded: {len(BGFINDER_CACHE)} products"
            )

        products = BGFINDER_CACHE

        query_lower = query.lower()

        for p in products:

            name = p.get("name", "")

            words = query_lower.split()

            if not all(
                word in name.lower()
                for word in words
            ):
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

        results.sort(
            key=lambda x: (
                x.get("bgg_rank") or 999999
            )
        )

        results = results[:50]
        
        return results

    except Exception as e:
        log.warning(f"BGFinder: {e}")
        return []

def scrape_jaiman(query):
    return scrape_shopify_json(
        "Jaiman Toys",
        "jaiman",
        "https://jaimantoys.com",
        query,
    )






def scrape_befikar(query):
    """Shop Befikar — WooCommerce shop"""
    results = []
    url = f"https://shopbefikar.com/?s={requests.utils.quote(query)}&post_type=product"
    html = get_page(url)
    if not html:
        return results

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("li.product, .product")[:10]
    for card in cards:
        title_el = card.select_one(".woocommerce-loop-product__title, h2, h3")
        price_el = card.select_one(".price ins .amount, .price .amount, .price")
        img_el = card.select_one("img")
        link_el = card.select_one("a[href]")

        title = title_el.get_text(strip=True) if title_el else None
        price = clean_price(price_el.get_text() if price_el else "")
        img = img_el.get("data-src") or img_el.get("src") if img_el else None
        link = link_el.get("href") if link_el else url

        if title:
            results.append({
                "site": "Shop Befikar",
                "site_id": "befikar",
                "title": title,
                "price": price,
                "image": img,
                "url": link,
            })
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


def search_all(query, enabled_sites=None):
    if enabled_sites is None:
        enabled_sites = list(SCRAPERS.keys())

    results_map = {}
    errors_map = {}
    lock = threading.Lock()

    cache_key = (
        query.lower(),
        tuple(sorted(enabled_sites))
    )

    cached = SEARCH_CACHE.get(cache_key)

    if cached and (time.time() - cached["time"]) < CACHE_TTL:
        return cached["data"]

    def run_scraper(site_id, scraper_fn):
        try:
            start = time.time()
            items = scraper_fn(query)
            elapsed = round(time.time() - start, 2)
            log.info(f"{site_id}: {len(items)} results in {elapsed}s")
            with lock:
                results_map[site_id] = {"items": items, "elapsed": elapsed, "error": None}
        except Exception as e:
            log.error(f"{site_id} scraper error: {e}")
            with lock:
                results_map[site_id] = {"items": [], "elapsed": 0, "error": str(e)}

    threads = []
    for site_id in enabled_sites:
        if site_id in SCRAPERS:
            t = threading.Thread(
                target=run_scraper, args=(site_id, SCRAPERS[site_id]), daemon=True
            )
            threads.append(t)
            t.start()

    for t in threads:
        t.join(timeout=TIMEOUT + 2)

    if len(SEARCH_CACHE) > 500:
        SEARCH_CACHE.clear()
    
    SEARCH_CACHE[cache_key] = {
        "time": time.time(),
        "data": results_map
    }

    return results_map


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
