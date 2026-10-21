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
            extraction_queue.cancel_extraction_job(options["job_id"])
            return {**self._processor(sku, options), "status": "partial"}
        extraction_queue.configure_extraction_processor(processor)
        created = extraction_queue.enqueue_extraction_job(self._skus(2), mode="precise")
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
        created = extraction_queue.enqueue_extraction_job(self._skus(35))
        extraction_queue.cancel_extraction_job(created["id"])

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
