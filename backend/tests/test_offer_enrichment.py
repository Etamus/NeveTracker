import unittest
from datetime import datetime
from types import SimpleNamespace

from bs4 import BeautifulSoup

from services.extraction_scheduler import _due_run_key
from services.offer_enrichment import (
    DETAIL_CACHE,
    _jsonld_offer_details,
    _parse_payment_details,
    normalize_postal_code,
)
from services.scraper_service import _merge_cached_offer_details, _price_detail_values


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

    def test_postal_code_and_manual_schedule_slot(self):
        self.assertEqual(normalize_postal_code("01001-000"), "01001000")
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

    def test_missing_refresh_details_preserve_previous_logistics(self):
        previous = SimpleNamespace(
            shipping_cost=17.91,
            delivery_min_days=3,
            delivery_max_days=5,
            logistics_captured_at=datetime(2026, 8, 19, 12, 0),
        )
        details = _price_detail_values({"price": 20.0}, fallback=previous)
        self.assertEqual(details["shipping_cost"], 17.91)
        self.assertEqual(details["delivery_min_days"], 3)
        self.assertEqual(details["delivery_max_days"], 5)
        self.assertEqual(
            details["logistics_captured_at"],
            datetime(2026, 8, 19, 12, 0),
        )

    def test_fresh_response_keeps_known_logistics_when_source_omits_them(self):
        url = "https://example.com/produto?tracking=1"
        cached = {
            "https://example.com/produto": {
                "url": url,
                "shipping_cost": 12.5,
                "delivery_min_days": 2,
                "delivery_max_days": 4,
                "is_cached": True,
            }
        }
        merged = _merge_cached_offer_details(
            {"url": url, "price": 100, "shipping_cost": None, "_bucket": "store"},
            cached,
        )
        self.assertEqual(merged["shipping_cost"], 12.5)
        self.assertEqual(merged["delivery_max_days"], 4)
        self.assertEqual(merged["_bucket"], "store")
        self.assertNotIn("is_cached", merged)


if __name__ == "__main__":
    unittest.main()
