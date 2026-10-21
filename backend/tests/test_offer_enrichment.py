import unittest
from datetime import datetime
from unittest.mock import patch
from unittest.mock import Mock

from bs4 import BeautifulSoup

from services.extraction_scheduler import _due_run_key
from services.offer_enrichment import (
    DETAIL_CACHE,
    _fetch_details,
    _fetch_details_uncached,
    _jsonld_offer_details,
    _parse_payment_details,
    enrich_offers,
    normalize_postal_code,
)
from services.scraper_service import _price_detail_values


class OfferEnrichmentTests(unittest.TestCase):
    def setUp(self):
        DETAIL_CACHE.clear()

    def test_payment_prices_and_difference(self):
        details = _parse_payment_details(
            "R$ 11,61 10% à Vista ou R$ 12,90 em 1x de R$ 12,90 sem juros",
            12.90,
        )
        self.assertEqual(details["cash_price"], 11.61)
        self.assertEqual(details["installment_price"], 12.90)
        self.assertEqual(details["installment_count"], 1)
        self.assertEqual(details["payment_difference"], 1.29)

    def test_jsonld_shipping(self):
        soup = BeautifulSoup(
            """
            <script type="application/ld+json">
            {"@type":"Product","offers":{"@type":"Offer","availability":"https://schema.org/InStock",
            "shippingDetails":{"@type":"OfferShippingDetails","shippingRate":{"value":18.5},
            "deliveryTime":{"handlingTime":{"minValue":1,"maxValue":2,"unitCode":"DAY"},
            "transitTime":{"minValue":3,"maxValue":5,"unitCode":"DAY"}}}}}
            </script>
            """,
            "html.parser",
        )
        details = _jsonld_offer_details(soup)
        self.assertEqual(details["shipping_cost"], 18.5)
        self.assertEqual(details["delivery_min_days"], 4)
        self.assertEqual(details["delivery_max_days"], 7)
        self.assertEqual(details["availability"], "InStock")

    def test_jsonld_free_shipping_remains_zero(self):
        soup = BeautifulSoup(
            '<script type="application/ld+json">'
            '{"@type":"Product","offers":{"@type":"Offer",'
            '"shippingDetails":{"shippingRate":{"value":0,"maxValue":25}}}}'
            '</script>',
            "html.parser",
        )
        self.assertEqual(_jsonld_offer_details(soup)["shipping_cost"], 0)

    def test_jsonld_same_day_delivery_remains_zero(self):
        soup = BeautifulSoup(
            '<script type="application/ld+json">'
            '{"@type":"Product","offers":{"@type":"Offer",'
            '"shippingDetails":{"shippingRate":{"value":0},'
            '"deliveryTime":{"transitTime":{"minValue":0,"maxValue":0}}}}}'
            '</script>',
            "html.parser",
        )
        details = _jsonld_offer_details(soup)
        self.assertEqual(details["delivery_min_days"], 0)
        self.assertEqual(details["delivery_max_days"], 0)

    @patch("services.offer_enrichment._fetch_details_uncached")
    def test_failed_shipping_quote_is_retried(self, fetch):
        fetch.side_effect = [{}, {"shipping_cost": 12.5, "delivery_max_days": 3}]
        url = "https://loja.test/produto"
        self.assertEqual(_fetch_details(url, 30, "01001000"), {})
        self.assertEqual(_fetch_details(url, 30, "01001000")["shipping_cost"], 12.5)
        self.assertEqual(_fetch_details(url, 30, "01001000")["delivery_max_days"], 3)
        self.assertEqual(fetch.call_count, 2)

    @patch("services.offer_enrichment._session")
    def test_postal_code_is_not_reported_without_logistics(self, session_factory):
        response = Mock(status_code=200, url="https://www.refrigeracaomota.com.br/produto")
        response.text = "<html><body>" + ("Produto " * 100) + "</body></html>"
        session_factory.return_value.get.return_value = response
        details = _fetch_details_uncached(response.url, 18.47, "01001000")
        self.assertNotIn("destination_zip", details)
        self.assertNotIn("logistics_captured_at", details)

    @patch("services.offer_enrichment._session")
    def test_generic_jsonld_shipping_is_not_presented_as_zip_quote(self, session_factory):
        response = Mock(status_code=200, url="https://www.refrigeracaomota.com.br/produto")
        response.text = (
            '<html><body><script type="application/ld+json">'
            '{"@type":"Product","offers":{"@type":"Offer",'
            '"shippingDetails":{"shippingRate":{"value":15}}}}'
            '</script>' + ("Produto " * 100) + '</body></html>'
        )
        session_factory.return_value.get.return_value = response
        details = _fetch_details_uncached(response.url, 18.47, "01001000")
        self.assertEqual(details["shipping_cost"], 15)
        self.assertNotIn("destination_zip", details)

    @patch("services.offer_enrichment._shopify_shipping", return_value={
        "shipping_cost": 10.89, "delivery_max_days": 4,
    })
    @patch("services.offer_enrichment._session")
    def test_actual_zip_quote_keeps_postal_code(self, session_factory, _shipping):
        response = Mock(status_code=200, url="https://www.mgparts.com.br/products/sensor")
        response.text = "<html><body>" + ("Produto " * 100) + "</body></html>"
        session_factory.return_value.get.return_value = response
        details = _fetch_details_uncached(response.url, 18.47, "01001000")
        self.assertEqual(details["shipping_cost"], 10.89)
        self.assertEqual(details["destination_zip"], "01001000")

    @patch("services.offer_enrichment.enrich_offer")
    def test_one_failed_offer_does_not_abort_quick_results(self, enrich):
        def enrich_one(item, _postal_code):
            if item["url"].endswith("/1"):
                raise ValueError("pagina invalida")
            return item
        enrich.side_effect = enrich_one
        items = [{"url": "https://loja.test/1"}, {"url": "https://loja.test/2"}]
        self.assertEqual(enrich_offers(items, "01001000"), items)

    def test_postal_code_and_manual_schedule_slot(self):
        self.assertEqual(normalize_postal_code("01001-000"), "01001000")
        self.assertEqual(normalize_postal_code(None), "01001000")
        self.assertEqual(normalize_postal_code(""), "01001000")
        self.assertIsNone(normalize_postal_code("0100"))
        schedule = {
            "enabled": True,
            "start_time": "09:00",
            "end_time": "18:00",
            "days": {3},
            "repeat_every_hours": 2,
        }
        self.assertEqual(
            _due_run_key(schedule, datetime(2026, 8, 19, 16, 20)),
            "2026-08-19-1300",
        )

    def test_missing_refresh_details_do_not_inherit_previous_logistics(self):
        details = _price_detail_values({"price": 20.0, "destination_zip": "01001000"})
        self.assertIsNone(details["shipping_cost"])
        self.assertIsNone(details["delivery_max_days"])
        self.assertIsNone(details["logistics_captured_at"])
        self.assertEqual(details["destination_zip"], "01001000")


if __name__ == "__main__":
    unittest.main()
