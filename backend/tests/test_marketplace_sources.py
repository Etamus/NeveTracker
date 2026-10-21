import unittest
from unittest.mock import Mock, patch

from scrapers.comparison import _comparison_candidate
from services.bing_shopping import (
    RESULT_CACHE,
    _duckduckgo_destination,
    _parse_results,
    _product_page_offer,
    search_bing_shopping,
)
from services.mercado_livre_api import search_mercado_livre
from services.store_registry import find_target_store, target_store_label


class MercadoLivreSourceTests(unittest.TestCase):
    def tearDown(self):
        search_mercado_livre.cache_clear()

    @patch("services.mercado_livre_api.session.get")
    @patch("services.mercado_livre_api.get_runtime_provider_settings", return_value={"enabled": True, "configured": True, "max_results": 100})
    def test_official_search_maps_payment_shipping_and_availability(self, settings, get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "paging": {"total": 1},
            "results": [
                {
                    "id": "MLB123",
                    "title": "Peca W10531315",
                    "price": 100,
                    "permalink": "https://produto.mercadolivre.com.br/MLB-123",
                    "seller": {"id": 55, "nickname": "LOJA TESTE"},
                    "installments": {"quantity": 2, "amount": 52.5},
                    "shipping": {"free_shipping": True},
                    "available_quantity": 10,
                    "attributes": [{"id": "BRAND", "value_name": "Consul"}],
                }
            ],
        }
        get.return_value = response

        result = search_mercado_livre("W10531315")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["installment_price"], 105)
        self.assertEqual(result[0]["payment_difference"], 5)
        self.assertEqual(result[0]["shipping_cost"], 0)
        self.assertEqual(result[0]["availability"], "Disponivel")
        self.assertEqual(result[0]["discovery_source"], "mercado_livre_api")

    def test_comparator_offer_keeps_installment_details(self):
        result = _comparison_candidate(
            {
                "name": "Peca W10531315",
                "price": 100,
                "url": "/lead?oid=1",
                "merchantName": "Magazine Luiza",
                "installments": {
                    "amount_months": 2,
                    "price": 52.5,
                    "total_value": 105,
                },
                "_source_name": "Buscape",
                "_base_url": "https://www.buscape.com.br",
            },
            "W10531315",
        )

        self.assertEqual(result["installment_price"], 105)
        self.assertEqual(result["payment_difference"], 5)
        self.assertEqual(result["cash_price"], 100)

    def test_comparator_rejects_installment_total_below_cash_price(self):
        result = _comparison_candidate(
            {
                "name": "Peca W10531315",
                "price": 100,
                "url": "/lead?oid=2",
                "merchantName": "Magazine Luiza",
                "installments": {
                    "amount_months": 2,
                    "price": 49,
                    "total_value": 98,
                },
                "_source_name": "Buscape",
                "_base_url": "https://www.buscape.com.br",
            },
            "W10531315",
        )

        self.assertIsNone(result["installment_price"])
        self.assertIsNone(result["installment_count"])


class StoreRegistryTests(unittest.TestCase):
    def test_resolves_store_from_merchant_alias_or_url(self):
        self.assertEqual(target_store_label("Loja oficial Magalu"), "Magazine Luiza")
        self.assertEqual(
            target_store_label("Oferta", "https://produto.mercadolivre.com.br/MLB-1"),
            "Mercado Livre",
        )
        self.assertEqual(find_target_store("MGParts")["key"], "mg_parts")

    def test_unknown_merchant_remains_discovered(self):
        self.assertIsNone(find_target_store("Loja regional sem cadastro"))


class BingShoppingTests(unittest.TestCase):
    def setUp(self):
        RESULT_CACHE.clear()

    def test_maps_store_payment_shipping_and_lead_time(self):
        result = _parse_results(
            """
            <ol>
              <li class="br-item product-result">
                <a href="/shop/productpage?q=W10531315&amp;product=123">
                  <img alt="Peca original W10531315 Brastemp" src="https://example.com/product.jpg">
                  <div class="br-pdItemName">Peca original W10531315</div>
                </a>
                <div class="br-focusPrice">R$ 100,00</div>
                <div class="br-sellerName">Loja oficial Magalu</div>
                <div>10x de R$ 11,00. Frete gratis, entrega em 2 a 4 dias.</div>
              </li>
            </ol>
            """,
            "W10531315",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "Magazine Luiza")
        self.assertEqual(result[0]["store"], "Loja oficial Magalu")
        self.assertEqual(result[0]["installment_price"], 110)
        self.assertEqual(result[0]["payment_difference"], 10)
        self.assertEqual(result[0]["shipping_cost"], 0)
        self.assertEqual(result[0]["delivery_min_days"], 2)
        self.assertEqual(result[0]["delivery_max_days"], 4)
        self.assertEqual(result[0]["discovery_source"], "bing_shopping")

    def test_ignores_shell_without_product_cards(self):
        self.assertEqual(_parse_results("<html><title>Bing Shopping</title></html>", "W10531315"), [])

    def test_ignores_bing_card_from_another_sku(self):
        result = _parse_results(
            """
            <li class="product-result">
              <a href="/shop/productpage?product=other">
                <img alt="Motor de geladeira W10399715">
                <div class="br-pdItemName">Motor W10399715</div>
              </a>
              <div class="br-focusPrice">R$ 100,00</div>
              <div class="br-sellerName">Magazine Luiza</div>
            </li>
            """,
            "W10531315",
        )
        self.assertEqual(result, [])

    def test_decodes_duckduckgo_product_url(self):
        result = _duckduckgo_destination(
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.dufrio.com.br%2Fproduto.html"
        )
        self.assertEqual(result, "https://www.dufrio.com.br/produto.html")

    @patch("services.bing_shopping.create_resilient_session")
    def test_reads_offer_from_discovered_product_page(self, create_session):
        response = Mock(status_code=200, url="https://www.dufrio.com.br/produto.html")
        response.text = """
            <script type="application/ld+json">
              {
                "@type": "Product",
                "name": "Kit Sensor W10531315 Brastemp",
                "image": "https://example.com/product.jpg",
                "offers": {
                  "@type": "Offer",
                  "price": "89.90",
                  "availability": "https://schema.org/InStock"
                }
              }
            </script>
        """
        create_session.return_value.get.return_value = response

        result = _product_page_offer(
            ("https://www.dufrio.com.br/produto.html", "Kit Sensor W10531315", "W10531315")
        )

        self.assertEqual(result["price"], 89.9)
        self.assertEqual(result["source"], "Dufrio")
        self.assertEqual(result["discovery_source"], "duckduckgo_product_page")

    @patch("services.bing_shopping._fetch_results")
    def test_search_uses_no_credentials(self, fetch_results):
        fetch_results.return_value = []

        self.assertEqual(search_bing_shopping("W10531315"), [])
        fetch_results.assert_called_once_with("W10531315")
