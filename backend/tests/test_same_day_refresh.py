import copy
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base
from models.product import ExtractionSchedule, KnownSku, LeadTimeQuote, OfferObservation, PriceHistory, ProductSource, ScrapeCursor
from services.lead_time_service import quote_key
from services.product_sources import canonical_source_url
from services.same_day_refresh import scheduled_price_check, unchanged_catalog_stores, day_start_utc
from services.vtex_public import catalog_offers


class SameDayRefreshTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.engine=create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db=sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose);self.addCleanup(self.db.close)
        self.now=datetime(2026,11,13,18)
        self.ceps=['01002020','30130003']
        self.products=[{'productId':'1','productName':'Geladeira BRM46MB','productReference':'BRM46MB',
            'brand':'Brastemp','link':'https://www.brastemp.com.br/geladeira-brm46mb/p',
            'items':[{'itemId':'42','name':'127V','sellers':[{'sellerId':'1','sellerName':'Brastemp',
                'commertialOffer':{'Price':1000,'IsAvailable':True,'AvailableQuantity':2}}]}]}]
        self.item=catalog_offers('brastemp','BRM46MB',self.products)[0]
        self.db.add(ProductSource(sku='BRM46MB',store='brastemp',seller='Brastemp',
            url=canonical_source_url(self.item['url']),validation_status='ok',active=True))
        for cep in self.ceps:
            value={**self.item,'destination_zip':cep,'shipping_cost':25,'delivery_max_days':3}
            self.db.add(LeadTimeQuote(quote_key=quote_key('BRM46MB',value,cep),sku='BRM46MB',
                source='Brastemp',seller='Brastemp',url=value['url'],uf='SP',postal_code=cep,
                shipping_cost=25,delivery_max_days=3,status='quoted',item_json=json.dumps(value),
                captured_at=self.now-timedelta(hours=1)))
        self.db.commit()
        handle=patch('services.vtex_public.catalog',return_value=self.products)
        self.catalog=handle.start();self.addCleanup(handle.stop)

    def check(self, now=None, ceps=None):
        return unchanged_catalog_stores(self.db,'BRM46MB',['brastemp'],ceps or self.ceps,now or self.now)[0]

    def test_unchanged_complete_same_day_reuses_every_destination_without_retimestamping(self):
        result=self.check()
        self.assertEqual(set(result),{'brastemp'});self.assertEqual(len(result['brastemp']),2)
        self.catalog.assert_called_once()
        self.assertTrue(all(row['is_cached'] and row['_same_day_price_checked'] for row in result['brastemp']))
        self.assertEqual({row['destination_zip'] for row in result['brastemp']},set(self.ceps))
        self.assertTrue(all(row['logistics_captured_at']==(self.now-timedelta(hours=1)).isoformat() for row in result['brastemp']))
        self.assertTrue(all(row.captured_at==self.now-timedelta(hours=1) for row in self.db.query(LeadTimeQuote)))

    def test_new_day_does_not_skip(self):
        self.assertEqual(self.check(now=self.now+timedelta(days=1)),{})
        self.catalog.assert_not_called()

    def test_unfinished_discovery_prevents_same_day_shortcut(self):
        self.db.add(ScrapeCursor(
            sku='BRM46MB', store='brastemp', kind='search', key='BRM46MB',
            next_url='https://www.brastemp.com.br/busca?q=BRM46MB', completed=False,
        ))
        self.db.commit()
        self.assertEqual(self.check(), {})
        self.catalog.assert_not_called()

    def test_midnight_uses_brazilian_calendar_not_utc_date(self):
        self.assertEqual(day_start_utc(datetime(2026,11,14,2,59)),datetime(2026,11,13,3))
        self.assertEqual(day_start_utc(datetime(2026,11,14,3)),datetime(2026,11,14,3))

    def test_price_stock_new_seller_or_variant_change_requires_normal_extraction(self):
        for change in ['price','stock','seller','variant']:
            with self.subTest(change=change):
                products=copy.deepcopy(self.products)
                variant=products[0]['items'][0]
                if change=='price':variant['sellers'][0]['commertialOffer']['Price']=1200
                if change=='stock':variant['sellers'][0]['commertialOffer']['IsAvailable']=False
                if change=='seller':variant['sellers'].append({**variant['sellers'][0],'sellerId':'2','sellerName':'Outra loja'})
                if change=='variant':products[0]['items'].append({**copy.deepcopy(variant),'itemId':'43','name':'220V'})
                self.catalog.return_value=products
                self.assertEqual(self.check(),{})

    def test_missing_failed_or_incomplete_quote_requires_normal_extraction(self):
        row=self.db.query(LeadTimeQuote).first()
        for changes in [{'captured_at':self.now-timedelta(days=1)}, {'status':'failed'}, {'shipping_cost':None}, {'delivery_max_days':None}]:
            original={key:getattr(row,key) for key in changes}
            for key,value in changes.items():setattr(row,key,value)
            self.db.commit();self.assertEqual(self.check(),{})
            for key,value in original.items():setattr(row,key,value)
            self.db.commit()
        self.assertEqual(self.check(ceps=[*self.ceps,'90010170']),{})

    def test_timeout_empty_catalog_or_unknown_store_never_means_unchanged(self):
        self.catalog.side_effect=TimeoutError('timeout');self.assertEqual(self.check(),{})
        self.catalog.side_effect=None;self.catalog.return_value=[];self.assertEqual(self.check(),{})
        self.catalog.reset_mock()
        result,_=unchanged_catalog_stores(self.db,'BRM46MB',['shopee','mercado_livre'],self.ceps,self.now)
        self.assertEqual(result,{});self.catalog.assert_not_called()

    def test_existing_validation_failure_does_not_skip(self):
        self.db.query(ProductSource).one().validation_status='blocked';self.db.commit()
        self.assertEqual(self.check(),{});self.catalog.assert_not_called()

    def test_native_product_batch_fetches_catalog_once_for_all_variants(self):
        from services.precise_extraction import _collect_sources
        from services.product_resolver import ProductIdentity
        sources=[]
        for number in range(8):
            variant=copy.deepcopy(self.products[0]['items'][0]);variant['itemId']=str(100+number)
            self.products[0]['items'].append(variant)
            item=catalog_offers('brastemp','BRM46MB',[{**self.products[0],'items':[variant]}])[0]
            sources.append(ProductSource(sku='BRM46MB',store='brastemp',seller='Brastemp',url=item['url'],active=True))
        self.db.add_all(sources);self.db.commit();self.catalog.reset_mock()
        control=SimpleNamespace(concurrent_requests_per_domain=2,request_timeout_seconds=5,
            playwright_product_fallback=False,playwright_shipping_fallback=False)
        with patch('services.precise_extraction.collect_sources_with_scrapy') as crawler:
            items,stores=_collect_sources(self.db,ProductIdentity(sku='BRM46MB',model='BRM46MB'),sources,None,control)
        self.catalog.assert_called_once();crawler.assert_not_called()
        self.assertEqual(len(items),8);self.assertEqual(stores,{'brastemp'})
        self.assertEqual(len({row['_vtex_sku'] for row in items}),8)

    def test_shopee_retires_only_duplicate_seller_cursor_not_product_sources_or_search(self):
        from services.scrape_pagination import retire_redundant_seller_pages, enqueue_seller_offer_page
        from services.product_resolver import ProductIdentity
        self.db.add_all([ScrapeCursor(sku='BRM46MB',store='shopee',kind=kind,key=kind,
            next_url='https://shopee.com.br/product/1/2',completed=False) for kind in ['seller_offers','search']])
        self.db.add(ProductSource(sku='BRM46MB',store='shopee',seller='Loja A',url='https://shopee.com.br/product/1/2',active=True))
        self.db.commit();retire_redundant_seller_pages(self.db,'BRM46MB');self.db.commit();self.db.expire_all()
        self.assertTrue(self.db.query(ScrapeCursor).filter_by(kind='seller_offers').one().completed)
        self.assertFalse(self.db.query(ScrapeCursor).filter_by(kind='search').one().completed)
        self.assertTrue(self.db.query(ProductSource).filter_by(store='shopee').one().active)
        self.assertIsNone(enqueue_seller_offer_page(self.db,ProductIdentity(sku='BRM46MB'),'shopee',
            {'name':'Geladeira BRM46MB','url':'https://shopee.com.br/product/1/2','seller':'Loja A'}))

    def test_failed_index_search_cannot_be_unlocked_by_store_login(self):
        from services.scrape_pagination import browser_retry_due
        cursor=SimpleNamespace(kind='indexed_search',store='shopee',completed=False,last_status='BLOCKED',
            last_attempted_at=self.now,browser_attempted_at=None,next_url='https://bing.com/search?q=BRM46MB')
        with patch('services.marketplace_intervention.released_after',return_value=True):
            self.assertFalse(browser_retry_due(cursor,self.now))

    def test_queue_skips_heavy_collection_only_after_real_same_day_check(self):
        from routes.products import process_extraction_queue_item
        with patch('routes.products.SessionLocal',sessionmaker(bind=self.engine)), \
             patch('services.same_day_refresh.day_start_utc',return_value=day_start_utc(self.now)), \
             patch('routes.products._search_sku_summary') as heavy, \
             patch('services.multi_postal_extraction.collect_destinations',return_value=[]) as shipping:
            result=process_extraction_queue_item('BRM46MB',dict(mode='precise',force_refresh=True,
                stores=['brastemp'],postal_codes=self.ceps,attempt=1))
        heavy.assert_not_called();self.assertEqual(result['status'],'cached')
        self.assertEqual(len(result['_offers']),2)
        self.assertIn('sem alterações',result['message'])
        self.assertEqual(shipping.call_args.args[2],[])

    def test_ordinary_quick_phase_passes_its_store_filter_to_actual_scraper_entrypoint(self):
        from routes.products import _search_sku_summary
        from schemas.product import SkuBatchRequest
        summary={'results':[],'stores':[],'comparison':[],'fresh_count':0,'processed_count':0}
        with patch('services.quick_extraction.run_quick_extraction',return_value=summary) as collect:
            _search_sku_summary('BRM46MB',SkuBatchRequest(skus=['BRM46MB'],mode='quick',
                force_refresh=True,stores=['brastemp']),self.db)
        self.assertEqual(collect.call_args.kwargs['stores'],['brastemp'])

    def seed_scheduled_price(self, cep='01002020', timestamp=None):
        from services.scraper_service import _save_result

        captured = timestamp or self.now - timedelta(hours=1)
        item = {**self.item, 'destination_zip': cep, 'shipping_cost': 0,
                'delivery_min_days': 2, 'delivery_max_days': 4, 'delivery_days_type': 'business',
                'fulfilled_by': 'Brastemp', 'logistics_captured_at': captured}
        _save_result(item, self.db, observed_at=captured)
        self.db.commit()
        return captured

    def test_schedule_updates_the_database_and_history_but_preserves_original_shipping_observation(self):
        from routes.products import _find_sku_products
        from services.price_observations import history_report, local_day

        captured = self.seed_scheduled_price()
        for mode in ('quick', 'precise'):
            with self.subTest(mode=mode):
                now = self.now + timedelta(hours=mode == 'precise')
                unchanged, _, _ = scheduled_price_check(self.db, 'BRM46MB', ['brastemp'], self.ceps[0], mode, now=now)
                result = unchanged['brastemp'][0]
                self.assertEqual(result['last_update'], now)
                self.assertEqual(result['shipping_cost'], 0)
                self.assertEqual(result['delivery_max_days'], 4)
                self.assertEqual(result['fulfilled_by'], 'Brastemp')
                self.assertEqual(result['logistics_captured_at'], captured)
                price = self.db.query(PriceHistory).one()
                self.assertEqual(price.timestamp, now)
                self.assertEqual(price.logistics_captured_at, captured)
                latest = self.db.query(OfferObservation).order_by(OfferObservation.observed_at.desc()).first()
                self.assertEqual(latest.observed_at, now)
                self.assertEqual(latest.origin, 'scheduled_price_check')
                self.assertFalse(latest.changed)
        self.assertEqual(self.db.query(OfferObservation).count(), 3)
        self.assertEqual(self.db.query(LeadTimeQuote).count(), 2)
        visible = _find_sku_products(self.db, 'BRM46MB', [self.ceps[0]])
        self.assertEqual(visible[0][1].timestamp, now)
        report = history_report(self.db, local_day(now), local_day(now), skus=['BRM46MB'])
        self.assertEqual(report['items'][0]['checks'], 3)
        self.assertEqual(report['items'][0]['observed_at'], now)

    def test_schedule_retries_do_not_duplicate_a_confirmed_observation_in_the_same_job(self):
        self.seed_scheduled_price()
        for hours in (0, 1):
            unchanged, _, _ = scheduled_price_check(self.db, 'BRM46MB', ['brastemp'], self.ceps[0], 'precise',
                                                    now=self.now + timedelta(hours=hours),
                                                    job_created_at=self.now - timedelta(minutes=5))
            self.assertIn('brastemp', unchanged)
        self.assertEqual(self.db.query(OfferObservation).count(), 2)

    def test_first_refresh_of_a_new_day_uses_normal_extraction_without_price_probe(self):
        self.seed_scheduled_price(timestamp=datetime(2026, 11, 13, 2, 59))
        for mode in ('quick', 'precise'):
            result = scheduled_price_check(self.db, 'BRM46MB', ['brastemp'], self.ceps[0], mode,
                                           now=datetime(2026, 11, 13, 3))
            self.assertEqual(result, ({}, {}, {}))
        self.catalog.assert_not_called()

    def test_schedule_honors_new_prices_and_reuses_the_probe_as_input_to_quick_extraction(self):
        from routes.products import process_extraction_queue_item

        self.seed_scheduled_price()
        updated = copy.deepcopy(self.products)
        updated[0]['items'][0]['sellers'][0]['commertialOffer']['Price'] = 1200
        self.catalog.return_value = updated
        actual_check = scheduled_price_check
        with patch('routes.products.SessionLocal', sessionmaker(bind=self.engine)), \
             patch('services.same_day_refresh.scheduled_price_check', side_effect=lambda *a, **k: actual_check(*a, **k, now=self.now)), \
             patch('services.quick_extraction.first_page') as repeated:
            result = process_extraction_queue_item('BRM46MB', dict(
                kind='scheduled', refresh_if_stale=True, force_refresh=True, mode='quick',
                stores=['brastemp'], postal_code=self.ceps[0]))
        self.catalog.assert_called_once()
        repeated.assert_not_called()
        self.assertEqual(result['status'], 'updated')
        self.assertTrue(any(item['current_price'] == 1200 for item in result['_offers']))
        self.assertEqual(self.db.query(OfferObservation).filter_by(origin='scheduled_price_check').count(), 0)

    def test_unchanged_scheduled_item_skips_the_normal_collector_in_both_modes(self):
        from routes.products import process_extraction_queue_item

        self.seed_scheduled_price()
        actual_check = scheduled_price_check
        with patch('routes.products.SessionLocal', sessionmaker(bind=self.engine)), \
             patch('services.same_day_refresh.scheduled_price_check', side_effect=lambda *a, **k: actual_check(*a, **k, now=self.now)), \
             patch('routes.products._search_sku_summary') as heavy:
            for mode in ('quick', 'precise'):
                result = process_extraction_queue_item('BRM46MB', dict(
                    kind='scheduled', refresh_if_stale=True, force_refresh=True, mode=mode,
                    stores=['brastemp'], postal_code=self.ceps[0]))
                self.assertEqual(result['status'], 'cached', result)
                self.assertEqual(result['results_count'], 1)
                self.assertEqual(result['_offers'][0]['current_price'], 1000)
        heavy.assert_not_called()

    def test_quick_schedule_reads_unseen_stores_in_the_same_wave_and_does_not_repeat_requests(self):
        from routes.products import process_extraction_queue_item

        self.seed_scheduled_price()
        consul_products = copy.deepcopy(self.products)
        consul_products[0]['link'] = 'https://www.consul.com.br/geladeira-brm46mb/p'
        consul_products[0]['items'][0]['sellers'][0]['sellerName'] = 'Consul'
        self.catalog.side_effect = lambda store, *_a, **_k: self.products if store == 'brastemp' else consul_products
        actual_check = scheduled_price_check
        with patch('routes.products.SessionLocal', sessionmaker(bind=self.engine)), \
             patch('services.same_day_refresh.scheduled_price_check', side_effect=lambda *a, **k: actual_check(*a, **k, now=self.now)):
            result = process_extraction_queue_item('BRM46MB', dict(
                kind='scheduled', refresh_if_stale=True, force_refresh=True, mode='quick',
                stores=['brastemp', 'consul'], postal_code=self.ceps[0]))
        self.assertEqual(result['status'], 'updated')
        self.assertCountEqual([item['source'] for item in result['_offers']], ['Brastemp', 'Consul'])
        self.assertCountEqual([call.args[0] for call in self.catalog.call_args_list], ['brastemp', 'consul'])

    def test_incomplete_or_mismatched_probes_never_renew_old_prices(self):
        self.seed_scheduled_price()
        old = self.db.query(PriceHistory).one().timestamp
        self.catalog.side_effect = [TimeoutError('timeout'), [], [None]]
        for _ in range(3):
            unchanged, _, _ = scheduled_price_check(self.db, 'BRM46MB', ['brastemp'], self.ceps[0], 'precise', now=self.now)
            self.assertEqual(unchanged, {})
        self.assertEqual(self.db.query(PriceHistory).one().timestamp, old)
        self.assertEqual(self.db.query(OfferObservation).count(), 1)

    def test_precise_schedule_rechecks_confirmed_empty_store_but_recovers_new_offers_or_page_failures(self):
        self.db.add(ScrapeCursor(sku='BRM46MB', store='dufrio', kind='search', key='BRM46MB',
                                 completed=True, last_status='OK', candidates_found=0,
                                 next_url='https://www.dufrio.com.br/search?q=BRM46MB',
                                 last_checked_at=self.now - timedelta(hours=2)))
        self.db.commit()
        def page(store, url, *, diagnostics, **kwargs):
            diagnostics['status'] = status
            return results, next_url, parsed
        with patch('crawlers.paged_discovery.fetch_search_page', side_effect=page):
            for results, next_url, parsed, status, expected in (
                ([], None, True, 'OK', {'dufrio': []}),
                ([], 'https://www.dufrio.com.br/?page=2', True, 'OK', {}),
                ([], None, False, 'BLOCKED', {}),
                ([{'url': 'https://www.dufrio.com.br/geladeira', 'name': 'Geladeira BRM46MB', 'price': 1200}], None, True, 'OK', {}),
            ):
                actual, _, _ = scheduled_price_check(self.db, 'BRM46MB', ['dufrio'], self.ceps[0], 'precise', now=self.now)
                self.assertEqual(actual, expected)
        self.assertEqual(self.db.query(OfferObservation).count(), 0)

    def test_schedule_with_different_destination_and_disabled_option_uses_the_normal_collector(self):
        from routes.products import process_extraction_queue_item

        self.seed_scheduled_price()
        actual_check = scheduled_price_check
        summary = {'results': [], 'stores': [], 'comparison': [], 'fresh_count': 0}
        with patch('routes.products.SessionLocal', sessionmaker(bind=self.engine)), \
             patch('services.same_day_refresh.scheduled_price_check', side_effect=lambda *a, **k: actual_check(*a, **k, now=self.now)) as check, \
             patch('routes.products._search_sku_summary', return_value=(summary, 'updated')) as heavy:
            for mode in ('quick', 'precise'):
                process_extraction_queue_item('BRM46MB', dict(
                    kind='scheduled', refresh_if_stale=True, force_refresh=True, mode=mode,
                    stores=['brastemp'], postal_code='90010170'))
                self.assertEqual(heavy.call_args.args[1].postal_code, '90010170')
                process_extraction_queue_item('BRM46MB', dict(
                    kind='scheduled', refresh_if_stale=False, force_refresh=True, mode=mode,
                    stores=['brastemp'], postal_code=self.ceps[0]))
            self.assertEqual(heavy.call_count, 4)
            self.assertEqual(check.call_count, 2)
        self.catalog.assert_not_called()
        self.assertEqual(self.db.query(OfferObservation).count(), 1)

    def test_scheduler_worker_persists_confirmed_prices_and_finishes_the_slot_in_both_modes(self):
        from routes.products import process_extraction_queue_item
        from services import extraction_queue as queue, extraction_scheduler as scheduler

        self.seed_scheduled_price()
        self.db.add_all([KnownSku(sku='BRM46MB'), ExtractionSchedule(id=1, only_stale=True)])
        self.db.commit()
        sessions = sessionmaker(bind=self.engine)
        actual_enqueue = queue.enqueue_extraction_job
        actual_check = scheduled_price_check
        with patch.object(queue, 'SessionLocal', sessions), patch.object(scheduler, 'SessionLocal', sessions), \
             patch('routes.products.SessionLocal', sessions), patch.object(queue, '_processor', process_extraction_queue_item), \
             patch.object(scheduler, 'enqueue_extraction_job', side_effect=lambda *a, **k: actual_enqueue(*a, **k, stores=['brastemp'])), \
             patch('services.same_day_refresh.scheduled_price_check', side_effect=lambda *a, **k: actual_check(*a, **k, now=checked_at)), \
             patch('routes.products._search_sku_summary') as heavy:
            for hours, mode in enumerate(('quick', 'precise')):
                checked_at = self.now + timedelta(hours=hours)
                slot = f'2026-11-13-1{hours}00'
                job = scheduler._run_cycle(self.ceps[0], slot, True, mode, only_stale=True)
                self.assertEqual(job['mode'], mode)
                while queue._process_job_batch(job['id']) == 'queued':
                    pass
                state = queue.get_extraction_job(job['id'])
                self.assertEqual(state['status'], 'completed')
                self.assertEqual(state['processed_skus'], 1)
                self.db.expire_all()
                self.assertEqual(self.db.get(ExtractionSchedule, 1).last_run_key, slot)
                self.assertEqual(self.db.query(PriceHistory).one().timestamp, checked_at)
        heavy.assert_not_called()

    def test_changed_catalog_price_is_collected_in_quick_mode_without_reusing_old_price(self):
        from services.scraper_service import run_all_scrapers
        products=copy.deepcopy(self.products)
        products[0]['items'][0]['sellers'][0]['commertialOffer']['Price']=1200
        with patch('services.scraper_service.scrape_comparison_offers',return_value=[]), \
             patch('services.scraper_service.search_bing_shopping',return_value=[]), \
             patch('services.scraper_service.get_search_provider_names',return_value=[]), \
             patch('services.scraper_service.enrich_offers',side_effect=lambda rows,*args,**kwargs:rows):
            prices_before = self.db.query(PriceHistory).count()
            preview = run_all_scrapers('BRM46MB', self.db, stores=['brastemp'],
                                       catalog_snapshots={'brastemp': products}, persist=False)
            self.assertEqual(preview['results'][0]['current_price'], 1200)
            self.assertEqual(preview['processed_count'], 0)
            self.assertEqual(self.db.query(PriceHistory).count(), prices_before)
            result=run_all_scrapers('BRM46MB',self.db,stores=['brastemp'],catalog_snapshots={'brastemp':products})
        self.assertEqual(len(result['results']),1)
        self.assertEqual(result['results'][0]['current_price'],1200)
        self.assertIn('idsku=42',result['results'][0]['url'])

    def test_stockout_can_be_reused_only_when_every_destination_confirmed_it_today(self):
        self.products[0]['items'][0]['sellers'][0]['commertialOffer']['IsAvailable']=False
        unavailable=catalog_offers('brastemp','BRM46MB',self.products)[0]
        for row in self.db.query(LeadTimeQuote):
            row.status='out_of_stock';row.shipping_cost=None;row.delivery_max_days=None
            row.item_json=json.dumps({**unavailable,'destination_zip':row.postal_code})
        self.db.commit()
        result=self.check()
        self.assertEqual(len(result['brastemp']),2)
        self.assertTrue(all(row['availability']=='out_of_stock' and row['price'] is None for row in result['brastemp']))

    def test_address_cache_shares_only_city_and_uf_and_returns_independent_results(self):
        from services.delivery_destinations import resolve_cep,_postal_cache
        _postal_cache.clear();self.addCleanup(_postal_cache.clear)
        with patch('services.delivery_destinations.requests.get') as get:
            get.return_value.json.return_value={'uf':'SP','localidade':'São Paulo'}
            for _ in range(47):
                result=resolve_cep('01002020')
                self.assertEqual(result['uf'],'SP');self.assertNotIn('shipping_cost',result)
                result['uf']='XX'
            self.assertEqual(get.call_count,1)

    def test_address_lookup_error_is_not_cached_as_valid_address(self):
        from services.delivery_destinations import resolve_cep,_postal_cache
        _postal_cache.clear();self.addCleanup(_postal_cache.clear)
        with patch('services.delivery_destinations.requests.get') as get:
            get.side_effect=[TimeoutError('timeout'),SimpleNamespace(raise_for_status=lambda:None,
                json=lambda:{'uf':'SP','localidade':'São Paulo'})]
            with self.assertRaises(TimeoutError):resolve_cep('01002020')
            self.assertEqual(resolve_cep('01002020')['uf'],'SP');self.assertEqual(get.call_count,2)


if __name__=='__main__':unittest.main()
