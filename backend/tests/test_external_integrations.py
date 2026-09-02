import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from schemas.integration import IntegrationUpdateRequest
from services import integration_settings
from services.external_market_providers import (
    search_apify,
    search_bright_data,
    search_dataforseo,
)


INTEGRATION_ENV_KEYS = {
    "BRIGHT_DATA_ENABLED",
    "BRIGHT_DATA_API_KEY",
    "BRIGHT_DATA_SERP_ZONE",
    "BRIGHT_DATA_USAGE_MODE",
    "BRIGHT_DATA_MAX_RESULTS",
    "APIFY_ENABLED",
    "APIFY_API_TOKEN",
    "APIFY_ACTOR_ID",
    "APIFY_USAGE_MODE",
    "APIFY_MAX_RESULTS",
    "DATAFORSEO_ENABLED",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "DATAFORSEO_USAGE_MODE",
    "DATAFORSEO_MAX_RESULTS",
    "MERCADO_LIVRE_API_ENABLED",
    "MERCADO_LIVRE_ACCESS_TOKEN",
    "MERCADO_LIVRE_USAGE_MODE",
    "MERCADO_LIVRE_MAX_RESULTS",
}


class IntegrationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.previous_env = {key: os.environ.get(key) for key in INTEGRATION_ENV_KEYS}
        for key in INTEGRATION_ENV_KEYS:
            os.environ.pop(key, None)
        self.temporary_directory = TemporaryDirectory()
        self.env_path_patch = patch.object(
            integration_settings,
            "ENV_FILE_PATH",
            Path(self.temporary_directory.name) / ".env",
        )
        self.env_path_patch.start()

    def tearDown(self):
        self.env_path_patch.stop()
        self.temporary_directory.cleanup()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_saves_secret_without_returning_it(self):
        result = integration_settings.update_integration_settings(
            "bright_data",
            IntegrationUpdateRequest(
                enabled=True,
                api_key="secret-api-key",
                zone="serp_zone",
                usage_mode="fallback",
                max_results=30,
            ),
        )

        self.assertTrue(result["configured"])
        self.assertTrue(result["enabled"])
        self.assertNotIn("api_key", result)
        self.assertEqual(result["api_key_masked"], "********-key")
        self.assertIn("BRIGHT_DATA_API_KEY='secret-api-key'", integration_settings.ENV_FILE_PATH.read_text())

    def test_blank_secret_preserves_current_key(self):
        os.environ["APIFY_API_TOKEN"] = "existing-token"
        result = integration_settings.update_integration_settings(
            "apify",
            IntegrationUpdateRequest(
                enabled=True,
                api_key="",
                actor_id="damilo/google-shopping-apify",
                usage_mode="always",
                max_results=20,
            ),
        )

        self.assertTrue(result["configured"])
        self.assertEqual(os.environ["APIFY_API_TOKEN"], "existing-token")
        self.assertEqual(result["usage_mode"], "always")

    def test_cannot_enable_incomplete_provider(self):
        with self.assertRaisesRegex(ValueError, "API key e zona"):
            integration_settings.update_integration_settings(
                "bright_data",
                IntegrationUpdateRequest(enabled=True, zone="serp_zone"),
            )

    def test_dataforseo_requires_login_and_password(self):
        with self.assertRaisesRegex(ValueError, "login e API password"):
            integration_settings.update_integration_settings(
                "dataforseo",
                IntegrationUpdateRequest(
                    enabled=True,
                    api_key="api-password",
                    login="",
                ),
            )

    def test_store_provider_supports_search_options_and_larger_free_limit(self):
        result = integration_settings.update_integration_settings(
            "mercado_livre",
            IntegrationUpdateRequest(
                enabled=True,
                api_key="ml-token",
                usage_mode="always",
                max_results=500,
            ),
        )

        self.assertTrue(result["configured"])
        self.assertTrue(result["supports_usage_mode"])
        self.assertTrue(result["supports_limit"])
        self.assertEqual(result["usage_mode"], "always")
        self.assertEqual(result["max_results"], 500)

    def test_every_provider_exposes_official_credential_link_and_cost_description(self):
        items = integration_settings.get_integration_settings()["items"]

        self.assertEqual(len(items), 4)
        for item in items:
            self.assertTrue(item["credential_url"].startswith("https://"))
            self.assertTrue(
                any(
                    marker in item["description"].lower()
                    for marker in ("grátis", "créditos", "tarifa")
                )
            )

    @patch("services.integration_settings.get_runtime_provider_settings")
    def test_fallback_runs_when_a_target_store_is_missing(self, get_settings):
        get_settings.return_value = {
            "enabled": True,
            "configured": True,
            "usage_mode": "fallback",
        }

        self.assertTrue(
            integration_settings.should_run_provider(
                "apify",
                current_offer_count=50,
                missing_store_count=1,
            )
        )


