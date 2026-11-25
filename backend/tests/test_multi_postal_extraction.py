import json
import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch,MagicMock
from io import BytesIO
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
from models.product import Product,PriceHistory,LeadTimeQuote,LeadTimeDestination,ExtractionJob,ExtractionJobItem
from schemas.product import SkuBatchRequest
from services.scraper_service import _save_result
from services.multi_postal_extraction import collect_destinations
from services.complete_export import complete_workbook
from services.product_sources import canonical_source_url
from services.vtex_public import catalog_offers,simulate
from services.magalu_public_shipping import shipping_request
from services.product_resolver import ProductIdentity,resolve_candidate


class MultiPostalTests(unittest.TestCase):
    @patch('services.vtex_public.quote_offer')
    @patch('services.delivery_destinations.requests.get')
    def test_definitively_missing_postal_code_is_cached_without_quoting_store(self, lookup, quote):
        from services.delivery_destinations import _postal_cache
        cep = '00000000'
        _postal_cache.pop(cep, None)
        self.addCleanup(_postal_cache.pop, cep, None)
        lookup.return_value.json.return_value = {'erro': True}
        for _ in range(2):
            values = collect_destinations(self.db, 'BRM46MB', [self.item], [cep])
            self.assertEqual(values[0]['destination_zip'], cep)
            self.assertIn(cep, values[0]['_logistics_error'])
            self.assertNotIn('shipping_cost', values[0])
        self.assertEqual(lookup.call_count, 1)
        quote.assert_not_called()

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_quote_inserted_by_other_collector_during_http_is_updated_atomically(self,quote,resolve):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from services.lead_time_service import quote_key
        with TemporaryDirectory() as folder:
            engine=create_engine('sqlite:///'+str(Path(folder)/'race.db'))
            Base.metadata.create_all(engine)
            factory=sessionmaker(bind=engine,autoflush=False)
            def respond(item,cep):
                with factory() as other:
                    other.add(LeadTimeQuote(quote_key=quote_key('BRM46MB',{**item,'seller':'Brastemp'},cep),
                        sku='BRM46MB',source='Brastemp',seller='Brastemp',url=item['url'],uf='SP',
                        postal_code=cep,item_json='{}'))
                    other.commit()
                return {**item,'destination_zip':cep,'shipping_cost':19,'delivery_max_days':4,'_price_verified':True}
            quote.side_effect=respond
            try:
                with factory() as db:
                    collect_destinations(db,'BRM46MB',[self.item],['01002020'])
                    row=db.query(LeadTimeQuote).one()
                    self.assertEqual(row.shipping_cost,19)
                    self.assertEqual(row.status,'quoted')
            finally:engine.dispose()

    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.engine=create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db=sessionmaker(bind=self.engine,autoflush=False)()
        self.item=dict(sku='BRM46MB',name='Geladeira BRM46MB',brand='Brastemp',category='Geladeira',image_url=None,
            price=100,source='Brastemp',store='Brastemp',url='https://www.brastemp.com.br/geladeira/p?idsku=42&seller_id=1&sc=2',availability='in_stock')

    def tearDown(self):
        self.db.close();self.engine.dispose()

    def test_observation_saved_after_collection_keeps_its_time_and_newer_prices(self):
        from models.product import OfferObservation
        first = datetime(2026, 11, 18, 12, 10)
        second = datetime(2026, 11, 18, 12, 15)
        _save_result(self.item, self.db, observed_at=second)
        self.db.commit()
        _save_result({**self.item, 'price': 123}, self.db, observed_at=first)
        _save_result({**self.item, 'price': 234}, self.db, observed_at=second)
        self.db.commit()
        row = self.db.query(PriceHistory).one()
        self.assertEqual((row.timestamp, row.price), (second, 100))
        self.assertEqual(self.db.query(OfferObservation).one().observed_at, second)

    def test_request_deduplicates_and_rejects_invalid_postals(self):
        p=SkuBatchRequest(skus=['BRM46MB'],postal_codes=['01002-020','01002020','30130-003'])
        self.assertEqual(p.postal_codes,['01002020','30130003'])
        with self.assertRaises(ValueError):SkuBatchRequest(skus=['BRM46MB'],postal_codes=['123'])
        with self.assertRaises(ValueError):SkuBatchRequest(skus=['BRM46MB'],stores=['unknown'])
        self.assertEqual(SkuBatchRequest(skus=['BRM46MB']).postal_codes,[])

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_finished_postal_is_committed_while_another_postal_is_still_waiting(self,quote,resolve):
        from threading import Event
        saved=Event()
        def respond(item,cep):
            if cep=='30130003':
                if not saved.wait(timeout=3):raise AssertionError('O CEP pronto ficou esperando o lento')
            return {**item,'destination_zip':cep,'shipping_cost':20,'delivery_max_days':4,'_price_verified':True}
        quote.side_effect=respond
        def progress(message):
            if '01002020' in message and self.db.query(LeadTimeQuote).filter_by(postal_code='01002020',status='quoted').count():
                saved.set()
        result=collect_destinations(self.db,'BRM46MB',[self.item],['30130003','01002020'],progress=progress)
        self.assertTrue(saved.is_set());self.assertEqual(len(result),2)
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='quoted').count(),2)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_parallel_destinations_keep_two_request_limit_per_host_and_every_result(self,quote,resolve):
        import threading,time
        active=peak=0;lock=threading.Lock()
        def respond(item,cep):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(.02)
            with lock:active-=1
            return {**item,'destination_zip':cep,'shipping_cost':20,'delivery_max_days':4,'_price_verified':True}
        quote.side_effect=respond
        ceps=['01002020','30130003','80530908','20211110','90010170']
        result=collect_destinations(self.db,'BRM46MB',[self.item],ceps)
        self.assertEqual(peak,2);self.assertEqual(len(result),5)
        self.assertEqual({item['destination_zip'] for item in result},set(ceps))
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='quoted').count(),5)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.shopee_shipping.quote_destinations')
    def test_shopee_batch_saves_every_postal_and_only_failed_destinations_retry(self,batch,resolve):
        ceps=['30130003','80530908','20211110','90010170','01002020']
        item={**self.item,'source':'Shopee','store':'Loja A','url':'https://shopee.com.br/product/1/2'}
        start=datetime.utcnow()
        batch.return_value={cep:{**item,'destination_zip':cep,'shipping_cost':index+10,'delivery_max_days':index+1,'_price_verified':True} for index,cep in enumerate(ceps)}
        batch.return_value[ceps[-1]]={**item,'destination_zip':ceps[-1],'is_cached':True,'_logistics_error':'Falha temporária'}
        result=collect_destinations(self.db,'BRM46MB',[item],ceps,refreshed_after=start)
        batch.assert_called_once_with(item,ceps);self.assertEqual(len(result),5)
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='quoted').count(),4)
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='failed').count(),1)
        with patch('services.shopee_shipping.quote_offer',return_value={**item,'destination_zip':ceps[-1],'shipping_cost':25,'delivery_max_days':7,'_price_verified':True}) as retry:
            result=collect_destinations(self.db,'BRM46MB',[item],ceps,refreshed_after=start)
        self.assertEqual(batch.call_count,1);retry.assert_called_once()
        self.assertEqual(retry.call_args.args[1],ceps[-1]);self.assertEqual(len(result),5)
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='quoted').count(),5)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.shopee_shipping.quote_destinations')
    def test_batch_commits_each_listing_before_opening_the_next_and_resumes_without_duplicates(self,batch,resolve):
        ceps=['30130003','01002020']
        items=[{**self.item,'source':'Shopee','store':'Loja A','url':'https://shopee.com.br/product/1/'+str(product)} for product in [2,3]]
        def quoted(item):return {cep:{**item,'destination_zip':cep,'shipping_cost':20,'delivery_max_days':4,'_price_verified':True} for cep in ceps}
        batch.side_effect=[quoted(items[0]),KeyboardInterrupt('Interrupted process')]
        start=datetime.utcnow()
        with self.assertRaises(KeyboardInterrupt):collect_destinations(self.db,'BRM46MB',items,ceps,refreshed_after=start)
        self.assertEqual(self.db.query(LeadTimeQuote).count(),2)
        batch.reset_mock();batch.side_effect=lambda item,postals:quoted(item)
        result=collect_destinations(self.db,'BRM46MB',items,ceps,refreshed_after=start)
        batch.assert_called_once();self.assertEqual(batch.call_args.args[0]['url'],items[1]['url'])
        self.assertEqual(self.db.query(LeadTimeQuote).count(),4);self.assertEqual(len(result),4)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.shopee_shipping.quote_destinations',return_value=None)
    @patch('services.shopee_shipping.quote_offer')
    def test_single_postal_fallback_remains_available_when_batch_is_unavailable(self,single,batch,resolve):
        item={**self.item,'source':'Shopee','store':'Loja A','url':'https://shopee.com.br/product/1/2'}
        single.side_effect=lambda item,cep:{**item,'destination_zip':cep,'shipping_cost':20,'delivery_max_days':4,'_price_verified':True}
        result=collect_destinations(self.db,'BRM46MB',[item],['30130003','01002020'])
        batch.assert_called_once();self.assertEqual(single.call_count,2);self.assertEqual(len(result),2)

    @patch('services.shopee_shipping.quote_destinations')
    @patch('services.shopee_shipping.quote_offer')
    def test_read_only_shopee_batch_requests_each_destination_once(self, single, batch):
        item = {**self.item, 'source': 'Shopee', 'store': 'Loja A', 'url': 'https://shopee.com.br/product/1/2'}
        ceps = ['30130003', '01002020']
        batch.return_value = {cep: {**item, 'destination_zip': cep, 'shipping_cost': 20, 'delivery_max_days': 4} for cep in ceps}
        result = collect_destinations(self.db, 'BRM46MB', [item], ceps, persist=False)
        batch.assert_called_once_with(item, ceps)
        single.assert_not_called()
        self.assertEqual([row['destination_zip'] for row in result], ceps)
        self.assertEqual(self.db.query(LeadTimeQuote).count(), 0)

    def test_separate_postals_preserve_all_prices_without_duplicate_products(self):
        for cep,cost in [('01002020',15),('30130003',33)]:
            _save_result({**self.item,'destination_zip':cep,'shipping_cost':cost},self.db)
        self.db.commit()
        self.assertEqual(self.db.query(Product).count(),1)
        self.assertEqual(self.db.query(PriceHistory).count(),2)
        _save_result({**self.item,'destination_zip':'01002020','shipping_cost':21},self.db);self.db.commit()
        rows=self.db.query(PriceHistory).all()
        self.assertEqual({r.destination_zip:r.shipping_cost for r in rows},{'01002020':21,'30130003':33})

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_quoted_destinations_checkpoint_and_resume(self,quote,resolve):
        quote.side_effect=lambda item,cep:{**item,'shipping_cost':12,'delivery_max_days':3,'destination_zip':cep,'_price_verified':True}
        start=datetime.utcnow()
        result=collect_destinations(self.db,'BRM46MB',[self.item],['01002020','30130003'],refreshed_after=start)
        self.assertEqual(len(result),2);self.assertEqual(quote.call_count,2)
        self.assertEqual(self.db.query(LeadTimeQuote).count(),2)
        result=collect_destinations(self.db,'BRM46MB',[self.item],['01002020','30130003'],refreshed_after=start)
        self.assertEqual(quote.call_count,2);self.assertEqual(len(result),2)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_resume_retries_failed_postals_and_reuses_successful_quotes(self,quote,resolve):
        start=datetime.utcnow()
        quote.side_effect=lambda item,cep:{**item,'shipping_cost':12,'delivery_max_days':3,'destination_zip':cep,'_price_verified':True} if cep=='01002020' else (_ for _ in ()).throw(ValueError('Login'))
        collect_destinations(self.db,'BRM46MB',[self.item],['01002020','30130003'],refreshed_after=start)
        quote.reset_mock()
        quote.side_effect=lambda item,cep:{**item,'shipping_cost':20,'delivery_max_days':5,'destination_zip':cep,'_price_verified':True}
        result=collect_destinations(self.db,'BRM46MB',[self.item],['01002020','30130003'],refreshed_after=start)
        quote.assert_called_once();self.assertEqual(quote.call_args.args[1],'30130003')
        self.assertEqual({item['destination_zip']:item['shipping_cost'] for item in result},{'01002020':12,'30130003':20})

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_wrong_destination_does_not_save_shipping(self,quote,resolve):
        quote.side_effect=lambda item,cep:{**item,'shipping_cost':0,'destination_zip':'99999999'}
        collect_destinations(self.db,'BRM46MB',[{**self.item,'shipping_cost':5,'delivery_max_days':7}],['01002020'])
        self.assertEqual(self.db.query(PriceHistory).count(),0)
        row=self.db.query(LeadTimeQuote).one()
        self.assertEqual(row.status,'failed');self.assertIsNone(row.shipping_cost);self.assertIsNone(row.delivery_max_days)

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_stockout_is_saved_for_every_destination(self,quote,resolve):
        result=collect_destinations(self.db,'BRM46MB',[{**self.item,'availability':'out_of_stock','price':None}],['01002020','30130003'])
        quote.assert_not_called();self.assertEqual(len(result),2)
        self.assertEqual(self.db.query(PriceHistory).count(),2)
        self.assertTrue(all(r.status=='out_of_stock' for r in self.db.query(LeadTimeQuote)))

    @patch('services.delivery_destinations.resolve_cep', return_value={'uf': 'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_reuses_exact_fresh_product_quote_without_duplicate_observation(self, quote, _resolve):
        from datetime import timedelta
        from models.product import OfferObservation

        start = datetime.utcnow() - timedelta(minutes=2)
        captured = datetime.utcnow() - timedelta(minutes=1)
        first_cep, other_cep = '01002020', '01001000'
        first_quote = {**self.item, 'destination_zip': first_cep, 'shipping_cost': 0,
                       'delivery_max_days': 4, 'logistics_captured_at': captured}
        _save_result(first_quote, self.db)
        self.db.commit()
        saved = self.db.query(PriceHistory).one()
        quote.side_effect = lambda item, cep: {
            **item, 'destination_zip': cep, 'shipping_cost': 24, 'delivery_max_days': 8,
        }
        results = collect_destinations(
            self.db, 'BRM46MB', [{**first_quote, 'last_update': saved.timestamp, 'is_cached': False}],
            [first_cep, other_cep], refreshed_after=start, reuse_collected_quotes=True,
        )
        self.assertEqual([call.args[1] for call in quote.call_args_list], [other_cep])
        self.assertEqual({item['destination_zip']: item['shipping_cost'] for item in results}, {first_cep: 0, other_cep: 24})
        self.assertEqual(self.db.query(OfferObservation).count(), 2)
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(postal_code=first_cep).one().captured_at, captured)

    def test_native_url_preserves_variant_seller_and_sales_channel(self):
        url=canonical_source_url(self.item['url'])
        self.assertIn('idsku=42',url);self.assertIn('sc=2',url);self.assertIn('seller_id=1',url)

    @patch('services.vtex_public.catalog_offers')
    def test_native_source_uses_its_registered_sales_channel(self, catalog_offers):
        from types import SimpleNamespace
        from services.vtex_public import collect_source, index_catalog_offers

        identity = ProductIdentity(sku='BRM46MB')
        entry = {**self.item, '_vtex_sku': '42', '_vtex_seller': '1'}
        catalog_offers.return_value = [
            {**entry, '_vtex_channel': '1', 'price': 100},
            {**entry, '_vtex_channel': '2', 'price': None, 'availability': 'out_of_stock'},
        ]
        index = index_catalog_offers('brastemp', identity.sku, [])
        registered = SimpleNamespace(store='brastemp', url=self.item['url'])
        result = collect_source(registered, identity, None, offer_index=index)
        self.assertEqual(result['items'][0]['_vtex_channel'], '2')
        self.assertEqual(result['items'][0]['availability'], 'out_of_stock')
        without_channel = SimpleNamespace(store='brastemp', url=registered.url.split('&sc=')[0])
        self.assertEqual(collect_source(without_channel, identity, None, offer_index=index)['items'][0]['price'], 100)
        unknown_channel = SimpleNamespace(url=without_channel.url + '&sc=3')
        self.assertEqual(collect_source(unknown_channel, identity, None, offer_index=index)['status'], 'INVALID_MATCH')
        catalog_offers.assert_called_once()

    def test_native_catalog_uses_published_sales_channel_and_stockout(self):
        products=[{'productName':'Geladeira BRM46MB','link':'https://www.brastemp.com.br/test/p','items':[{'itemId':'42','name':'127V','sellers':[{'sellerId':'1','sellerName':'Brastemp','addToCartLink':'/checkout/cart/add?sc=2','commertialOffer':{'IsAvailable':False,'Price':100}}]}]}]
        offers=catalog_offers('brastemp','BRM46MB',products)
        self.assertEqual(offers[0]['_vtex_channel'],'2');self.assertIsNone(offers[0]['price']);self.assertEqual(offers[0]['availability'],'out_of_stock')

    def test_channel_alias_requires_identical_product_seller_price_and_one_published_channel(self):
        from services.vtex_public import published_catalog_listing
        alias = {**self.item, 'url':'https://www.brastemp.com.br/geladeira/p?skuId=42&seller_id=1'}
        index = {('42','1'): {'2':self.item}}
        self.assertEqual(published_catalog_listing(alias,index),self.item)
        for changed in [{'price':99}, {'availability':'out_of_stock'}, {'url':alias['url'].replace('42','43')},
                        {'url':alias['url'].replace('geladeira','outra')}, {'url':alias['url']+'&sc=1'}]:
            self.assertIsNone(published_catalog_listing({**alias, **changed}, index))
        self.assertIsNone(published_catalog_listing(alias, {('42','1'):{'1':self.item,'2':self.item}}))

    def test_vtex_refuses_other_seller_variant_or_postal_and_preserves_estimate_units(self):
        from services.vtex_public import simulation_shipping
        row = {'id': '42', 'seller': '1'}
        estimate = {'price': 2200, 'deliveryChannel': 'delivery', 'shippingEstimate': '1500m'}
        data = {'postalCode': '01002-020', 'items': [row], 'logisticsInfo': [{'itemIndex': 0, 'slas': [estimate]}]}
        details = simulation_shipping(data, '01002020', '42', '1')
        self.assertEqual((details['delivery_min_days'], details['delivery_max_days'], details['delivery_days_type']),
                         (None, 2, 'calendar'))
        for wrong in [{'postalCode': '30111-000'}, {'items': [{**row, 'seller': '2'}]}, {'items': [{**row, 'id': '43'}]}]:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                simulation_shipping({**data, **wrong}, '01002020', '42', '1')

    @patch('services.vtex_public.simulate')
    def test_vtex_pix_and_location_specific_stock_recovery(self, simulate_quote):
        from services.vtex_public import quote_offer
        row = {'availability': 'available', 'sellingPrice': 265200, 'quantity': 1}
        data = {'items': [row], 'totals': [{'id': 'Items', 'value': 265200}], 'paymentData': {
            'installmentOptions': [{'paymentName': 'Pix', 'installments': [
                {'count': 1, 'total': 251940}, {'count': 2, 'total': 80000}]}]}}
        simulate_quote.return_value = ({'shipping_cost': 99.99, 'delivery_max_days': 3}, data)
        known = {**self.item, 'destination_zip': '01002020', 'availability': 'out_of_stock', 'price': None}
        with patch('services.delivery_destinations.resolve_cep', return_value={'uf': 'MG'}):
            values = collect_destinations(self.db, self.item['sku'], [known], ['01002020', '30111000'])
        saved = {item['destination_zip']: item for item in values}
        self.assertEqual(saved['01002020']['availability'], 'out_of_stock')
        self.assertEqual((saved['30111000']['availability'], saved['30111000']['cash_price']), ('in_stock', 2519.4))
        simulate_quote.assert_called_once()
        data['totals'].append({'id': 'Shipping', 'value': 9999})
        self.assertEqual(quote_offer(self.item, '30111000')['price'], 2652)

    @patch('services.vtex_public.quote_offer', side_effect=TimeoutError('Cotação demorou'))
    def test_native_catalog_price_survives_shipping_endpoint_failure(self, quote):
        from types import SimpleNamespace
        from services.vtex_public import collect_source
        source = SimpleNamespace(url=self.item['url'])
        index = {('42', '1'): {'2': self.item}}
        result = collect_source(source, ProductIdentity(sku='BRM46MB'), '30111000', offer_index=index)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['items'][0]['price'], 100)
        self.assertNotIn('shipping_cost', result['items'][0])

    @patch('services.delivery_destinations.resolve_cep', return_value={'uf': 'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_complete_checkpoint_needs_no_address_or_store_requests(self, quote, resolve):
        start = datetime.utcnow()
        ceps = ['30111000','01001000']
        quote.side_effect = lambda item, cep: {**item, 'destination_zip': cep, 'shipping_cost': 9, 'delivery_max_days': 4}
        first = collect_destinations(self.db, self.item['sku'], [self.item], ceps, refreshed_after=start)
        resolve.reset_mock()
        quote.reset_mock()
        resumed = collect_destinations(self.db, self.item['sku'], [self.item], ceps, refreshed_after=start)
        self.assertCountEqual(first, resumed)
        resolve.assert_not_called()
        quote.assert_not_called()

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.vtex_public.quote_offer')
    def test_two_newer_observations_for_one_listing_replace_their_own_checkpoints(self, quote, resolve):
        from datetime import timedelta
        from models.product import OfferObservation
        from services.lead_time_service import quote_key
        now = datetime.utcnow()
        start = now - timedelta(minutes=15)
        observations = []
        for cep, cost in [('01001000', 5), ('30111000', 12)]:
            current = {**self.item,'seller':'Brastemp','destination_zip':cep,'is_cached':False,
                       'shipping_cost':cost,'delivery_max_days':3,'logistics_captured_at':now}
            _save_result(current, self.db)
            observations.append({**current,'last_update':now})
            self.db.add(LeadTimeQuote(quote_key=quote_key(self.item['sku'],current,cep),sku=self.item['sku'],
                source='Brastemp',seller='Brastemp',url=current['url'],postal_code=cep,uf='SP',status='quoted',
                captured_at=now-timedelta(minutes=3),shipping_cost=2,delivery_max_days=1,item_json='{}'))
        self.db.commit()
        results = collect_destinations(self.db, self.item['sku'], observations, ['01001000','30111000'],
                                       refreshed_after=start, reuse_collected_quotes=True)
        self.assertEqual({r['destination_zip']:r['shipping_cost'] for r in results},{'01001000':5,'30111000':12})
        self.assertEqual(self.db.query(OfferObservation).count(),2)
        self.assertEqual({r.shipping_cost for r in self.db.query(LeadTimeQuote)}, {5,12})
        quote.assert_not_called()

    @patch('services.vtex_public.Session')
    def test_midea_laundry_family_fallback_avoids_rejected_accented_search(self,session):
        from services.vtex_public import catalog
        first=MagicMock();first.json.return_value=[]
        family=MagicMock();family.json.return_value=[{'productId':'42','productName':'Máquina MA512W150A'}]
        get=session.return_value.__enter__.return_value.get;get.side_effect=[first,family]
        self.assertEqual(catalog('midea','MA512W150A')[0]['productId'],'42')
        self.assertEqual(get.call_args.kwargs['params']['ft'],'maquina')

    @patch('services.vtex_public.Session')
    def test_simulation_selects_lowest_paid_delivery_and_keeps_its_lead_time(self,session):
        response=session.return_value.__enter__.return_value.post.return_value
        response.json.return_value={'logisticsInfo':[{'itemIndex':0,'slas':[{'deliveryChannel':'delivery','price':0,'shippingEstimate':'7bd'},{'deliveryChannel':'delivery','price':990,'shippingEstimate':'3bd'},{'deliveryChannel':'delivery','price':1990,'shippingEstimate':'1bd'},{'deliveryChannel':'pickup-in-point','price':1,'shippingEstimate':'1bd'}]}]}
        details,_=simulate({'_vtex_store':'brastemp','_vtex_sku':'42','_vtex_seller':'1','_vtex_channel':'2'},'01002020')
        self.assertEqual(details['shipping_cost'],9.9);self.assertEqual(details['delivery_max_days'],3)
        self.assertEqual(session.return_value.__enter__.return_value.post.call_args.kwargs['params'],{'sc':'2'})

    def test_magalu_sends_merchant_sku_and_exact_postal_code(self):
        request=shipping_request({'seller':{'id':'partner','sku':'merchant-42','category':'3p'},'price':{'price':'123.45'},'variationId':'platform-42'}, {},'30130003')
        self.assertEqual(request['product']['id'],'merchant-42');self.assertEqual(request['zipcode'],'30130003')

    @patch('services.magalu_public_shipping.simulate',return_value=({'destination_zip':'01001000','shipping_cost':12,'delivery_max_days':3},{}))
    def test_shipping_identity_uses_product_heading_instead_of_kit_mentions_in_description(self,simulate):
        from services.magalu_public_shipping import quote_offer
        item={**self.item,'sku':'W10531315','name':'Sensor W10531315'}
        context=({'title':'Sensor de Temperatura W10531315','description':'Também disponível em kit 10 unidades.',
                  'reference':'W10531315','price':{'price':'15'},'available':True},{})
        result=quote_offer(item,'01001000',page_context=context)
        self.assertEqual(result['shipping_cost'],12);self.assertEqual(result['delivery_max_days'],3)
        simulate.assert_called_once()

    @patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'})
    @patch('services.magalu_public_shipping.simulate_destinations')
    @patch('services.magalu_public_shipping.product_page')
    def test_magalu_reads_one_fresh_page_for_all_27_independent_postal_quotes(self,page,simulate,resolve):
        from services.lead_time_destinations import CAPITALS
        page.return_value=({'title':'Geladeira BRM46MB','price':{'price':'100'},'available':True},{})
        simulate.side_effect=lambda product,structure,ceps,timeout,batch_size:{cep:({'destination_zip':cep,'shipping_cost':int(cep[-3:])+1,'delivery_max_days':4},{}) for cep in ceps}
        item={**self.item,'source':'Magazine Luiza','store':'partner-Magalu','url':'https://www.magazinevoce.com.br/loja/geladeira-brm46mb/p/123/ed/refr/'}
        ceps=[row['postal_code'] for row in CAPITALS]
        result=collect_destinations(self.db,'BRM46MB',[item],ceps)
        self.assertEqual(len(result),27);self.assertEqual(page.call_count,1);self.assertEqual(simulate.call_count,3)
        self.assertEqual({row['destination_zip'] for row in result},set(ceps))
        self.assertTrue(all(row['shipping_cost']==int(row['destination_zip'][-3:])+1 for row in result))
        self.assertEqual(self.db.query(LeadTimeQuote).filter_by(status='quoted').count(),27)
        # A new extraction obtains fresh product data again.
        collect_destinations(self.db,'BRM46MB',[item],ceps[:2])
        self.assertEqual(page.call_count,2)

    def test_full_appliance_does_not_accept_compatible_parts_or_aquarium_filters(self):
        for sku,title in [('RE31','Prateleira geladeira Electrolux RE31'),('IB6','Kit filtro geladeira IB6'),('HI330','Refil para filtro hang-on Leecom HI-330'),('LEP15','4 Vara Suspensão Lavadora Electrolux LEP15')]:
            self.assertFalse(resolve_candidate(ProductIdentity(sku=sku),{'name':title}).accepted,title)
        self.assertTrue(resolve_candidate(ProductIdentity(sku='LEP15',name='Máquina de Lavar Electrolux LEP15 e Filtro Fiapos'),{'name':'Máquina Lavar Electrolux LEP15 com Cesto Inox'}).accepted)

    def test_complete_export_has_all_views_and_exact_job_scope(self):
        for cep in ['01002020','30130003','99999999']:_save_result({**self.item,'destination_zip':cep,'shipping_cost':12},self.db)
        job=ExtractionJob(id='test-job',request_key='test',status='completed',postal_codes_json='["01002020","30130003"]',stores_json='["brastemp"]')
        job.items=[ExtractionJobItem(sku='BRM46MB',position=0)];self.db.add(job);self.db.commit()
        book=load_workbook(complete_workbook(self.db,'test-job'))
        self.assertEqual(set(book.sheetnames),{'Database','Ofertas','Analytics por SKU','Preço por loja','Comparar lojas','Lead Time detalhes','Frete por estado','Histórico de preços'})
        sheet=book['Database'];headers=[c.value for c in sheet[1]];index=headers.index('CEP')
        self.assertEqual({row[index] for row in sheet.iter_rows(min_row=2,values_only=True)},{'01002020','30130003'})
        self.assertTrue(sheet.cell(2,headers.index('Atualizado em (UTC)')+1).is_date)
        self.assertEqual(sheet.cell(2,headers.index('Frete')+1).number_format,'#,##0.00')
        self.assertEqual(len(book['Preço por loja']._charts),1)

    def test_shipping_map_uses_custom_postal_without_borrowing_capital_quote(self):
        from services.lead_time_service import get_shipping_map
        _save_result({**self.item,'destination_zip':'01002020','shipping_cost':12},self.db)
        self.db.add(LeadTimeDestination(postal_code='01002020',uf='SP',city='São Paulo',position=0))
        self.db.add(LeadTimeQuote(quote_key='other-postal',sku='BRM46MB',source='Brastemp',seller='Brastemp',url=self.item['url'],uf='SP',postal_code='01001000',item_json='{}',status='quoted',shipping_cost=1,captured_at=datetime.utcnow()))
        self.db.commit()
        state=next(row for row in get_shipping_map(self.db,'BRM46MB')['states'] if row['uf']=='SP')
        self.assertEqual(state['postal_code'],'01002020');self.assertEqual(state['quote']['shipping_cost'],12)

    def test_complete_export_without_job_keeps_empty_grid_cells_and_exact_quote_errors(self):
        _save_result({**self.item,'destination_zip':'01002020','shipping_cost':12},self.db)
        _save_result({**self.item,'sku':'BWF18AB','name':'Lavadora BWF18AB','source':'Consul',
            'store':'Outro vendedor','url':'https://www.consul.com.br/other/p','destination_zip':'30130003'},self.db)
        self.db.add(LeadTimeQuote(quote_key='export-error',sku='BRM46MB',source='Brastemp',seller='Brastemp',
            url=self.item['url'],uf='SP',postal_code='01002020',item_json='{}',status='failed',error='Entrega indisponível'))
        self.db.commit()
        book=load_workbook(complete_workbook(self.db))
        self.assertEqual(book['Database'].max_row,3)
        rows=list(book['Lead Time detalhes'].iter_rows(min_row=2,values_only=True))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][-1],'Entrega indisponível')
        self.assertNotIn('Cobertura da extração',book.sheetnames)

    def test_export_fills_sparse_fields_and_recovers_only_the_same_offer_at_the_same_cep(self):
        from services.lead_time_service import quote_key
        for cep in ['01002020', '30130003']:
            _save_result({**self.item,'destination_zip':cep}, self.db)
        cep = '01002020'
        self.db.add(LeadTimeQuote(quote_key=quote_key('BRM46MB',{**self.item,'seller':'Brastemp'},cep),
            sku='BRM46MB',source='Brastemp',seller='Brastemp',url=self.item['url'],uf='SP',postal_code=cep,
            item_json='{}',status='quoted',shipping_cost=0,delivery_max_days=2,captured_at=datetime.utcnow()))
        self.db.commit()
        sheet=load_workbook(complete_workbook(self.db))['Database']
        headers=[cell.value for cell in sheet[1]]
        rows={row[headers.index('CEP')]:dict(zip(headers,row)) for row in sheet.iter_rows(min_row=2,values_only=True)}
        self.assertEqual(rows['01002020']['Frete'],0)
        self.assertEqual(rows['01002020']['Prazo máximo'],2)
        self.assertEqual(rows['30130003']['Frete'],'Não informado')
        self.assertEqual(rows['30130003']['Entregue por'],'Não informado')
        self.assertEqual(rows['30130003']['A prazo'],'Não informado')

    def test_excel_download_sends_binary_blocks_instead_of_one_chunk_per_newline(self):
        from routes.analysis import excel
        payload=b'a\n'*100000
        response=excel(BytesIO(payload),'all.xlsx')
        async def consume():
            return [chunk async for chunk in response.body_iterator]
        chunks=asyncio.run(consume())
        self.assertEqual(b''.join(chunks),payload)
        self.assertEqual(len(chunks),4)
        self.assertEqual(response.headers['content-length'],str(len(payload)))

    def test_daily_export_does_not_relabel_previous_observations_as_today(self):
        from datetime import timedelta
        _save_result({**self.item,'destination_zip':'01002020'},self.db)
        self.db.commit()
        threshold=datetime.utcnow()+timedelta(seconds=1)
        row=self.db.query(PriceHistory).one();row.timestamp=threshold-timedelta(days=1)
        job=ExtractionJob(id='daily-job',request_key='daily',status='completed',postal_codes_json='["01002020"]',stores_json='["brastemp"]')
        job.items=[ExtractionJobItem(sku='BRM46MB',position=0)];self.db.add(job);self.db.commit()
        book=load_workbook(complete_workbook(self.db,'daily-job',fresh_after=threshold))
        self.assertEqual(book['Database'].max_row,1)
        self.assertEqual(book['Histórico de preços'].max_row,1)
        self.assertNotIn('Cobertura da extração',book.sheetnames)


if __name__=='__main__':unittest.main()
