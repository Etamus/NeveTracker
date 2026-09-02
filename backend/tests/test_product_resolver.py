import unittest

from services.product_resolver import ProductIdentity, resolve_candidate


class ProductResolverTests(unittest.TestCase):
    def test_exact_ean_has_full_confidence(self):
        identity = ProductIdentity(sku="W10531315", ean="7891234567890")
        match = resolve_candidate(identity, {"name": "Peça original", "ean": "7891234567890"})
        self.assertTrue(match.accepted)
        self.assertEqual(match.confidence, 1.0)
        self.assertEqual(match.method, "ean_exact")

    def test_exact_mpn_has_full_confidence(self):
        identity = ProductIdentity(sku="W10531315", mpn="W10531315")
        match = resolve_candidate(identity, {"name": "Peça Whirlpool", "mpn": "W10531315"})
        self.assertTrue(match.accepted)
        self.assertEqual(match.method, "mpn_exact")

    def test_model_and_brand_are_high_confidence(self):
        identity = ProductIdentity(sku="INT-1", model="W10531315", brand="Whirlpool")
        match = resolve_candidate(identity, {"name": "Whirlpool W10531315 original"})
        self.assertTrue(match.accepted)
        self.assertEqual(match.confidence, 0.95)

    def test_similar_accessory_is_not_accepted(self):
        identity = ProductIdentity(
            sku="W10531315",
            model="W10531315",
            brand="Whirlpool",
            name="Placa eletrônica Whirlpool W10531315",
        )
        match = resolve_candidate(
            identity,
            {"name": "Cabo adaptador universal para placa eletrônica Whirlpool"},
        )
        self.assertFalse(match.accepted)
        self.assertEqual(match.method, "invalid_match")


if __name__ == "__main__":
    unittest.main()
