import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import ExtractionJob, ExtractionJobItem, PriceHistory, Product
from report_extraction import write_report


class ExtractionReportTests(unittest.TestCase):
    def test_report_keeps_empty_skus_and_separates_old_offers_from_new_observations(self):
        engine = create_engine("sqlite://", poolclass=StaticPool,
                               connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        started = datetime.utcnow() - timedelta(hours=1)
        with factory() as db:
            job = ExtractionJob(id="report-test", request_key="test", total_skus=2,
                                mode="precise", scope="all", created_at=started)
            job.items = [ExtractionJobItem(sku="W11199342", position=0),
                         ExtractionJobItem(sku="W11299803", position=1)]
            db.add(job)
            for seller, updated in [("Old seller", started - timedelta(days=1)),
                                    ("New seller", started + timedelta(minutes=1))]:
                product = Product(name="=UNTRUSTED()", sku="W11199342")
                db.add(product)
                db.flush()
                db.add(PriceHistory(product_id=product.id, price=100, store=seller,
                                    source="Amazon Brasil", url="https://example.test/" + str(product.id),
                                    timestamp=updated, is_cached=False))
            db.commit()
        try:
            with tempfile.TemporaryDirectory() as folder, patch("report_extraction.SessionLocal", factory):
                output = Path(folder) / "report"
                state = write_report("report-test", output)
                self.assertEqual(state["total_skus"], 2)
                self.assertEqual(state["fresh_observations"], 1)
                self.assertEqual(state["skus_with_fresh_observations"], 1)
                self.assertEqual(state["saved_offers_including_older_cache"], 2)
                self.assertEqual(len(json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))["items"]), 2)
                workbook = load_workbook(output.with_suffix(".xlsx"), read_only=True)
                rows = list(workbook["SKUs"].values)
                self.assertEqual(rows[2][0], "W11299803")
                self.assertEqual(rows[2][5:7], (0, 0))
                self.assertEqual(workbook["Ofertas salvas"]["B2"].data_type, "s")
                workbook.close()
        finally:
            engine.dispose()