class ExternalProviderTests(unittest.TestCase):
    def setUp(self):
        self.quota_patch = patch(
            "services.external_market_providers.reserve_provider_quota"
        )
        self.quota_patch.start()

    def tearDown(self):
        self.quota_patch.stop()

    @patch("services.external_market_providers.get_runtime_provider_settings")
    @patch("services.external_market_providers.SESSION.post")
    def test_bright_data_maps_google_shopping_offer(self, post, get_settings):
        get_settings.return_value = {
            "api_key": "bright-key",
            "zone": "serp_zone",
            "max_results": 20,
        }
        response = Mock(status_code=200)
        response.json.return_value = {
            "shopping_results": [
                {
                    "title": "Sensor Brastemp W10531315",
                    "price": "R$ 129,90",
                    "link": "https://www.magazineluiza.com.br/produto/123",
                    "merchant": "Magazine Luiza",
                    "delivery": "Frete gratis, entrega em 2 a 4 dias",
                }
            ]
        }
        post.return_value = response

        result = search_bright_data("W10531315")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["price"], 129.9)
        self.assertEqual(result[0]["store"], "Magazine Luiza")
        self.assertEqual(result[0]["shipping_cost"], 0)
        self.assertEqual(result[0]["delivery_min_days"], 2)
        self.assertEqual(result[0]["delivery_max_days"], 4)
        self.assertEqual(result[0]["discovery_source"], "bright_data")

    @patch("services.external_market_providers.get_runtime_provider_settings")
    @patch("services.external_market_providers.SESSION.post")
    def test_apify_uses_brazil_profile_and_maps_offer(self, post, get_settings):
        get_settings.return_value = {
            "api_key": "apify-token",
            "actor_id": "damilo/google-shopping-apify",
            "max_results": 30,
        }
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "productTitle": "Peca Consul W10531315",
                "priceValue": 99.5,
                "productUrl": "https://lojista.example/produto",
                "sellerName": "Lojista Exemplo",
                "shippingInfo": "Entrega em 3 dias",
            }
        ]
        post.return_value = response

        result = search_apify("W10531315")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["delivery_min_days"], 3)
        self.assertEqual(result[0]["sku"], "W10531315")
        request = post.call_args
        self.assertEqual(request.kwargs["json"]["country"], "br")
        self.assertEqual(request.kwargs["json"]["language"], "pt-br")
        self.assertEqual(request.kwargs["json"]["num"], "30")

    @patch("services.external_market_providers.time.sleep")
    @patch("services.external_market_providers.get_runtime_provider_settings")
    @patch("services.external_market_providers.SESSION.get")
    @patch("services.external_market_providers.SESSION.post")
    def test_dataforseo_posts_and_collects_merchant_task(
        self,
        post,
        get,
        get_settings,
        _sleep,
    ):
        get_settings.return_value = {
            "api_key": "api-password",
            "login": "conta@example.com",
            "max_results": 20,
        }
        post_response = Mock(status_code=200)
        post_response.json.return_value = {
            "tasks": [{"id": "task-123", "status_code": 20100, "status_message": "Task Created.", "result": None}]
        }
        result_response = Mock(status_code=200)
        result_response.json.return_value = {
            "tasks": [
                {
                    "id": "task-123",
                    "status_code": 20000,
                    "result": [
                        {
                            "items": [
                                {
                                    "title": "Peca Consul W10531315",
                                    "price": {"current": 89.9},
                                    "seller": "Lojista DataForSEO",
                                    "url": "https://seller.example/item",
                                    "delivery_info": {
                                        "delivery_message": "Entrega em 3 dias"
                                    },
                                    "product_availability": "in_stock",
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        post.return_value = post_response
        get.return_value = result_response

        result = search_dataforseo("W10531315")

        self.assertEqual(result[0]["price"], 89.9)
        self.assertEqual(result[0]["delivery_min_days"], 3)
        self.assertEqual(result[0]["availability"], "in_stock")
        self.assertEqual(post.call_args.kwargs["json"][0]["location_code"], 2076)

    @patch("services.external_market_providers.time.sleep")
    @patch("services.external_market_providers.get_runtime_provider_settings")
    @patch("services.external_market_providers.SESSION.get")
    @patch("services.external_market_providers.SESSION.post")
    def test_dataforseo_expands_product_into_seller_offers(
        self,
        post,
        get,
        get_settings,
        _sleep,
    ):
        get_settings.return_value = {
            "api_key": "api-password",
            "login": "conta@example.com",
            "max_results": 20,
        }
        product_task = Mock(status_code=200)
        product_task.json.return_value = {
            "tasks": [
                {
                    "id": "product-task",
                    "status_code": 20000,
                    "result": [
                        {
                            "items": [
                                {
                                    "title": "Peca Consul W10531315",
                                    "price": 100,
                                    "seller_name": "Google Shopping",
                                    "url": "https://shopping.example/product",
                                    "product_id": "google-product-123",
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        seller_task = Mock(status_code=200)
        seller_task.json.return_value = {
            "tasks": [
                {
                    "id": "seller-task",
                    "status_code": 20100,
                    "result": None,
                }
            ]
        }
        seller_result = Mock(status_code=200)
        seller_result.json.return_value = {
            "tasks": [
                {
                    "id": "seller-task",
                    "status_code": 20000,
                    "result": [
                        {
                            "items": [
                                {
                                    "title": "Peca Consul W10531315",
                                    "url": "https://lojista.example/produto",
                                    "base_price": 95.5,
                                    "shipping_price": 14.9,
                                    "total_price": 110.4,
                                    "currency": "BRL",
                                    "seller_name": "Lojista Direto",
                                    "details": "Entrega em 2 a 4 dias",
                                    "displayed_payment_breakdown": "2x de R$ 50,00",
                                    "product_availability": "in_stock",
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        post.side_effect = [product_task, seller_task]
        get.return_value = seller_result

        result = search_dataforseo("W10531315")

        self.assertEqual(result[0]["store"], "Lojista Direto")
        self.assertEqual(result[0]["price"], 95.5)
        self.assertEqual(result[0]["shipping_cost"], 14.9)
        self.assertEqual(result[0]["delivery_min_days"], 2)
        self.assertEqual(result[0]["delivery_max_days"], 4)
        self.assertEqual(result[0]["installment_price"], 100)
        self.assertEqual(post.call_args_list[1].kwargs["json"][0]["product_id"], "google-product-123")

if __name__ == "__main__":
    unittest.main()
