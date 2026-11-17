"""Offline regression tests for the scrapers and search engine.

Run:  python -m unittest discover -s tests -v
No network access needed: HTTP calls are mocked with realistic store markup.
"""

import os
import sys
import time
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402


def fake_response(text="", status=200, json_data=None):
    r = mock.Mock()
    r.text = text
    r.status_code = status
    r.json.side_effect = (lambda: json_data) if json_data is not None else ValueError("no json")
    return r


CARD = """<li class="product type-product">
  <a href="https://shopbefikar.com/product/{slug}/" class="woocommerce-LoopProduct-link woocommerce-loop-product__link">
    {img}
    <h2 class="woocommerce-loop-product__title">{title}</h2>
    <span class="price">{price}</span>
  </a>
  <a href="?add-to-cart=12" class="button add_to_cart_button">Add to cart</a>
</li>"""

LAZY_IMG = ('<img src="data:image/svg+xml,%3Csvg%3E" '
            'data-lazy-src="https://shopbefikar.com/wp-content/uploads/{slug}.jpg">')

SALE = ('<del aria-hidden="true"><span class="woocommerce-Price-amount amount"><bdi>'
        '<span class="woocommerce-Price-currencySymbol">&#8377;</span>3,999.00</bdi></span></del> '
        '<span class="screen-reader-text">Original price was: &#8377;3,999.00.</span>'
        '<ins><span class="woocommerce-Price-amount amount"><bdi>'
        '<span class="woocommerce-Price-currencySymbol">&#8377;</span>2,999.00</bdi></span></ins>'
        '<span class="screen-reader-text">Current price is: &#8377;2,999.00.</span>')
SMALL_SALE = ('<del><span class="amount">&#8377;99</span></del>'
              '<ins><span class="amount">&#8377;79</span></ins>')
RANGE = ('<span class="amount">&#8377;500.00</span> &ndash; '
         '<span class="amount">&#8377;800.00</span>')
REGULAR = '<span class="amount">&#8377;1,299.00</span>'


def search_page(*cards):
    return ('<html><body class="archive search woocommerce-page"><ul class="products">'
            + "".join(cards) + "</ul></body></html>")


SINGLE_PRODUCT_PAGE = """<html><head>
<link rel="canonical" href="https://shopbefikar.com/product/catan/">
</head><body class="product-template-default single single-product woocommerce">
<div id="product-1" class="product type-product">
  <div class="woocommerce-product-gallery images">
    <a href="https://shopbefikar.com/wp-content/uploads/catan-full.jpg">
      <img src="https://shopbefikar.com/wp-content/uploads/catan-600.jpg"></a>
  </div>
  <div class="summary entry-summary">
    <h1 class="product_title entry-title">Catan</h1>
    <p class="price">""" + SALE + """</p>
  </div>
  <section class="related products"><h2>Related products</h2>
    <ul class="products">""" + CARD.format(slug="uno", img="", title="Uno", price=REGULAR) + """</ul>
  </section>
</div></body></html>"""


class BefikarPriceTests(unittest.TestCase):
    """Item 1: sale / range prices were glued together or dropped."""

    def scrape(self, html):
        with mock.patch.object(app.requests, "get", return_value=fake_response(html)):
            return {r["title"]: r for r in app.scrape_befikar("catan")}

    def test_sale_price_uses_current_price(self):
        r = self.scrape(search_page(CARD.format(slug="a", img="", title="Sale", price=SALE)))
        self.assertEqual(r["Sale"]["price"], "₹2,999")

    def test_small_sale_not_concatenated(self):
        r = self.scrape(search_page(CARD.format(slug="a", img="", title="Small", price=SMALL_SALE)))
        self.assertEqual(r["Small"]["price"], "₹79")  # was ₹9,979

    def test_price_range_uses_lowest(self):
        r = self.scrape(search_page(CARD.format(slug="a", img="", title="Range", price=RANGE)))
        self.assertEqual(r["Range"]["price"], "₹500")

    def test_regular_price(self):
        r = self.scrape(search_page(CARD.format(slug="a", img="", title="Reg", price=REGULAR)))
        self.assertEqual(r["Reg"]["price"], "₹1,299")

    def test_no_price(self):
        r = self.scrape(search_page(CARD.format(slug="a", img="", title="None", price="")))
        self.assertIsNone(r["None"]["price"])


class BefikarPageTests(unittest.TestCase):

    def scrape(self, html):
        with mock.patch.object(app.requests, "get", return_value=fake_response(html)):
            return app.scrape_befikar("catan")

    def test_single_result_redirect_returns_the_product(self):
        """Item 2: used to return 'Related products' + unrelated games."""
        results = self.scrape(SINGLE_PRODUCT_PAGE)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["title"], "Catan")
        self.assertEqual(r["price"], "₹2,999")
        self.assertEqual(r["url"], "https://shopbefikar.com/product/catan/")
        self.assertEqual(r["image"], "https://shopbefikar.com/wp-content/uploads/catan-600.jpg")

    def test_link_is_product_not_add_to_cart(self):
        r = self.scrape(search_page(CARD.format(slug="catan", img="", title="Catan", price=REGULAR)))[0]
        self.assertEqual(r["url"], "https://shopbefikar.com/product/catan/")

    def test_lazy_loaded_image(self):
        """Item 3: used to return the data: placeholder."""
        html = search_page(CARD.format(slug="catan", img=LAZY_IMG.format(slug="catan"),
                                       title="Catan", price=REGULAR))
        self.assertEqual(self.scrape(html)[0]["image"],
                         "https://shopbefikar.com/wp-content/uploads/catan.jpg")

    def test_srcset_image(self):
        img = ('<img src="data:image/gif;base64,R0lGOD" '
               'data-srcset="https://shopbefikar.com/a-300.jpg 300w, https://shopbefikar.com/a-600.jpg 600w">')
        html = search_page(CARD.format(slug="a", img=img, title="A", price=REGULAR))
        self.assertEqual(self.scrape(html)[0]["image"], "https://shopbefikar.com/a-300.jpg")

    def test_missing_image(self):
        html = search_page(CARD.format(slug="a", img="", title="A", price=REGULAR))
        self.assertIsNone(self.scrape(html)[0]["image"])

    def test_empty_search_page(self):
        html = '<html><body class="search search-no-results"><p class="woocommerce-info">No products were found</p></body></html>'
        self.assertEqual(self.scrape(html), [])


