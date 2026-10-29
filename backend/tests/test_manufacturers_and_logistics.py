import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from bs4 import BeautifulSoup
from scrapy.http import HtmlResponse
from services.product_resolver import ProductIdentity
from services.shipping_selection import shipping_sort_key
from services.structured_products import shipping_details
from services.offer_enrichment import _loja_integrada_rate_details, _manufacturer_shipping
from services.manufacturer_pages import selected_product
from services.fulfillment import fulfillment_details
from services.store_registry import find_target_store
from services.product_resolver import resolve_candidate
from unittest.mock import patch


class ManufacturerLogisticsTests(unittest.TestCase):
    def test_quick_path_preserves_requested_appliance_codes(self):
        from services.scraper_service import _primary_sku, _normalize_result
        for sku in 'CIX06AXONA CIX06AX BWF18AB BRO88AK CRD37MB CRM44MB BRM46MK BWJ14AB LEE18 IM8S MD-RT411FGF01 IF44 IF44S LEE15'.split():
            with self.subTest(sku=sku):
                self.assertEqual(_primary_sku(sku.lower()), sku)
        self.assertIsNone(_normalize_result({'name': 'GARANTIA ESTENDIDA IF44', 'price': 200, 'store': 'Electrolux', 'url': 'https://loja.electrolux.com.br/garantia-if44/p'}, 'IF44'))
    def test_hydrated_model_is_used_for_shipping_offer_selection(self):
        from services.offer_enrichment import _structured_offer
        product = {'sku': '1273', 'name': 'MD-RT411FGF011 - 127v', 'gtin': '7908198015390',
                   'isVariantOf': {'name': 'Geladeira Duplex Midea'},
                   'offers': {'offers': [{'price': 2099, 'seller': {'identifier': '1'}}]}}
        html = '<script type="application/ld+json">'+json.dumps({'@type':'Product','name':'Geladeira Duplex Midea','offers':{'@type':'Offer','price':2099}})+'</script>'
        html += '<script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'data':{'product':product}}}})+'</script>'
        offer, ambiguous = _structured_offer(BeautifulSoup(html, 'html.parser'), {'sku':'MD-RT411FGF01','model':'MD-RT411FGF01','store':'Midea'}, 'https://www.midea.com.br/geladeira/p?skuId=1273')
        self.assertFalse(ambiguous)
        self.assertEqual(offer['price'], 2099)
    def test_sitemap_discovery_filters_warranties_and_keeps_exact_model(self):
        from services.manufacturer_pages import sitemap_candidates
        urls = ['https://loja.electrolux.com.br/garantia-estendida-if44/p',
                'https://loja.electrolux.com.br/geladeira-if44s/p',
                'https://loja.electrolux.com.br/geladeira-if44/p']
        with patch('services.manufacturer_pages.manufacturer_sitemap', return_value=urls):
            self.assertEqual([item['url'] for item in sitemap_candidates('electrolux', 'IF44')], urls[2:])

    def test_delivery_filter_and_export_do_not_guess_from_seller(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database import Base
        from models.product import Product, PriceHistory
        from routes.products import _analytics_sheet_query
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            p = Product(sku='IF44', name='Geladeira IF44'); db.add(p); db.flush()
            db.add_all([PriceHistory(product_id=p.id, store='Parceiro', source='Amazon Brasil', price=100, fulfillment_type='2P'),
                        PriceHistory(product_id=p.id, store='Parceiro', source='Amazon Brasil', price=110, fulfillment_type='3P'),
                        PriceHistory(product_id=p.id, store='Outro', source='Amazon Brasil', price=120)])
            # The latest-price query is per product. Use separate products for
            # independent listings, as the real persistence contract requires.
            db.flush()
            prices = db.query(PriceHistory).all()
            for index, price in enumerate(prices):
                listing=Product(sku='IF44',name='Geladeira IF44');db.add(listing);db.flush();price.product_id=listing.id
            db.commit()
            self.assertEqual(_analytics_sheet_query(db, 'IF44', '', None, None, ['2P']).count(), 1)
            self.assertEqual(_analytics_sheet_query(db, 'IF44', '', None, None, ['3P']).count(), 1)
            self.assertEqual(_analytics_sheet_query(db, 'IF44', '', None, None, ['unknown']).count(), 1)
        engine.dispose()
    def test_appliance_codes_do_not_accept_warranty_or_spare_parts(self):
        identity = ProductIdentity(sku='IF44', model='IF44')
        self.assertFalse(resolve_candidate(identity, {'name': 'GARANTIA ESTENDIDA (IF44)'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Placa geladeira IF44'}).accepted)
        self.assertFalse(resolve_candidate(identity, {'name': 'Geladeira IF44S'}).accepted)
        self.assertTrue(resolve_candidate(identity, {'name': 'Geladeira IF44'}).accepted)
    def test_cheapest_paid_shipping_keeps_its_own_lead_time(self):
        rates = [{'price': 0, 'name': 'Promocional', 'deliveryTime': 8},
                 {'price': 25, 'name': 'Expresso', 'deliveryTime': 1},
                 {'price': 12, 'name': 'Normal', 'deliveryTime': 5}]
        result = _loja_integrada_rate_details(rates)
        self.assertEqual((result['shipping_cost'], result['delivery_max_days']), (12, 5))
        self.assertEqual(_loja_integrada_rate_details(rates[:1])['shipping_cost'], 0)

    def test_structured_quote_prefers_paid_and_skips_wrong_destination(self):
        details = [{'shippingRate': {'value': cost}, 'shippingDestination': {'postalCode': cep},
                    'deliveryTime': {'transitTime': {'minValue': days, 'maxValue': days, 'unitCode': 'DAY'}}}
                   for cost, cep, days in [(0, '01001000', 9), (15, '01001000', 4), (1, '99999999', 1)]]
        result = shipping_details({'shippingDetails': details}, '01001000')
        self.assertEqual((result['shipping_cost'], result['delivery_max_days']), (15, 4))

    def test_missing_cost_never_becomes_zero(self):
        self.assertLess(shipping_sort_key(12), shipping_sort_key(0))
        self.assertLess(shipping_sort_key(0), shipping_sort_key(None))

    def test_delivery_requires_explicit_evidence(self):
        self.assertEqual(fulfillment_details('<div>Vendido por Parceiro</div>', 'Amazon Brasil', 'Parceiro'), {})
        result = fulfillment_details('<div id="merchant-info">Enviado por Amazon. Vendido por Parceiro</div>', 'Amazon Brasil', 'Parceiro')
        self.assertEqual(result['fulfillment_type'], '2P')
        result = fulfillment_details('<div data-testid="shipping">Entregue por Parceiro</div>', 'Magazine Luiza', 'Parceiro')
        self.assertEqual(result['fulfillment_type'], '3P')
        unknown = fulfillment_details('<div data-testid="shipping">Entregue por Transportadora XYZ</div>', 'Magazine Luiza', 'Parceiro')
        self.assertNotIn('fulfillment_type', unknown)

    def test_brand_cannot_change_marketplace(self):
        self.assertEqual(find_target_store('Magazine Luiza', 'Brastemp', 'https://www.magazineluiza.com.br/a/p/123/')['key'], 'magalu')

    def test_selected_manufacturer_not_recommendation_or_similar_model(self):
        product = {'sku': '123', 'name': '220V', 'isVariantOf': {'name': 'Geladeira Consul CRM44MB'},
                   'offers': {'offers': [{'price': 1800, 'availability': 'https://schema.org/OutOfStock', 'seller': {'identifier': '1'}}]}}
        raw = json.dumps({'props': {'pageProps': {'data': {'product': product}}}})
        response = HtmlResponse(url='https://www.consul.com.br/geladeira/p', body=('<script id="__NEXT_DATA__">'+raw+'</script>').encode(), encoding='utf-8')
        source = SimpleNamespace(store='consul')
        self.assertIsNone(selected_product(response, source, ProductIdentity(sku='CRM44MC'), 'Consul'))
        result = selected_product(response, source, ProductIdentity(sku='CRM44MB'), 'Consul')
        self.assertEqual(result['offers'][0]['seller']['name'], 'Consul')
        self.assertIn('skuId=123', result['offers'][0]['url'])

    def test_public_quote_uses_cents_delivery_only_and_matching_item(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'logisticsInfo': [{'itemIndex': 0, 'slas': [
            {'price': 0, 'deliveryChannel': 'pickup-in-point', 'shippingEstimate': '1bd'},
            {'price': 0, 'deliveryChannel': 'delivery', 'shippingEstimate': '8bd'},
            {'price': 1590, 'deliveryChannel': 'delivery', 'shippingEstimate': '4bd', 'name': 'Normal'}]}]}
        product = {'sku': '123', 'offers': {'offers': [{'seller': {'identifier': '1'}}]}}
        soup = BeautifulSoup('<script id="__NEXT_DATA__">'+json.dumps({'props': {'pageProps': {'data': {'product': product}}}})+'</script>', 'html.parser')
        result = _manufacturer_shipping(session, soup, 'https://www.consul.com.br/a/p?skuId=123', '01001000', {})
        self.assertEqual((result['shipping_cost'], result['delivery_max_days']), (15.9, 4))
        self.assertEqual(session.post.call_args.kwargs['json']['items'], [{'id': '123', 'quantity': 1, 'seller': '1'}])
        session.post.reset_mock()
        self.assertEqual(_manufacturer_shipping(session, soup, 'https://www.consul.com.br/a/p?skuId=456', '01001000', {}), {})
        session.post.assert_not_called()
