import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from bs4 import BeautifulSoup

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crawlers.paged_discovery import STORE_HOSTS, _fetch_html, fetch_amazon_offer_page, fetch_search_page, fetch_seller_offer_page, search_url
from database import Base
from models.product import KnownSku, PriceHistory, ProductSource, ScrapeCursor
from services.precise_extraction import _discover_store, _source_batch, run_precise_extraction
from services.product_resolver import ProductIdentity
from services.product_sources import active_sources, mark_source_result
from services.scrape_pagination import coverage_by_store, discover_html_pages, enqueue_seller_offer_page
from services.store_registry import TARGET_STORES


class PagedScrapingTests(unittest.TestCase):
    @patch('crawlers.paged_discovery._fetch_html')
    def test_variant_sku_hidden_from_search_card_requires_pdp_verification(self, fetch_html):
        url = search_url('mg_parts', 'W11647660')
        html = '<product-card><a class="product-card-title" href="/products/compressor-emr80hlr-127v">Compressor Embraco EMR80HLR 127V</a></product-card>'
        fetch_html.return_value = BeautifulSoup(html, 'html.parser'), url
        items, _, success = fetch_search_page('mg_parts', url)
        self.assertTrue(success)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['_requires_pdp_verification'])
        self.assertNotIn('price', items[0])
    def test_file_database_discovers_independent_stores_with_separate_sessions(self):
        import tempfile
        from pathlib import Path
        from threading import Barrier, Lock, get_ident
        gate, mutex = Barrier(3), Lock()
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine("sqlite:///" + (Path(directory) / "parallel.db").as_posix(),
                                   connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            factory = sessionmaker(bind=engine)
            def discover(session, identity, store, control, **kwargs):
                with mutex:
                    seen.append((get_ident(), id(session), store["key"]))
                gate.wait(timeout=10)
                session.add(ScrapeCursor(sku=identity.sku, store=store["key"], kind="search",
                                        key=identity.sku, next_url=search_url(store["key"], identity.sku),
                                        completed=True, last_checked_at=datetime.utcnow()))
                session.commit()
                return []
            try:
                with factory() as db, patch("services.precise_extraction._discover_store", side_effect=discover):
                    db.add(KnownSku(sku="W10531315"))
                    db.commit()
                    summary = run_precise_extraction("W10531315", db, collection_after=datetime.utcnow())
                    self.assertEqual(len({row[0] for row in seen}), 3)
                    self.assertEqual(len({row[1] for row in seen}), 3)
                    self.assertNotIn(id(db), {row[1] for row in seen})
                    self.assertEqual(db.query(ScrapeCursor).count(), 3)
                    self.assertTrue(summary["more_work"])
            finally:
                engine.dispose()

    def test_failed_http_page_is_not_fetched_again_during_same_job(self):
        from services.scrape_pagination import _advance
        from unittest.mock import Mock
        now = datetime.utcnow()
        url = search_url("amazon", self.identity.sku)
        cursor = ScrapeCursor(sku=self.identity.sku, store="amazon", kind="search", key=self.identity.sku,
                             initial_url=url, next_url=url, completed=False, updated_at=now,
                             last_status="BLOCKED", last_attempted_at=now - timedelta(hours=1),
                             browser_attempted_at=now - timedelta(hours=1), browser_attempted_url=url)
        self.db.add(cursor)
        self.db.commit()
        fetch = Mock()
        self.assertEqual(_advance(cursor, url, fetch, {"collection_after": now - timedelta(hours=2)}), [])
        fetch.assert_not_called()
        self.assertFalse(cursor.completed)

    def test_pending_browser_reuses_http_failure_instead_of_repeating_request(self):
        from services.scrape_pagination import _advance
        from unittest.mock import Mock
        now = datetime.utcnow()
        url = search_url("amazon", self.identity.sku)
        cursor = ScrapeCursor(sku=self.identity.sku, store="amazon", kind="search", key=self.identity.sku,
                             initial_url=url, next_url=url, completed=False, updated_at=now,
                             last_status="TIMEOUT", last_attempted_at=now)
        self.db.add(cursor)
        self.db.commit()
        fetch = Mock(return_value=([{ "name": "Sensor W10531315" }], None, True))
        _advance(cursor, url, fetch, {"collection_after": now - timedelta(minutes=1)})
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.kwargs["use_browser"])
        self.assertTrue(cursor.completed)

    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        self.identity = ProductIdentity(sku="W10531315")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_every_store_has_a_search_page(self):
        self.assertEqual(set(STORE_HOSTS), {store["key"] for store in TARGET_STORES})
        for store in TARGET_STORES:
            self.assertIn("w10531315", search_url(store["key"], "W10531315").lower())

    def test_access_released_during_other_stores_does_not_lose_skipped_marketplace_work(self):
        from services.marketplace_intervention import require_access,_write,_path
        require_access('shopee',self.identity.sku,'Login')
        visited=[]
        def discover(db,identity,store,*args,**kwargs):
            visited.append(store['key'])
            _write(_path('shopee','access-request'),{'status':'verified'})
            return []
        with patch('services.precise_extraction._discover_store',side_effect=discover),patch('services.precise_extraction._collect_sources',return_value=([],set())):
            result=run_precise_extraction(self.identity.sku,self.db,stores=['shopee','brastemp'],collection_after=datetime.utcnow())
        self.assertEqual(visited,['brastemp'])
        self.assertEqual(result['waiting_marketplaces'],['shopee'])

    @patch('services.scrape_pagination.fetch_seller_offer_page', return_value=([], None, True))
    @patch('services.scrape_pagination.fetch_search_page')
    def test_new_search_route_retries_a_cursor_blocked_by_the_old_transport(self, fetch_page, seller_page):
        from services.scrape_pagination import _cursor
        old_url = search_url('mercado_livre', self.identity.sku) + '_OrderId_PRICE_ASC'
        cursor = _cursor(self.db, self.identity.sku, 'mercado_livre', 'search', self.identity.sku, old_url)
        cursor.last_status = 'BLOCKED'
        cursor.last_attempted_at = datetime.utcnow()
        cursor.browser_attempted_at = datetime.utcnow()
        cursor.browser_attempted_url = old_url
        self.db.commit()
        def page(_store, url, timeout, **kwargs):
            if kwargs.get('use_browser'):
                kwargs['diagnostics'].update(status='OK')
                return [{'name': 'Sensor W10531315', 'seller': 'New seller',
                         'url': 'https://produto.mercadolivre.com.br/MLB-123'}], None, True
            kwargs['diagnostics'].update(status='BLOCKED', reason='HTTP 403')
            return [], None, False
        fetch_page.side_effect = page
        found = discover_html_pages(self.db, self.identity, {'key': 'mercado_livre'}, 5)
        self.assertEqual(found[0]['seller'], 'New seller')
        self.assertEqual(cursor.last_status, 'OK_BROWSER')
        self.assertEqual(cursor.browser_attempted_url, search_url('mercado_livre', self.identity.sku))

    @patch("services.scrape_pagination.fetch_search_page", return_value=([], None, True))
    def test_discovery_always_searches_sku_even_with_other_identifiers(self, fetch_page):
        identity = ProductIdentity(
            sku="W10531315", ean="7890000000000", mpn="MPN12345", model="MOD12345",
        )
        discover_html_pages(
            self.db, identity, {"key": "dufrio"}, 5,
        )
        search_keys = {
            row.key for row in self.db.query(ScrapeCursor).filter_by(store="dufrio").all()
        }
        self.assertIn("W10531315", search_keys)


    @patch("crawlers.paged_discovery._fetch_html")
    def test_mota_decodes_product_cards_from_template(self, fetch_html):
        url = search_url("refrigeracao_mota", "W10531315")
        template = (
            '<li class="product"><a class="product-loop-title" '
            'href="/loja/sensor-w10531315/"></a>'
            '<h2 class="woocommerce-loop-product__title">Sensor W10531315</h2></li>'
        )
        html = f'<script type="text/template">{json.dumps(template)}</script>'
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, _next, success = fetch_search_page("refrigeracao_mota", url)
        self.assertTrue(success)
        self.assertEqual(items[0]["url"], "https://www.refrigeracaomota.com.br/loja/sensor-w10531315/")

    @patch("crawlers.paged_discovery._fetch_html")
    def test_store_search_ignores_unrelated_product_cards(self, fetch_html):
        url = search_url("mg_parts", "W10531315")
        html = (
            '<product-card><a class="product-card-title" href="/products/w10531315">'
            'Sensor W10531315</a></product-card>'
            '<product-card><a class="product-card-title" href="/products/outro">'
            'Produto diferente</a></product-card>'
        )
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, _next, success = fetch_search_page("mg_parts", url)
        self.assertTrue(success)
        self.assertEqual(len(items), 1)

    @patch("crawlers.paged_discovery._fetch_html")
    def test_amazon_search_reads_all_cards_and_next_page(self, fetch_html):
        cards = "".join(
            f'<div data-component-type="s-search-result"><h2><a href="/dp/B{i:09d}">'
            f'W10531315 item {i}</a></h2></div>'
            for i in range(55)
        )
        url = "https://www.amazon.com.br/s?k=W10531315"
        html = f'{cards}<a class="s-pagination-next" href="/s?k=W10531315&page=2">Next</a>'
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, next_url, success = fetch_search_page("amazon", url)
        self.assertTrue(success)
        self.assertEqual(len(items), 55)
        self.assertEqual(next_url, "https://www.amazon.com.br/s?k=W10531315&page=2")

    @patch("crawlers.paged_discovery._fetch_html")
    def test_amazon_seller_page_keeps_sellers_and_follows_next(self, fetch_html):
        url = "https://www.amazon.com.br/gp/offer-listing/B000000001"
        html = """
        <div class="olpOffer"><div class="olpSellerName">
          <a href="/sp?seller=SELLER1">Seller One</a>
        </div></div>
        <div class="olpOffer"><div class="olpSellerName">
          <a href="/sp?seller=SELLER2">Seller Two</a>
        </div></div>
        <a class="s-pagination-next" href="/gp/offer-listing/B000000001?page=2">Next</a>
        """
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, next_url, success = fetch_amazon_offer_page("B000000001", url, self.identity)
        self.assertTrue(success)
        self.assertEqual([item["seller"] for item in items], ["Seller One", "Seller Two"])
        self.assertEqual([item["url"].split("?m=")[-1] for item in items], ["SELLER1", "SELLER2"])
        self.assertIn("page=2", next_url)

    @patch("crawlers.paged_discovery._fetch_html")
    def test_retail_seller_page_reads_individual_prices_and_next_page(self, fetch_html):
        url = "https://www.magazineluiza.com.br/sensor/p/abc123/"
        html = """
        <html><body><h1>Sensor W10531315</h1>
        <script type="application/ld+json">
        {"@type":"Product","name":"Sensor W10531315","offers":{"@type":"AggregateOffer","offers":[
          {"@type":"Offer","seller":{"name":"Loja A"},"price":"18.47","url":"/sensor/p/abc123/?seller_id=a"},
          {"@type":"Offer","seller":{"name":"Loja B"},"price":"21.90","url":"/sensor/p/abc123/?seller_id=b"}
        ]}}
        </script><a rel="next" href="/sensor/p/abc123/?page=2">Proxima</a>
        </body></html>
        """
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, next_url, success = fetch_seller_offer_page("magalu", url, self.identity, 5)
        self.assertTrue(success)
        self.assertEqual([(item["seller"], item["price"]) for item in items], [
            ("Loja A", 18.47), ("Loja B", 21.9),
        ])
        self.assertIn("page=2", next_url)

    def test_retail_seller_cursor_groups_urls_of_same_product(self):
        store = "magalu"
        one = {"name": "Sensor W10531315", "url": "https://www.magazineluiza.com.br/sensor/p/abc123/?seller_id=a"}
        two = {"name": "Sensor W10531315", "url": "https://www.magazineluiza.com.br/sensor/p/abc123/?seller_id=b"}
        first = enqueue_seller_offer_page(self.db, self.identity, store, one)
        second = enqueue_seller_offer_page(self.db, self.identity, store, two)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ScrapeCursor).count(), 1)
        self.assertEqual(first.initial_url, "https://www.magazineluiza.com.br/sensor/p/abc123")

    @patch("services.scrape_pagination.BROWSER_ATTEMPTS_PER_RUN", 0)
    @patch("services.scrape_pagination.fetch_seller_offer_page", return_value=([], None, False))
    @patch("services.scrape_pagination.fetch_search_page", return_value=([], None, True))
    def test_failed_seller_pages_do_not_starve_remaining_pages(self, _search, _offers):
        for index in range(6):
            enqueue_seller_offer_page(self.db, self.identity, "magalu", {
                "name": "Sensor W10531315",
                "url": f"https://www.magazineluiza.com.br/sensor/p/item{index}/",
            })
        store = {"key": "magalu"}
        discover_html_pages(self.db, self.identity, store, 5)
        self.assertEqual(_offers.call_count, 4)
        discover_html_pages(self.db, self.identity, store, 5)
        self.assertEqual(
            self.db.query(ScrapeCursor).filter(
                ScrapeCursor.kind == "seller_offers",
                ScrapeCursor.last_attempted_at.isnot(None),
            ).count(),
            6,
        )

    @patch("crawlers.paged_discovery._fetch_html")
    def test_magalu_search_follows_next_page(self, fetch_html):
        url = "https://www.magazineluiza.com.br/busca/W10531315/"
        html = """
        <a href="/peca/p/abc123/">Peça W10531315</a>
        <a rel="next" href="/busca/W10531315/?page=2">Próxima</a>
        """
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, next_url, success = fetch_search_page("magalu", url)
        self.assertTrue(success)
        self.assertEqual(len(items), 1)
        self.assertIn("page=2", next_url)

    @patch("crawlers.paged_discovery.render_discovery_page")
    def test_browser_search_uses_same_parser_and_rejects_other_domain(self, render):
        url = "https://www.magazineluiza.com.br/busca/W10531315/"
        render.return_value = (
            '<html><body><a href="/sensor/p/abc123/">Sensor W10531315</a></body></html>',
            url,
        )
        diagnostics = {}
        items, _next, success = fetch_search_page(
            "magalu", url, 5, diagnostics=diagnostics, use_browser=True,
        )
        self.assertTrue(success)
        self.assertEqual(len(items), 1)
        render.return_value = ("<html><body>Sensor W10531315</body></html>", "https://outro.example/")
        items, _next, success = fetch_search_page(
            "magalu", url, 5, diagnostics=diagnostics, use_browser=True,
        )
        self.assertFalse(success)
        self.assertEqual(items, [])
        self.assertEqual(diagnostics["status"], "BLOCKED")

    @patch("crawlers.paged_discovery._fetch_html")
    def test_mercado_livre_search_follows_next_page(self, fetch_html):
        url = "https://lista.mercadolivre.com.br/W10531315_OrderId_PRICE_ASC"
        html = """
        <li class="ui-search-layout__item">
          <a class="poly-component__title" href="https://produto.mercadolivre.com.br/MLB-123456">
            Peça W10531315
          </a>
        </li>
        <li class="andes-pagination__button--next">
          <a href="/W10531315_Desde_51">Seguinte</a>
        </li>
        """
        fetch_html.return_value = BeautifulSoup(html, "html.parser"), url
        items, next_url, success = fetch_search_page("mercado_livre", url)
        self.assertTrue(success)
        self.assertEqual(len(items), 1)
        self.assertIn("Desde_51", next_url)

    @patch("crawlers.paged_discovery._fetch_html")
    def test_empty_rendered_shell_is_retried(self, fetch_html):
        url = "https://www.amazon.com.br/s?k=W10531315"
        fetch_html.return_value = BeautifulSoup("<html><body><div id='app'></div></body></html>", "html.parser"), url
        _items, _next_url, success = fetch_search_page("amazon", url)
        self.assertFalse(success)

    @patch("crawlers.paged_discovery._fetch_html")
    def test_explicit_no_results_finishes_search(self, fetch_html):
        url = "https://www.magazineluiza.com.br/busca/W10531315/"
        fetch_html.return_value = BeautifulSoup("<html><body>Nenhum produto encontrado</body></html>", "html.parser"), url
        items, next_url, success = fetch_search_page("magalu", url)
        self.assertTrue(success)
        self.assertEqual(items, [])
        self.assertIsNone(next_url)

    @patch("services.scrape_pagination.fetch_seller_offer_page", return_value=([], None, True))
    @patch("services.scrape_pagination.fetch_search_page")
    def test_search_cursor_resumes_after_reopening_database_session(self, fetch_page, _seller_page):
        fetch_page.side_effect = [
            ([{"name": "W10531315", "url": "https://www.magazineluiza.com.br/p/one"}],
             "https://www.magazineluiza.com.br/busca/W10531315/?page=2", True),
            ([{"name": "W10531315", "url": "https://www.magazineluiza.com.br/p/two"}],
             None, True),
        ]
        store = {"key": "magalu"}
        self.assertEqual(len(discover_html_pages(self.db, self.identity, store, 5)), 1)
        self.db.commit()
        self.db.close()
        self.db = self.session_factory()
        self.assertEqual(len(discover_html_pages(self.db, self.identity, store, 5)), 1)
        self.db.commit()
        self.assertEqual(discover_html_pages(self.db, self.identity, store, 5), [])
        self.assertEqual(fetch_page.call_count, 2)
        self.assertTrue(self.db.query(ScrapeCursor).filter_by(kind="search").one().completed)

    @patch("services.scrape_pagination.BROWSER_ATTEMPTS_PER_RUN", 0)
    @patch("services.scrape_pagination.fetch_search_page")
    def test_failed_page_keeps_cursor_for_retry(self, fetch_page):
        fetch_page.side_effect = [([], None, False), ([{
            "name": "W10531315", "url": "https://www.magazineluiza.com.br/p/one",
        }], None, True)]
        store = {"key": "magalu"}
        self.assertEqual(discover_html_pages(self.db, self.identity, store, 5), [])
        self.db.commit()
        cursor = self.db.query(ScrapeCursor).one()
        self.assertFalse(cursor.completed)
        first_url = cursor.next_url
        self.assertEqual(len(discover_html_pages(self.db, self.identity, store, 5)), 1)
        self.assertEqual(fetch_page.call_args_list[0].args[1], first_url)
        self.assertEqual(fetch_page.call_args_list[1].args[1], first_url)

    @patch("services.scrape_pagination.fetch_search_page")
    def test_pagination_cycle_finishes_instead_of_repeating_pages(self, fetch_page):
        base = search_url("dufrio", "W10531315")
        fetch_page.side_effect = [
            ([], f"{base}&p=2", True),
            ([], f"{base}&p=3", True),
            ([], f"{base}&p=2", True),
        ]
        store = {"key": "dufrio"}
        for _ in range(3):
            discover_html_pages(self.db, self.identity, store, 5)
        cursor = self.db.query(ScrapeCursor).one()
        self.assertTrue(cursor.completed)
        self.assertEqual(cursor.pages_completed, 3)
        discover_html_pages(self.db, self.identity, store, 5)
        self.assertEqual(fetch_page.call_count, 3)

    @patch("services.scrape_pagination.fetch_search_page")
    def test_browser_recovers_failed_search_and_records_progress(self, fetch_page):
        def side_effect(_store, _url, _timeout, **kwargs):
            if kwargs.get("use_browser"):
                return ([{"name": "Sensor W10531315", "url": "https://www.dufrio.com.br/sensor"}], None, True)
            kwargs["diagnostics"].update(status="BLOCKED", reason="HTTP 403")
            return [], None, False

        fetch_page.side_effect = side_effect
        items = discover_html_pages(self.db, self.identity, {"key": "dufrio"}, 5)
        cursor = self.db.query(ScrapeCursor).one()
        self.assertEqual(items[0]["discovery_method"], "browser_pages")
        self.assertEqual(cursor.last_status, "OK_BROWSER")
        self.assertEqual(cursor.pages_completed, 1)
        self.assertEqual(cursor.candidates_found, 1)
        self.assertTrue(cursor.completed)

    @patch("services.scrape_pagination.fetch_search_page")
    def test_browser_budget_retries_remaining_stores_in_next_batch(self, fetch_page):
        def side_effect(_store, _url, _timeout, **kwargs):
            kwargs["diagnostics"].update(status="BLOCKED", reason="HTTP 403")
            return [], None, False

        fetch_page.side_effect = side_effect
        stores = [{"key": key, "label": key} for key in (
            "mercado_livre", "shopee", "leroy_merlin", "magalu",
        )]
        progress = {}
        for store in stores:
            discover_html_pages(self.db, self.identity, store, 5, progress)
        self.assertEqual(progress["browser_attempts"], 3)
        coverage = coverage_by_store(self.db, self.identity.sku, stores)
        self.assertEqual(sum(row["browser_pending"] for row in coverage), 1)
        self.assertEqual(sum(row["failed_pages"] for row in coverage), 4)
        self.assertTrue(all(row["failures"][0]["reason"] for row in coverage))

        discover_html_pages(self.db, self.identity, stores[-1], 5, {})
        coverage = coverage_by_store(self.db, self.identity.sku, stores)
        self.assertEqual(sum(row["browser_pending"] for row in coverage), 0)
        self.assertEqual(sum(call.kwargs.get("use_browser", False) for call in fetch_page.call_args_list), 4)

    @patch("services.scrape_pagination.fetch_search_page")
    def test_failed_browser_page_uses_retry_cooldown(self, fetch_page):
        fetch_page.side_effect = lambda _store, _url, _timeout, **kwargs: (
            kwargs["diagnostics"].update(status="BLOCKED", reason="HTTP 403") or ([], None, False)
        )
        store = {"key": "magalu"}
        discover_html_pages(self.db, self.identity, store, 5)
        self.assertEqual(fetch_page.call_count, 2)
        discover_html_pages(self.db, self.identity, store, 5)
        self.assertEqual(fetch_page.call_count, 2)

    @patch("services.scrape_pagination.fetch_seller_offer_page", return_value=([], None, True))
    @patch("services.scrape_pagination.fetch_search_page")
    def test_completed_search_restarts_after_refresh_interval(self, fetch_page, _seller_page):
        fetch_page.return_value = ([{
            "name": "W10531315", "url": "https://www.magazineluiza.com.br/p/one",
        }], None, True)
        store = {"key": "magalu"}
        discover_html_pages(self.db, self.identity, store, 5)
        cursor = self.db.query(ScrapeCursor).filter_by(kind="search").one()
        cursor.updated_at = datetime.utcnow() - timedelta(hours=25)
        self.db.commit()
        self.assertEqual(len(discover_html_pages(self.db, self.identity, store, 5)), 1)
        self.assertEqual(fetch_page.call_count, 2)

    @patch("services.scrape_pagination.fetch_amazon_offer_page")
    @patch("services.scrape_pagination.fetch_search_page")
    def test_amazon_seller_pages_continue_across_runs(self, fetch_search, fetch_offers):
        fetch_search.return_value = ([
            {"name": "W10531315", "url": f"https://www.amazon.com.br/dp/B{i:09d}"}
            for i in range(7)
        ], None, True)
        fetch_offers.side_effect = lambda asin, _url, _identity, _timeout, **_kwargs: (
            [{"name": "W10531315", "asin": asin, "seller": asin, "url": f"https://www.amazon.com.br/dp/{asin}"}],
            None, True,
        )
        store = {"key": "amazon"}
        first = discover_html_pages(self.db, self.identity, store, 5)
        self.db.commit()
        second = discover_html_pages(self.db, self.identity, store, 5)
        self.db.commit()
        self.assertEqual(len(first), 12)
        self.assertEqual(len(second), 2)
        self.assertEqual(fetch_offers.call_count, 7)

    @patch("services.scrape_pagination.fetch_search_page")
    def test_unrelated_amazon_cards_do_not_create_seller_page_cursors(self, fetch_search):
        fetch_search.return_value = ([
            {"name": "Produto sem o codigo solicitado", "url": "https://www.amazon.com.br/dp/B000000001"},
        ], None, True)
        discover_html_pages(self.db, self.identity, {"key": "amazon"}, 5)
        self.assertEqual(
            self.db.query(ScrapeCursor).filter_by(kind="seller_offers").count(), 0,
        )

    def test_source_batches_rotate_after_failure_and_restart(self):
        self.db.add_all([
            ProductSource(
                sku="W10531315", store="amazon", seller=f"Seller {i}",
                url=f"https://www.amazon.com.br/dp/B{i:09d}",
                match_confidence=0.9, match_method="model_exact",
                active=True, validation_status="discovered",
            )
            for i in range(150)
        ])
        self.db.commit()
        first = _source_batch(active_sources(self.db, "W10531315", {"amazon"}))
        self.assertEqual(len(first), 12)
        first_ids = {source.id for source in first}
        for source in first:
            mark_source_result(source, "TIMEOUT", "timeout")
        self.assertIsNotNone(first[0].last_attempted_at)
        self.assertIsNone(first[0].last_validated_at)
        self.db.commit()
        self.db.close()
        self.db = self.session_factory()
        second = _source_batch(active_sources(self.db, "W10531315", {"amazon"}))
        self.assertEqual(len(second), 12)
        self.assertFalse(first_ids & {source.id for source in second})

    @patch("services.precise_extraction._direct_candidates", return_value=[])
    @patch("services.precise_extraction.discover_html_pages")
    def test_html_discovery_persists_more_than_100_sellers(self, discover_pages, _direct):
        discover_pages.return_value = [
            {
                "name": "W10531315",
                "seller": f"Seller {i}",
                "url": f"https://www.amazon.com.br/dp/B{i:09d}",
            }
            for i in range(150)
        ]
        control = type("Control", (), {
            "request_timeout_seconds": 5,
            "external_discovery_fallback": False,
        })()
        with patch("builtins.print"):
            saved = _discover_store(self.db, self.identity, {"key": "amazon"}, control)
        self.assertEqual(len(saved), 150)
        self.assertEqual(self.db.query(ProductSource).count(), 150)

    @patch("crawlers.paged_discovery.create_resilient_session")
    def test_verification_page_does_not_complete_cursor(self, create_session):
        session = create_session.return_value
        response = session.get.return_value
        response.status_code = 200
        response.url = "https://www.amazon.com.br/s?k=W10531315"
        response.text = (
            '<html><head><meta http-equiv="refresh" content="5; URL=/s?k=W10531315"></head>'
            '<body></body></html>'
        )
        self.assertIsNone(_fetch_html(response.url, 5))

    @patch("services.precise_extraction._discover_store", return_value=[])
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    @patch("services.precise_extraction.get_control")
    def test_precise_collection_finishes_all_sources_across_batches(
        self, get_control, collect_scrapy, _discover,
    ):
        get_control.return_value = type("Control", (), {
            "concurrent_requests_per_domain": 2,
            "request_timeout_seconds": 5,
            "playwright_shipping_fallback": False,
        })()
        collect_scrapy.side_effect = lambda sources, *_args: {
            source.id: {"status": "TIMEOUT", "reason": "timeout"}
            for source in sources
        }
        self.db.add(KnownSku(sku="W10531315"))
        self.db.add_all([
            ProductSource(
                sku="W10531315", store="dufrio", seller=f"Seller {i}",
                url=f"https://www.dufrio.com.br/p/{i}",
                match_confidence=0.9, match_method="model_exact",
                active=True, validation_status="discovered",
            )
            for i in range(25)
        ])
        self.db.commit()
        started = datetime.utcnow() - timedelta(seconds=1)
        summaries = [
            run_precise_extraction(
                "W10531315", self.db, scope="sites",
                collection_after=started, resume=index > 0,
            )
            for index in range(3)
        ]
        self.assertEqual([summary["pending_sources"] for summary in summaries], [13, 1, 0])
        self.assertEqual([summary["more_work"] for summary in summaries], [True, True, True])
        self.assertEqual(summaries[-1]["pending_sources"], 0)
        self.assertEqual(summaries[-1]["pending_stores"], 3)

    @patch("services.precise_extraction._discover_store", return_value=[])
    @patch("services.precise_extraction.collect_sources_with_scrapy")
    @patch("services.precise_extraction.get_control")
    def test_listing_price_without_shipping_does_not_claim_quoted_zip(
        self, get_control, collect_scrapy, _discover,
    ):
        get_control.return_value = type("Control", (), {
            "concurrent_requests_per_domain": 2,
            "request_timeout_seconds": 5,
            "playwright_shipping_fallback": False,
        })()
        source = ProductSource(
            sku="W10531315", store="dufrio", seller="Dufrio",
            url="https://www.dufrio.com.br/sensor-w10531315.html",
            listing_name="Sensor W10531315", listing_price=18.47,
            listing_method="html_pages", listing_seen_at=datetime.utcnow(),
            match_confidence=0.9, match_method="model_exact", active=True,
        )
        self.db.add(source)
        self.db.commit()
        collect_scrapy.return_value = {source.id: {"status": "BLOCKED", "reason": "HTTP 403"}}

        run_precise_extraction("W10531315", self.db, postal_code="01001000", scope="sites")

        price = self.db.query(PriceHistory).one()
        self.assertEqual(price.price, 18.47)
        self.assertIsNone(price.shipping_cost)
        self.assertIsNone(price.delivery_max_days)
        self.assertIsNone(price.destination_zip)
