import unittest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from database import Base
from models.product import PriceHistory, Product
from services.quick_extraction import run_quick_extraction, first_page


class QuickExtractionTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine)
        self.db=Session(self.engine);self.addCleanup(self.engine.dispose);self.addCleanup(self.db.close)
        self.offer=dict(name='Sensor W10531315',sku='W10531315',price=42,source='Dufrio',store='Dufrio',url='https://www.dufrio.com.br/sensor-w10531315')

    @patch('services.quick_extraction.first_page')
    @patch('services.offer_enrichment.enrich_offer',side_effect=AssertionError('Quick must not enrich PDPs'))
    def test_quick_saves_valid_listings_without_enrichment_or_wrong_materials(self,enrich,search):
        search.return_value=[self.offer,{**self.offer,'name':'Sensor W11199342','sku':'W11199342','url':'https://www.dufrio.com.br/other'}]
        result=run_quick_extraction('W10531315',self.db,stores=['dufrio'])
        self.assertEqual(result['fresh_count'],1);self.assertEqual(self.db.query(Product).one().sku,'W10531315')
        enrich.assert_not_called();self.assertEqual(search.call_count,1)

    @patch('services.quick_extraction.first_page')
    def test_quick_keeps_out_of_stock_and_does_not_claim_other_cep_shipping(self,search):
        search.return_value=[{**self.offer,'price':None,'availability':'out_of_stock','shipping_cost':12,'destination_zip':'99999999'}]
        run_quick_extraction('W10531315',self.db,postal_code='01001000',stores=['dufrio'])
        row=self.db.query(PriceHistory).one()
        self.assertEqual(row.availability,'out_of_stock');self.assertIsNone(row.shipping_cost)

    @patch('services.quick_extraction.first_page')
    def test_quick_cached_offer_does_not_quote_another_destination(self, search):
        search.return_value = [{**self.offer, 'destination_zip':'30111-000', 'shipping_cost':15, 'delivery_max_days':4}]
        quoted = run_quick_extraction('W10531315', self.db, postal_code='30111000', stores=['dufrio'])
        self.assertEqual(quoted['results'][0]['shipping_cost'], 15)
        search.return_value = []
        cached = run_quick_extraction('W10531315', self.db, postal_code='01001000', stores=['dufrio'])
        self.assertEqual(cached['fresh_count'], 0)
        self.assertEqual(cached['results'][0]['current_price'], 42)
        self.assertIsNone(cached['results'][0].get('delivery_max_days'))
        self.assertIsNone(cached['results'][0].get('shipping_cost'))
        self.assertEqual(self.db.query(PriceHistory).one().shipping_cost, 15)

    @patch('services.quick_extraction.first_page')
    def test_quick_prefers_exact_cached_zip_but_a_fresh_price_wins(self, search):
        from routes.products import _search_sku_summary
        from schemas.product import SkuBatchRequest

        for cep, cost in [('01001000', 11), ('30111000', 15)]:
            search.return_value = [{**self.offer, 'destination_zip':cep, 'shipping_cost':cost}]
            run_quick_extraction('W10531315', self.db, postal_code=cep, stores=['dufrio'])
        search.return_value = []
        result = run_quick_extraction('W10531315', self.db, postal_code='01001000', stores=['dufrio'])
        self.assertEqual(result['results'][0]['shipping_cost'], 11)
        summary, status = _search_sku_summary('W10531315', SkuBatchRequest(
            skus=['W10531315'], postal_code='01001000', mode='quick', force_refresh=False), self.db)
        self.assertEqual(status, 'cached')
        self.assertEqual([item.get('shipping_cost') for item in summary['results']], [11])
        search.return_value = [{**self.offer, 'price':55}]
        current = run_quick_extraction('W10531315', self.db, postal_code='01001000', stores=['dufrio'])
        self.assertEqual(current['results'][0]['current_price'], 55)
        self.assertIsNone(current['results'][0].get('shipping_cost'))
        search.return_value = []
        rechecked = run_quick_extraction('W10531315', self.db, postal_code='01001000', stores=['dufrio'])
        self.assertEqual(rechecked['results'][0]['current_price'], 55)
        self.assertIsNone(rechecked['results'][0].get('shipping_cost'))

    @patch('services.quick_extraction.first_page')
    def test_unknown_marketplace_seller_is_not_reported_as_the_platform(self, search):
        search.return_value = [{**self.offer, 'source':'Amazon Brasil', 'store':'Amazon Brasil',
            'url':'https://www.amazon.com.br/dp/B000000001', '_seller_unknown':True}]
        result = run_quick_extraction('W10531315', self.db, stores=['amazon'], sellers={'amazon':['Amazon Brasil']})
        self.assertEqual(result['fresh_count'], 0)
        result = run_quick_extraction('W10531315', self.db, stores=['amazon'])
        self.assertEqual(result['results'][0]['source'], 'Amazon Brasil')
        self.assertEqual(result['results'][0]['store'], 'Não informado')

    @patch('crawlers.paged_discovery._fetch_html_result')
    def test_mg_parts_reads_live_card_format_and_stock_badge(self, fetch):
        from bs4 import BeautifulSoup
        from crawlers.paged_discovery import search_url

        url = search_url('mg_parts', 'W10531315')
        card = '''<product-card class="product-card">
            <a class="product-card-title" href="/products/sensor-w10531315{variant}">Sensor W10531315</a>
            {badge}<span class="price"><del><span class="amount">R$ 17,90</span></del>
            <ins><span class="amount discounted">R$ 16,11</span></ins></span></product-card>'''
        html = card.format(variant='-A', badge='') + card.format(variant='-B', badge='<span class="badge out-of-stock">Esgotado</span>')
        fetch.return_value = ((BeautifulSoup(html, 'html.parser'), url), 'OK', None)
        result = run_quick_extraction('W10531315', self.db, stores=['mg_parts'])
        self.assertEqual(result['fresh_count'], 2)
        rows = {item['url']: item for item in result['results']}
        self.assertEqual(rows['https://www.mgparts.com.br/products/sensor-w10531315-A']['current_price'], 16.11)
        self.assertEqual(rows['https://www.mgparts.com.br/products/sensor-w10531315-B']['availability'], 'out_of_stock')
        self.assertIsNone(rows['https://www.mgparts.com.br/products/sensor-w10531315-B']['current_price'])
        fetch.assert_called_once()

    @patch('services.marketplace_intervention.suspension',return_value=None)
    @patch('crawlers.paged_discovery.fetch_search_page',return_value=([],None,True))
    def test_first_page_disables_browser_and_uses_short_timeout(self,fetch,suspension):
        first_page({'key':'amazon'},'W10531315')
        self.assertFalse(fetch.call_args.kwargs['use_browser']);self.assertEqual(fetch.call_args.kwargs['timeout'],5)

    @patch('services.marketplace_intervention.suspension', return_value=None)
    @patch('services.vtex_public.catalog_offers', return_value=[])
    @patch('services.vtex_public.catalog', return_value=[])
    def test_quick_vtex_requests_only_the_first_page(self, catalog, offers, suspension):
        first_page({'key': 'brastemp'}, 'BRM46MB')
        catalog.assert_called_once_with('brastemp', 'BRM46MB', 5, first_page_only=True)

    @patch('services.marketplace_intervention.suspension', return_value=None)
    @patch('crawlers.paged_discovery._fetch_html_result')
    def test_quick_reads_card_price_and_ignores_crossed_out_value(self, fetch, suspension):
        from bs4 import BeautifulSoup
        from crawlers.paged_discovery import search_url

        url = search_url('friolar', 'W10531315')
        soup = BeautifulSoup('''
            <div class="listagem-item">
             <a class="nome-produto" href="/sensor-w10531315">Sensor W10531315</a>
             <div class="preco-produto"><del><strong>R$ 79,90</strong></del>
              <strong class="preco-promocional">R$ 42,90</strong></div>
            </div>
        ''', 'html.parser')
        fetch.return_value = ((soup, url), 'OK', None)
        result = run_quick_extraction('W10531315', self.db, stores=['friolar'])
        self.assertEqual(result['fresh_count'], 1)
        self.assertEqual(self.db.query(PriceHistory).one().price, 42.90)
        fetch.assert_called_once()

    def test_quick_keeps_completed_store_and_does_not_wait_for_slow_store(self):
        from threading import Event

        release = Event()
        blocked = Event()

        def first(store, _sku):
            if store['key'] == 'amazon':
                blocked.set()
                release.wait(5)
                return []
            self.assertTrue(blocked.wait(1))
            return [self.offer]

        try:
            with patch('services.quick_extraction.settings', return_value={'quick_timeout_seconds': 0.5}), \
                 patch('services.quick_extraction.first_page', side_effect=first):
                result = run_quick_extraction('W10531315', self.db, stores=['amazon', 'dufrio'])
            self.assertFalse(release.is_set())
            self.assertTrue(blocked.is_set())
            self.assertEqual(result['fresh_count'], 1)
            self.assertEqual([issue['store'] for issue in result['limitations']], ['amazon'])
        finally:
            release.set()

    def test_quick_queue_does_not_wait_for_manual_access_or_switch_to_precise(self):
        from routes.products import process_extraction_queue_item
        from sqlalchemy.orm import sessionmaker
        summary={'results':[],'stores':[],'comparison':[],'fresh_count':0,'processed_count':0}
        with patch('routes.products.SessionLocal',sessionmaker(bind=self.engine)), \
             patch('routes.products._search_sku_summary',return_value=(summary,'updated')) as search, \
             patch('services.marketplace_intervention.waiting_stores',return_value=['shopee']), \
             patch('services.multi_postal_extraction.collect_destinations') as quote:
            result=process_extraction_queue_item('W10531315',{'mode':'quick','force_refresh':True,
                'stores':['shopee'],'remaining_waiting_stores':['shopee'],'postal_codes':['01001000','30111000']})
        self.assertEqual(search.call_args.args[1].mode,'quick')
        self.assertEqual(result['_waiting_stores'],[]);quote.assert_not_called()
