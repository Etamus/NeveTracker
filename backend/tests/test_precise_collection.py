import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scrapy.http import HtmlResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from crawlers.base import collect_http, parse_product_response
from crawlers.mercado_livre import collect_mercado_livre
from crawlers.profiles import DEFAULT_PROFILE
from crawlers.shopee import collect_shopee
from database import Base
from models.product import ProductSource
from services.product_resolver import ProductIdentity, resolve_candidate
from services.product_sources import upsert_product_source
from services.playwright_fallback import collect_shipping_with_playwright


FIXTURE = Path(__file__).parent / "fixtures" / "product_pdp.html"
HTTP_STORES = (
    "amazon",
    "magalu",
    "leroy_merlin",
    "dufrio",
    "friolar",
    "refrigeracao_mota",
    "mg_parts",
    "gold_service",
    "comclick",
)


class PreciseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_bytes()
        self.identity = ProductIdentity(
            sku="W10531315",
            mpn="W10531315",
            model="W10531315",
            brand="Whirlpool",
        )

    def _source(self, store="dufrio", seller="Loja Teste"):
        return SimpleNamespace(
            store=store,
            seller=seller,
            url="https://example.test/produto/W10531315",
            external_product_id=None,
            asin=None,
            item_id=None,
            shop_id=None,
            ean=None,
            mpn="W10531315",
            model="W10531315",
            brand="Whirlpool",
        )

    def test_standard_pdp_contract_for_every_http_store(self):
        for store in HTTP_STORES:
            with self.subTest(store=store):
                response = HtmlResponse(
                    url=f"https://{store}.example/produto/W10531315",
                    body=self.html,
                    encoding="utf-8",
                )
                result = parse_product_response(response, self._source(store), self.identity, DEFAULT_PROFILE)
                self.assertEqual(result["status"], "OK")
                item = result["item"]
                self.assertEqual(item["seller"], "Loja Teste")
                self.assertEqual(item["cash_price"], 129.90)
                self.assertEqual(item["installment_count"], 3)
                self.assertEqual(item["shipping_cost"], 15.50)
                self.assertEqual(item["delivery_max_days"], 4)
                self.assertEqual(item["availability"], "in_stock")

    def test_out_of_stock_is_preserved(self):
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=self.html.replace(b"InStock", b"OutOfStock"),
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["availability"], "out_of_stock")

    def test_missing_freight_does_not_invent_a_value(self):
        html = self.html.replace(
            b',\n          "shippingDetails": {\n            "@type": "OfferShippingDetails",\n            "shippingRate": {"@type": "MonetaryAmount", "value": "15.50"},\n            "deliveryTime": {\n              "@type": "ShippingDeliveryTime",\n              "transitTime": {"@type": "QuantitativeValue", "minValue": 2, "maxValue": 4}\n            }\n          }',
            b"",
        )
        response = HtmlResponse(
            url="https://example.test/produto/W10531315",
            body=html,
            encoding="utf-8",
        )
        result = parse_product_response(response, self._source(), self.identity, DEFAULT_PROFILE)
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("shipping_cost", result["item"])

    @patch("crawlers.base.create_resilient_session")
    def test_timeout_is_reported_without_invalidating_match(self, create_session):
        session = create_session.return_value
        session.get.side_effect = TimeoutError("timeout")
        result = collect_http(self._source(), self.identity, None, DEFAULT_PROFILE, 5)
        self.assertEqual(result["status"], "TIMEOUT")

    def test_product_sources_keep_multiple_sellers(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            for seller in ("Seller A", "Seller B"):
                candidate = {
                    "name": "Whirlpool W10531315",
                    "url": "https://amazon.example/dp/B000000001",
                    "seller": seller,
                    "brand": "Whirlpool",
                }
                match = resolve_candidate(self.identity, candidate)
                upsert_product_source(db, self.identity, "amazon", candidate, match)
            db.commit()
            self.assertEqual(db.query(ProductSource).count(), 2)
        finally:
            db.close()
            engine.dispose()

    @patch("crawlers.mercado_livre.create_resilient_session")
    def test_mercado_livre_collects_official_item_and_real_seller(self, create_session):
        item_response = Mock(status_code=200)
        item_response.json.return_value = {
            "title": "Peça Whirlpool W10531315",
            "price": 120,
            "available_quantity": 2,
            "seller_id": 42,
            "permalink": "https://produto.mercadolivre.com.br/MLB-123456",
            "category_id": "MLB1",
            "attributes": [
                {"id": "BRAND", "value_name": "Whirlpool"},
                {"id": "MODEL", "value_name": "W10531315"},
            ],
            "pictures": [],
        }
        seller_response = Mock(status_code=200)
        seller_response.json.return_value = {"nickname": "VENDEDOR REAL"}
        create_session.return_value.get.side_effect = [item_response, seller_response]
        source = self._source("mercado_livre", "Mercado Livre")
        source.item_id = "MLB123456"
        result = collect_mercado_livre(source, self.identity)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["seller"], "VENDEDOR REAL")

    @patch("crawlers.shopee.create_resilient_session")
    def test_shopee_resolves_shop_name(self, create_session):
        item_response = Mock(status_code=200)
        item_response.json.return_value = {
            "data": {
                "name": "Peça Whirlpool W10531315",
                "price": 11000000,
                "stock": 3,
                "brand": {"name": "Whirlpool"},
                "attributes": [{"name": "Modelo", "value": "W10531315"}],
            }
        }
        shop_response = Mock(status_code=200)
        shop_response.json.return_value = {"data": {"name": "LOJA OFICIAL"}}
        create_session.return_value.get.side_effect = [item_response, shop_response]
        source = self._source("shopee", "Shopee Loja 42")
        source.item_id = "123"
        source.shop_id = "42"
        result = collect_shopee(source, self.identity)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["item"]["seller"], "LOJA OFICIAL")

    @patch("playwright.sync_api.sync_playwright")
    def test_playwright_shipping_fallback(self, sync_playwright):
        body = Mock()
        body.inner_text.return_value = "Frete R$ 20,00. Entrega em 3 a 5 dias."
        zip_input = Mock()
        zip_input.first = zip_input
        zip_input.count.return_value = 0
        page = Mock()
        page.locator.side_effect = lambda selector: body if selector == "body" else zip_input
        browser = Mock()
        browser.new_page.return_value = page
        runtime = Mock()
        runtime.chromium.launch.return_value = browser
        context = sync_playwright.return_value
        context.__enter__.return_value = runtime
        context.__exit__.return_value = False

        result = collect_shipping_with_playwright(
            "https://example.test/produto",
            "01310100",
            5,
        )
        self.assertEqual(result["shipping_cost"], 20.0)
        self.assertEqual(result["delivery_min_days"], 3)
        self.assertEqual(result["delivery_max_days"], 5)


if __name__ == "__main__":
    unittest.main()
