import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services.offer_enrichment import enrich_offer
from crawlers.base import parse_product_response
from crawlers.profiles import DEFAULT_PROFILE
from scrapy.http import HtmlResponse
from services.product_resolver import ProductIdentity


class PrecisionOptimizationTests(unittest.TestCase):
    @patch('services.precise_extraction.enrich_offers')
    @patch('services.precise_extraction.collect_shipping_with_playwright')
    @patch('services.precise_extraction.collect_sources_with_scrapy')
    def test_completed_http_attempt_keeps_browser_recovery_and_two_host_parallelism(self, crawl, shipping, enrich):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import Base
        from models.product import ProductSource
        from services.precise_extraction import _collect_sources
        from services.product_resolver import resolve_candidate
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        identity = ProductIdentity(sku='W10531315')
        active, peak = 0, 0
        lock = threading.Lock()
        def quote(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.04)
            with lock:
                active -= 1
            return {'shipping_cost': 12, 'delivery_max_days': 4, 'logistics_source': 'browser_test', 'destination_zip': '01001000'}
        shipping.side_effect = quote
        with sessionmaker(bind=engine)() as db:
            sources = [ProductSource(sku=identity.sku, store=store, seller=label, url=url, active=True) for store,label,url in [
                ('dufrio','Dufrio','https://www.dufrio.com.br/sensor-w10531315'),
                ('friolar','Friolar','https://www.friolarpecas.com.br/sensor-w10531315'),
                ('magalu','Magazine Luiza','https://www.magazineluiza.com.br/sensor-w10531315/p/123/ed/peca/')]]
            db.add_all(sources);db.commit()
            results = {}
            for source in sources:
                item = {'name':'Sensor W10531315','sku':identity.sku,'price':20,'url':source.url,'store':source.seller,
                        'seller':source.seller,'source':source.seller,'brand':'Brastemp','category':'Sensor','image_url':None,
                        '_http_logistics_attempted_zip':'01001000'}
                results[source.id] = {'status':'OK','items':[item], 'match':resolve_candidate(identity,item)}
            crawl.return_value = results
            items, stores = _collect_sources(db, identity, sources, '01001000', SimpleNamespace(
                concurrent_requests_per_domain=2, request_timeout_seconds=5,
                playwright_product_fallback=False, playwright_shipping_fallback=True))
            enrich.assert_not_called()
            self.assertEqual(shipping.call_count, 3)
            self.assertEqual(peak, 2)
            self.assertEqual(len(items), 3)
            self.assertTrue(all(item['shipping_cost']==12 for item in items))
        engine.dispose()
    def setUp(self):
        self.url = 'https://www.dufrio.com.br/sensor-w10531315'
        product = {'@type': 'Product', 'name': 'Sensor W10531315', 'offers': {'@type': 'Offer',
                   'seller': {'name': 'Loja A'}, 'price': 20, 'shippingDetails': {'shippingRate': {'value': 12},
                   'deliveryTime': {'transitTime': {'minValue': 2, 'maxValue': 4, 'unitCode': 'DAY'}}}}}
        self.html = '<html><body><script type="application/ld+json">'+json.dumps(product)+'</script></body></html>'

    @patch('services.offer_enrichment._session')
    def test_single_offer_reuses_html_without_a_second_get(self, session):
        source = SimpleNamespace(store='dufrio', seller='Loja A', brand=None, external_product_id=None, asin=None, item_id=None)
        result = parse_product_response(HtmlResponse(url=self.url, body=self.html.encode(), encoding='utf-8'),
                                        source, ProductIdentity(sku='W10531315'), DEFAULT_PROFILE)
        enriched = enrich_offer(result['item'], '01001000', force_refresh=True)
        session.return_value.get.assert_not_called()
        self.assertEqual((enriched['shipping_cost'], enriched['delivery_max_days']), (12, 4))
        self.assertNotIn('_page_context', enriched)

    @patch('services.offer_enrichment._session')
    def test_a_different_seller_url_cannot_reuse_other_offer_html(self, session):
        session.return_value.get.return_value = SimpleNamespace(status_code=200, text=self.html, url=self.url)
        item = {'name': 'Sensor W10531315', 'sku': 'W10531315', 'price': 20, 'store': 'Loja A', 'url': self.url,
                '_page_context': {'offer_url': self.url+'?seller=other', 'page_url': self.url, 'html': self.html}}
        enrich_offer(item, '01001000', force_refresh=True)
        session.return_value.get.assert_called_once()

    def test_multiple_sellers_do_not_share_page_context(self):
        product = {'@type': 'Product', 'name': 'Sensor W10531315', 'offers': [
            {'@type': 'Offer', 'price': 20, 'seller': {'name': 'A'}},
            {'@type': 'Offer', 'price': 30, 'seller': {'name': 'B'}}]}
        response = HtmlResponse(url=self.url, body=('<script type="application/ld+json">'+json.dumps(product)+'</script>').encode(), encoding='utf-8')
        source = SimpleNamespace(store='dufrio', seller=None, brand=None, external_product_id=None, asin=None, item_id=None)
        result = parse_product_response(response, source, ProductIdentity(sku='W10531315'), DEFAULT_PROFILE)
        self.assertEqual(len(result['items']), 2)
        self.assertTrue(all('_page_context' not in item for item in result['items']))
