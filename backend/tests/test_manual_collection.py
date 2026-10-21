import unittest
from collect_web_candidates import verified_candidates
from services.product_resolver import ProductIdentity


class ManualCollectionTests(unittest.TestCase):
    def test_discovery_rejects_wrong_hosts_search_pages_and_untrusted_prices(self):
        identity = ProductIdentity(sku="W11199342")
        rows = [
            {"name": "Filtro W11199342", "url": "https://www.mgparts.com.br/products/filtro-w11199342", "price": 1},
            {"name": "Filtro W11199342", "url": "https://www.mgparts.com.br.attacker.test/products/filtro-w11199342"},
            {"name": "Filtro W11199342", "url": "https://www.magazineluiza.com.br/busca/w11199342/"},
            {"name": "Filtro W11199342", "url": "http://www.mgparts.com.br/products/filtro-w11199342"},
        ]
        tasks = list(verified_candidates(rows, [identity]))
        self.assertEqual(len(tasks), 1)
        self.assertNotIn("price", tasks[0][2])
        self.assertTrue(tasks[0][2]["_seller_unknown"])
