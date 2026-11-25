import json
import unittest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from database import Base
from models.product import ExtractionJob, ExtractionJobItem
from services import extraction_queue as queue


class StagedExtractionTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        for name, value in [('SessionLocal', self.sessions), ('SKU_PAUSE_SECONDS', 0), ('PRECISE_SKU_SLICE_ROUNDS', 1), ('DEFER_MARKETPLACES', True)]:
            handle = patch.object(queue, name, value); handle.start(); self.addCleanup(handle.stop)
        self.original = queue._processor
        self.addCleanup(lambda: queue.configure_extraction_processor(self.original))
        self.addCleanup(self.engine.dispose)

    def drain(self, job):
        for _ in range(30):
            status = queue._process_job_batch(job['id'])
            if status != 'queued':
                return status
        self.fail('Fila não terminou em 30 passagens')

    @staticmethod
    def result(sku, options):
        return {'status':'updated', '_offers':[
            {'sku':sku,'source':store,'store':store,'url':'https://example.test/'+store,'destination_zip':cep}
            for store in options['stores'] for cep in options['postal_codes']], 'message':'OK'}

    def test_all_skus_and_postals_finish_ordinary_stores_before_marketplaces_in_both_modes(self):
        for mode in ['quick', 'precise']:
            with self.subTest(mode=mode):
                calls=[]
                def process(sku, options):
                    calls.append((sku, options['store_phase'], options['stores']))
                    return self.result(sku, options)
                queue.configure_extraction_processor(process)
                job=queue.enqueue_extraction_job(['BRM46MB','BLF61AR','IF44'], mode=mode,
                    stores=['brastemp','mercado_livre','shopee'], postal_codes=['01002020','30130003'])
                self.assertEqual(self.drain(job), 'completed')
                self.assertEqual([phase for _,phase,_ in calls], ['general']*3+['marketplaces']*3)
                self.assertTrue(all(stores==['brastemp'] for _,_,stores in calls[:3]))
                self.assertTrue(all(set(stores)=={'mercado_livre','shopee'} for _,_,stores in calls[3:]))
                saved=queue.get_extraction_job(job['id'])
                self.assertEqual(saved['processed_skus'],3); self.assertEqual(saved['success_count'],3)
                with self.sessions() as db:
                    for item in db.query(ExtractionJobItem).filter_by(job_id=job['id']):
                        self.assertEqual(len(json.loads(item.results_json)),6)

    def test_started_precise_material_resumes_before_starting_more_materials(self):
        job=queue.enqueue_extraction_job(['W10531315','W11199342'],mode='precise',stores=['dufrio'])
        with self.sessions() as db:
            item=db.query(ExtractionJobItem).filter_by(job_id=job['id'],sku='W10531315').one()
            item.attempts=4;db.commit();expected=item.id
        with patch.object(queue,'PRECISE_SKU_SLICE_ROUNDS',12):
            ids,precise=queue._job_item_ids(job['id'])
        self.assertTrue(precise);self.assertEqual(ids,[expected])

    def test_ready_general_phase_does_not_wait_for_deferred_marketplaces(self):
        from datetime import datetime
        from models.product import ScrapeCursor
        job=queue.enqueue_extraction_job(['W10531315','W11199342'],mode='precise',stores=['dufrio','shopee'])
        with self.sessions() as db:
            item=db.query(ExtractionJobItem).filter_by(job_id=job['id'],sku='W10531315').one()
            item.attempts=99;db.add(ScrapeCursor(sku=item.sku,store='dufrio',kind='search',key=item.sku,
                initial_url='https://www.dufrio.com.br/search',next_url='https://www.dufrio.com.br/search',completed=True,last_checked_at=datetime.utcnow()))
            db.commit();expected=item.id
        ids,_=queue._job_item_ids(job['id'])
        self.assertEqual(ids,[expected])

    def test_refreshed_known_store_counts_as_checked_without_redundant_search(self):
        from datetime import datetime
        from models.product import ProductSource
        job=queue.enqueue_extraction_job(['W10531315','W11199342'],mode='precise',stores=['dufrio','shopee'])
        with self.sessions() as db:
            item=db.query(ExtractionJobItem).filter_by(job_id=job['id'],sku='W10531315').one()
            item.attempts=99;db.add(ProductSource(sku=item.sku,store='dufrio',seller='Dufrio',
                url='https://www.dufrio.com.br/sensor',active=True,validation_status='ok',last_attempted_at=datetime.utcnow()))
            db.commit();expected=item.id
        ids,_=queue._job_item_ids(job['id'])
        self.assertEqual(ids,[expected])

    def test_newly_finished_material_returns_turn_to_older_partial_material(self):
        from datetime import datetime,timedelta
        job=queue.enqueue_extraction_job(['W10531315','W11199342','W11198953'],mode='precise',stores=['dufrio','shopee'])
        with self.sessions() as db:
            old=db.query(ExtractionJobItem).filter_by(job_id=job['id'],sku='W10531315').one()
            old.attempts=99;old.started_at=datetime.utcnow()-timedelta(minutes=5)
            finished=db.query(ExtractionJobItem).filter_by(job_id=job['id'],sku='W11199342').one()
            finished.attempts=5;finished.started_at=datetime.utcnow();finished.store_phase='marketplaces'
            db.commit();expected=old.id
        ids,_=queue._job_item_ids(job['id'])
        self.assertEqual(ids,[expected])

    def test_restart_upgrades_old_pending_job_without_repeating_completed_skus_or_losing_results(self):
        with self.sessions() as db:
            job=ExtractionJob(id='old-job',request_key='old',status='running',mode='quick',scope='all',
                total_skus=2,processed_skus=1,success_count=1,stores_json='["brastemp","shopee"]')
            old={'sku':'BLF61AR','source':'Brastemp','store':'Brastemp','url':'https://example.test/old','destination_zip':'01002020'}
            job.items=[ExtractionJobItem(sku='BRM46MB',position=0,status='completed',attempts=1),
                ExtractionJobItem(sku='BLF61AR',position=1,status='running',attempts=16,
                    waiting_stores_json='["shopee"]',resume_stores_json='["shopee"]',results_json=json.dumps([old]))]
            db.add(job);db.commit()
        queue._recover_interrupted_jobs()
        calls=[]
        def process(sku,options):
            calls.append((sku,options['stores'],options['attempt']))
            return self.result(sku,{**options,'postal_codes':['01002020']})
        queue.configure_extraction_processor(process)
        self.assertEqual(self.drain({'id':'old-job'}),'completed')
        self.assertEqual(calls,[('BLF61AR',['shopee'],17)])
        with self.sessions() as db:
            pending=db.query(ExtractionJobItem).filter_by(job_id='old-job',sku='BLF61AR').one()
            self.assertEqual(len(json.loads(pending.results_json)),2)
            self.assertEqual(db.get(ExtractionJob,'old-job').processed_skus,2)

    def test_suspended_partial_marketplace_waits_instead_of_retrying_while_other_skus_progress(self):
        from services.marketplace_intervention import require_access
        calls=[]
        def process(sku,options):
            calls.append((sku,options['store_phase']))
            result=self.result(sku,options)
            if options['store_phase']=='marketplaces':
                require_access('shopee',sku,'Verificação na busca')
                result.update(status='partial',_waiting_stores=['shopee'])
            return result
        queue.configure_extraction_processor(process)
        job=queue.enqueue_extraction_job(['BRM46MB','BLF61AR'],mode='precise',stores=['brastemp','shopee'])
        self.assertEqual(self.drain(job),'waiting_access')
        self.assertEqual(queue.get_extraction_job(job['id'])['processed_skus'],0)
        self.assertIsNone(queue._next_job_id())
        count=len(calls)
        queue._recover_interrupted_jobs()
        self.assertIsNone(queue._next_job_id());self.assertEqual(len(calls),count)
        from services.marketplace_intervention import _path, _write
        _write(_path('shopee','access-request'),{'status':'verified'})
        queue.configure_extraction_processor(self.result)
        self.assertEqual(queue._next_job_id(),job['id'])
        self.assertEqual(self.drain(job),'completed')

    def test_pause_between_stages_is_preserved_and_progress_counts_actual_phase(self):
        queue.configure_extraction_processor(self.result)
        job=queue.enqueue_extraction_job(['BLF61AR'],mode='precise',stores=['brastemp','shopee'],owner_user_id=7)
        self.assertEqual(queue._process_job_batch(job['id']),'queued')
        halfway=queue.get_extraction_job(job['id'])
        self.assertEqual(halfway['processed_skus'],0)
        self.assertGreater(halfway['work_progress_percent'],0)
        queue.set_extraction_paused(job['id'],7,True)
        queue._recover_interrupted_jobs();self.assertIsNone(queue._next_job_id())
        queue.set_extraction_paused(job['id'],7,False)
        self.assertEqual(self.drain(job),'completed')


if __name__=='__main__':unittest.main()
