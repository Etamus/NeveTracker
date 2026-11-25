import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import ExtractionJob, ExtractionJobItem, KnownSku
from routes.products import _sku_batch_result, delete_local_sku, process_extraction_queue_item
from services import extraction_queue


class ExtractionQueueTests(unittest.TestCase):
    def test_observed_material_progress_uses_only_requested_skus_stores_and_postals(self):
        from services.scraper_service import _save_result
        sku=self._skus(1)[0]
        job=extraction_queue.enqueue_extraction_job([sku],mode='precise',stores=['dufrio'],postal_codes=['01001000'])
        with self.session_factory() as db:
            item=dict(sku=sku,name=sku,brand=None,category=None,image_url=None,price=100,store='Dufrio',source='Dufrio',url='https://www.dufrio.com.br/produto',destination_zip='01001000')
            _save_result(item,db)
            _save_result({**item,'destination_zip':'30130003'},db)
            _save_result({**item,'source':'Magazine Luiza','store':'Outra loja','url':'https://www.magazineluiza.com.br/produto/p/abc'},db)
            _save_result({**item,'sku':'W11199342','url':'https://www.dufrio.com.br/outra-peca'},db)
            db.commit()
        state=extraction_queue.get_extraction_job(job['id'])
        self.assertEqual(state['observed_skus'],1)
        self.assertEqual(state['observations_saved'],1)
        self.assertEqual(state['processed_skus'],0)
        from sqlalchemy import event
        with self.session_factory() as db:
            queries = []
            connection = db.connection()
            def capture(_conn, _cursor, sql, _values, _context, _executemany):
                queries.append(sql.lower())
            event.listen(connection, 'before_cursor_execute', capture)
            try:
                lightweight = extraction_queue.serialize_job(db.get(ExtractionJob, job['id']), include_metrics=False)
            finally:
                event.remove(connection, 'before_cursor_execute', capture)
        self.assertEqual(lightweight['processed_skus'], state['processed_skus'])
        self.assertEqual(lightweight['store_steps_done'], state['store_steps_done'])
        self.assertNotIn('quotes_checked', lightweight)
        self.assertFalse(any('lead_time_quotes' in sql or 'offer_observations' in sql for sql in queries))

    def test_progress_never_commits_parent_pending_changes(self):
        job=extraction_queue.enqueue_extraction_job(self._skus(1),mode='precise')
        with self.session_factory() as parent:
            row=parent.get(ExtractionJob,job['id'])
            row.status='running';parent.commit()
            row.message='Uncommitted collector data'
            parent.flush()
            extraction_queue.update_job_progress(job['id'],self._skus(1)[0],'UI tick',db=parent)
            parent.rollback()
        with self.session_factory() as check:
            self.assertNotEqual(check.get(ExtractionJob,job['id']).message,'Uncommitted collector data')

    def test_material_with_all_stores_checked_is_finalized_before_new_material(self):
        from datetime import datetime
        from models.product import ScrapeCursor
        from services.store_registry import TARGET_STORES
        job = extraction_queue.enqueue_extraction_job(self._skus(2), mode="precise")
        with self.session_factory() as db:
            item = db.query(ExtractionJobItem).filter_by(job_id=job["id"], sku=self._skus(2)[1]).one()
            item.attempts = 2
            for store in TARGET_STORES:
                db.add(ScrapeCursor(sku=item.sku, store=store['key'], kind='search', key=item.sku,
                                   next_url='https://example.test/search', completed=True, last_checked_at=datetime.utcnow()))
            db.commit()
            ready_id = item.id
        ids, precise = extraction_queue._job_item_ids(job["id"])
        self.assertTrue(precise)
        self.assertEqual(ids, [ready_id])
        extraction_queue.configure_extraction_processor(self._processor)
        extraction_queue._process_job_batch(job["id"])
        result = extraction_queue.get_extraction_job(job["id"])
        self.assertEqual(result["processed_skus"], 1)

    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        control = patch('services.marketplace_intervention._path', side_effect=lambda store, kind: Path(folder.name) / f'{store}-{kind}.json')
        control.start(); self.addCleanup(control.stop)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.session_patch = patch.object(
            extraction_queue, "SessionLocal", self.session_factory
        )
        self.pause_patch = patch.object(extraction_queue, "SKU_PAUSE_SECONDS", 0)
        self.session_patch.start()
        # Keep regression coverage for persisted jobs from the legacy runner;
        # the staged pipeline is exercised separately with its default enabled.
        legacy = patch.object(extraction_queue, 'DEFER_MARKETPLACES', False)
        legacy.start(); self.addCleanup(legacy.stop)
        self.pause_patch.start()
        self.original_processor = extraction_queue._processor

    def tearDown(self):
        extraction_queue.configure_extraction_processor(self.original_processor)
        self.pause_patch.stop()
        self.session_patch.stop()
        self.engine.dispose()

    @staticmethod
    def _skus(count):
        return [f"W{10500000 + index}" for index in range(count)]

    def test_access_wait_continues_other_skus_and_resumes_each_store_without_losing_offers(self):
        import json
        from services.marketplace_intervention import require_access, _write, _path
        calls=[]
        def process(sku, options):
            calls.append((sku,options['stores']))
            initial=options['attempt']==1
            if initial:
                require_access('shopee',sku,'Login necessário')
                require_access('mercado_livre',sku,'Verificação necessária')
            store='Brastemp' if initial else options['stores'][0]
            return {**self._processor(sku,options),'_waiting_stores':['shopee','mercado_livre'] if initial else [],
                    '_offers':[dict(sku=sku,source=store,store=store,url='https://example.test/'+store,destination_zip='01002020')]}
        extraction_queue.configure_extraction_processor(process)
        job=extraction_queue.enqueue_extraction_job(self._skus(3),mode='precise',stores=['brastemp','mercado_livre','shopee'])
        for _ in range(3):extraction_queue._process_job_batch(job['id'])
        result=extraction_queue.get_extraction_job(job['id'],include_items=True)
        self.assertEqual(result['status'],'waiting_access');self.assertEqual(result['waiting_skus'],3)
        self.assertEqual(result['visited_skus'],3);self.assertEqual(result['processed_skus'],0)
        self.assertIsNone(extraction_queue._next_job_id())
        # Persistence recovery must not silently discard a user's access wait.
        extraction_queue._recover_interrupted_jobs()
        self.assertIsNone(extraction_queue._next_job_id())
        _write(_path('mercado_livre','access-request'),{'status':'verified'})
        self.assertEqual(extraction_queue._next_job_id(),job['id'])
        for _ in range(3):extraction_queue._process_job_batch(job['id'])
        self.assertEqual([stores for _,stores in calls[3:]], [['mercado_livre']]*3)
        self.assertEqual(extraction_queue.get_extraction_job(job['id'])['waiting_skus'],3)
        self.assertIsNone(extraction_queue._next_job_id())
        _write(_path('shopee','access-request'),{'status':'verified'})
        self.assertEqual(extraction_queue._next_job_id(),job['id'])
        for _ in range(3):extraction_queue._process_job_batch(job['id'])
        result=extraction_queue.get_extraction_job(job['id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['processed_skus'],3)
        self.assertEqual(result['success_count'],3);self.assertEqual(result['waiting_access'],[])
        self.assertEqual([stores for _,stores in calls[6:]], [['shopee']]*3)
        with self.session_factory() as db:
            for item in db.query(ExtractionJobItem):
                self.assertEqual(len(json.loads(item.results_json)),3)

    def test_waiting_job_keeps_exclusive_lock_and_owner_can_cancel_all_pending_items(self):
        from services.marketplace_intervention import require_access
        require_access('shopee','BLF61AR','Login')
        extraction_queue.configure_extraction_processor(lambda sku,options:{**self._processor(sku,options),'_waiting_stores':['shopee']})
        job=extraction_queue.enqueue_extraction_job(self._skus(2),owner_user_id=1)
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'waiting_access')
        with self.assertRaises(HTTPException) as conflict:
            extraction_queue.enqueue_extraction_job(['BLF61AR'],owner_user_id=2)
        self.assertEqual(conflict.exception.status_code,409)
        with self.assertRaises(HTTPException):extraction_queue.cancel_extraction_job(job['id'],owner_user_id=2)
        cancelled = extraction_queue.cancel_extraction_job(job['id'], owner_user_id=1)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(extraction_queue._next_job_id())
        result=extraction_queue.get_extraction_job(job['id'],include_items=True)
        self.assertTrue(all(item['status']=='cancelled' for item in result['items']))
        self.assertEqual(result['waiting_access'],[])

    def test_disabling_login_releases_wait_as_public_only_work(self):
        from services.marketplace_intervention import require_access,configure
        require_access('shopee','BLF61AR','Login')
        extraction_queue.configure_extraction_processor(lambda sku,options:{**self._processor(sku,options),'_waiting_stores':['shopee'] if options['attempt']==1 else []})
        job=extraction_queue.enqueue_extraction_job(self._skus(1),stores=['shopee'])
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'waiting_access')
        configure('shopee',False)
        self.assertEqual(extraction_queue._next_job_id(),job['id'])
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'completed')

    def test_access_buttons_are_owner_only_and_wait_is_visible_before_round_finishes(self):
        from services.marketplace_intervention import require_access
        from routes.products import resolve_job_access
        from models.auth import AuthUser
        job=extraction_queue.enqueue_extraction_job(self._skus(1),owner_user_id=7,stores=['shopee'])
        require_access('shopee',self._skus(1)[0],'Login')
        self.assertEqual(extraction_queue.get_extraction_job(job['id'])['waiting_access'][0]['store'],'shopee')
        with self.session_factory() as db,patch('services.marketplace_session.start_validation',return_value={'status':'opening'}) as start:
            with self.assertRaises(HTTPException) as denied:
                resolve_job_access(job['id'],'shopee','open',AuthUser(id=8,username='OTHER',role='administrator'),db)
            self.assertEqual(denied.exception.status_code,403);start.assert_not_called()
            result=resolve_job_access(job['id'],'shopee','open',AuthUser(id=7,username='OWNER',role='governor'),db)
            self.assertEqual(result['status'],'opening');start.assert_called_once_with('shopee')

    def test_quick_recovery_keeps_fast_mode_for_only_pending_marketplace(self):
        from datetime import datetime
        summary=dict(results=[],stores=[],comparison=[],fresh_count=0,processed_count=0)
        with patch('routes.products.SessionLocal',self.session_factory),patch('routes.products._search_sku_summary',return_value=(summary,'updated')) as search:
            result=process_extraction_queue_item('BLF61AR',dict(mode='quick',attempt=2,stores=['mercado_livre'],remaining_waiting_stores=['mercado_livre'],job_created_at=datetime.utcnow()))
        payload=search.call_args.args[1]
        self.assertEqual(payload.mode,'quick');self.assertEqual(payload.stores,['mercado_livre'])
        self.assertFalse(search.call_args.kwargs['resume']);self.assertEqual(result['_waiting_stores'],[])

    def test_pause_survives_restart_and_resume_keeps_completed_skus_and_round_attempts(self):
        job=extraction_queue.enqueue_extraction_job(self._skus(3),mode='precise',owner_user_id=7)
        calls=[]
        def process(sku,options):
            calls.append((sku,options['attempt']))
            if len(calls)==2:
                extraction_queue.set_extraction_paused(job['id'],7,True)
                return {**self._processor(sku,options),'status':'partial'}
            return self._processor(sku,options)
        extraction_queue.configure_extraction_processor(process)
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'queued')
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'paused')
        paused=extraction_queue.get_extraction_job(job['id'])
        self.assertEqual(paused['processed_skus'],1);self.assertEqual(paused['success_count'],1)
        extraction_queue._recover_interrupted_jobs()
        self.assertIsNone(extraction_queue._next_job_id())
        with self.assertRaises(HTTPException):extraction_queue.set_extraction_paused(job['id'],8,False)
        with self.assertRaises(HTTPException):extraction_queue.enqueue_extraction_job(['BLF61AR'],owner_user_id=8)
        extraction_queue.set_extraction_paused(job['id'],7,False)
        self.assertEqual(extraction_queue._next_job_id(),job['id'])
        while extraction_queue.get_extraction_job(job['id'])['status']=='queued':extraction_queue._process_job_batch(job['id'])
        final=extraction_queue.get_extraction_job(job['id'])
        self.assertEqual(final['status'],'completed');self.assertEqual(final['processed_skus'],3)
        self.assertEqual(calls.count((self._skus(3)[0],1)),1)
        self.assertIn((self._skus(3)[1],2),calls)

    def test_paused_pending_store_is_not_resumed_by_validation_until_owner_resumes(self):
        from services.marketplace_intervention import require_access,_write,_path
        require_access('shopee','BLF61AR','CAPTCHA')
        extraction_queue.configure_extraction_processor(lambda sku,options:{**self._processor(sku,options),'_waiting_stores':['shopee']})
        job=extraction_queue.enqueue_extraction_job(self._skus(1),owner_user_id=7,stores=['shopee'])
        extraction_queue._process_job_batch(job['id'])
        extraction_queue.set_extraction_paused(job['id'],7,True)
        _write(_path('shopee','access-request'),{'status':'verified'})
        self.assertIsNone(extraction_queue._next_job_id())
        self.assertEqual(extraction_queue.get_extraction_job(job['id'])['status'],'paused')
        cancelled = extraction_queue.cancel_extraction_job(job['id'], owner_user_id=7)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertIsNone(extraction_queue._next_job_id())

    def test_transport_retry_is_visible_without_login_buttons_and_resumes_after_backoff(self):
        from datetime import datetime,timedelta,timezone
        from services.marketplace_intervention import defer_access,_write,_path
        def process(sku,options):
            if options['attempt']==1:defer_access('shopee',sku,'Aba ocupada')
            return {**self._processor(sku,options),'_waiting_stores':['shopee'] if options['attempt']==1 else []}
        extraction_queue.configure_extraction_processor(process)
        job=extraction_queue.enqueue_extraction_job(self._skus(1),owner_user_id=7,stores=['shopee'])
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'waiting_access')
        self.assertEqual(extraction_queue.get_extraction_job(job['id'])['waiting_access'][0]['status'],'retrying')
        self.assertIsNone(extraction_queue._next_job_id())
        _write(_path('shopee','access-request'),{'status':'retrying','retry_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()})
        self.assertEqual(extraction_queue._next_job_id(),job['id'])
        self.assertEqual(extraction_queue._process_job_batch(job['id']),'completed')

    def test_exhausted_transport_closes_with_occurrences_and_preserves_offers(self):
        import json
        from datetime import datetime, timedelta, timezone
        from services.marketplace_intervention import MAX_TRANSPORT_RETRIES, _path, _write

        def process(sku, options):
            return {**self._processor(sku, options), '_waiting_stores': ['shopee'],
                    '_offers': [{'sku': sku, 'url': 'https://example.test/offer'}]}

        extraction_queue.configure_extraction_processor(process)
        job = extraction_queue.enqueue_extraction_job(self._skus(1), stores=['shopee'], owner_user_id=7)
        self.assertEqual(extraction_queue._process_job_batch(job['id']), 'waiting_access')
        _write(_path('shopee', 'access-request'), {
            'status': 'retrying', 'attempts': MAX_TRANSPORT_RETRIES,
            'retry_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        })
        self.assertIsNone(extraction_queue._next_job_id())
        final = extraction_queue.get_extraction_job(job['id'], include_items=True)
        self.assertEqual(final['status'], 'completed_with_errors')
        self.assertEqual(final['error_count'], 1)
        self.assertEqual(final['processed_skus'], 1)
        self.assertFalse(final['cancel_requested'])
        with self.session_factory() as db:
            item = db.query(ExtractionJobItem).filter_by(job_id=job['id']).one()
            self.assertEqual(len(json.loads(item.results_json)), 1)

    @staticmethod
    def _processor(sku, _options):
        return {
            "sku": sku,
            "status": "updated",
            "message": "Processado.",
            "results_count": 1,
            "stores_count": 1,
            "comparison_count": 1,
            "in_database": True,
            "offer_count": 1,
            "store_count": 1,
        }

    def test_processes_65_skus_in_batches_of_30(self):
        extraction_queue.configure_extraction_processor(self._processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(65))

        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
        first = extraction_queue.get_extraction_job(created["id"])
        self.assertEqual(first["processed_skus"], 30)
        self.assertEqual(first["batches_completed"], 1)

        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
        second = extraction_queue.get_extraction_job(created["id"])
        self.assertEqual(second["processed_skus"], 60)
        self.assertEqual(second["batches_completed"], 2)

        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "completed")
        completed = extraction_queue.get_extraction_job(created["id"], include_items=True)
        self.assertEqual(completed["processed_skus"], 65)
        self.assertEqual(completed["success_count"], 65)
        self.assertEqual(completed["batches_completed"], 3)
        self.assertEqual(len(completed["items"]), 65)

    def test_precise_item_finishes_its_remaining_rounds_before_next_sku(self):
        calls = []

        def processor(sku, options):
            calls.append(options["attempt"])
            result = self._processor(sku, options)
            result["status"] = "partial" if len(calls) == 1 else "updated"
            return result

        extraction_queue.configure_extraction_processor(processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(1), mode="precise")
        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "completed")
        completed = extraction_queue.get_extraction_job(created["id"], include_items=True)
        self.assertEqual(completed["processed_skus"], 1)
        self.assertEqual(completed["visited_skus"], 1)
        self.assertEqual(completed["items"][0]["status"], "updated")
        self.assertEqual(calls, [1, 2])

    def test_large_precise_job_increments_completed_skus_early(self):
        def processor(sku, options):
            return {
                **self._processor(sku, options),
                "status": "partial" if options["attempt"] == 1 else "updated",
            }
        extraction_queue.configure_extraction_processor(processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(95), mode="precise")
        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
        first = extraction_queue.get_extraction_job(created["id"])
        self.assertEqual((first["processed_skus"], first["visited_skus"]), (1, 1))
        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
        second = extraction_queue.get_extraction_job(created["id"])
        self.assertEqual((second["processed_skus"], second["visited_skus"]), (2, 2))

    def test_repeated_active_request_reuses_same_job(self):
        skus = self._skus(31)
        first = extraction_queue.enqueue_extraction_job(skus, kind="database")
        second = extraction_queue.enqueue_extraction_job(skus, kind="database")

        self.assertEqual(first["id"], second["id"])

    def test_job_uses_reference_postal_code_when_not_supplied(self):
        created = extraction_queue.enqueue_extraction_job(["W10531315"], postal_code=None)
        with self.session_factory() as db:
            job = db.query(ExtractionJob).filter_by(id=created["id"]).one()
            self.assertEqual(job.postal_code, "01001000")

    def test_precise_single_sku_continues_while_pages_remain(self):
        summary = {
            "more_work": True, "pending_sources": 5,
            "coverage_by_store": [], "failed_sources": 0,
            "failed_sources_without_price": 0,
        }
        options = {
            "force_refresh": True, "scope": "all", "mode": "precise", "kind": "single",
            "postal_code": "01001000", "job_created_at": None,
        }
        with patch("routes.products.SessionLocal", self.session_factory), \
             patch("routes.products._search_sku_summary", return_value=(summary, "updated")), \
             patch("routes.products._sku_batch_result", side_effect=lambda *_args: {"status": "updated", "message": "Coleta."}):
            single = process_extraction_queue_item("W10531315", {**options, "attempt": 1})
            batch_first = process_extraction_queue_item("W10531315", {**options, "kind": "database", "attempt": 1})
            batch_second = process_extraction_queue_item("W10531315", {**options, "kind": "database", "attempt": 2})
        self.assertEqual(single["status"], "partial")
        self.assertEqual(batch_first["status"], "partial")
        self.assertEqual(batch_second["status"], "partial")

    def test_partial_sku_does_not_starve_other_skus(self):
        calls = []
        def processor(sku, options):
            calls.append(sku)
            result = self._processor(sku, options)
            result["status"] = "partial" if sku == self._skus(1)[0] else "updated"
            return result
        extraction_queue.configure_extraction_processor(processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(31), mode="precise")
        with patch.object(extraction_queue, "PRECISE_SKU_SLICE_ROUNDS", 3):
            self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
            self.assertEqual(calls, [self._skus(1)[0]] * 3)
            self.assertEqual(extraction_queue._process_job_batch(created["id"]), "queued")
        self.assertEqual(calls[-1], self._skus(31)[1])
        job = extraction_queue.get_extraction_job(created["id"])
        self.assertEqual(job["processed_skus"], 1)
        self.assertEqual(job["visited_skus"], 2)

    def test_precise_cancel_stops_between_rounds(self):
        calls = []
        def processor(sku, options):
            calls.append(sku)
            extraction_queue.cancel_extraction_job(options["job_id"], owner_user_id=1)
            return {**self._processor(sku, options), "status": "partial"}
        extraction_queue.configure_extraction_processor(processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(2), mode="precise", owner_user_id=1)
        self.assertEqual(extraction_queue._process_job_batch(created["id"]), "cancelled")
        self.assertEqual(len(calls), 1)
        self.assertEqual(extraction_queue.get_extraction_job(created["id"])["processed_skus"], 0)

    def test_partial_rounds_advance_activity_without_claiming_sku_completion(self):
        from datetime import timedelta
        from models.product import ScrapeCursor
        extraction_queue.configure_extraction_processor(lambda sku, options: {**self._processor(sku, options), 'status': 'partial'})
        created = extraction_queue.enqueue_extraction_job(self._skus(2), mode='precise')
        with patch.object(extraction_queue, 'PRECISE_SKU_SLICE_ROUNDS', 1):
            extraction_queue._process_job_batch(created['id'])
        first = extraction_queue.get_extraction_job(created['id'])
        self.assertEqual(first['processed_skus'], 0)
        self.assertEqual(first['started_rounds'], 1)
        self.assertGreater(first['work_progress_percent'], 0)
        with self.session_factory() as db:
            job = db.query(ExtractionJob).filter_by(id=created['id']).one()
            for store, age in [('amazon', 1), ('magalu', 1), ('dufrio', -1)]:
                db.add(ScrapeCursor(sku=self._skus(2)[0], store=store, kind='search', key='test',
                                   next_url='https://example.test/', last_checked_at=job.created_at + timedelta(seconds=age)))
            db.commit()
        advanced = extraction_queue.get_extraction_job(created['id'])
        self.assertEqual(advanced['store_steps_done'], 2)
        self.assertGreater(advanced['work_progress_percent'], first['work_progress_percent'])
        self.assertLess(advanced['work_progress_percent'], 100)

    def test_finished_precise_job_reaches_full_activity_progress(self):
        extraction_queue.configure_extraction_processor(self._processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(1), mode='precise')
        extraction_queue._process_job_batch(created['id'])
        finished = extraction_queue.get_extraction_job(created['id'])
        self.assertEqual(finished['work_progress_percent'], 100)
        self.assertEqual(finished['progress_percent'], 100)

    def test_cancel_keeps_completed_work_and_skips_remaining_items(self):
        extraction_queue.configure_extraction_processor(self._processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(35), owner_user_id=1)
        extraction_queue.cancel_extraction_job(created["id"], owner_user_id=1)

        status = extraction_queue._process_job_batch(created["id"])
        cancelled = extraction_queue.get_extraction_job(created["id"], include_items=True)

        self.assertEqual(status, "cancelled")
        self.assertEqual(cancelled["processed_skus"], 0)
        self.assertTrue(all(item["status"] == "cancelled" for item in cancelled["items"]))

    def test_recovers_running_job_after_restart(self):
        created = extraction_queue.enqueue_extraction_job(self._skus(2))
        db = self.session_factory()
        try:
            job = db.query(ExtractionJob).filter(ExtractionJob.id == created["id"]).one()
            item = (
                db.query(ExtractionJobItem)
                .filter(ExtractionJobItem.job_id == created["id"])
                .order_by(ExtractionJobItem.position.asc())
                .first()
            )
            job.status = "running"
            job.current_sku = item.sku
            item.status = "running"
            db.commit()
        finally:
            db.close()

        extraction_queue._recover_interrupted_jobs()
        recovered = extraction_queue.get_extraction_job(created["id"], include_items=True)

        self.assertEqual(recovered["status"], "queued")
        self.assertIsNone(recovered["current_sku"])
        self.assertEqual(recovered["items"][0]["status"], "queued")

    def test_active_queue_protects_sku_from_deletion(self):
        sku = "W10531315"
        extraction_queue.enqueue_extraction_job([sku])
        db = self.session_factory()
        try:
            db.add(KnownSku(sku=sku))
            db.commit()
            with self.assertRaises(HTTPException) as context:
                delete_local_sku(sku=sku, db=db)
            self.assertEqual(context.exception.status_code, 409)
        finally:
            db.close()

    def test_single_sku_result_distinguishes_fresh_offers_from_cache(self):
        with self.session_factory() as db:
            summary = {
                "results": [{"name": "W10531315"}],
                "stores": [],
                "comparison": [],
                "processed_count": 0,
                "fresh_count": 1,
            }
            verified = _sku_batch_result(db, "W10531315", summary, "updated")
            self.assertIn("1 oferta verificada", verified["message"])
            self.assertIn("0 ofertas novas", verified["message"])

            summary["fresh_count"] = 0
            empty = _sku_batch_result(db, "W10531315", summary, "updated")
            self.assertIn("sem ofertas novas", empty["message"])
            cached = _sku_batch_result(db, "W10531315", summary, "cached")
            self.assertIn("Cache local", cached["message"])


if __name__ == "__main__":
    unittest.main()
