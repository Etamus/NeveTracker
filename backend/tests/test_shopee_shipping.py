import unittest
from unittest.mock import patch
from types import SimpleNamespace
from services.shopee_shipping import parse_shipping


class ShopeeShippingTests(unittest.TestCase):
    @patch('services.marketplace_intervention.require_access')
    @patch('services.marketplace_intervention.defer_access')
    @patch('services.shopee_page_transport.read_page',return_value={'status':'OK','payloads':[],'postal_retry_required':True})
    def test_silent_postal_transport_is_retried_automatically_with_all_destinations_preserved(self,read,defer,require):
        from services.shopee_shipping import quote_destinations
        ceps=['01002020','30130003']
        result=quote_destinations(dict(sku='BRM46MB',source='Shopee',store='Loja A',url='https://shopee.com.br/product/1/2'),ceps)
        self.assertEqual(set(result),set(ceps))
        self.assertTrue(all(row['is_cached'] for row in result.values()))
        defer.assert_called_once();require.assert_not_called()

    @patch('services.marketplace_intervention.require_access')
    @patch('services.marketplace_intervention.defer_access')
    @patch('services.shopee_page_transport.read_page')
    def test_verified_product_with_delivery_refusal_retries_without_requesting_login(self,read,defer,require):
        from services.shopee_shipping import quote_destinations
        payload=self.payload([],postal='01002020')
        payload['data']['item'].update(name='Geladeira BRM46MB',price=10000000,stock=1)
        read.return_value={'status':'OK','payloads':[{'data':payload},{'data':{'error':90309999}}]}
        result=quote_destinations(dict(sku='BRM46MB',source='Shopee',store='Loja A',url='https://shopee.com.br/product/1/2'),['01002020'])
        self.assertTrue(result['01002020']['_price_verified'])
        defer.assert_called_once();require.assert_not_called()

    @patch('services.shopee_page_transport.read_page')
    def test_verified_price_is_preserved_when_delivery_is_not_published(self,read):
        from services.shopee_shipping import quote_destinations
        payload=self.payload([],postal='01002020')
        payload['data']['item'].update(name='Geladeira BRM46MB',price=10000000,stock=1)
        read.return_value={'status':'OK','payloads':[{'data':payload}],'postal_errors':{}}
        item=dict(sku='BRM46MB',source='Shopee',store='Loja A',url='https://shopee.com.br/product/1/2',price=900)
        result=quote_destinations(item,['01002020'])['01002020']
        self.assertTrue(result['_price_verified']);self.assertFalse(result['is_cached'])
        self.assertEqual(result['price'],100)
        self.assertNotIn('shipping_cost',result);self.assertNotIn('delivery_max_days',result)
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)

    def payload(self, channels, postal='01002020'):
        return {'data':{'item':{'shop_id':1,'item_id':2,'currency':'BRL'},'product_shipping':{'delivery_address':{'zip_code':postal},'ungrouped_channel_infos':channels}}}

    def channel(self,cost,days=3):
        return {'name':'Logística do vendedor','price':{'single_value':cost},'channel_delivery_info':{'estimated_delivery_time_min':days,'estimated_delivery_time_max':days,'display_mode':'edt_by_date'}}

    @patch('services.shopee_page_transport.read_page')
    def test_five_postal_quotes_share_one_page_and_preserve_exact_cost_and_deadline(self,read):
        from services.shopee_shipping import quote_destinations
        ceps=['30130003','80530908','20211110','90010170','01002020']
        payloads=[]
        for index,cep in enumerate(ceps):
            payload=self.payload([self.channel((10+index)*100000,index+1)],postal=cep)
            payload['data']['item'].update(name='Geladeira BRM46MB',price=10000000,stock=1)
            payloads.append({'url':'https://shopee.com.br/api/v4/pdp/get_pc','data':payload,'requested_postal_code':cep})
        read.return_value={'status':'OK','payloads':payloads,'postal_errors':{}}
        item=dict(sku='BRM46MB',name='Geladeira BRM46MB',source='Shopee',store='Loja A',url='https://shopee.com.br/product/1/2',price=100)
        result=quote_destinations(item,ceps)
        read.assert_called_once();self.assertEqual(read.call_args.kwargs['postal_codes'],ceps)
        for index,cep in enumerate(ceps):
            self.assertEqual(result[cep]['destination_zip'],cep)
            self.assertEqual((result[cep]['shipping_cost'],result[cep]['delivery_max_days']),(10+index,index+1))
            self.assertTrue(result[cep]['_price_verified']);self.assertEqual(result[cep]['price'],100)

    @patch('services.shopee_page_transport.read_page')
    def test_batch_does_not_borrow_a_quote_from_another_postal_or_item(self,read):
        from services.shopee_shipping import quote_destinations
        read.return_value={'status':'OK','payloads':[{'requested_postal_code':'30130003','data':self.payload([self.channel(1000000)],postal='01002020')}],'postal_errors':{}}
        result=quote_destinations(dict(sku='BRM46MB',source='Shopee',store='Loja A',url='https://shopee.com.br/product/1/2',price=100,shipping_cost=0,delivery_max_days=1),['30130003','01002020'])
        self.assertTrue(all('shipping_cost' not in item and 'delivery_max_days' not in item for item in result.values()))

    @patch('services.marketplace_intervention.enabled',return_value=False)
    def test_batch_account_disabled_preserves_the_existing_anonymous_fallback(self,enabled):
        from services.shopee_shipping import quote_destinations
        with patch('services.shopee_page_transport.read_page') as page:
            self.assertIsNone(quote_destinations({'sku':'BRM46MB'},['30130003','01002020']))
        page.assert_not_called()

    @patch('services.shopee_page_transport.read_page')
    def test_variant_shipping_requires_the_response_to_identify_the_requested_voltage(self, read):
        from services.shopee_shipping import quote_destinations
        payload = self.payload([self.channel(1000000)])
        payload['data']['item'].update(name='Geladeira BRM46MB 220V', price=10000000, stock=1)
        read.return_value = {'status': 'OK', 'payloads': [{'data': payload}]}
        item = {'sku': 'BRM46MBANA', 'source': 'Shopee', 'url': 'https://shopee.com.br/product/1/2'}
        self.assertNotIn('shipping_cost', quote_destinations(item, ['01002020'])['01002020'])
        item['sku'] = 'BRM46MBBNA'
        self.assertEqual(quote_destinations(item, ['01002020'])['01002020']['shipping_cost'], 10)

    def test_requires_exact_destination_and_seller_item(self):
        data=self.payload([self.channel(1000000)])
        self.assertEqual(parse_shipping(data,1,2,'30130003'),{})
        self.assertEqual(parse_shipping(data,1,3,'01002020'),{})
        self.assertEqual(parse_shipping(data,9,2,'01002020'),{})

    def test_selects_lowest_paid_service_with_its_corresponding_deadline(self):
        result=parse_shipping(self.payload([self.channel(0,7),self.channel(1500000,4),self.channel(900000,6)]),1,2,'01002020')
        self.assertEqual((result['shipping_cost'],result['delivery_max_days']),(9,6))
        self.assertEqual(result['fulfillment_type'],'3P')

    def test_explicit_free_shipping_is_kept_when_no_paid_service_exists(self):
        result=parse_shipping(self.payload([self.channel(0,0)]),1,2,'01002020')
        self.assertEqual((result['shipping_cost'],result['delivery_max_days']),(0,0))

    def test_coupon_charge_does_not_replace_published_ordinary_charge(self):
        channel=self.channel(990000);channel['price_before_discount']={'single_value':4990000}
        result=parse_shipping(self.payload([channel]),1,2,'01002020')
        self.assertEqual(result['shipping_cost'],49.9)
        self.assertIn('sem cupom',result['shipping_method'])

    def test_placeholders_missing_price_and_other_currency_are_not_quotations(self):
        self.assertEqual(parse_shipping(self.payload([self.channel(-1)]),1,2,'01002020'),{})
        data=self.payload([self.channel(1000000)]);data['data']['item']['currency']='IDR'
        self.assertEqual(parse_shipping(data,1,2,'01002020'),{})

    def test_unknown_day_unit_is_not_invented(self):
        channel=self.channel(1000000);channel['channel_delivery_info'].pop('display_mode')
        result=parse_shipping(self.payload([channel]),1,2,'01002020')
        self.assertIsNone(result['delivery_days_type'])

    @patch('services.marketplace_session.ensure_account_browser',return_value='http://127.0.0.1:9472')
    @patch('services.marketplace_access.limited',return_value=None)
    @patch('services.shopee_page_transport.read_page')
    def test_account_collection_includes_only_the_requested_postal_quote(self,page,limit,browser):
        from services.shopee_public import collect_account_product
        from services.product_resolver import ProductIdentity
        payload=self.payload([self.channel(1000000)])
        payload['data']['item'].update(title='Sensor W10531315',price=2999000,stock=2)
        page.return_value={'status':'OK','payloads':[{'url':'https://shopee.com.br/api/v4/pdp/get_pc','data':payload}]}
        source=SimpleNamespace(url='https://shopee.com.br/product/1/2',seller=None,shop_id='1',item_id='2')
        identity=ProductIdentity(sku='W10531315')
        confirmed=collect_account_product(source,identity,'01002020')
        self.assertEqual(confirmed['item']['shipping_cost'],10)
        mismatch=collect_account_product(source,identity,'30130003')
        self.assertEqual(mismatch['status'],'OK')
        self.assertNotIn('shipping_cost',mismatch['item'])
        self.assertIn('_logistics_error',mismatch['item'])
