import unittest
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scrapy.http import HtmlResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crawlers.base import collect_http, parse_product_response
from crawlers.mercado_livre import collect_mercado_livre
from crawlers.profiles import DEFAULT_PROFILE
from crawlers.registry import HTTP_COLLECTORS
from crawlers.shopee import collect_shopee
from database import Base
from models.product import ProductSource
from services.product_resolver import ProductIdentity, resolve_candidate
from services.product_sources import upsert_product_source
from services.precise_extraction import _collect_sources, _discover_store, _discovery_batch, _source_due, _stores_requiring_discovery, _valid_offer
from services.playwright_fallback import collect_shipping_with_playwright
from services.store_registry import TARGET_STORES, find_target_store


FIXTURE = Path(__file__).parent / "fixtures" / "product_pdp.html"
HTTP_STORES = (
    "electrolux", "midea", "consul", "brastemp",
    "amazon",
    "magalu",
    "leroy_merlin",
    "dufrio",
    "friolar",
    "refrigeracao_mota",
    "mg_parts",
    "gold_service",
    "comclick",
)


class PreciseCollectionTests(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        control = patch('services.marketplace_intervention._path', side_effect=lambda store, kind: Path(folder.name) / f'{store}-{kind}.json')
        control.start(); self.addCleanup(control.stop)
        self.html = FIXTURE.read_bytes()
        self.identity = ProductIdentity(
            sku="W10531315",
            mpn="W10531315",
            model="W10531315",
            brand="Whirlpool",
        )

    def _source(self, store="dufrio", seller="Loja Teste"):
        return SimpleNamespace(
            store=store,
            seller=seller,
            url="https://example.test/produto/W10531315",
            external_product_id=None,
            asin=None,
            item_id=None,
            shop_id=None,
            ean=None,
            mpn="W10531315",
            model="W10531315",
            brand="Whirlpool",
        )

    def test_recent_blocked_source_waits_before_retry(self):
        now = datetime.utcnow()
        source = SimpleNamespace(validation_status="blocked", last_attempted_at=now - timedelta(minutes=5))
        self.assertFalse(_source_due(source, now - timedelta(minutes=10), now))
        source.last_attempted_at = now - timedelta(minutes=31)
        self.assertTrue(_source_due(source, now - timedelta(minutes=10), now))
        source.validation_status = "ok"
        self.assertTrue(_source_due(source, now - timedelta(minutes=10), now))

    def test_bootstrap_recovers_each_seller_from_shared_pdp(self):
        from models.product import PriceHistory, Product
        from services.precise_extraction import _bootstrap_existing_sources
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        url = 'https://www.dufrio.com.br/sensor-w10531315'
        with sessionmaker(bind=engine)() as db:
            product = Product(sku=self.identity.sku, name="Sensor Whirlpool W10531315")
            other = Product(sku="W11199342", name="Outra peça")
            db.add_all([product, other])
            db.flush()
            for target, seller in [(product, 'A'), (product, 'B'), (other, 'B')]:
                db.add(PriceHistory(product_id=target.id, url=url, store=seller, source='Dufrio', price=10))
            db.add(ProductSource(sku=self.identity.sku, store='dufrio', seller='A', url=url, active=False))
            db.commit()
            self.assertEqual(_bootstrap_existing_sources(db, self.identity, [product]), 1)
            sources = {row.seller: row for row in db.query(ProductSource)}
            self.assertFalse(sources['A'].active)
            self.assertEqual(sources['B'].product_id, product.id)
            self.assertEqual(_bootstrap_existing_sources(db, self.identity, [product]), 0)

    def test_variant_bootstrap_reuses_only_confirmed_sources_in_selected_stores(self):
        from services.precise_extraction import _bootstrap_existing_sources
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        identity = ProductIdentity(sku='BRM46MBBNA', model='BRM46MB')
        with sessionmaker(bind=engine)() as db:
            db.add_all([
                ProductSource(sku='BRM46MB', store='magalu', seller='Loja A',
                              url='https://www.magazineluiza.com.br/geladeira/p/1/ed/refr/',
                              listing_name='Geladeira Brastemp BRM46MB 220V'),
                ProductSource(sku='BRM46MB', store='magalu', seller='Loja B',
                              url='https://www.magazineluiza.com.br/geladeira/p/2/ed/refr/',
                              listing_name='Geladeira Brastemp BRM46MB 127V'),
                ProductSource(sku='BRM46MB', store='shopee', seller='Loja C',
                              url='https://shopee.com.br/product/1/2',
                              listing_name='Geladeira Brastemp BRM46MB 220V'),
            ])
            db.commit()
            self.assertEqual(_bootstrap_existing_sources(db, identity, [], {'magalu'}), 1)
            saved = db.query(ProductSource).filter_by(sku=identity.sku).one()
            self.assertEqual((saved.store, saved.seller, saved.validation_status), ('magalu', 'Loja A', 'discovered'))

    def test_confirmed_old_channel_alias_leaves_current_views_and_keeps_history(self):
        from models.product import OfferObservation, PriceHistory
        from services.product_sources import canonical_source_url
        from services.scraper_service import _save_result, _cached_sku_observations
        from routes.products import _find_sku_products
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        sku = self.identity.sku
        url = 'https://www.brastemp.com.br/sensor/p'
        alias = canonical_source_url(url + '?skuId=42&seller_id=1')
        primary = canonical_source_url(url + '?idsku=42&seller_id=1&sc=2')
        old = dict(sku=sku, name='Sensor W10531315', brand='Brastemp', price=19, source='Brastemp',
                   store='Brastemp', seller='Brastemp', url=alias, image_url=None, category='Sensor')
        catalog = [{ 'productName': old['name'], 'link': url, 'items': [{'itemId':'42',
            'sellers':[{'sellerName':'Brastemp','sellerId':'1','addToCartLink':'/checkout/cart/add?sc=2',
                        'commertialOffer':{'IsAvailable':True,'Price':20}}]}]}]
        with sessionmaker(bind=engine, autoflush=False)() as db:
            _save_result(old, db)
            previous = ProductSource(sku=sku, store='brastemp', seller='Brastemp', url=alias)
            db.add(previous)
            db.commit()
            match = resolve_candidate(self.identity, old)
            current = {**old, 'price':20}
            with patch('services.precise_extraction.collect_sources_with_scrapy',
                    return_value={previous.id:{'status':'OK','item':current,'match':match}}):
                offers, _ = _collect_sources(db, self.identity, [previous], None,
                    SimpleNamespace(concurrent_requests_per_domain=2,request_timeout_seconds=5),
                    catalog_snapshots={'brastemp':catalog})
            for item in offers:
                _save_result(item, db)
            db.commit()
            self.assertEqual(previous.validation_status, 'consolidated')
            self.assertFalse(previous.active)
            self.assertEqual([r.url for _, r in _cached_sku_observations(db, sku)], [primary])
            self.assertEqual([r.url for _, r in _find_sku_products(db, sku)], [primary])
            self.assertEqual(db.query(PriceHistory).count(), 2)
            self.assertEqual(db.query(OfferObservation).count(), 2)

    @patch('services.precise_extraction.collect_sources_with_scrapy')
    def test_invalid_price_is_not_a_successful_store(self, scrape):
        source = self._source()
        source.id = 1
        item = {'url': 'https://www.dufrio.com.br/sensor-w10531315', 'name': 'Sensor W10531315',
                'source': 'Dufrio', 'store': 'A', 'seller': 'A', 'price': -1}
        scrape.return_value = {1: {'status': 'OK', 'item': item, 'match': resolve_candidate(self.identity, item)}}
        collected, success = _collect_sources(Mock(), self.identity, [source], None,
            SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5))
        self.assertEqual((collected, success, source.validation_status), ([], set(), 'parser_error'))
        self.assertFalse(hasattr(source, 'last_collected_at'))

    def test_native_collection_starts_as_soon_as_each_catalog_is_ready(self):
        from threading import Event
        ready = Event()
        sources = [self._source(store) for store in ('brastemp', 'consul', 'brastemp')]
        for i, source in enumerate(sources):
            source.id = i + 1
            source.url = f'https://www.{source.store}.com.br/sensor/p?idsku={i+1}&seller_id=1'
        def catalog(store, *_):
            if store == 'consul' and not ready.wait(1):
                raise TimeoutError('Outro catálogo atrasou a coleta')
            return []
        def collect(source, *_args, **_kwargs):
            if source.store == 'brastemp':
                ready.set()
            return {'status': 'NOT_FOUND'}
        with patch('services.vtex_public.catalog', side_effect=catalog) as read, \
             patch('services.vtex_public.collect_source', side_effect=collect):
            _collect_sources(Mock(), self.identity, sources, None,
                SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5))
        self.assertEqual(read.call_count, 2)
        self.assertEqual([source.validation_status for source in sources], ['not_found'] * 3)

    def test_discovery_batch_rotates_recently_attempted_store(self):
        db = Mock()
        db.query.return_value.filter.return_value.all.return_value = [("amazon", datetime.utcnow())]
        stores = [{"key": key} for key in (
            "amazon", "magalu", "shopee", "dufrio", "friolar", "mg_parts",
        )]
        batch = _discovery_batch(db, "W10531315", stores)
        self.assertEqual([store["key"] for store in batch], [
            "magalu", "shopee", "dufrio",
        ])

    def test_every_requested_store_has_a_precise_collector(self):
        configured = {store["key"] for store in TARGET_STORES}
        self.assertEqual(
            configured,
            set(HTTP_STORES) | {"mercado_livre", "shopee"},
        )
        self.assertTrue(set(HTTP_STORES) <= set(HTTP_COLLECTORS))

    def test_offer_validation_requires_matching_sku_seller_store_and_real_price(self):
        good = {
            "name": "Sensor Whirlpool W10531315", "sku": "W10531315",
            "source": "Dufrio", "store": "Dufrio", "seller": "Dufrio",
            "url": "https://www.dufrio.com.br/sensor-w10531315",
            "price": 18.47, "shipping_cost": 0.0,
            "delivery_min_days": "2", "delivery_max_days": "4",
        }
        self.assertTrue(_valid_offer(self.identity, good))
        self.assertEqual(good["delivery_max_days"], 4)
        for changes in (
            {"name": "Outro produto W99999999"},
            {"seller": "", "store": ""},
            {"url": "https://outro.example/sensor-w10531315"},
            {"price": float("nan")},
            {"price": -1},
            {"shipping_cost": -1},
            {"delivery_min_days": 5, "delivery_max_days": 2},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(_valid_offer(self.identity, {**good, **changes}))
        self.assertEqual(find_target_store("Duofrio")["key"], "dufrio")

    def test_standard_pdp_contract_for_every_http_store(self):
        for store in HTTP_STORES:
            with self.subTest(store=store):
                response = HtmlResponse(
                    url=f"https://{store}.example/produto/W10531315",
                    body=self.html,
                    encoding="utf-8",
                )
                result = parse_product_response(response, self._source(store), self.identity, DEFAULT_PROFILE)
                self.assertEqual(result["status"], "OK")
                item = result["item"]
                self.assertEqual(item["seller"], "Loja Teste")
                self.assertEqual(item["cash_price"], 129.90)
                self.assertEqual(item["installment_count"], 3)
                self.assertEqual(item["shipping_cost"], 15.50)
                self.assertEqual(item["delivery_max_days"], 4)
                self.assertEqual(item["availability"], "in_stock")

    def test_out_of_stock_is_preserved(self):
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=self.html.replace(b"InStock", b"OutOfStock"),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["availability"], "out_of_stock")

    def test_missing_freight_does_not_invent_a_value(self):
        product = {
            "@type": "Product",
            "name": "Peça Whirlpool W10531315",
            "mpn": "W10531315",
            "offers": {"@type": "Offer", "price": "129.90"},
        }
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=f'<script type="application/ld+json">{json.dumps(product)}</script>'.encode(),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("shipping_cost", result["item"])

    def test_multiple_jsonld_offers_keep_each_seller_and_price(self):
        product = {
            "@type": "Product",
            "name": "Peça Whirlpool W10531315",
            "mpn": "W10531315",
            "offers": [
                {"@type": "Offer", "price": "120.00", "seller": {"name": "Seller A"}},
                {"@type": "Offer", "price": "135.00", "seller": {"name": "Seller B"}},
            ],
        }
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=f'<script type="application/ld+json">{json.dumps(product)}</script>'.encode(),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(
            [(item["seller"], item["price"]) for item in result["items"]],
            [("Seller A", 120.0), ("Seller B", 135.0)],
        )

    def test_offer_price_specification_from_woocommerce(self):
        product = {
            "@type": "Product",
            "name": "Sensor Whirlpool W10531315",
            "offers": [{
                "@type": "Offer",
                "priceSpecification": [{
                    "@type": "UnitPriceSpecification",
                    "price": "18.47",
                    "priceCurrency": "BRL",
                }],
                "seller": {"name": "Refrigeracao Mota"},
            }],
        }
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=f'<script type="application/ld+json">{json.dumps(product)}</script>'.encode(),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["price"], 18.47)

    def test_aggregate_low_price_is_not_saved_as_a_seller_offer(self):
        product = {
            "@type": "Product",
            "name": "Peça Whirlpool W10531315",
            "mpn": "W10531315",
            "offers": {"@type": "AggregateOffer", "lowPrice": "99.00", "offerCount": 2},
        }
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=(
                '<meta property="product:price:amount" content="99.00">'
                f'<script type="application/ld+json">{json.dumps(product)}</script>'
            ).encode(),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "PARSER_ERROR")

    def test_aggregate_with_individual_offers_keeps_both_sellers(self):
        product = {
            "@type": "Product",
            "name": "Peça Whirlpool W10531315",
            "mpn": "W10531315",
            "offers": {
                "@type": "https://schema.org/AggregateOffer",
                "lowPrice": "99.00",
                "offers": [
                    {"@type": "Offer", "price": "120.00", "seller": "Seller A"},
                    {"@type": "Offer", "price": "135.00", "seller": "Seller B"},
                ],
            },
        }
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=f'<script type="application/ld+json">{json.dumps(product)}</script>'.encode(),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(
            [(item["seller"], item["price"]) for item in result["items"]],
            [("Seller A", 120.0), ("Seller B", 135.0)],
        )

    def test_free_shipping_is_zero_instead_of_missing(self):
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=self.html.replace(b'"value": "15.50"', b'"value": 0'),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["item"]["shipping_cost"], 0.0)

    def test_scalar_zero_shipping_rate_is_preserved(self):
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=self.html.replace(
                b'"shippingRate": {"@type": "MonetaryAmount", "value": "15.50"}',
                b'"shippingRate": 0',
            ),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["item"]["shipping_cost"], 0.0)

    @patch("crawlers.base.create_resilient_session")
    def test_timeout_is_reported_without_invalidating_match(self, create_session):
        session = create_session.return_value
        session.get.side_effect = TimeoutError("timeout")
        result = collect_http(self._source(), self.identity, None, DEFAULT_PROFILE, 5)
        self.assertEqual(result["status"], "TIMEOUT")

    @patch("crawlers.base.enrich_offer", side_effect=lambda item, _zip: item)
    @patch("crawlers.base.create_resilient_session")
    def test_product_without_shipping_does_not_claim_quoted_zip(self, create_session, _enrich):
        source = self._source()
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type":"Product","name":"Sensor W10531315",'
            '"offers":{"@type":"Offer","price":"18.47","seller":{"name":"Loja Teste"}}}'
            '</script></body></html>'
        ).encode()
        response = Mock(status_code=200, url=source.url, content=html, encoding="utf-8")
        create_session.return_value.get.return_value = response
        result = collect_http(source, self.identity, "01001000", DEFAULT_PROFILE, 5)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("shipping_zip_code", result["item"])

    def test_product_sources_keep_multiple_sellers(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            for seller in ("Seller A", "Seller B"):
                candidate = {
                    "name": "Whirlpool W10531315",
                    "url": "https://amazon.example/dp/B000000001",
                    "seller": seller,
                    "brand": "Whirlpool",
                }
                match = resolve_candidate(self.identity, candidate)
                upsert_product_source(db, self.identity, "amazon", candidate, match)
            db.commit()
            self.assertEqual(db.query(ProductSource).count(), 2)
        finally:
            db.close()
            engine.dispose()

    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_listing_price_survives_a_resumed_batch(self, collect_scrapy):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        db = session_factory()
        try:
            candidate = {
                "name": "Sensor W10531315",
                "price": 18.47,
                "url": "https://www.dufrio.com.br/sensor-w10531315.html",
                "seller": "Dufrio",
            }
            match = resolve_candidate(self.identity, candidate)
            source = upsert_product_source(db, self.identity, "dufrio", candidate, match)
            source.listing_method = "direct"
            db.commit()
            source_id = source.id
            db.close()

            db = session_factory()
            source = db.query(ProductSource).filter_by(id=source_id).one()
            collect_scrapy.return_value = {
                source_id: {"status": "BLOCKED", "reason": "HTTP 403"},
            }
            items, _stores = _collect_sources(
                db, self.identity, [source], "01001000",
                SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5),
            )
            self.assertEqual(items[0]["price"], 18.47)
            self.assertEqual(items[0]["discovery_source"], "direct_listing")
        finally:
            db.close()
            engine.dispose()

    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_failed_active_source_triggers_rediscovery(self, collect_scrapy):
        source = self._source()
        source.id = 1
        source.active = True
        collect_scrapy.return_value = {1: {"status": "BLOCKED", "reason": "HTTP 403"}}
        _items, successful_stores = _collect_sources(
            Mock(), self.identity, [source], None,
            SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5),
        )
        stores = [{"key": "dufrio", "scope": "sites"}]
        self.assertEqual(_stores_requiring_discovery(stores, successful_stores), stores)

    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_http_stores_share_one_parallel_crawl(self, collect_scrapy):
        sources = [self._source("amazon"), self._source("dufrio")]
        for index, source in enumerate(sources):
            source.id = index + 1
            source.active = True
        collect_scrapy.return_value = {
            source.id: {"status": "BLOCKED", "reason": "HTTP 403"} for source in sources
        }
        _collect_sources(
            Mock(), self.identity, sources, None,
            SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5),
        )
        collect_scrapy.assert_called_once()
        self.assertEqual(collect_scrapy.call_args.args[0], sources)

    def test_native_catalog_can_finish_before_slow_scrapy(self):
        from threading import Event

        http = self._source("dufrio")
        http.id = 1
        native = self._source("brastemp")
        native.id = 2
        native.url = "https://www.brastemp.com.br/sensor/p?idsku=42&seller_id=1"
        catalog_finished = Event()

        def collect_scrapy(sources, *_):
            if not catalog_finished.wait(2):
                return {1: {"status": "TIMEOUT", "reason": "catalog pipeline did not run"}}
            return {1: {"status": "NOT_FOUND"}}

        def read_catalog(*_):
            catalog_finished.set()
            return []

        with patch("services.precise_extraction.collect_sources_with_scrapy", side_effect=collect_scrapy), \
             patch("services.vtex_public.catalog", side_effect=read_catalog):
            _collect_sources(Mock(), self.identity, [http, native], None,
                             SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5))
        self.assertEqual(http.validation_status, "not_found")
        self.assertEqual(native.validation_status, "invalid_match")

    @patch("services.precise_extraction.collect_shipping_with_playwright")
    @patch("services.precise_extraction.enrich_offers")
    @patch("services.precise_extraction.upsert_product_source")
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_blocked_browser_domain_is_probed_only_once(self, collect_scrapy, upsert, enrich, browser):
        sources = [self._source("magalu", "Seller A"), self._source("magalu", "Seller B")]
        for index, source in enumerate(sources):
            source.id = index + 1
            source.url = f"https://www.magazineluiza.com.br/p/{index}/W10531315"
        items = [
            {"url": source.url, "name": "Sensor W10531315", "source": "Magazine Luiza",
             "store": source.seller, "seller": source.seller, "price": 20.0}
            for source in sources
        ]
        match = resolve_candidate(self.identity, items[0])
        collect_scrapy.return_value = {
            source.id: {"status": "OK", "item": item, "match": match}
            for source, item in zip(sources, items)
        }
        upsert.return_value = None
        enrich.side_effect = lambda offers, _postal_code, **_options: offers
        browser.return_value = {"_blocked_status": 403}
        collected, _ = _collect_sources(
            Mock(), self.identity, sources, "01001000",
            SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5,
                            playwright_shipping_fallback=True),
        )
        self.assertEqual(browser.call_count, 1)
        self.assertEqual(len(collected), 2)
        self.assertTrue(all("_blocked_status" not in item for item in collected))

    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_blocked_product_keeps_verified_listing_price_without_shipping(self, collect_scrapy):
        source = self._source("dufrio", "Dufrio")
        source.id = 1
        source.match_confidence = 0.9
        source.match_method = "sku_exact"
        source.listing_name = "Sensor W10531315"
        source.listing_price = 18.47
        source.listing_method = "direct"
        source.listing_seen_at = datetime.utcnow()
        collect_scrapy.return_value = {1: {"status": "BLOCKED", "reason": "HTTP 403"}}
        collected, successful_stores = _collect_sources(
            Mock(), self.identity, [source], "01001000",
            SimpleNamespace(concurrent_requests_per_domain=2, request_timeout_seconds=5),
        )
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["price"], 18.47)
        self.assertEqual(collected[0]["discovery_source"], "direct_listing")
        self.assertNotIn("shipping_cost", collected[0])
        self.assertEqual(successful_stores, set())

    @patch("services.precise_extraction.upsert_product_source")
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    def test_collect_source_keeps_multiple_sellers(self, collect_scrapy, upsert):
        source = self._source()
        source.id = 1
        source.url = "https://www.dufrio.com.br/sensor-w10531315"
        items = [
            {"url": source.url, "name": "Sensor Whirlpool W10531315", "source": "Dufrio", "store": "Seller A", "seller": "Seller A", "price": 120},
            {"url": source.url, "name": "Sensor Whirlpool W10531315", "source": "Dufrio", "store": "Seller B", "seller": "Seller B", "price": 135},
        ]
        upsert.side_effect = [SimpleNamespace(id=2), SimpleNamespace(id=3)]
        collect_scrapy.return_value = {1: {
            "status": "OK", "item": items[0], "items": items,
            "match": resolve_candidate(self.identity, {"name": "Whirlpool W10531315"}),
        }}
        collected, successful_stores = _collect_sources(
            Mock(), self.identity, [source], None,
            SimpleNamespace(
                concurrent_requests_per_domain=2,
                request_timeout_seconds=5,
                playwright_shipping_fallback=False,
            ),
        )
        self.assertEqual([item["seller"] for item in collected], ["Seller A", "Seller B"])
        self.assertEqual(successful_stores, {"dufrio"})
        self.assertEqual(source.seller, "Loja Teste")
        self.assertEqual(upsert.call_count, 2)

    def test_retail_discovery_runs_even_when_known_seller_succeeded(self):
        stores = [
            {"key": "amazon", "scope": "retail"},
            {"key": "dufrio", "scope": "sites"},
        ]
        self.assertEqual(
            _stores_requiring_discovery(stores, {"amazon", "dufrio"}),
            [stores[0]],
        )

    @patch("services.precise_extraction.upsert_product_source")
    @patch("services.precise_extraction._external_candidates")
    @patch("services.precise_extraction._direct_candidates")
    @patch("services.scraper_service._run_store_scrapers")
    @patch("services.precise_extraction.discover_indexed_pages", return_value=[])
    def test_failed_store_uses_external_discovery_when_direct_only_finds_old_url(
        self, indexed, quick, direct, external, upsert,
    ):
        old = {"name": "Whirlpool W10531315", "url": "https://www.dufrio.com.br/old", "seller": "A"}
        new = {"name": "Whirlpool W10531315", "url": "https://www.dufrio.com.br/new", "seller": "B"}
        direct.return_value = [old]
        external.return_value = [new]
        upsert.side_effect = lambda _db, _identity, _store, candidate, _match: SimpleNamespace(
            id=1 if candidate["url"] == old["url"] else 2,
        )
        sources = _discover_store(
            Mock(), self.identity, {"key": "dufrio", "direct_scrapers": (Mock(),)},
            SimpleNamespace(request_timeout_seconds=5, external_discovery_fallback=True),
            known_source_ids={1}, retry_failed=True,
        )
        external.assert_called_once()
        quick.assert_not_called()
        self.assertEqual([source.id for source in sources], [2])

    @patch("services.precise_extraction.enqueue_seller_offer_page")
    @patch("services.precise_extraction.discover_html_pages", return_value=[])
    @patch("services.precise_extraction.upsert_product_source")
    @patch("services.scraper_service._run_store_scrapers")
    @patch("services.precise_extraction._direct_candidates", return_value=[])
    def test_precise_uses_quick_store_scraper_when_discovery_is_empty(
        self, _direct, quick, upsert, _html, _seller_pages,
    ):
        quick.return_value = [{
            "name": "Sensor Whirlpool W10531315", "price": 18.47,
            "url": "https://www.dufrio.com.br/sensor-w10531315",
            "seller": "Lojista A", "source": "Dufrio", "brand": "Whirlpool",
        }]
        upsert.return_value = SimpleNamespace(id=7)
        store = {"key": "dufrio", "direct_scrapers": (Mock(),)}
        with patch("builtins.print"):
            saved = _discover_store(
                Mock(), self.identity, store,
                SimpleNamespace(request_timeout_seconds=5, external_discovery_fallback=False),
            )
        quick.assert_called_once_with(store, "W10531315")
        self.assertEqual([source.id for source in saved], [7])
        self.assertEqual(saved[0].listing_method, "quick_fallback")

    @patch("services.precise_extraction.enqueue_seller_offer_page")
    @patch("services.precise_extraction.discover_html_pages", return_value=[])
    @patch("services.precise_extraction.upsert_product_source")
    @patch("services.precise_extraction._direct_candidates")
    def test_direct_discovery_keeps_more_than_one_hundred_sellers(
        self, direct, upsert, _html, _seller_pages,
    ):
        direct.return_value = [
            {
                "name": "Sensor W10531315", "seller": f"Seller {index}",
                "url": f"https://produto.mercadolivre.com.br/MLB-{index + 100000}",
            }
            for index in range(105)
        ]
        upsert.side_effect = lambda _db, _identity, _store, candidate, _match: SimpleNamespace(
            id=int(candidate["seller"].split()[-1]) + 1,
        )
        with patch("builtins.print"):
            saved = _discover_store(
                Mock(), self.identity,
                {"key": "mercado_livre"},
                SimpleNamespace(request_timeout_seconds=5, external_discovery_fallback=False),
            )
        self.assertEqual(len(saved), 105)

    @patch("crawlers.mercado_livre.create_resilient_session")
    def test_mercado_livre_collects_official_item_and_real_seller(self, create_session):
        from crawlers.mercado_livre import _collect_official_item
        item_response = Mock(status_code=200)
        item_response.json.return_value = {
            "title": "Peça Whirlpool W10531315",
            "price": 120,
            "available_quantity": 2,
            "seller_id": 42,
            "permalink": "https://produto.mercadolivre.com.br/MLB-123456",
            "category_id": "MLB1",
            "attributes": [
                {"id": "BRAND", "value_name": "Whirlpool"},
                {"id": "MODEL", "value_name": "W10531315"},
            ],
            "pictures": [],
        }
        seller_response = Mock(status_code=200)
        seller_response.json.return_value = {"nickname": "VENDEDOR REAL"}
        create_session.return_value.get.side_effect = [item_response, seller_response]
        source = self._source("mercado_livre", "Mercado Livre")
        source.item_id = "MLB123456"
        result = _collect_official_item(source, self.identity)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["seller"], "VENDEDOR REAL")

    @patch("crawlers.shopee.create_resilient_session")
    def test_shopee_resolves_shop_name(self, create_session):
        item_response = Mock(status_code=200)
        item_response.json.return_value = {
            "data": {
                "name": "Peça Whirlpool W10531315",
                "price": 11000000,
                "stock": 3,
                "brand": {"name": "Whirlpool"},
                "attributes": [{"name": "Modelo", "value": "W10531315"}],
            }
        }
        shop_response = Mock(status_code=200)
        shop_response.json.return_value = {"data": {"name": "LOJA OFICIAL"}}
        create_session.return_value.get.side_effect = [item_response, shop_response]
        source = self._source("shopee", "Shopee Loja 42")
        source.url = 'https://shopee.com.br/product/42/123'
        source.item_id = "123"
        source.shop_id = "42"
        result = collect_shopee(source, self.identity)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["seller"], "LOJA OFICIAL")

    @patch("playwright.sync_api.sync_playwright")
    def test_playwright_shipping_fallback(self, sync_playwright):
        body = Mock()
        body.inner_text.return_value = "Frete R$ 20,00. Entrega em 3 a 5 dias."
        zip_input = Mock()
        zip_input.first = zip_input
        zip_input.count.return_value = 0
        page = Mock()
        page.url = "https://example.test/produto"
        page.locator.side_effect = lambda selector: body if selector == "body" else zip_input
        browser = Mock()
        browser.new_page.return_value = page
        runtime = Mock()
        runtime.chromium.launch.return_value = browser
        context = sync_playwright.return_value
        context.__enter__.return_value = runtime
        context.__exit__.return_value = False

        result = collect_shipping_with_playwright(
            "https://example.test/produto",
            "01310100",
            5,
        )
        self.assertEqual(result, {})

        zip_input.count.return_value = 1
        page.evaluate.side_effect = [None, [], ["Frete R$ 20,00. Entrega em 3 a 5 dias."]]
        zip_input.locator.return_value.locator.return_value.first.count.return_value = 1
        quoted = collect_shipping_with_playwright("https://example.test/produto", "01310100", 5)
        self.assertEqual(quoted["shipping_cost"], 20.0)
        self.assertEqual(quoted["delivery_min_days"], 3)
        self.assertEqual(quoted["delivery_max_days"], 5)
        self.assertEqual(quoted["destination_zip"], "01310100")


if __name__ == "__main__":
    unittest.main()
