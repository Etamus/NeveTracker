import base64
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from crawlers.indexed_discovery import fetch_indexed_page, indexed_search_url
from crawlers.paged_discovery import _checked_html, fetch_search_page
from models.product import ScrapeCursor
from services.precise_extraction import _discover_store, _external_candidates, _discovery_attempted_since
from services.product_resolver import ProductIdentity
from services.scrape_pagination import discover_indexed_pages
from services.playwright_fallback import render_discovery_page


class IndexedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.identity = ProductIdentity(sku="W11199342")

    def test_browser_waits_for_delayed_public_cards(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():
                self.skipTest('Chromium nao instalado neste ambiente')
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(b'<html><body><script>setTimeout(() => {document.body.innerHTML += \'<a href="/refil-w11199342/p/abc/">Refil W11199342</a>\';}, 1200);</script></body></html>')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            rendered = render_discovery_page(f'http://127.0.0.1:{server.server_port}/', 5)
            self.assertIsNotNone(rendered)
            self.assertIsNotNone(BeautifulSoup(rendered[0], 'html.parser').select_one('a[href*="/p/"]'))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def fetched(self, html):
        return (BeautifulSoup('<html><body>' + html + '</body></html>', 'html.parser'),
                indexed_search_url('bing', 'magalu', self.identity.sku)), 'OK', None

    @patch('crawlers.indexed_discovery._fetch_indexed_html')
    def test_domain_links_keep_distinct_sellers_without_snippet_prices(self, fetch):
        url = 'https://www.magazineluiza.com.br/refil-w11199342/p/abc123/ep/pcbp/'
        html = ''.join(f'<li class="b_algo"><h2><a href="{url}?seller_id={seller}">Refil W11199342 original</a></h2>R$ 99,90</li>'
                       for seller in ('lojaa', 'lojab'))
        html += '<li class="b_algo"><h2><a href="https://evil.test/w11199342">Refil W11199342</a></h2></li>'
        html += '<li class="b_algo"><h2><a href="https://www.magazineluiza.com.br/busca/w11199342/">Busca</a></h2></li>'
        fetch.return_value = self.fetched(html)
        items, _, success = fetch_indexed_page('bing', 'magalu', 'https://www.bing.com/search')
        self.assertTrue(success)
        self.assertEqual({item['seller'] for item in items}, {'lojaa-Magalu', 'lojab-Magalu'})
        self.assertTrue(all('price' not in item for item in items))

    @patch('crawlers.indexed_discovery._fetch_indexed_html')
    def test_bing_redirect_is_decoded(self, fetch):
        url = 'https://www.magazineluiza.com.br/refil-w11199342/p/abc123/ep/pcbp/'
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
        fetch.return_value = self.fetched(f'<li class="b_algo"><h2><a href="/ck/a?u=a1{encoded}">Refil W11199342</a></h2></li>')
        items, _, success = fetch_indexed_page('bing', 'magalu', 'https://www.bing.com/search')
        self.assertTrue(success)
        self.assertEqual(items[0]['url'], url)

    @patch('crawlers.indexed_discovery._fetch_indexed_html')
    def test_irrelevant_http_200_is_not_successful_empty_coverage(self, fetch):
        fetch.return_value = self.fetched('<li class="b_algo"><h2><a href="https://github.com/">GitHub</a></h2></li>')
        diagnostics = {}
        self.assertFalse(fetch_indexed_page('bing', 'magalu', 'https://www.bing.com/search', diagnostics=diagnostics)[2])
        self.assertEqual(diagnostics['status'], 'SEARCH_MISMATCH')

    def test_magalu_denial_is_not_a_product_page(self):
        html = '<html><head><title>Magazine Luiza | Não é possível acessar a página</title></head><body>Erro 403</body></html>'
        self.assertEqual(_checked_html(html, 'https://www.magazineluiza.com.br/')[1], 'BLOCKED')

    def test_previous_job_does_not_disable_supplemental_discovery_on_resume(self):
        now = datetime.utcnow()
        cursors = [SimpleNamespace(store='magalu', last_checked_at=now - timedelta(hours=1)),
                   SimpleNamespace(store='amazon', last_checked_at=now + timedelta(seconds=1))]
        self.assertEqual(_discovery_attempted_since(cursors, now), {'amazon'})

    @patch('services.precise_extraction.scrape_comparison_offers')
    @patch('services.precise_extraction.search_bing_shopping')
    def test_comparison_augments_shopping_when_shopping_already_found_seller(self, shopping, comparison):
        def item(seller):
            return {'url': f'https://www.magazineluiza.com.br/refil-w11199342/p/abc123/ep/pcbp/?seller_id={seller}',
                    'source': 'Magazine Luiza', 'seller': seller}
        shopping.return_value = [item('a')]
        comparison.return_value = [item('a'), item('b')]
        self.assertEqual(len(_external_candidates(self.identity, 'magalu')), 2)
        comparison.assert_called_once()

    @patch('services.precise_extraction.discover_indexed_pages', return_value=[])
    @patch('services.precise_extraction.discover_html_pages')
    @patch('services.precise_extraction.enqueue_seller_offer_page')
    @patch('services.precise_extraction.upsert_product_source')
    @patch('services.precise_extraction._external_candidates')
    def test_existing_valid_offer_does_not_disable_external_discovery(self, external, upsert, enqueue, html, indexed):
        def item(seller):
            return {'name': 'Refil original W11199342', 'seller': seller,
                    'url': f'https://www.magazineluiza.com.br/refil-w11199342/p/abc123/ep/pcbp/?seller_id={seller}'}
        html.return_value = [item('a')]
        external.return_value = [item('b')]
        upsert.side_effect = lambda db, identity, store, item, match: SimpleNamespace(id={'a': 1, 'b': 2}[item['seller']])
        rows = _discover_store(Mock(), self.identity, {'key': 'magalu', 'direct_scrapers': ()},
                               SimpleNamespace(request_timeout_seconds=5, external_discovery_fallback=True), known_source_ids={1})
        self.assertEqual({row.id for row in rows}, {1, 2})
        external.assert_called_once()

    @patch('services.scrape_pagination.fetch_indexed_page')
    def test_many_results_resume_without_total_candidate_cap(self, fetch):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        def page(provider, store, url, timeout, **kwargs):
            kwargs['diagnostics'].update(status='OK')
            second = 'page=2' in url
            return [{'name': f'Refil W11199342 {provider}-{second}-{n}'} for n in range(120)], (
                None if second else url + '&page=2'), True
        fetch.side_effect = page
        try:
            first = discover_indexed_pages(db, self.identity, {'key': 'magalu'}, 5)
            second = discover_indexed_pages(db, self.identity, {'key': 'magalu'}, 5)
            self.assertEqual(len(first) + len(second), 480)
            self.assertTrue(all(row.completed and row.pages_completed == 2 for row in db.query(ScrapeCursor)))
            self.assertEqual(discover_indexed_pages(db, self.identity, {'key': 'magalu'}, 5), [])
        finally:
            db.close()
            engine.dispose()

    @patch('crawlers.paged_discovery._fetch_html')
    def test_mercado_livre_card_preserves_vendor_and_current_price(self, fetch):
        html = '<li class="ui-search-layout__item"><div class="poly-card"><a class="poly-component__title" href="https://www.mercadolivre.com.br/refil-w11199342/p/MLB12345">Refil W11199342</a><span class="poly-component__seller">Por MB PARTS</span><s>R$ 166,42</s><div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">79</span><span class="andes-money-amount__cents">49</span></span></div></div></li>'
        fetch.return_value = BeautifulSoup(html, 'html.parser'), 'https://lista.mercadolivre.com.br/w11199342'
        items, _, success = fetch_search_page('mercado_livre', 'https://lista.mercadolivre.com.br/w11199342')
        self.assertTrue(success)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['seller'], 'MB PARTS')
        self.assertEqual(items[0]['price'], 79.49)
