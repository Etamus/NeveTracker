import asyncio
from datetime import datetime, timedelta
from io import BytesIO
import unittest
from unittest.mock import patch

from fastapi import UploadFile
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import KnownSku, PriceHistory, Product
from routes.products import (
    _register_skus,
    _search_sku_summary,
    _sku_status_map,
    delete_local_sku,
    import_skus_file,
)
from schemas.product import SkuBatchRequest


def _workbook_bytes(rows):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["SKU", "Descricao", "Subfamilia", "Categoria", "Cluster", "Mercado"])
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


class SkuImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_import_skus_with_metadata(self):
        upload = UploadFile(
            filename="catalogo.xlsx",
            file=_workbook_bytes(
                [
                    ["W10531315", "Compressor", "Refrigeracao", "Pecas", "A", "Competitivo"],
                    ["W10531315", "Duplicado", "", "", "", ""],
                    ["W10531316", "Termostato", "Refrigeracao", "Pecas", "B", "Cativo"],
                ]
            ),
        )

        response = asyncio.run(import_skus_file(file=upload, db=self.db))

        self.assertEqual(response["imported_count"], 2)
        self.assertEqual(response["created_count"], 2)
        first = self.db.query(KnownSku).filter(KnownSku.sku == "W10531315").one()
        self.assertEqual(first.description, "Compressor")
        self.assertEqual(first.subfamily, "Refrigeracao")
        self.assertEqual(first.category, "Pecas")
        self.assertEqual(first.cluster, "A")
        self.assertEqual(first.market_type, "Competitivo")

    def test_import_rejects_invalid_market_value(self):
        upload = UploadFile(
            filename="catalogo.xlsx",
            file=_workbook_bytes(
                [
                    ["W10531315", "Valido", "", "", "", "Cativo"],
                    ["W10531316", "Invalido", "", "", "", "Outro"],
                ]
            ),
        )

        response = asyncio.run(import_skus_file(file=upload, db=self.db))

        self.assertEqual(response["imported_count"], 1)
        self.assertEqual(response["skipped_count"], 1)
        self.assertEqual(
            response["errors"],
            [{"row": 3, "message": "Mercado deve ser Cativo ou Competitivo."}],
        )

    def test_registers_and_reads_more_than_sqlite_parameter_limit(self):
        skus = [f"W{10600000 + index}" for index in range(1205)]

        _register_skus(self.db, skus)
        statuses = _sku_status_map(self.db, skus)

        self.assertEqual(self.db.query(KnownSku).count(), 1205)
        self.assertEqual(len(statuses), 1205)
        self.assertTrue(all(not item["in_database"] for item in statuses.values()))

    @patch("routes.products.run_all_scrapers")
    def test_queue_skips_sku_refreshed_after_job_creation(self, run_all_scrapers):
        sku = "W10531315"
        queued_at = datetime.utcnow() - timedelta(minutes=2)
        product = Product(
            name=f"Peca {sku}",
            brand="Brastemp",
            category="Pecas",
            sku=sku,
        )
        product.prices.append(
            PriceHistory(
                price=100,
                store="Dufrio",
                source="Dufrio",
                url="https://www.dufrio.com.br/produto.html",
                timestamp=datetime.utcnow(),
            )
        )
        self.db.add_all([KnownSku(sku=sku), product])
        self.db.commit()

        _summary, status = _search_sku_summary(
            sku,
            SkuBatchRequest(skus=[sku], force_refresh=True),
            self.db,
            refreshed_after=queued_at,
        )

        self.assertEqual(status, "cached")
        run_all_scrapers.assert_not_called()

    def test_delete_sku_removes_catalog_and_price_history(self):
        sku = "W10531315"
        product = Product(
            name=f"Peca original {sku}",
            brand="Brastemp",
            category="Pecas",
            sku=sku,
        )
        product.prices.append(
            PriceHistory(
                price=100,
                store="Dufrio",
                source="Dufrio",
                url="https://www.dufrio.com.br/produto.html",
            )
        )
        self.db.add_all([KnownSku(sku=sku), product])
        self.db.commit()

        result = delete_local_sku(sku=sku, db=self.db)

        self.assertEqual(result["deleted_products"], 1)
        self.assertEqual(result["deleted_prices"], 1)
        self.assertEqual(self.db.query(KnownSku).count(), 0)
        self.assertEqual(self.db.query(Product).count(), 0)
        self.assertEqual(self.db.query(PriceHistory).count(), 0)
