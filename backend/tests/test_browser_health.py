import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from services.browser_health import browser_available, record_browser_result, reset_browser_health
from services.scrape_pagination import browser_retry_due


class BrowserHealthTests(unittest.TestCase):
    def test_all_inaccessible_catalog_urls_get_diagnostics_without_extra_queue_rounds(self):
        from services.scrape_pagination import _seller_page_batch
        url = 'https://www.mercadolivre.com.br/item/p/MLB123'
        for _ in range(2):
            record_browser_result(url, 'LOGIN_REQUIRED', 'http_catalog')
            record_browser_result(url, 'LOGIN_REQUIRED')
        cursors = [SimpleNamespace(store='mercado_livre', next_url=url + str(i), initial_url=url + str(i),
                                   completed=False, last_attempted_at=None, last_status=None,
                                   browser_attempted_url=None, browser_attempted_at=None) for i in range(20)]
        self.assertEqual(len(_seller_page_batch(cursors, 4)), 20)
        self.assertTrue(all(not cursor.completed for cursor in cursors))
    def test_http_catalog_login_pause_does_not_suppress_browser_or_listing_transport(self):
        url = 'https://www.mercadolivre.com.br/product/p/MLB123'
        record_browser_result(url, 'LOGIN_REQUIRED', 'http_catalog')
        record_browser_result(url, 'LOGIN_REQUIRED', 'http_catalog')
        self.assertFalse(browser_available(url, 'http_catalog'))
        self.assertTrue(browser_available(url, 'http_listing'))
        self.assertTrue(browser_available(url))
    def test_missing_shipping_form_does_not_disable_product_scraping(self):
        from services.browser_health import shipping_form_available, record_missing_shipping_form, record_shipping_form_found
        url = 'https://example.test/product'
        record_missing_shipping_form(url)
        self.assertTrue(shipping_form_available(url))
        record_missing_shipping_form(url)
        self.assertFalse(shipping_form_available(url))
        self.assertTrue(browser_available(url))
        self.assertTrue(shipping_form_available('https://other.test/product'))
        record_shipping_form_found(url)
        self.assertTrue(shipping_form_available(url))
    def setUp(self):
        reset_browser_health()

    def tearDown(self):
        reset_browser_health()

    def test_login_wall_pauses_only_blocked_host_and_recovers_after_cooldown(self):
        url = "https://www.mercadolivre.com.br/product"
        with patch("services.browser_health.time.monotonic", return_value=100):
            record_browser_result(url, "LOGIN_REQUIRED")
            self.assertTrue(browser_available(url))
            record_browser_result(url, "LOGIN_REQUIRED")
            self.assertFalse(browser_available(url))
            self.assertTrue(browser_available("https://lista.mercadolivre.com.br/sku"))
        with patch("services.browser_health.time.monotonic", return_value=701):
            self.assertTrue(browser_available(url))
            record_browser_result(url, "OK")
            self.assertTrue(browser_available(url))

    def test_blocked_page_is_limited_not_continuously_actionable(self):
        url = "https://example.test/product"
        record_browser_result(url, "BLOCKED")
        record_browser_result(url, "BLOCKED")
        cursor = SimpleNamespace(store="amazon", kind="seller_offers", next_url=url,
                                 initial_url=url, completed=False, last_status="BLOCKED",
                                 browser_attempted_at=None, browser_attempted_url=None)
        self.assertFalse(browser_retry_due(cursor))
        self.assertFalse(cursor.completed)

    def test_irrelevant_indexed_results_do_not_start_endless_browser_retries(self):
        cursor = SimpleNamespace(store="amazon", kind="indexed_search", completed=False,
                                 next_url="https://bing.com/search?q=sku", last_status="SEARCH_MISMATCH",
                                 browser_attempted_at=None, browser_attempted_url=None)
        self.assertFalse(browser_retry_due(cursor, datetime.utcnow()))
