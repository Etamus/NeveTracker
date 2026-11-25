import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services.product_resolver import ProductIdentity
from services.product_sources import source_identifiers
from services.shopee_public import item_ids, money, parse_payload, parse_public_html
from crawlers.shopee import collect_shopee


class ShopeePublicTests(unittest.TestCase):
    def setUp(self):
        self.identity = ProductIdentity(sku='W10531315', brand='Whirlpool')
        self.source = SimpleNamespace(store='shopee', seller=None, shop_id=None, item_id=None,
            url='https://shopee.com.br/Sensor-W10531315-i.42.123')
        self.item = {'shopid': 42, 'itemid': 123, 'name': 'Sensor W10531315', 'price': 2999000, 'stock': 3, 'currency': 'BRL'}

    def test_slug_and_product_urls_preserve_shop_identity(self):
        for url in [self.source.url+'?tracking=x', 'https://shopee.com.br/product/42/123/']:
            self.assertEqual(item_ids(url), ('42', '123'))
            result = source_identifiers({'url': url})
            self.assertEqual((result['shop_id'], result['item_id']), ('42', '123'))
        self.assertEqual(item_ids('https://fake.test/Sensor-W10531315-i.42.123'), (None, None))

    def test_fixed_point_conversion_including_one_real_and_less(self):
        self.assertEqual(money(100000), 1)
        self.assertEqual(money(50000), .5)
        self.assertEqual(money(2999000), 29.99)
        for value in [None, 0, -1, True, float('nan'), float('inf')]:
            self.assertIsNone(money(value))

    def test_metadata_redacted_is_not_price_or_out_of_stock(self):
        payload = {'item': {**self.item, 'price': None, 'stock': None}, 'product_price': None, 'removed_fields': ['product_price.price']}
        html = '<script type="text/mfe-initial-data">'+json.dumps({'initialState': {'PDP': payload}})+'</script>'
        result = parse_public_html(html, self.source, self.identity)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['candidate']['item_id'], '123')
        self.assertNotIn('item', result)

    def test_real_mfe_item_cache_wrapper_is_not_mistaken_for_a_product(self):
        payload = {'initialState': {'item': {'items': {'123': self.item}},
            'DOMAIN_PDP': {'data': {'PDP_BFF_DATA': {'cachedMap': {'42/123': {'item': self.item}}}}}}}
        html = '<script type="text/mfe-initial-data">'+json.dumps(payload)+'</script>'
        result = parse_public_html(html, self.source, self.identity)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['item']['price'], 29.99)

    def test_authoritative_redaction_is_not_hidden_by_a_second_partial_cache(self):
        item = {**self.item, 'price': None, 'stock': None}
        payload = {'initialState': {
            'DOMAIN_PDP': {'data': {'PDP_BFF_DATA': {'cachedMap': {'42/123': {'item': item, 'removed_fields': ['product_price.price']}}}}},
            'RW_VARIATION_SELECTION': {'data': {'itemLevel': {'item': item, 'product_price': None}}}}}
        html = '<script type="text/mfe-initial-data">'+json.dumps(payload)+'</script>'
        self.assertEqual(parse_public_html(html, self.source, self.identity)['status'], 'BLOCKED')

    def test_modern_public_payload_and_actual_shop_name(self):
        payload = {'data': {'item': {**self.item, 'price': None},
                            'product_price': {'price': {'single_value': 2999000}},
                            'shop_detailed': {'name': 'Vendedor real'}}}
        result = parse_payload(payload, self.source, self.identity)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['item']['price'], 29.99)
        self.assertEqual(result['item']['seller'], 'Vendedor real')
        self.assertNotIn('shipping_cost', result['item'])
        self.assertNotIn('delivery_max_days', result['item'])

    def test_another_item_or_currency_is_never_accepted(self):
        result = parse_payload({'data': {**self.item, 'itemid': 456}}, self.source, self.identity)
        self.assertNotEqual(result['status'], 'OK')
        result = parse_payload({'data': {**self.item, 'name': 'Sensor W99999999'}}, self.source, self.identity)
        self.assertEqual(result['status'], 'INVALID_MATCH')

    def test_personal_voucher_price_is_not_saved_as_ordinary_price(self):
        payload = {'data': {'item': {**self.item, 'price': 399900000},
            'product_price': {'has_final_price': True, 'price': {'single_value': 358708000},
                'installment_info': {'recommended_plan': {'months': 12, 'pay_per_month': 32491666}}}}}
        result = parse_payload(payload, self.source, self.identity)
        self.assertEqual(result['item']['price'], 3999)
        self.assertEqual(result['item']['installment_count'], 12)

    def test_model_stock_flags_are_used_when_numeric_stock_is_hidden(self):
        payload = {'data': {**self.item, 'stock': None, 'models': [{'has_stock': True}]}}
        self.assertEqual(parse_payload(payload, self.source, self.identity)['item']['availability'], 'in_stock')
        payload['data']['models'] = [{'has_stock': False}, {'has_stock': False}]
        self.assertEqual(parse_payload(payload, self.source, self.identity)['item']['availability'], 'out_of_stock')
        result = parse_payload({'data': {**self.item, 'currency': 'IDR'}}, self.source, self.identity)
        self.assertEqual(result['status'], 'INVALID_MATCH')

    def test_oos_without_price_is_retained_only_when_explicit(self):
        result = parse_payload({'data': {**self.item, 'price': None, 'stock': 0}}, self.source, self.identity)
        self.assertEqual(result['status'], 'OK')
        self.assertIsNone(result['item']['price'])
        self.assertEqual(result['item']['availability'], 'out_of_stock')

    def test_generic_shopee_label_does_not_merge_different_sellers(self):
        self.source.seller = 'Shopee'
        result = parse_payload({'data': self.item}, self.source, self.identity)
        self.assertEqual(result['item']['seller'], 'Shopee Loja 42')

    @patch('scrapers.shopee.create_resilient_session')
    def test_quick_scraper_keeps_oos_and_closes_session(self, create_session):
        from scrapers.shopee import scrape_shopee, cache
        cache.clear()
        api = Mock(status_code=200)
        api.json.return_value = {'items': [{'item_basic': {**self.item, 'stock': 0, 'price': 0}}]}
        create_session.return_value.get.side_effect = [Mock(status_code=200), api]
        result = scrape_shopee('W10531315')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['availability'], 'out_of_stock')
        self.assertIsNone(result[0]['price'])
        create_session.return_value.close.assert_called_once()
        cache.clear()

    def test_range_does_not_select_unconfirmed_variant(self):
        result = parse_payload({'data': {**self.item, 'price': None, 'price_min': 1000000, 'price_max': 2999000}}, self.source, self.identity)
        self.assertNotEqual(result['status'], 'OK')

    def test_200_with_anti_bot_error_is_blocked_not_empty_success(self):
        result = parse_payload({'error': 90309999, 'data': None}, self.source, self.identity)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('90309999', result['reason'])

    def test_challenge_html_does_not_parse_cart_or_price_recommendations(self):
        result = parse_public_html('<html><body>Login Necessário. Faça login para continuar. R$ 99,00</body></html>', self.source, self.identity)
        self.assertEqual(result['status'], 'BLOCKED')

    @patch('crawlers.shopee.create_resilient_session')
    def test_api_block_uses_public_html_once_and_closes_session(self, create_session):
        api = Mock(status_code=200)
        api.json.return_value = {'error': 90309999}
        html = Mock(status_code=200, text='<script type="text/mfe-initial-data">'+json.dumps({'item':self.item})+'</script>')
        create_session.return_value.get.side_effect = [api, html]
        result = collect_shopee(self.source, self.identity)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['item']['discovery_source'], 'shopee_public_html')
        self.assertEqual(create_session.return_value.get.call_count, 2)
        create_session.return_value.close.assert_called_once()

    @patch('crawlers.shopee.create_resilient_session')
    def test_rate_limit_is_not_retried_against_html(self, create_session):
        create_session.return_value.get.return_value = Mock(status_code=429)
        self.assertEqual(collect_shopee(self.source, self.identity)['status'], 'BLOCKED')
        self.assertEqual(create_session.return_value.get.call_count, 1)

    @patch('crawlers.shopee.create_resilient_session')
    def test_shop_lookup_failure_does_not_discard_valid_item(self, create_session):
        reply = Mock(status_code=200)
        reply.json.return_value = {'data': self.item}
        create_session.return_value.get.side_effect = [reply, RuntimeError('Shop unavailable')]
        result = collect_shopee(self.source, self.identity)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['item']['seller'], 'Shopee Loja 42')


if __name__ == '__main__':
    unittest.main()
