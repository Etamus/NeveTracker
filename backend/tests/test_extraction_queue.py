import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import ExtractionJob, ExtractionJobItem, KnownSku
from routes.products import delete_local_sku
from services import extraction_queue


class ExtractionQueueTests(unittest.TestCase):
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

    def test_repeated_active_request_reuses_same_job(self):
        skus = self._skus(31)
        first = extraction_queue.enqueue_extraction_job(skus, kind="database")
        second = extraction_queue.enqueue_extraction_job(skus, kind="database")

        self.assertEqual(first["id"], second["id"])

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


if __name__ == "__main__":
    unittest.main()
