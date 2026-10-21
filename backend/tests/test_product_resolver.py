import unittest

from services.product_resolver import ProductIdentity, canonical_brand, resolve_candidate
from services.scraper_service import _tokens


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

    def test_brastem_is_canonicalized_in_precise_and_quick_matching(self):
        identity = ProductIdentity(sku="W10531315", model="W10531315", brand="Brastemp")
        match = resolve_candidate(identity, {"name": "Sensor BRASTEM W10531315"})
        self.assertTrue(match.accepted)
        self.assertEqual(match.method, "brand_model_exact")
        self.assertEqual(_tokens("BRASTEM W10531315"), _tokens("Brastemp W10531315"))
        self.assertEqual(canonical_brand("BRASTEM"), "Brastemp")

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

    def test_longer_model_does_not_match_by_substring(self):
        identity = ProductIdentity(sku="ABC123", model="ABC123", brand="Whirlpool")
        match = resolve_candidate(identity, {"name": "Peça ABC1234", "url": "https://loja.test/ABC1234"})
        self.assertFalse(match.accepted)

    def test_model_split_by_punctuation_still_matches(self):
        identity = ProductIdentity(sku="W10531315", model="W10531315")
        match = resolve_candidate(identity, {"name": "Peça W105-31315"})
        self.assertTrue(match.accepted)
        self.assertEqual(match.method, "model_exact")

    def test_unrelated_url_query_does_not_confirm_model(self):
        identity = ProductIdentity(sku="ABC123", model="ABC123")
        match = resolve_candidate(identity, {
            "name": "Peça genérica",
            "url": "https://loja.test/outra-peca?tracking=ABC123",
        })
        self.assertFalse(match.accepted)

    def test_conflicting_structured_model_rejects_matching_title(self):
        identity = ProductIdentity(sku="ABC123", model="ABC123")
        match = resolve_candidate(identity, {"name": "Peça ABC123", "model": "ABC1234"})
        self.assertFalse(match.accepted)

    def test_model_with_variant_text_keeps_exact_identifier(self):
        identity = ProductIdentity(sku="ABC123", model="ABC123")
        match = resolve_candidate(identity, {"model": "ABC123 220V", "name": "Peça 220V"})
        self.assertTrue(match.accepted)


if __name__ == "__main__":
    unittest.main()
