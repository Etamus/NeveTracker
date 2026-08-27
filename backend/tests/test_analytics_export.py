import unittest

from openpyxl import load_workbook

from schemas.product import AnalyticsExportRequest
from services.analytics_export import build_analytics_workbook


class AnalyticsExportTests(unittest.TestCase):
    def test_complete_offer_columns_are_exported(self):
        payload = AnalyticsExportRequest.model_validate(
            {
                "query": "SKU1, SKU2",
                "rows": [
                    {
                        "sku": "W10531315",
                        "name": "Sensor",
                        "subfamily": "Refrigeração",
                        "sku_category": "Peças",
                        "cluster": "Linha branca",
                        "market_type": "Competitivo",
                        "store_count": 1,
                        "offer_count": 1,
                        "pma": 11.61,
                        "cheapest_price": 11.61,
                        "cheapest_store": "Dufrio",
                        "offers": [
                            {
                                "store": "Dufrio",
                                "source": "Dufrio",
                                "current_price": 11.61,
                                "cash_price": 11.61,
                                "installment_price": 12.90,
                                "payment_difference": 1.29,
                                "shipping_cost": 17.91,
                                "delivery_min_days": 10,
                                "delivery_max_days": 10,
                                "url": "https://example.com/product",
                            }
                        ],
                    }
                ],
            }
        )
        workbook = load_workbook(build_analytics_workbook(payload), read_only=True)
        headers = list(next(workbook["Ofertas"].iter_rows(values_only=True)))
        self.assertIn("À vista", headers)
        self.assertIn("Frete", headers)
        self.assertIn("Prazo máximo (dias)", headers)
        self.assertIn("Subfamília", headers)
        self.assertIn("Mercado", headers)
        self.assertEqual(workbook["Analytics"]["D2"].value, "Refrigeração")
        self.assertEqual(workbook["Analytics"]["G2"].value, "Competitivo")
        self.assertEqual(workbook.sheetnames, ["Analytics", "Lojistas", "Ofertas"])


if __name__ == "__main__":
    unittest.main()
