import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from crawlers.mercado_livre import collect_mercado_livre, _item_id
from crawlers.mercado_livre_public import _product_url, embedded_states, parse_product_html, parse_search_html
from crawlers.paged_discovery import _checked_html
from services.mercado_livre_browser import page_status
from services.product_resolver import ProductIdentity
from services.product_sources import canonical_source_url, source_identifiers
from services.scraper_service import _offer_url_identity
from bs4 import BeautifulSoup


CATALOG = 'https://www.mercadolivre.com.br/refil-w11199342/p/MLB38102516'


def money(amount):
    whole, cents = f'{amount:.2f}'.split('.')
    return f'<span class="andes-money-amount"><span class="andes-money-amount__fraction">{whole}</span><span class="andes-money-amount__cents">{cents}</span></span>'


class MercadoLivrePublicTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)

    def source(self, url=CATALOG):
        return SimpleNamespace(store='mercado_livre', url=url, seller='Previous seller', brand=None,
                               external_product_id=None, asin=None, item_id=None)

    def test_catalog_id_is_not_an_official_api_item_id(self):
        source = self.source()
        source.item_id = 'MLB38102516'
        self.assertIsNone(_item_id(source))
        self.assertIsNone(source_identifiers({'url': CATALOG})['item_id'])

    def test_fragment_preserves_the_physical_seller_listing(self):
        url = _product_url(CATALOG + '#wid=MLB123456&tracking_id=ignore', CATALOG)
        self.assertEqual(source_identifiers({'url': url})['item_id'], 'MLB123456')
        self.assertIn('pdp_filters=item_id%3AMLB123456', canonical_source_url(url))
        self.assertIn('wid=MLB123456', _offer_url_identity(url))

    def test_two_sellers_on_same_catalog_are_not_merged(self):
        a = _product_url(CATALOG + '#wid=MLB111', CATALOG)
        b = _product_url(CATALOG + '#wid=MLB222', CATALOG)
        self.assertNotEqual(canonical_source_url(a), canonical_source_url(b))
        self.assertNotEqual(_offer_url_identity(a), _offer_url_identity(b))

    def test_universal_product_urls_are_supported(self):
        url = 'https://www.mercadolivre.com.br/refil-w11199342/up/MLBU1437389159#wid=MLB3034034808'
        self.assertIn('/up/MLBU1437389159', _product_url(url, CATALOG))
        self.assertEqual(source_identifiers({'url': _product_url(url, CATALOG)})['item_id'], 'MLB3034034808')

    def test_ad_tracking_link_uses_published_item_id_without_requesting_ad_redirect(self):
        link = 'https://click1.mercadolivre.com.br/mclics/clicks/external/MLB/count?a=opaque&pdp_filters=item_id%3AMLB123'
        self.assertEqual(_product_url(link, CATALOG), 'https://produto.mercadolivre.com.br/MLB-123')
        self.assertIsNone(_product_url('https://mercadolivre.com.br.evil.test/MLB-123', CATALOG))

    def test_html_200_login_is_not_an_empty_search(self):
        status, _ = page_status('<body>Para continuar, acesse sua conta</body>', 'https://www.mercadolivre.com.br/gz/account-verification', 200)
        self.assertEqual(status, 'LOGIN_REQUIRED')
        status, _ = page_status('<body>This page requires JavaScript to work.</body>', CATALOG, 200)
        self.assertEqual(status, 'JS_REQUIRED')

    def test_noscript_redirect_does_not_discard_rendered_marketplace_cards(self):
        html = '<html><head><noscript><meta http-equiv="refresh" content="0;URL=/gz/webdevice/config?noscript=true"></noscript></head><body><div class="poly-card">Refil W11199342</div></body></html>'
        self.assertEqual(_checked_html(html, 'https://lista.mercadolivre.com.br/w11199342')[1], 'OK')
        self.assertEqual(_checked_html(html.replace('<noscript>', '').replace('</noscript>', ''), CATALOG)[1], 'PARSER_ERROR')

    def test_embedded_javascript_is_parsed_without_executing_it(self):
        raw = "window.__PRELOADED_STATE__ = {results: [{polycard: {metadata: {url: '" + CATALOG + "', item_id:'MLB123'}, components: [{id:'title',title:{text:'Refil original W11199342'}},{id:'seller',seller:{text:'MB PARTS'}},{id:'price',price:{current_price:{value:88.64,currency:'BRL'}}}],}}],}; throw new Error('must not execute');"
        html = '<script>' + raw + '</script>'
        items, _ = parse_search_html(html, 'https://lista.mercadolivre.com.br/w11199342')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['seller'], 'MB PARTS')
        self.assertEqual(items[0]['price'], 88.64)
        self.assertEqual(items[0]['item_id'], 'MLB123')

    def test_search_does_not_cap_candidates_at_50_or_100(self):
        cards = ''.join(f'<div class="poly-card"><a class="poly-component__title" href="{CATALOG}#wid=MLB{i}">Refil W11199342</a><span class="poly-component__seller">Seller {i}</span><div class="poly-price__current">{money(99)}</div></div>' for i in range(120))
        items, _ = parse_search_html(cards, 'https://lista.mercadolivre.com.br/w11199342')
        self.assertEqual(len(items), 120)

    def test_no_seller_does_not_create_a_marketplace_wide_price(self):
        html = f'<div class="poly-card"><a class="poly-component__title" href="{CATALOG}">Refil W11199342</a><div class="poly-price__current">{money(88)}</div></div>'
        items, _ = parse_search_html(html, 'https://lista.mercadolivre.com.br/w11199342')
        self.assertIsNone(items[0]['price'])
        self.assertTrue(items[0]['_seller_unknown'])

    def test_react_next_button_without_href_produces_a_resumable_page(self):
        html = '<a data-andes-pagination-control="next" href="">Seguinte</a><a data-andes-pagination-item="true" data-andes-state="selected">2</a>'
        _, next_url = parse_search_html(html, 'https://lista.mercadolivre.com.br/w11199342_Desde_49_NoIndex_True')
        self.assertEqual(next_url, 'https://lista.mercadolivre.com.br/w11199342_Desde_97_NoIndex_True')
        _, next_url = parse_search_html(html.replace('control="next"', 'control="next" data-andes-state="disabled"'), CATALOG)
        self.assertIsNone(next_url)

    def test_primary_price_cannot_be_assigned_to_the_other_seller(self):
        html = f'''<html><body><h1 class="ui-pdp-title">Refil original W11199342</h1>
            <li class="ui-pdp-other-sellers-item" id="other-sellers-item-MLB222"><div class="ui-pdp-price__second-line">{money(155.11)}</div><a class="ui-pdp-seller__link">WG SOLUCOES</a><input name="item_id" value="MLB222"><button type="submit">Comprar</button></li>
            <div class="ui-pdp-price__second-line">{money(88.64)}</div><form class="ui-pdp-buybox"><a class="ui-pdp-seller-summary__link">MB PARTS</a><input name="item_id" value="MLB111"><button type="submit">Comprar</button></form></body></html>'''
        result = parse_product_html(html, CATALOG, self.source(), ProductIdentity(sku='W11199342'))
        self.assertEqual({item['seller']: item['price'] for item in result['items']}, {'MB PARTS': 88.64, 'WG SOLUCOES': 155.11})

    def test_new_offer_picker_preserves_each_seller_and_price(self):
        html = '<html><body><h1 class="ui-pdp-title">Refil original W11199342</h1>'
        for item_id, seller, price in [('MLB111', 'MB PARTS', 88.64), ('MLB222', 'MGPARTS', 93.09)]:
            html += f'<li class="ui-pdp-buy-box-offers__offer-list-item"><input class="andes-radio__input" value="{item_id}"><div class="ui-pdp-price__second-line">{money(price)}</div>Vendido por Loja oficial {seller}</li>'
        result = parse_product_html(html + '</body></html>', CATALOG, self.source(), ProductIdentity(sku='W11199342'))
        self.assertEqual({item['seller']: item['price'] for item in result['items']}, {'MB PARTS': 88.64, 'MGPARTS': 93.09})

    def test_hidden_variant_quantity_does_not_turn_two_refills_into_one_unit_quote(self):
        html = f'<html><body><h1 class="ui-pdp-title">Refil original W11199342</h1><div class="ui-pdp-outside_variations__title">Quantidade de refis: 2</div><div class="ui-pdp-price__second-line">{money(160)}</div><form class="ui-pdp-buybox"><a class="ui-pdp-seller-summary__link">MB PARTS</a><input name="item_id" value="MLB111"></form></body></html>'
        result = parse_product_html(html, CATALOG, self.source(), ProductIdentity(sku='W11199342'))
        self.assertEqual(result['status'], 'INVALID_MATCH')


    @patch('crawlers.mercado_livre.get_runtime_provider_settings')
    @patch('crawlers.mercado_livre.collect_public_product')
    @patch('crawlers.mercado_livre.create_resilient_session')
    def test_successful_scraping_does_not_require_or_call_the_api(self, api, public, settings):
        public.return_value = {'status': 'OK', 'items': [{'seller': 'MB PARTS', 'price': 88.64}]}
        self.assertEqual(collect_mercado_livre(self.source(), ProductIdentity(sku='W11199342'))['status'], 'OK')
        api.assert_not_called()
        settings.assert_not_called()

    @patch('crawlers.mercado_livre.get_runtime_provider_settings', return_value={'enabled': False, 'configured': False})
    @patch('crawlers.mercado_livre.collect_public_product', return_value={'status': 'BLOCKED', 'reason': 'HTTP 403'})
    @patch('crawlers.mercado_livre.create_resilient_session')
    def test_disabled_api_is_not_called_after_a_public_block(self, api, public, settings):
        self.assertEqual(collect_mercado_livre(self.source(), ProductIdentity(sku='W11199342'))['status'], 'BLOCKED')
        api.assert_not_called()

    @patch('services.precise_extraction.collect_product_source')
    @patch('services.precise_extraction.upsert_product_source')
    def test_sellers_verified_in_one_pdp_do_not_stay_pending_for_another_collection(self, upsert, collect):
        from services.precise_extraction import _collect_sources, _source_due
        from services.product_resolver import resolve_candidate
        from datetime import datetime
        original = self.source()
        original.id = 1
        new = SimpleNamespace(id=2, last_attempted_at=None, validation_status='discovered')
        item = {'name': 'Refil original W11199342', 'sku': 'W11199342', 'seller': 'MB PARTS',
                'store': 'MB PARTS', 'source': 'Mercado Livre', 'url': CATALOG, 'price': 88.64}
        identity = ProductIdentity(sku='W11199342')
        collect.return_value = {'status': 'OK', 'item': item, 'items': [item], 'match': resolve_candidate(identity, item)}
        upsert.return_value = new
        started = datetime.utcnow()
        control = SimpleNamespace(request_timeout_seconds=5, concurrent_requests_per_domain=1,
                                  playwright_product_fallback=False, playwright_shipping_fallback=False)
        items, _ = _collect_sources(Mock(), identity, [original], None, control)
        self.assertEqual(len(items), 1)
        self.assertFalse(_source_due(new, started, datetime.utcnow()))
