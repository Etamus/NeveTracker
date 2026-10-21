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
from models.product import CrawlerControl, KnownSku, PriceHistory, Product, ProductSource, ScrapeCursor
from routes.products import (
    _register_skus,
    _search_sku_summary,
    _sku_status_map,
    create_extraction_job,
    delete_local_sku,
    import_skus_file,
    list_product_sources,
    process_extraction_queue_item,
    search_sku_batch,
)
from schemas.product import SkuBatchRequest
from services.sku_metadata_seed import SKU_CATEGORIES, seed_sku_business_metadata


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
    @patch("routes.products.run_precise_extraction", return_value={"results": [], "comparison": []})
    def test_forced_precise_job_does_not_skip_stores_because_other_collector_saved_a_price(self, precise):
        product = Product(name="Filtro W11199342", sku="W11199342")
        self.db.add(product)
        self.db.flush()
        self.db.add(PriceHistory(product_id=product.id, price=100, store="Loja", url="https://example.test/sku",
                                 timestamp=datetime.utcnow()))
        self.db.commit()
        _, status = _search_sku_summary("W11199342", SkuBatchRequest(skus=["W11199342"], mode="precise", force_refresh=True),
                                        self.db, refreshed_after=datetime.utcnow() - timedelta(minutes=1))
        precise.assert_called_once()
        self.assertEqual(status, "updated")
    def test_import_preserves_external_codes_and_own_price(self):
        upload = UploadFile(filename="reference.csv", file=BytesIO(
            b"SKU;Meu codigo;ID Webglobal;Meu preco\nW11199342;000123;380;1.234,56\nW11199343;000124;381;0\n"
        ))
        response = asyncio.run(import_skus_file(file=upload, db=self.db))
        self.assertEqual(response["imported_count"], 2)
        first = self.db.query(KnownSku).filter_by(sku="W11199342").one()
        self.assertEqual(first.internal_code, "000123")
        self.assertEqual(first.external_id, "380")
        self.assertEqual(first.reference_price, 1234.56)
        self.assertIsNone(self.db.query(KnownSku).filter_by(sku="W11199343").one().reference_price)

    def test_import_rejects_invalid_own_prices_without_overwriting_existing_data(self):
        upload = UploadFile(filename="reference.csv", file=BytesIO(
            b"SKU;Meu preco\nW11199342;NaN\nW11199343;-2\nW11199344;12.90\n"
        ))
        response = asyncio.run(import_skus_file(file=upload, db=self.db))
        self.assertEqual(response["imported_count"], 1)
        self.assertEqual(len(response["errors"]), 2)

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

    def test_business_metadata_uses_category_and_clears_portfolio(self):
        self.db.add(KnownSku(sku="W11509387", portfolio="CDP Bronze"))
        self.db.commit()

        seed_sku_business_metadata(self.db)

        item = self.db.query(KnownSku).filter(KnownSku.sku == "W11509387").one()
        self.assertEqual(item.category, SKU_CATEGORIES["W11509387"])
        self.assertEqual(item.cluster, "CONSUMÍVEIS & ACESSÓRIOS")
        self.assertEqual(item.subfamily, "Filtros")
        self.assertIsNone(item.portfolio)

    def test_registers_and_reads_more_than_sqlite_parameter_limit(self):
        skus = [f"W{10600000 + index}" for index in range(1205)]

        _register_skus(self.db, skus)
        statuses = _sku_status_map(self.db, skus)

        self.assertEqual(self.db.query(KnownSku).count(), 1205)
        self.assertEqual(len(statuses), 1205)
        self.assertTrue(all(not item["in_database"] for item in statuses.values()))

    def test_manual_queue_respects_requested_extraction_mode(self):
        self.db.add(CrawlerControl(id=1, default_mode="precise"))
        self.db.commit()
        payload = SkuBatchRequest(skus=["W11509387"], mode="quick")

        with patch("routes.products.enqueue_extraction_job", return_value={"id": "job"}) as enqueue:
            response = create_extraction_job(payload=payload, _actor=None, db=self.db)

        self.assertEqual(response["id"], "job")
        self.assertEqual(enqueue.call_args.kwargs["mode"], "quick")

    def test_manual_queue_uses_global_mode_when_not_requested(self):
        self.db.add(CrawlerControl(id=1, default_mode="precise"))
        self.db.commit()
        payload = SkuBatchRequest(skus=["W11509387"])
        with patch("routes.products.enqueue_extraction_job") as enqueue:
            create_extraction_job(payload=payload, _actor=None, db=self.db)
        self.assertEqual(enqueue.call_args.kwargs["mode"], "precise")

    @patch("routes.products.run_all_scrapers")
    def test_local_batch_search_accepts_more_than_30_without_scraping(self, run_all_scrapers):
        skus = [f"W{11700000 + index}" for index in range(45)]
        self.db.add_all(KnownSku(sku=sku) for sku in skus)
        self.db.commit()

        response = search_sku_batch(
            payload=SkuBatchRequest(skus=skus, local_only=True),
            db=self.db,
        )

        self.assertEqual(response["skus"], skus)
        run_all_scrapers.assert_not_called()

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

    @patch("routes.products.run_precise_extraction")
    def test_precise_queue_resume_collects_even_when_cached(self, run_precise):
        sku = "W10531315"
        product = Product(name=f"Peca {sku}", brand="Whirlpool", category="Pecas", sku=sku)
        product.prices.append(PriceHistory(
            price=100, store="Dufrio", source="Dufrio",
            url="https://www.dufrio.com.br/produto.html",
        ))
        self.db.add(product)
        self.db.commit()
        run_precise.return_value = {
            "results": [], "stores": [], "comparison": [], "source_health": [],
            "more_work": True,
        }
        _summary, status = _search_sku_summary(
            sku, SkuBatchRequest(skus=[sku], mode="precise", force_refresh=False),
            self.db, collection_after=datetime.utcnow() - timedelta(minutes=1), resume=True,
        )
        self.assertEqual(status, "updated")
        run_precise.assert_called_once()

    @patch("routes.products._search_sku_summary")
    def test_precise_queue_marks_unfinished_sources_as_partial(self, search_summary):
        search_summary.return_value = ({
            "results": [], "stores": [], "comparison": [],
            "more_work": True, "pending_sources": 10,
        }, "updated")
        with patch("routes.products.SessionLocal", side_effect=sessionmaker(bind=self.engine)):
            result = process_extraction_queue_item("W10531315", {
                "mode": "precise", "force_refresh": True, "scope": "all", "attempt": 1,
                "job_created_at": datetime.utcnow(),
            })
        self.assertEqual(result["status"], "partial")
        self.assertIn("10 fontes", result["message"])

    @patch("routes.products._search_sku_summary")
    def test_precise_queue_reports_searches_that_could_not_advance(self, search_summary):
        search_summary.return_value = ({
            "results": [], "stores": [], "comparison": [],
            "more_work": False, "pending_pages": 3,
        }, "updated")
        with patch("routes.products.SessionLocal", side_effect=sessionmaker(bind=self.engine)):
            result = process_extraction_queue_item("W10531315", {
                "mode": "precise", "force_refresh": True, "scope": "all", "attempt": 2,
                "job_created_at": datetime.utcnow(),
            })
        self.assertEqual(result["status"], "updated")
        self.assertIn("3 paginas de loja nao avancaram", result["message"])

    @patch("routes.products._search_sku_summary")
    def test_precise_queue_reports_blocked_product_without_listing_price(self, search_summary):
        search_summary.return_value = ({
            "results": [], "stores": [], "comparison": [],
            "more_work": False, "pending_pages": 0,
            "failed_sources": 2, "failed_sources_without_price": 1,
            "coverage_by_store": [
                {"label": "Friolar", "failed_pages": 0, "failed_sources": 1},
                {"label": "Gold Service", "failed_pages": 0, "failed_sources": 1},
            ],
        }, "updated")
        with patch("routes.products.SessionLocal", side_effect=sessionmaker(bind=self.engine)):
            result = process_extraction_queue_item("W10531315", {
                "mode": "precise", "force_refresh": True, "scope": "all", "attempt": 1,
                "job_created_at": datetime.utcnow(),
            })
        self.assertEqual(result["status"], "updated")
        self.assertIn("2 paginas de produto falharam (1 sem preco de listagem)", result["message"])
        self.assertIn("Lojas afetadas: Friolar", result["message"])

    def test_sources_endpoint_reports_coverage_and_failure_by_store(self):
        self.db.add(ScrapeCursor(
            sku="W10531315", store="magalu", kind="search", key="W10531315",
            initial_url="https://www.magazineluiza.com.br/busca/W10531315/",
            next_url="https://www.magazineluiza.com.br/busca/W10531315/",
            completed=False, last_status="BLOCKED", failure_reason="HTTP 403",
            pages_completed=0, candidates_found=0,
        ))
        self.db.add(ProductSource(
            sku="W10531315", store="magalu", seller="Seller A",
            url="https://www.magazineluiza.com.br/p/one", active=True,
        ))
        self.db.add(ProductSource(
            sku="W10531315", store="friolar", seller="Friolar",
            url="https://www.friolarpecas.com.br/p/one", active=True,
            validation_status="blocked", failure_reason="HTTP 403",
        ))
        self.db.commit()
        response = list_product_sources(sku="W10531315", active=None, _actor=None, db=self.db)
        magalu = next(row for row in response["coverage_by_store"] if row["store"] == "magalu")
        self.assertEqual(magalu["sources"], 1)
        self.assertEqual(magalu["sellers"], 1)
        self.assertEqual(magalu["failed_pages"], 1)
        self.assertEqual(magalu["failures"][0]["reason"], "HTTP 403")
        friolar = next(row for row in response["coverage_by_store"] if row["store"] == "friolar")
        self.assertEqual(friolar["failed_sources_without_price"], 1)
        self.assertEqual(friolar["source_failures"][0]["reason"], "HTTP 403")
        self.assertEqual(len(response["coverage_by_store"]), 11)

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
