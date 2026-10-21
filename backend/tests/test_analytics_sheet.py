import unittest
from datetime import datetime, timedelta
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from models.product import KnownSku, PriceHistory, Product
from routes.products import product_router


class AnalyticsSheetTests(unittest.TestCase):
    def test_summary_keeps_requested_material_without_fabricating_price_or_stock(self):
        self.db.add(KnownSku(sku="W11677210", description="Compressor"))
        self.db.commit()
        body = self.client.get("/products/analytics/sheet/summary", params={"skus": "W11677210 W10531315"}).json()
        self.assertEqual(body["total"], 2)
        missing = next(x for x in body["items"] if x["sku"] == "W11677210")
        self.assertEqual(missing["offer_count"], 0)
        self.assertEqual(missing["store_count"], 0)
        self.assertIsNone(missing["cheapest_price"])
        self.assertIsNone(missing["availability"])
        selected = self.client.get("/products/analytics/sheet/summary", params={"skus": "W11677210", "source": "Amazon Brasil"}).json()
        self.assertEqual(selected["total"], 0)
        exported = self.client.get("/products/analytics/sheet/export", params={"skus": "W11677210"})
        self.assertEqual(exported.status_code, 200)
        workbook = load_workbook(BytesIO(exported.content), read_only=True)
        rows = list(workbook["Analytics"].values)
        self.assertEqual(rows[1][0], "W11677210")
        self.assertIsNone(rows[1][10])
        self.assertIsNone(rows[1][12])
        workbook.close()
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        app = FastAPI()
        app.include_router(product_router, prefix="/products")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

        self.db.add(KnownSku(sku="W10531315", description="Compressor", cluster="A"))
        for sku, seller, source, price in [
            ("W10531315", "Seller A", "Amazon Brasil", 120),
            ("W10531315", "Seller B", "Amazon Brasil", 135),
            ("W11199342", "Seller A", "Mercado Livre", 80),
        ]:
            product = Product(name=f"Peca {sku}", sku=sku, brand="Whirlpool", category="Pecas")
            self.db.add(product)
            self.db.flush()
            self.db.add(PriceHistory(
                product_id=product.id, store=seller, source=source,
                price=price, url=f"https://example.test/{product.id}",
                timestamp=datetime(2026, 9, 30, 12),
                shipping_cost=0 if seller == "Seller A" else None,
            ))
        self.db.commit()

        # A title may mention a compatible SKU without being that SKU.
        other = Product(name="Peca W10901034 compativel W10531315", sku="W10901034")
        self.db.add(other)
        self.db.flush()
        self.db.add(PriceHistory(
            product_id=other.id, store="Other", source="Shopee", price=50,
            url="https://example.test/other", timestamp=datetime(2026, 9, 30, 12),
        ))
        self.db.commit()

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_filters_skus_source_and_individual_seller(self):
        response = self.client.get("/products/analytics/sheet", params={
            "skus": "W10531315, W11199342", "source": "Amazon Brasil", "seller": "Seller B",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["store"], "Seller B")
        self.assertEqual(body["items"][0]["description"], "Compressor")
        self.assertIsNone(body["items"][0].get("shipping_cost"))

    def test_pagination_keeps_sellers_distinct_and_latest_price(self):
        first = self.db.query(Product).filter(Product.sku == "W10531315").first()
        self.db.add(PriceHistory(
            product_id=first.id, store="Seller A", source="Amazon Brasil",
            price=110, url=f"https://example.test/{first.id}",
            timestamp=datetime(2026, 9, 30, 12) + timedelta(hours=1),
        ))
        self.db.commit()

        first_page = self.client.get("/products/analytics/sheet", params={
            "skus": "W10531315", "page_size": 1, "page": 1,
        }).json()
        second_page = self.client.get("/products/analytics/sheet", params={
            "skus": "W10531315", "page_size": 1, "page": 2,
        }).json()
        self.assertEqual(first_page["total"], 2)
        self.assertEqual({first_page["items"][0]["store"], second_page["items"][0]["store"]}, {"Seller A", "Seller B"})
        self.assertEqual(first_page["items"][0]["current_price"], 110)

        options = self.client.get("/products/analytics/sheet/options", params={"source": "Amazon Brasil"}).json()
        self.assertEqual(options["sellers"], ["Seller A", "Seller B"])
        self.assertEqual(options["sources"], ["Amazon Brasil", "Mercado Livre", "Shopee"])

    def test_summary_uses_same_filters_and_aggregates_all_offers(self):
        response = self.client.get("/products/analytics/sheet/summary", params={
            "skus": "W10531315 W11199342", "source": "Amazon Brasil",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertEqual(item["sku"], "W10531315")
        self.assertEqual(item["offer_count"], 2)
        self.assertEqual(item["seller_count"], 2)
        self.assertEqual(item["store_count"], 1)
        self.assertEqual(item["pma"], 127.5)
        self.assertEqual(item["highest_price"], 135)
        self.assertIsNone(item["reference_difference"])
        self.assertEqual(item["cheapest_store"], "Seller A")
        self.assertEqual(item["cluster"], "A")

        seller_only = self.client.get("/products/analytics/sheet/summary", params={
            "seller": "Seller B",
        }).json()
        self.assertEqual(seller_only["items"][0]["offer_count"], 1)

    def test_own_price_is_separate_from_market_average_and_excludes_stockouts(self):
        known = self.db.query(KnownSku).filter_by(sku="W10531315").one()
        known.reference_price = 150
        known.internal_code = "INTERNAL-01"
        known.external_id = "380"
        product = Product(name="Peca W10531315", sku="W10531315")
        self.db.add(product)
        self.db.flush()
        self.db.add(PriceHistory(product_id=product.id, store="Unavailable", source="Amazon Brasil",
                                 price=9999, availability="out_of_stock", url="https://example.test/oos"))
        self.db.commit()
        body = self.client.get("/products/analytics/sheet/summary", params={"skus": "W10531315"}).json()
        item = body["items"][0]
        self.assertEqual(item["highest_price"], 135)
        self.assertEqual(item["reference_price"], 150)
        self.assertEqual(item["reference_difference"], 30)
        self.assertEqual(item["difference_value"], 7.5)
        self.assertEqual(item["internal_code"], "INTERNAL-01")
        response = self.client.get("/products/analytics/sheet/export", params={"skus": "W10531315"})
        workbook = load_workbook(BytesIO(response.content), read_only=True)
        row = dict(zip(*list(workbook["Analytics"].values)[:2]))
        self.assertEqual(row["Maior preço"], 135)
        self.assertEqual(row["Meu preço"], 150)
        self.assertEqual(row["Meu preço - melhor preço"], 30)
        self.assertEqual(row["ID externo"], "380")
        workbook.close()

    def test_summary_and_export_compare_cash_prices_when_available(self):
        price = self.db.query(PriceHistory).filter_by(store="Seller B").one()
        price.cash_price = 100
        self.db.commit()
        summary = self.client.get("/products/analytics/sheet/summary", params={"skus": "W10531315"}).json()["items"][0]
        self.assertEqual(summary["cheapest_price"], 100)
        self.assertEqual(summary["highest_price"], 120)
        self.assertEqual(summary["pma"], 110)
        self.assertEqual(summary["cheapest_store"], "Seller B")
        export = self.client.get("/products/analytics/sheet/export", params={"skus": "W10531315"})
        workbook = load_workbook(BytesIO(export.content), read_only=True)
        row = dict(zip(*list(workbook["Analytics"].values)[:2]))
        self.assertEqual(row["Melhor preço"], 100)
        self.assertEqual(row["Maior preço"], 120)
        workbook.close()

    def test_multiple_stores_and_sellers_use_checkbox_union_within_each_filter(self):
        params = [
            ("source", "Amazon Brasil"), ("source", "Mercado Livre"),
            ("seller", "Seller A"), ("seller", "Seller B"),
        ]
        offers = self.client.get("/products/analytics/sheet", params=params).json()
        self.assertEqual(offers["total"], 3)
        self.assertEqual({item["source"] for item in offers["items"]}, {"Amazon Brasil", "Mercado Livre"})
        self.assertEqual({item["store"] for item in offers["items"]}, {"Seller A", "Seller B"})

        options = self.client.get("/products/analytics/sheet/options", params=params[:2]).json()
        self.assertEqual(options["sellers"], ["Seller A", "Seller B"])

        summary = self.client.get("/products/analytics/sheet/summary", params=params).json()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(sum(item["offer_count"] for item in summary["items"]), 3)

        export = self.client.get("/products/analytics/sheet/export", params=params)
        self.assertEqual(export.status_code, 200)
        workbook = load_workbook(BytesIO(export.content), read_only=True)
        self.assertEqual(len(list(workbook["Ofertas"].values)), 4)
        workbook.close()

    def test_excel_export_respects_sku_and_seller_filters(self):
        response = self.client.get("/products/analytics/sheet/export", params={
            "skus": "W10531315, W11199342", "seller": "Seller A",
        })
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), read_only=True)
        self.assertEqual(workbook.sheetnames, ["Analytics", "Lojistas", "Ofertas"])
        offers = list(workbook["Ofertas"].values)
        self.assertEqual(len(offers), 3)
        self.assertEqual({row[0] for row in offers[1:]}, {"W10531315", "W11199342"})
        self.assertEqual({row[7] for row in offers[1:]}, {"Seller A"})
        self.assertEqual(len(list(workbook["Analytics"].values)), 3)
        workbook.close()


if __name__ == "__main__":
    unittest.main()
