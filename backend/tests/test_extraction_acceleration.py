import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from database import Base
from models.product import CrawlerControl, ExtractionJobItem, ScrapeCursor
from services import extraction_queue as queue
from services import extraction_resources as resources


class ExtractionAccelerationTests(unittest.TestCase):
    def test_initial_shopee_only_batch_reuses_one_session_without_competition(self):
        state = {'active': 0, 'peak': 0}
        lock = threading.Lock()

        def processor(sku, options):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['active'], state['peak'])
            time.sleep(.03)
            with lock:
                state['active'] -= 1
            return {'status': 'updated'}

        queue.configure_extraction_processor(processor)
        job = queue.enqueue_extraction_job(
            ['W10531315', 'W10531316', 'W10531317'], mode='precise', stores=['shopee'],
        )
        self.assertEqual(queue._process_job_batch(job['id']), 'completed')
        self.assertEqual(state['peak'], 1)

    def test_retry_that_became_suspended_does_not_start_another_sku_attempt(self):
        from services.marketplace_intervention import defer_access
        job = queue.enqueue_extraction_job(['W10531315'], mode='precise', stores=['shopee'])
        with self.sessions() as db:
            item = db.query(ExtractionJobItem).filter_by(job_id=job['id']).one()
            item.store_phase = 'marketplaces'
            item.resume_stores_json = json.dumps(['shopee'])
            item_id = item.id
            db.commit()
        defer_access('shopee', 'W10531315', 'Sessão ocupada.')
        self.assertIsNone(queue._mark_item_running(job['id'], item_id))
        with self.sessions() as db:
            item = db.get(ExtractionJobItem, item_id)
            self.assertEqual(item.status, 'waiting_access')
            self.assertEqual(item.attempts, 0)

    def test_cancel_interrupts_quotes_and_preserves_completed_items(self):
        started = threading.Event()
        quotes = []

        def quote(index):
            quotes.append(index)
            started.set()
            resources.interruptible_sleep(10)

        def processor(sku, options):
            list(resources.fair_map(quote, range(200), key=lambda _: 'quote.test', workers=1))
            return {'status': 'updated'}

        job = queue.enqueue_extraction_job(
            ['W10531315', 'W10531316'], mode='precise', stores=['dufrio'], owner_user_id=1,
        )
        first_id = queue._job_item_ids(job['id'])[0][0]
        queue._mark_item_running(job['id'], first_id)
        queue._finish_item(job['id'], first_id, {'status': 'updated', '_offers': [{'sku': 'W10531315'}]})
        queue.configure_extraction_processor(processor)
        with ThreadPoolExecutor(max_workers=1) as pool:
            run = pool.submit(queue._process_job_batch, job['id'])
            self.assertTrue(started.wait(2))
            receipt = queue.cancel_extraction_job(job['id'], owner_user_id=1)
            self.assertTrue(receipt['cancel_requested'])
            self.assertEqual(run.result(timeout=2), 'cancelled')

        final = queue.get_extraction_job(job['id'], include_items=True)
        self.assertEqual(quotes, [0])
        self.assertEqual(final['processed_skus'], 1)
        self.assertEqual(final['error_count'], 0)
        self.assertEqual([item['status'] for item in final['items']], ['updated', 'cancelled'])
        with self.sessions() as db:
            self.assertEqual(json.loads(db.get(ExtractionJobItem, first_id).results_json), [{'sku': 'W10531315'}])
        self.assertEqual(resources.snapshot()['active_requests'], 0)

    def test_cancel_signals_collector_before_waiting_for_database_writer(self):
        started = threading.Event()

        def processor(sku, options):
            with self.sessions() as writer:
                queue._begin_queue_write(writer)
                started.set()
                resources.interruptible_sleep(10)
            return {'status': 'updated'}

        job = queue.enqueue_extraction_job(['W10531315'], mode='quick', owner_user_id=1)
        queue.configure_extraction_processor(processor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            run = pool.submit(queue._process_job_batch, job['id'])
            self.assertTrue(started.wait(2))
            receipt = pool.submit(queue.cancel_extraction_job, job['id'], 1)
            self.assertTrue(receipt.result(timeout=2)['cancel_requested'])
            self.assertEqual(run.result(timeout=2), 'cancelled')

    def test_progress_persists_and_restores_connection_timeout_even_when_writer_is_busy(self):
        from models.product import ExtractionJob
        from sqlalchemy import text
        job=queue.enqueue_extraction_job(['W10531315'],mode='precise',stores=['dufrio'])
        queue._mark_item_running(job['id'],queue._job_item_ids(job['id'])[0][0])
        queue.update_job_progress(job['id'],'W10531315','verified tick')
        with self.sessions() as check:
            self.assertEqual(check.get(ExtractionJob,job['id']).message,'verified tick')
        with self.sessions() as writer:
            writer.execute(text('BEGIN IMMEDIATE'))
            queue.update_job_progress(job['id'],'W10531316','busy tick')
            writer.rollback()
        with self.engine.connect() as first, self.engine.connect() as second:
            self.assertEqual(first.exec_driver_sql('PRAGMA busy_timeout').scalar(),30000)
            self.assertEqual(second.exec_driver_sql('PRAGMA busy_timeout').scalar(),30000)

    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(folder.name)/'queue.db'),
                                    connect_args={'check_same_thread':False, 'timeout':30})
        @event.listens_for(self.engine, 'connect')
        def configure_sqlite(connection, _):
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA busy_timeout=30000')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(lambda: resources.configure(SimpleNamespace(**resources.DEFAULTS)))
        self.addCleanup(lambda: queue.configure_extraction_processor(None))
        handle = patch.object(queue, 'SessionLocal', self.sessions)
        handle.start();self.addCleanup(handle.stop)

    def test_parallel_file_database_has_no_lost_counters_or_destinations_and_keeps_global_barrier(self):
        calls=[];active=peak=0;lock=threading.Lock()
        def processor(sku, options):
            nonlocal active, peak
            with lock:
                active+=1;peak=max(peak,active);calls.append(options['store_phase'])
            time.sleep(.025)
            queue.update_job_progress(options['job_id'],sku,'working '+sku)
            with lock:active-=1
            return {'status':'updated','_offers':[{'sku':sku,'source':store,'store':store,
                'url':'https://example.test/'+sku+'/'+store,'destination_zip':cep}
                for store in options['stores'] for cep in options['postal_codes']]}
        queue.configure_extraction_processor(processor)
        job=queue.enqueue_extraction_job([f'W{10531315+i}' for i in range(12)],mode='precise',
            stores=['dufrio','mercado_livre','shopee'],postal_codes=['01001000','30130003'])
        for _ in range(20):
            if queue._process_job_batch(job['id'])!='queued':break
        state=queue.get_extraction_job(job['id'])
        self.assertGreater(peak,1)
        self.assertEqual(state['processed_skus'],12);self.assertEqual(state['success_count'],12)
        self.assertEqual(state['error_count'],0);self.assertEqual(state['status'],'completed')
        self.assertEqual(calls,['general']*12+['marketplaces']*12)
        with self.sessions() as db:
            for item in db.query(ExtractionJobItem):
                self.assertEqual(len(json.loads(item.results_json)),6)

    def test_finished_worker_refills_before_slowest_material_finishes(self):
        started=set();lock=threading.Lock();slow_done=threading.Event();refilled=threading.Event()
        with self.sessions() as db:
            db.add(CrawlerControl(id=1,precise_sku_workers=2));db.commit()
        def processor(sku, options):
            with lock:started.add(sku)
            if sku=='W10531315':
                refilled.wait(2);slow_done.set()
            if sku=='W10531317' and not slow_done.is_set():refilled.set()
            return {'status':'updated'}
        queue.configure_extraction_processor(processor)
        with patch.object(queue,'DEFER_MARKETPLACES',False):
            job=queue.enqueue_extraction_job(['W10531315','W10531316','W10531317'],mode='precise',stores=['dufrio'])
            queue._process_job_batch(job['id'])
        self.assertTrue(refilled.is_set())

    def test_precise_acceleration_overlaps_phases_and_preserves_every_offer(self):
        skus = ['W10531315', 'W10531316', 'W10531317']
        postals = ['01001000', '30130003']
        stores = ['dufrio', 'mercado_livre', 'shopee']
        snapshots = []
        with patch.object(resources, '_sample_hardware'), patch.dict(resources._hardware, {
            'logical_cpus': 4, 'cpu_percent': 10, 'available_memory_mb': 2048,
        }):
            for accelerated in (False, True):
                with self.subTest(accelerated=accelerated):
                    with self.sessions() as db:
                        control = db.get(CrawlerControl, 1) or CrawlerControl(id=1)
                        control.precise_sku_workers = 2
                        control.hardware_acceleration = accelerated
                        db.add(control)
                        db.commit()
                    market_started = threading.Event()
                    overlap = []

                    def processor(sku, options):
                        phase = options['store_phase']
                        if phase == 'general' and sku == skus[0]:
                            overlap.append(market_started.wait(2 if accelerated else .15))
                        elif phase == 'marketplaces':
                            market_started.set()
                        return {'status': 'updated', '_offers': [
                            dict(sku=sku, source=store, store=store, url=f'https://example.test/{store}/{sku}',
                                 destination_zip=cep, price=35, shipping_cost=8, delivery_max_days=4)
                            for store in options['stores'] for cep in options['postal_codes']
                        ]}

                    queue.configure_extraction_processor(processor)
                    job = queue.enqueue_extraction_job(skus, mode='precise', stores=stores, postal_codes=postals)
                    for _ in range(12):
                        if queue._process_job_batch(job['id']) != 'queued':
                            break
                    self.assertEqual(overlap, [accelerated])
                    state = queue.get_extraction_job(job['id'])
                    self.assertEqual(state['status'], 'completed')
                    self.assertEqual(state['processed_skus'], len(skus))
                    with self.sessions() as db:
                        rows = db.query(ExtractionJobItem).filter_by(job_id=job['id']).all()
                        payload = sorted(json.dumps(offer, sort_keys=True) for row in rows for offer in json.loads(row.results_json))
                    self.assertEqual(len(payload), len(skus) * len(stores) * len(postals))
                    snapshots.append(payload)
        self.assertEqual(*snapshots)

    def test_progressive_queue_keeps_ordinary_results_during_marketplace_verification(self):
        from services.marketplace_intervention import _path, _write, require_access

        with self.sessions() as db:
            db.add(CrawlerControl(id=1, precise_sku_workers=2, hardware_acceleration=True))
            db.commit()
        skus = ['W10531315', 'W10531316']
        waiting = threading.Event()
        checked_after_wait = []

        def processor(sku, options):
            store = options['stores'][0]
            if store == 'shopee':
                require_access('shopee', sku, 'Verificação solicitada')
                waiting.set()
                return {'status': 'partial', '_waiting_stores': ['shopee']}
            if sku == skus[0]:
                checked_after_wait.append(waiting.wait(2))
            return {'status': 'updated', '_offers': [
                dict(sku=sku, source=store, store=store, url=f'https://example.test/{store}/{sku}')
            ]}

        queue.configure_extraction_processor(processor)
        with patch.object(resources, '_sample_hardware'), patch.dict(resources._hardware, {
            'logical_cpus': 4, 'cpu_percent': 10, 'available_memory_mb': 2048,
        }):
            job = queue.enqueue_extraction_job(skus, mode='precise', stores=['dufrio', 'shopee'])
            for _ in range(12):
                status = queue._process_job_batch(job['id'])
                if status != 'queued':
                    break
        self.assertEqual(checked_after_wait, [True])
        self.assertEqual(status, 'waiting_access')
        with self.sessions() as db:
            rows = db.query(ExtractionJobItem).filter_by(job_id=job['id']).all()
            self.assertEqual({row.sku for row in rows}, set(skus))
            for row in rows:
                self.assertEqual(row.status, 'waiting_access')
                self.assertEqual([offer['source'] for offer in json.loads(row.results_json)], ['dufrio'])

        resumed = []

        def after_verification(sku, options):
            resumed.append((sku, options['stores']))
            return {'status': 'updated', '_offers': [
                dict(sku=sku, source='shopee', store='shopee', url=f'https://example.test/shopee/{sku}')
            ]}

        _write(_path('shopee', 'access-request'), {'status': 'verified'})
        queue.configure_extraction_processor(after_verification)
        self.assertEqual(queue._next_job_id(), job['id'])
        for _ in range(12):
            status = queue._process_job_batch(job['id'])
            if status != 'queued':
                break
        self.assertEqual(status, 'completed')
        self.assertCountEqual(resumed, [(sku, ['shopee']) for sku in skus])
        with self.sessions() as db:
            for row in db.query(ExtractionJobItem).filter_by(job_id=job['id']):
                self.assertCountEqual([offer['source'] for offer in json.loads(row.results_json)], ['dufrio','shopee'])

    def test_audit_cursor_cannot_keep_a_precise_run_open(self):
        from services.precise_extraction import run_precise_extraction
        from services.scrape_pagination import browser_retry_due
        with self.sessions() as db:
            db.add(CrawlerControl(id=1,playwright_product_fallback=False,playwright_shipping_fallback=False))
            audit=ScrapeCursor(sku='W10531315',store='dufrio',kind='verified_live',key='audit',
                next_url='https://www.dufrio.com.br/',completed=False,last_status='OK')
            db.add_all([audit,ScrapeCursor(sku='W10531315',store='dufrio',kind='search',key='W10531315',
                next_url='https://www.dufrio.com.br/search',completed=True,last_status='OK',
                last_checked_at=datetime.utcnow(),last_attempted_at=datetime.utcnow())]);db.commit()
            self.assertFalse(browser_retry_due(audit))
            with patch('services.precise_extraction._collect_sources',return_value=([],set())), \
                 patch('services.precise_extraction._discover_store') as discover:
                summary=run_precise_extraction('W10531315',db,stores=['dufrio'],resume=True,
                    collection_after=datetime.utcnow()-timedelta(minutes=1))
            self.assertFalse(summary['more_work']);discover.assert_not_called()
            db.refresh(audit);self.assertTrue(audit.completed)

    def test_shared_fair_budget_is_global_and_slow_host_does_not_block_other_hosts(self):
        resources.configure(SimpleNamespace(**{**resources.DEFAULTS,'quote_workers':6,'concurrent_requests_per_domain':2}))
        counters={};peak={};lock=threading.Lock();fast=threading.Event()
        def work(pair):
            host,index=pair
            with lock:
                counters[host]=counters.get(host,0)+1;peak[host]=max(peak.get(host,0),counters[host])
            # Nested transport must not consume a second global capacity slot.
            with resources.request_slot(host):
                if host=='slow':fast.wait(2)
                else:fast.set()
                time.sleep(.005)
            with lock:counters[host]-=1
            return pair
        def collect(_):
            return [result for _,result in resources.fair_map(work,
                [('slow',i) for i in range(12)]+[('fast',i) for i in range(12)],key=lambda pair:pair[0])]
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs=list(pool.map(collect,range(3)))
        self.assertTrue(fast.is_set());self.assertTrue(all(len(result)==24 for result in outputs))
        self.assertTrue(all(value<=2 for value in peak.values()))
        self.assertEqual(resources.snapshot()['active_requests'],0)

    def test_failed_tasks_release_shared_capacity(self):
        def fail(_):raise ValueError('transport failed')
        with self.assertRaises(ValueError):list(resources.fair_map(fail,range(4),key=lambda _:'failed'))
        self.assertEqual(resources.snapshot()['active_requests'],0)

    def test_forced_precise_round_does_not_load_all_saved_destinations_first(self):
        from routes.products import _search_sku_summary
        from schemas.product import SkuBatchRequest
        payload=SkuBatchRequest(skus=['W10531315'],mode='precise',force_refresh=True)
        with self.sessions() as db,patch('routes.products._find_sku_products') as cached, \
             patch('routes.products.run_precise_extraction',return_value={'results':[],'stores':[],'comparison':[]}) as precise:
            summary,status=_search_sku_summary('W10531315',payload,db)
        self.assertEqual(status,'updated');cached.assert_not_called();precise.assert_called_once()

    def test_scoped_history_query_preserves_every_destination_and_legacy_matching_product(self):
        from models.product import Product,PriceHistory
        from routes.products import _find_sku_products
        with self.sessions() as db:
            current=Product(sku='W10531315',name='Sensor W10531315');legacy=Product(name='Original W10531315')
            other=Product(sku='W11199342',name='Outro material')
            db.add_all([current,legacy,other]);db.flush()
            for product in [current,legacy,other]:
                for cep in ['01001000','30130003']:
                    for day in [1,2]:
                        db.add(PriceHistory(product_id=product.id,price=10+day,store='Dufrio',source='Dufrio',
                            url=f'https://www.dufrio.com.br/p/{product.id}',destination_zip=cep,
                            timestamp=datetime(2026,11,day)))
            db.commit()
            rows=_find_sku_products(db,'W10531315')
            self.assertEqual(len(rows),4)
            self.assertEqual({product.id for product,_ in rows},{current.id,legacy.id})
            self.assertTrue(all(price.price==12 for _,price in rows))

    def test_hardware_adaptation_never_drops_work_and_can_be_disabled(self):
        resources.configure(SimpleNamespace(**resources.DEFAULTS))
        with patch.object(resources,'_sample_hardware'),patch.dict(resources._hardware, {'cpu_percent':95,'available_memory_mb':700}):
            self.assertEqual(resources.hardware_workers(8),1)
            resources.configure(SimpleNamespace(**{**resources.DEFAULTS,'adaptive_parallelism':False}))
            self.assertEqual(resources.hardware_workers(8),8)

    def test_hidden_controls_use_fixed_values_for_legacy_rows_and_old_clients(self):
        from schemas.product import CrawlerControlRequest
        from services.crawler_control import FIXED_SETTINGS, get_control, serialize_control, update_control

        legacy = {field: False for field in FIXED_SETTINGS}
        legacy["discovery_stores_per_round"] = 6
        with self.sessions() as db:
            db.add(CrawlerControl(id=1, **legacy))
            db.commit()
            effective = serialize_control(get_control(db))
            self.assertEqual({field: effective[field] for field in FIXED_SETTINGS}, FIXED_SETTINGS)
            for field in ("adaptive_parallelism", "reuse_public_browser", "discovery_stores_per_round"):
                self.assertEqual(resources.settings()[field], FIXED_SETTINGS[field])

            update_control(db, CrawlerControlRequest(**legacy, api_with_scraping=False, quote_workers=16))
            stored = db.get(CrawlerControl, 1)
            self.assertEqual({field: getattr(stored, field) for field in FIXED_SETTINGS}, FIXED_SETTINGS)
            self.assertFalse(stored.api_with_scraping)
            self.assertEqual(stored.quote_workers, 16)

    def test_opt_in_acceleration_persists_and_respects_hardware_pressure(self):
        from schemas.product import CrawlerControlRequest
        from services.crawler_control import get_control, update_control

        with self.sessions() as db, patch.object(resources, '_sample_hardware'), patch.dict(
            resources._hardware, {'logical_cpus': 16, 'cpu_percent': 15, 'available_memory_mb': 16000}
        ):
            self.assertFalse(get_control(db).hardware_acceleration)
            self.assertEqual(queue._sku_workers(db, True), 8)
            self.assertEqual(resources.snapshot()['io_budget'], 12)
            update_control(db, CrawlerControlRequest(hardware_acceleration=True))
            self.assertTrue(get_control(db).hardware_acceleration)
            self.assertEqual(resources.effective_workers('precise_sku_workers'), 24)
            self.assertEqual(queue._sku_workers(db, True), 8)
            self.assertEqual(queue._sku_workers(db, False), 4)
            self.assertEqual(resources.snapshot()['io_budget'], 64)
            with patch.dict(resources._hardware, {'cpu_percent': 87}):
                self.assertEqual(queue._sku_workers(db, True), 8)
                self.assertEqual(resources.snapshot()['io_budget'], 12)
            with patch.dict(resources._hardware, {'available_memory_mb': 700}):
                self.assertEqual(resources.snapshot()['io_budget'], 1)

    def test_accelerated_io_keeps_store_and_shopee_limits(self):
        with patch.object(resources, '_sample_hardware'), patch.dict(
            resources._hardware, {'logical_cpus': 16, 'cpu_percent': 15, 'available_memory_mb': 16000}
        ):
            resources.configure(SimpleNamespace(**{**resources.DEFAULTS, 'hardware_acceleration': True}))
            self.assertTrue(resources._reserve('shopee'))
            self.assertFalse(resources._reserve('shopee'))
            self.assertTrue(resources._reserve('amazon'))
            self.assertTrue(resources._reserve('amazon'))
            self.assertFalse(resources._reserve('amazon'))
            resources._release('shopee')
            resources._release('amazon')
            resources._release('amazon')

    def test_unrecoverable_search_mismatch_does_not_remain_an_actionable_page(self):
        from services.scrape_pagination import browser_retry_due
        cursor=ScrapeCursor(kind='search',store='refrigeracao_mota',sku='300001610',key='name',
            next_url='https://www.refrigeracaomota.com.br/page/2/',completed=False,
            last_status='SEARCH_MISMATCH',last_attempted_at=datetime.utcnow())
        self.assertFalse(browser_retry_due(cursor))

    def test_fresh_page_is_parsed_once_but_all_postals_are_quoted_independently(self):
        from services.multi_postal_extraction import collect_destinations
        from services import offer_enrichment as enrichment
        from models.product import LeadTimeQuote
        from unittest.mock import MagicMock
        item=dict(sku='W10531315',name='Sensor W10531315',brand='Brastemp',category='Sensor',image_url=None,
            price=20,source='Dufrio',store='Dufrio',url='https://www.dufrio.com.br/sensor-w10531315',availability='in_stock')
        html='<script type="application/ld+json">'+json.dumps({'@context':'https://schema.org','@type':'Product',
            'name':item['name'],'sku':item['sku'],'offers':{'@type':'Offer','price':20,'priceCurrency':'BRL'}})+'</script>'
        response=SimpleNamespace(status_code=200,text=html,url=item['url'])
        session=MagicMock();session.__enter__.return_value=session;session.get.return_value=response
        ceps=[f'{10000000+i:08d}' for i in range(27)]
        def shipping(session,soup,url,cep):
            return {'shipping_cost':ceps.index(cep)+1,'delivery_max_days':ceps.index(cep)+2,'logistics_source':'live_fixture'}
        with self.sessions() as db,patch('services.delivery_destinations.resolve_cep',return_value={'uf':'SP'}), \
             patch.object(enrichment,'_session',return_value=session), \
             patch.object(enrichment,'_dufrio_shipping',side_effect=shipping) as quote, \
             patch.object(enrichment,'_structured_offer',wraps=enrichment._structured_offer) as parse:
            results=collect_destinations(db,item['sku'],[item],ceps)
            self.assertEqual(len(results),27);self.assertEqual(quote.call_count,27);self.assertEqual(parse.call_count,1)
            self.assertEqual(session.get.call_count,1)
            self.assertEqual(db.query(LeadTimeQuote).filter_by(status='quoted').count(),27)
            for result in results:
                index=ceps.index(result['destination_zip'])
                self.assertEqual(result['shipping_cost'],index+1)
                self.assertEqual(result['delivery_max_days'],index+2)

    def test_pause_prevents_new_workers_and_checkpoint_recovery_preserves_payload(self):
        queue.configure_extraction_processor(lambda sku,options:{'status':'updated'})
        job=queue.enqueue_extraction_job(['W10531315','W11199342'],mode='precise',owner_user_id=1)
        queue.set_extraction_paused(job['id'],1,True)
        self.assertEqual(queue._process_job_batch(job['id']),'paused')
        with self.sessions() as db:
            item=db.query(ExtractionJobItem).filter_by(job_id=job['id']).first()
            item.status='running';item.results_json='[{"sku":"W10531315"}]';db.commit()
        queue._recover_interrupted_jobs()
        with self.sessions() as db:
            item=db.query(ExtractionJobItem).filter_by(job_id=job['id']).first()
            self.assertEqual(item.status,'queued');self.assertEqual(len(json.loads(item.results_json)),1)
        self.assertEqual(queue.get_extraction_job(job['id'])['status'],'paused')