class ErrorReportingTests(unittest.TestCase):
    """Items 5 and 7: failures used to be reported as empty results."""

    def setUp(self):
        app.SEARCH_CACHE.clear()

    def run_one(self, site_id, **get_kwargs):
        with mock.patch.object(app.requests, "get", **get_kwargs):
            return app.search_all("catan", [site_id])[site_id]

    def test_cloudflare_block_is_reported(self):
        page = "<html><title>Just a moment...</title><div id='challenge-platform'></div></html>"
        res = self.run_one("befikar", return_value=fake_response(page, status=403))
        self.assertEqual(res["items"], [])
        self.assertIn("blocked", res["error"])

    def test_http_error_is_reported(self):
        res = self.run_one("befikar", return_value=fake_response("oops", status=500))
        self.assertEqual(res["error"], "Store returned HTTP 500")

    def test_network_error_is_reported(self):
        res = self.run_one("befikar", side_effect=requests.ConnectionError("refused"))
        self.assertIn("Could not reach store", res["error"])

    def test_timeout_is_reported(self):
        res = self.run_one("befikar", side_effect=requests.Timeout())
        self.assertEqual(res["error"], "Store took too long to respond")

    def test_shopify_error_is_reported(self):
        res = self.run_one("tma", return_value=fake_response("x", status=404))
        self.assertEqual(res["error"], "Store returned HTTP 404")

    def test_shopify_non_json_is_reported(self):
        res = self.run_one("tma", return_value=fake_response("<html>"))
        self.assertIn("non-JSON", res["error"])

    def test_bgfinder_layout_change_is_reported(self):
        with mock.patch.object(app, "BGFINDER_CACHE", None):
            res = self.run_one("bgfinder", return_value=fake_response("<html></html>"))
        self.assertIn("products-data not found", res["error"])

    def test_successful_empty_search_has_no_error(self):
        res = self.run_one("tma", return_value=fake_response(
            json_data={"resources": {"results": {"products": []}}}))
        self.assertEqual(res, {"items": [], "elapsed": res["elapsed"], "error": None})

    def test_unexpected_crash_is_reported(self):
        def boom(q):
            raise KeyError("price")
        with mock.patch.dict(app.SCRAPERS, {"befikar": boom}):
            res = app.search_all("catan", ["befikar"])["befikar"]
        self.assertIn("KeyError", res["error"])

    def test_api_returns_errors_to_frontend(self):
        with mock.patch.object(app.requests, "get", return_value=fake_response("x", status=503)):
            body = app.app.test_client().get("/api/search?q=catan&sites=befikar").get_json()
        self.assertEqual(body["by_site"]["befikar"]["error"], "Store returned HTTP 503")


class TimeoutAndCacheTests(unittest.TestCase):
    """Item 6: slow stores vanished and later mutated the cached result."""

    def setUp(self):
        app.SEARCH_CACHE.clear()
        self.timeout_patch = mock.patch.object(app, "TIMEOUT", -1.7)  # deadline = 0.3s
        self.timeout_patch.start()

    def tearDown(self):
        self.timeout_patch.stop()
        app.SEARCH_CACHE.clear()

    def test_slow_store_reported_and_result_stable(self):
        def slow(q):
            time.sleep(0.8)
            return [{"title": "Late"}]

        fast = lambda q: [{"title": "Fast", "site": "TMA", "site_id": "tma", "price": "₹1"}]
        with mock.patch.dict(app.SCRAPERS, {"befikar": slow, "tma": fast}):
            res = app.search_all("catan", ["befikar", "tma"])
            self.assertEqual(res["befikar"]["error"], "Store took too long to respond")
            self.assertEqual(len(res["tma"]["items"]), 1)

            time.sleep(1.0)  # let the slow thread finish
            self.assertEqual(res["befikar"]["items"], [], "late thread mutated the response")
            self.assertEqual(app.SEARCH_CACHE, {}, "partial result was cached")

    def test_deadline_is_shared_not_per_store(self):
        def slow(q):
            time.sleep(0.8)
            return []
        with mock.patch.dict(app.SCRAPERS, {s: slow for s in app.SCRAPERS}):
            start = time.time()
            app.search_all("catan", list(app.SCRAPERS))
            self.assertLess(time.time() - start, 0.6)

    def test_complete_results_are_cached_and_isolated(self):
        calls = []

        def ok(q):
            calls.append(q)
            return [{"title": "Catan"}]

        with mock.patch.dict(app.SCRAPERS, {"tma": ok}), mock.patch.object(app, "TIMEOUT", 5):
            first = app.search_all("catan", ["tma"])
            first["tma"]["items"].append({"title": "mutated by caller"})
            second = app.search_all("catan", ["tma"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["tma"]["items"], [{"title": "Catan"}])

    def test_unknown_site_ignored(self):
        with mock.patch.dict(app.SCRAPERS, {"tma": lambda q: []}, clear=True):
            self.assertEqual(list(app.search_all("x", ["tma", "nope"])), ["tma"])


if __name__ == "__main__":
    unittest.main()
