import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from threading import Thread
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from scrapy.http import HtmlResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from crawlers.base import parse_product_response
from crawlers.profiles import DEFAULT_PROFILE
from database import Base
from models.product import PriceHistory, Product, ProductSource, ScrapeCursor
from routes.products import _latest_products_query
from services.http_client import create_resilient_session
from services.offer_enrichment import DETAIL_CACHE, _fetch_details, _jsonld_offer_details, _shopify_shipping
from services.playwright_fallback import _browser_platform_quote, _parse_shipping_quote, _woocommerce_rate_details, collect_product_with_playwright, render_product_page
from services.precise_extraction import _collect_sources, _external_candidates, run_precise_extraction
from services.scrape_pagination import _advance, _seller_page_batch
from services.store_registry import TARGET_STORES
from services.product_resolver import ProductIdentity, identity_from_models, resolve_candidate
from services.scraper_service import _build_comparison_rows, _normalize_result, _save_result
from services.structured_products import delivery_range, merge_offer_details, shipping_details
from services.analytics_export import _excel_datetime


class ScrapingAccuracyTests(unittest.TestCase):
    def test_full_appliance_sku_needs_code_or_unambiguous_voltage(self):
        from services.product_resolver import ProductIdentity, resolve_candidate
        identity = ProductIdentity(sku='BWJ14A9BNA', model='BWJ14A9')
        self.assertTrue(resolve_candidate(identity, {'name': 'Lavadora Brastemp BWJ14A9 220 Volts'}).accepted)
        self.assertTrue(resolve_candidate(identity, {'name': 'Lavadora Brastemp', 'model': 'BWJ14A9BNA'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Lavadora Brastemp BWJ14A9 127V'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Lavadora Brastemp BWJ14A9 127V ou 220V'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Lavadora Brastemp BWJ14A9', 'url': 'https://example.test/BWJ14A9BNA'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Lavadora Brastemp BWJ14A9ANA 220V'}).accepted)

    def setUp(self):
        from services.browser_health import reset_browser_health
        reset_browser_health()
        self.addCleanup(reset_browser_health)
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.identity = ProductIdentity(sku="W10531315", model="W10531315", name="Sensor W10531315")
        self.source = SimpleNamespace(
            store="dufrio", seller="Seller A", brand=None,
            url="https://www.dufrio.com.br/sensor-w10531315",
            external_product_id=None, asin=None, item_id=None,
        )
        DETAIL_CACHE.clear()

    def parse(self, html):
        response = HtmlResponse(url=self.source.url, body=html.encode(), encoding="utf-8")
        return parse_product_response(response, self.source, self.identity, DEFAULT_PROFILE, "01001000")

    def json_html(self, value, body=""):
        return f'<html><body>{body}<script type="application/ld+json">{json.dumps(value)}</script></body></html>'

    def product(self, **changes):
        return {"@type": "Product", "name": "Sensor W10531315", "sku": "W10531315",
                "offers": {"@type": "Offer", "price": 20, "seller": {"name": "Seller A"}}, **changes}

    def test_recommended_product_before_actual_product_is_ignored(self):
        html = self.json_html([
            self.product(name="Outro sensor W99999999", sku="W99999999", offers={"price": 1}),
            self.product(),
        ])
        result = self.parse(html)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["price"], 20)
        self.assertEqual(result["item"]["name"], "Sensor W10531315")

    def test_graph_references_resolve_offer_seller_and_shipping(self):
        html = self.json_html({"@graph": [
            self.product(**{"@id": "#product", "@type": "https://schema.org/Product",
                            "offers": {"@id": "#offer"}}),
            {"@id": "#offer", "@type": "Offer", "price": 24, "priceCurrency": "BRL",
             "seller": {"@id": "#seller"}, "shippingDetails": {"@id": "#shipping"}},
            {"@id": "#seller", "@type": "Organization", "name": "Seller A"},
            {"@id": "#shipping", "@type": "OfferShippingDetails", "shippingRate": {"value": 0},
             "deliveryTime": {"handlingTime": {"value": 1}, "transitTime": {"minValue": 2, "maxValue": 4}}},
        ]})
        result = self.parse(html)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["seller"], "Seller A")
        self.assertEqual(result["item"]["shipping_cost"], 0)
        self.assertEqual(result["item"]["delivery_max_days"], 5)

    def test_microdata_product_and_offer_are_collected(self):
        html = '''<div itemscope itemtype="https://schema.org/Product">
          <h1 itemprop="name">Sensor W10531315</h1><meta itemprop="sku" content="W10531315">
          <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
            <meta itemprop="priceCurrency" content="BRL"><meta itemprop="price" content="21.90">
            <div itemprop="seller" itemscope itemtype="https://schema.org/Organization">
              <span itemprop="name">Seller A</span>
            </div>
          </div>
        </div>'''
        result = self.parse(html)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["price"], 21.90)
        self.assertEqual(result["item"]["seller"], "Seller A")

    def test_rdfa_product_and_offer_are_collected(self):
        html = '''<div vocab="https://schema.org/" typeof="Product" resource="#product">
          <h1 property="name">Sensor W10531315</h1><meta property="sku" content="W10531315">
          <div property="offers" typeof="Offer" resource="#offer">
            <meta property="priceCurrency" content="BRL"><meta property="price" content="22.90">
            <div property="seller" typeof="Organization"><span property="name">Seller A</span></div>
          </div>
        </div>'''
        result = self.parse(html)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["price"], 22.90)

    def test_structured_sku_can_confirm_a_title_without_code(self):
        result = self.parse(self.json_html(self.product(name="Sensor original de temperatura")))
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["match"].method, "sku_exact")

    def test_other_structured_product_does_not_use_page_url_as_proof(self):
        result = self.parse(self.json_html(self.product(name="Outro sensor", sku="W99999999")))
        self.assertEqual(result["status"], "INVALID_MATCH")

    def test_currency_negative_nonfinite_and_expired_prices_are_not_collected(self):
        offers = [
            {"price": 20, "priceCurrency": "USD"}, {"price": "-20.00"},
            {"price": float("inf")}, {"price": True},
            {"price": 20, "priceValidUntil": "2000-01-01"},
        ]
        for offer in offers:
            with self.subTest(offer=offer):
                result = self.parse(self.json_html(self.product(offers=offer)))
                self.assertEqual(result["status"], "PARSER_ERROR")

    def test_member_only_and_minimum_quantity_prices_are_not_public_unit_prices(self):
        for restriction in ({"validForMemberTier": "Prime"}, {"eligibleQuantity": {"minValue": 10}}):
            result = self.parse(self.json_html(self.product(offers={"price": 10, **restriction})))
            self.assertEqual(result["status"], "PARSER_ERROR")

    def test_free_shipping_minimum_basket_is_respected(self):
        details = shipping_details({"price": 20, "shippingDetails": [
            {"shippingRate": {"value": 0, "eligibleTransactionVolume": {"minPrice": 99}}},
            {"shippingRate": {"value": 15}},
        ]})
        self.assertEqual(details["shipping_cost"], 15)

    def test_installments_ignore_recommendation_panel(self):
        html = self.json_html(self.product(), '<h1>Sensor W10531315</h1><p>2x de R$ 11,00</p>'
                              '<div class="recommendations">10x de R$ 80,00</div>')
        item = self.parse(html)["item"]
        self.assertEqual((item["installment_count"], item["installment_price"]), (2, 22))

    def test_shipping_stays_with_requested_seller(self):
        soup = BeautifulSoup(self.json_html(self.product(offers=[
            {"seller": {"name": "Seller A"}, "price": 20, "shippingDetails": {"shippingRate": {"value": 15}}},
            {"seller": {"name": "Seller B"}, "price": 22, "shippingDetails": {"shippingRate": {"value": 0}}},
        ])), "html.parser")
        item = {"sku": "W10531315", "name": "Sensor W10531315", "seller": "Seller A"}
        details = _jsonld_offer_details(soup, item, "01001000", self.source.url)
        self.assertEqual(details["shipping_cost"], 15)
        item["seller"] = "Unknown seller"
        self.assertEqual(_jsonld_offer_details(soup, item, "01001000", self.source.url), {})

    def test_shipping_does_not_use_another_country_or_postal_code(self):
        offer = {"shippingDetails": [
            {"shippingDestination": {"addressCountry": "US"}, "shippingRate": {"value": 0}},
            {"shippingDestination": {"addressCountry": "BR", "postalCode": "22041-001"}, "shippingRate": {"value": 1}},
            {"shippingDestination": {"addressCountry": "BR", "postalCode": "01001-000"},
             "shippingRate": {"value": 12}, "deliveryTime": {"transitTime": {"value": 48, "unitCode": "HUR"}}},
        ]}
        details = shipping_details(offer, "01001000")
        self.assertEqual((details["shipping_cost"], details["delivery_max_days"]), (12, 2))

    def test_handling_and_transit_hours_are_summed_before_rounding(self):
        details = shipping_details({"shippingDetails": {"shippingRate": {"value": 5},
            "deliveryTime": {"handlingTime": {"value": 12, "unitCode": "HUR"},
                             "transitTime": {"value": 12, "unitCode": "HUR"}}}})
        self.assertEqual(details["delivery_max_days"], 1)

    def test_unknown_handling_maximum_does_not_become_zero(self):
        details = shipping_details({"shippingDetails": {"shippingRate": {"value": 5},
            "deliveryTime": {"handlingTime": {"minValue": 1},
                             "transitTime": {"minValue": 2, "maxValue": 4}}}})
        self.assertEqual(details["delivery_min_days"], 3)
        self.assertNotIn("delivery_max_days", details)

    def test_new_quote_does_not_borrow_old_days(self):
        old = {"shipping_cost": 10, "delivery_max_days": 3, "destination_zip": "22041001"}
        updated = merge_offer_details(old, {"shipping_cost": 15, "logistics_source": "dufrio", "destination_zip": "01001000"})
        self.assertNotIn("delivery_max_days", updated)
        self.assertEqual(updated["destination_zip"], "01001000")

    def test_multi_unit_pack_is_not_compared_to_one_catalog_part(self):
        for title in ("Kit 10 Sensores W10531315", "Sensor W10531315 02 unidades"):
            self.assertFalse(resolve_candidate(self.identity, {"name": title}).accepted)
            self.assertIsNone(_normalize_result({"name": title, "price": 20, "url": self.source.url}, "W10531315"))
        self.assertTrue(resolve_candidate(self.identity, {"name": "Kit 2,7k Sensor W10531315"}).accepted)

    def test_historical_multipack_cannot_define_identity_of_the_catalog_sku(self):
        known = SimpleNamespace(sku="W11199342", description=None, brand=None)
        old = SimpleNamespace(name="Kit 2 Refil Consul W11199342", brand="Magazine Luiza")
        identity = identity_from_models(known, [old])
        self.assertIsNone(identity.name)
        self.assertIsNone(identity.brand)
        self.assertTrue(resolve_candidate(identity, {"name": "Refil Consul W11199342 original"}).accepted)
        self.assertFalse(resolve_candidate(identity, {"name": old.name}).accepted)

    def test_known_identifier_prevents_fuzzy_match_without_code(self):
        self.assertFalse(resolve_candidate(self.identity, {"name": "Sensor de temperatura original"}).accepted)

    def test_compatibility_code_does_not_turn_cable_into_sensor(self):
        self.assertFalse(resolve_candidate(self.identity, {"name": "Cabo adaptador para sensor W10531315"}).accepted)

    def test_empty_structured_sku_is_not_strong_evidence(self):
        identity = ProductIdentity(sku="", name="Sensor de temperatura")
        result = resolve_candidate(identity, {"name": "Sensor de temperatura"})
        self.assertNotEqual(result.method, "sku_exact")

    def test_robot_check_with_http_200_is_a_block_not_a_wrong_product(self):
        result = self.parse('<html><head><title>Robot Check</title></head><body>Verifique o acesso</body></html>')
        self.assertEqual(result["status"], "BLOCKED")

    def test_delivery_range_is_not_concatenated_into_thirty_five_days(self):
        self.assertEqual(delivery_range("3 a 5 dias uteis"), (3, 5))
        self.assertEqual(delivery_range("Transportadora 123 entrega 10 dias"), (10, 10))
        self.assertEqual(delivery_range("48 horas"), (2, 2))

    def test_free_shipping_advertisement_and_pickup_are_not_delivery_quotes(self):
        self.assertEqual(_parse_shipping_quote("Frete gratis para compras acima de R$ 99,00"), {})
        self.assertEqual(_parse_shipping_quote("Retirar na loja R$ 0,00 em 1 dia"), {})
        details = _parse_shipping_quote("PAC Frete R$ 15,00 em 3 - 5 dias uteis")
        self.assertEqual((details["shipping_cost"], details["delivery_min_days"], details["delivery_max_days"]), (15, 3, 5))
        self.assertEqual(details["delivery_days_type"], "business")

    def test_browser_quote_reads_public_rates_and_excludes_pickup(self):
        page = Mock()
        page.evaluate.return_value = {"rates": [
            {"name": "Retirar pessoalmente", "price": 0, "deliveryTime": "1"},
            {"name": "PAC", "price": 20, "deliveryTime": "5"},
            {"name": "SEDEX", "price": 11.94, "deliveryTime": "1"},
        ]}
        details = _browser_platform_quote(page, "01001000", 5)
        self.assertEqual((details["shipping_cost"], details["delivery_max_days"]), (11.94, 1))
        self.assertEqual(details["destination_zip"], "01001000")
        self.assertEqual(details["logistics_source"], "loja_integrada_browser")

    def test_woocommerce_shipping_uses_minor_units_and_does_not_invent_days(self):
        quote = _woocommerce_rate_details({"shipping_rates": [{"shipping_rates": [
            {"rate_id": "local_pickup:1", "name": "Retirada", "price": "0", "currency_code": "BRL", "currency_minor_unit": 2},
            {"rate_id": "pac", "name": "PAC", "price": "2190", "currency_code": "BRL", "currency_minor_unit": 2, "meta_data": []},
        ]}]})
        self.assertEqual(quote["shipping_cost"], 21.9)
        self.assertIsNone(quote["delivery_max_days"])

    @patch("playwright.sync_api.sync_playwright")
    def test_browser_is_closed_when_navigation_fails(self, playwright):
        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
        browser.new_page.return_value.goto.side_effect = TimeoutError("navigation timeout")
        self.assertEqual(render_product_page(self.source.url, 5)["status"], "BROWSER_ERROR")
        browser.close.assert_called_once()

    @patch("services.precise_extraction.upsert_product_source", return_value=None)
    @patch("services.precise_extraction.collect_product_with_playwright")
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_precise_collects_price_from_browser_after_http_block(self, http, browser, _upsert):
        self.source.id = 1
        http.return_value = {1: {"status": "BLOCKED", "reason": "HTTP 403"}}
        item = {"name": "Sensor W10531315", "source": "Dufrio", "store": "Seller A",
                "seller": "Seller A", "url": self.source.url, "price": 20}
        browser.return_value = {"status": "OK", "items": [item], "match": resolve_candidate(self.identity, item)}
        control = SimpleNamespace(concurrent_requests_per_domain=1, request_timeout_seconds=5,
                                  playwright_product_fallback=True, playwright_shipping_fallback=False)
        items, stores = _collect_sources(Mock(), self.identity, [self.source], None, control)
        self.assertEqual((items[0]["price"], stores), (20, {"dufrio"}))
        self.assertEqual(self.source.validation_status, "ok")

    @patch("services.offer_enrichment._postal_state", return_value=None)
    def test_shopify_quote_uses_the_matching_variant(self, _state):
        session = Mock()
        product = Mock(status_code=200)
        product.json.return_value = {"variants": [
            {"id": 1, "sku": "OTHER", "available": True},
            {"id": 2, "sku": "W10531315", "available": True},
        ]}
        rates = Mock(status_code=200)
        rates.json.return_value = {"shipping_rates": [{"name": "PAC", "price": 10, "delivery_days": [3, 5]}]}
        session.get.side_effect = [product, rates]
        session.post.return_value.status_code = 200
        quote = _shopify_shipping(session, "https://loja.test/products/sensor", "01001000", "W10531315")
        self.assertEqual(session.post.call_args.kwargs["data"]["id"], 2)
        self.assertEqual(quote["delivery_max_days"], 5)

    def test_unavailable_offer_cannot_be_the_cheapest(self):
        common = {"name": "Sensor W10531315", "sku": "W10531315", "url": self.source.url}
        rows = _build_comparison_rows([
            {**common, "current_price": 1, "store": "Sold out", "availability": "out_of_stock"},
            {**common, "current_price": 20, "store": "Seller A", "availability": "InStock"},
        ], "W10531315")
        self.assertEqual((rows[0]["cheapest_price"], rows[0]["store_count"]), (20, 1))

    def test_same_seller_in_two_storefronts_keeps_both_comparison_entries(self):
        common = {"name": "Sensor W10531315", "sku": "W10531315", "url": self.source.url, "store": "Dufrio"}
        rows = _build_comparison_rows([
            {**common, "source": "Dufrio", "current_price": 20},
            {**common, "source": "Magazine Luiza", "current_price": 22},
        ], "W10531315")
        self.assertEqual((rows[0]["store_count"], len(rows[0]["offers"])), (2, 2))

    def test_excel_utc_observation_is_converted_to_sao_paulo(self):
        self.assertEqual(_excel_datetime(datetime(2026, 10, 6, 15, 30)), datetime(2026, 10, 6, 12, 30))

    @patch("services.offer_enrichment._fetch_details_uncached")
    def test_manual_refresh_bypasses_logistics_cache(self, fetch):
        fetch.side_effect = [{"shipping_cost": 10}, {"shipping_cost": 12}]
        self.assertEqual(_fetch_details(self.source.url, 20, "01001000")["shipping_cost"], 10)
        self.assertEqual(_fetch_details(self.source.url, 20, "01001000", force_refresh=True)["shipping_cost"], 12)

    @patch("services.offer_enrichment._fetch_details_uncached")
    def test_shared_url_cache_does_not_mix_sellers(self, fetch):
        fetch.side_effect = [{"shipping_cost": 10}, {"shipping_cost": 12}]
        for seller, cost in (("Seller A", 10), ("Seller B", 12)):
            self.assertEqual(_fetch_details(self.source.url, 20, "01001000", item={"seller": seller})["shipping_cost"], cost)

    @patch("services.playwright_fallback.render_product_page")
    def test_rendered_product_uses_same_matching_and_seller_parser(self, render):
        render.return_value = {"status": "OK", "url": self.source.url, "html": self.json_html(self.product())}
        result = collect_product_with_playwright(self.source, self.identity, "01001000", 5)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["discovery_source"], "browser_pdp")
        self.assertEqual(result["item"]["seller"], "Seller A")

    def test_actual_browser_reads_delayed_product_and_quote_in_one_page(self):
        product = json.dumps(self.product())
        queries = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path.startswith("/quote"):
                    queries.append(parse_qs(urlparse(handler.path).query))
                    body = json.dumps([{"name": "SEDEX", "price": 11.94, "deliveryTime": "1"}]).encode()
                    content_type = "application/json"
                else:
                    body = ("<html><body><h1>Sensor W10531315</h1><script>"
                            "window.PRODUTO_ID='42';window.URL_PRODUTO_FRETE_CALCULAR=location.origin+'/quote';"
                            "setTimeout(()=>{const node=document.createElement('script');node.type='application/ld+json';"
                            f"node.textContent=JSON.stringify({product});document.body.appendChild(node);"
                            "},100);</script></body></html>").encode()
                    content_type = "text/html"
                handler.send_response(200)
                handler.send_header("Content-Type", content_type)
                handler.end_headers()
                handler.wfile.write(body)
            def log_message(handler, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.source.url = f"http://127.0.0.1:{server.server_port}/product/W10531315"
            result = collect_product_with_playwright(self.source, self.identity, "01001000", 5, include_shipping=True)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["item"]["price"], 20)
            self.assertEqual((result["item"]["shipping_cost"], result["item"]["delivery_max_days"]), (11.94, 1))
            self.assertEqual(result["item"]["destination_zip"], "01001000")
            self.assertEqual(queries[0]["cep"], ["01001000"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    @patch("services.precise_extraction._collect_sources", return_value=([], set()))
    def test_two_extractions_on_same_day_finish_without_repeating_checked_stores(self, _collect):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        visited = []
        def discover(db, identity, store, _control, **_options):
            visited.append(store["key"])
            cursor = db.query(ScrapeCursor).filter_by(sku=identity.sku, store=store["key"], kind="search", key=identity.sku).first()
            if cursor is None:
                cursor = ScrapeCursor(sku=identity.sku, store=store["key"], kind="search", key=identity.sku,
                                      initial_url="https://store.test/search", next_url="https://store.test/search")
                db.add(cursor)
            cursor.completed = True
            cursor.last_status = "OK"
            cursor.last_attempted_at = cursor.last_checked_at = cursor.updated_at = datetime.utcnow()
            db.commit()
            return []
        with sessionmaker(bind=engine)() as db, patch("services.precise_extraction._discover_store", side_effect=discover):
            obsolete = ScrapeCursor(sku="W10531315", store="magalu", kind="search", key="Old incorrect title",
                                    initial_url="https://store.test/old-search", next_url="https://store.test/old-search",
                                    completed=False, last_status="BLOCKED", last_attempted_at=datetime.utcnow() - timedelta(days=2))
            db.add(obsolete)
            db.commit()
            for _run in range(2):
                started = datetime.utcnow()
                for batch in range(6):
                    summary = run_precise_extraction("W10531315", db, collection_after=started, resume=batch > 0)
                    if not summary["more_work"]:
                        break
                self.assertFalse(summary["more_work"])
                self.assertEqual(summary["pending_stores"], 0)
                self.assertTrue(obsolete.completed)
        self.assertEqual(len(visited), 2 * len(TARGET_STORES))
        engine.dispose()

    def test_recently_blocked_page_records_check_without_repeating_network(self):
        now = datetime.utcnow()
        cursor = SimpleNamespace(completed=False, last_status="BLOCKED", next_url=self.source.url,
                                 browser_attempted_url=self.source.url, last_attempted_at=now - timedelta(minutes=5))
        cursor.browser_attempted_at = now - timedelta(minutes=5)
        fetch = Mock()
        self.assertEqual(_advance(cursor, self.source.url, fetch, {}), [])
        self.assertGreaterEqual(cursor.last_checked_at, now)
        self.assertEqual(cursor.last_attempted_at, now - timedelta(minutes=5))
        fetch.assert_not_called()

    def test_recent_http_failure_does_not_suppress_browser_retry_from_old_job(self):
        now = datetime.utcnow()
        cursor = SimpleNamespace(completed=False, last_status="BLOCKED", next_url=self.source.url,
                                 browser_attempted_url=self.source.url, browser_attempted_at=now-timedelta(days=2),
                                 last_attempted_at=now-timedelta(minutes=5), store="magalu", visited_urls="[]",
                                 pages_completed=0, candidates_found=0)
        fetch = Mock(side_effect=[([], None, False), ([], None, False)])
        _advance(cursor, self.source.url, fetch, {})
        self.assertEqual(fetch.call_count, 2)
        self.assertGreaterEqual(cursor.browser_attempted_at, now)

    def test_cooled_down_pages_cannot_occupy_entire_seller_batch(self):
        now = datetime.utcnow()
        cooled = [SimpleNamespace(completed=False, last_status="BLOCKED", last_attempted_at=now,
                                  browser_attempted_at=now, next_url="https://shop.test/1",
                                  browser_attempted_url="https://shop.test/1") for _ in range(5)]
        pending = SimpleNamespace(completed=False, last_status="BLOCKED", last_attempted_at=now,
                                  browser_attempted_at=None, browser_attempted_url=None, next_url="https://shop.test/2")
        self.assertEqual(_seller_page_batch([*cooled,pending],5),[pending])

    def test_parser_exception_is_recorded_and_cannot_leave_page_unattempted_forever(self):
        cursor = SimpleNamespace(completed=False, last_status=None, next_url=self.source.url,
                                 browser_attempted_url=None, browser_attempted_at=None,
                                 last_attempted_at=None, store="dufrio")
        fetch = Mock(side_effect=ValueError("bad page parser"))
        self.assertEqual(_advance(cursor, self.source.url, fetch, {}), [])
        self.assertEqual(cursor.last_status, "PARSER_ERROR")
        self.assertIsNotNone(cursor.last_attempted_at)
        self.assertEqual(cursor.browser_attempted_url, self.source.url)

    @patch("services.precise_extraction.collect_product_with_playwright")
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_server_requested_pause_does_not_trigger_browser_probe(self, http, browser):
        self.source.id = 1
        http.return_value = {1: {"status": "BLOCKED", "reason": "HTTP 429", "_retry_after": "120"}}
        control = SimpleNamespace(concurrent_requests_per_domain=1, request_timeout_seconds=5,
                                  playwright_product_fallback=True, playwright_shipping_fallback=True)
        _collect_sources(Mock(), self.identity, [self.source], None, control)
        browser.assert_not_called()

    @patch("services.external_market_providers.run_external_market_provider")
    @patch("services.precise_extraction.scrape_comparison_offers", return_value=[])
    @patch("services.precise_extraction.search_bing_shopping", return_value=[])
    def test_precise_external_fallback_does_not_call_paid_provider(self, _bing, _comparison, paid):
        self.assertEqual(_external_candidates(self.identity, "dufrio"), [])
        paid.assert_not_called()

    def test_retry_after_cannot_hold_worker_and_post_is_not_retried(self):
        with create_resilient_session() as session:
            retry = session.get_adapter("https://").max_retries
            self.assertFalse(retry.is_retry("GET", 429, True))
            self.assertFalse(retry.is_retry("GET", 503, True))
            self.assertFalse(retry.is_retry("POST", 503))
            self.assertTrue(retry.is_retry("GET", 503))

    def test_reused_listing_keeps_observation_time_and_cannot_erase_new_quote(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            offer = {"name": "Sensor W10531315", "price": 20, "store": "Seller A",
                     "source": "Dufrio", "url": self.source.url, "brand": "Brastemp",
                     "category": "Sensor", "image_url": None, "sku": "W10531315"}
            seen = datetime.utcnow() - timedelta(hours=2)
            _save_result({**offer, "is_cached": True, "captured_at": seen.isoformat()}, db)
            db.commit()
            row = db.query(PriceHistory).one()
            self.assertEqual(row.timestamp, seen)
            self.assertTrue(row.is_cached)
            _save_result({**offer, "shipping_cost": 15, "delivery_max_days": 4}, db)
            db.commit()
            refreshed = row.timestamp
            _save_result({**offer, "is_cached": True, "captured_at": seen.isoformat()}, db)
            db.commit()
            self.assertEqual(row.timestamp, refreshed)
            self.assertEqual(row.shipping_cost, 15)
        engine.dispose()

    def test_invalid_source_is_excluded_but_blocked_source_keeps_cache(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            for seller, status in (("Invalid", "invalid_match"), ("Blocked", "blocked")):
                _save_result({"name": "Sensor W10531315", "price": 20, "store": seller,
                              "source": "Dufrio", "url": self.source.url, "brand": "Brastemp",
                              "category": "Sensor", "image_url": None, "sku": "W10531315"}, db)
                db.add(ProductSource(sku="W10531315", store="dufrio", seller=seller,
                                     url=self.source.url, validation_status=status))
            db.commit()
            rows = _latest_products_query(db).all()
            self.assertEqual([price.store for _product, price in rows], ["Blocked"])
        engine.dispose()

    def test_seller_case_change_updates_same_listing_without_leaving_old_cheaper_product(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            offer = {"name": "Sensor W10531315", "price": 10, "store": "Seller A",
                     "source": "Dufrio", "url": self.source.url, "brand": "Brastemp",
                     "category": "Sensor", "image_url": None, "sku": "W10531315"}
            _save_result(offer, db)
            db.commit()
            _save_result({**offer, "store": "SELLER A", "price": 20}, db)
            db.commit()
            # Windows can give two observations the same clock tick. The later
            # database record must win without duplicating this seller.
            for price in db.query(PriceHistory).all():
                price.timestamp = datetime(2026, 10, 6, 12, 0, 0)
            db.commit()
            rows = _latest_products_query(db).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1].price, 20)
        engine.dispose()

    def test_latest_offer_lookup_preserves_sku_seller_and_legacy_postal_scope(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            offer = {"name": "Sensor W10531315", "price": 20, "store": "Peças A",
                     "source": "Magalu", "url": self.source.url, "brand": "Brastemp",
                     "category": "Sensor", "image_url": None, "sku": "W10531315"}
            for sku, seller, cep in (('W10531315','Peças A','01001-000'), ('W10531315','PEÇAS A','30111000'),
                                     ('W10531315','Peças B','01001000'), ('W11199342','Peças A','01001000')):
                _save_result({**offer, 'sku':sku, 'store':seller, 'destination_zip':cep}, db)
            db.commit()
            _save_result({**offer, 'source':'Magazine Luiza', 'store':'Pecas A',
                          'destination_zip':'01001000', 'price':25, 'delivery_max_days':3}, db)
            db.commit()
            newest = db.query(PriceHistory).filter_by(price=25).one()
            original = db.query(PriceHistory).filter_by(destination_zip='01001-000').one()
            self.assertEqual(newest.product_id, original.product_id)
            self.assertEqual(db.query(Product).count(), 3)
            self.assertEqual(newest.delivery_max_days, 3)
