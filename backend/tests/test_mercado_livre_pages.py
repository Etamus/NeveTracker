import json
import unittest
from services.mercado_livre_pages import initial_state,parse_primary,parse_calculator,selected_item_id
from services.product_resolver import ProductIdentity
from services.shopee_dom import search_cards


def html(state,extra=''):
    data={'appProps':{'pageProps':{'initialState':state}}}
    return '<html><head><script id="__NORDIC_RENDERING_CTX__">_n.ctx.r='+json.dumps(data)+';</script></head><body>'+extra+'</body></html>'


class MercadoLivrePagesTests(unittest.TestCase):
    def test_voltage_link_uses_the_requested_option_and_discards_old_item_id(self):
        from urllib.parse import parse_qs, urlsplit
        from services.mercado_livre_pages import voltage_variant_url
        from services.product_resolver import ProductIdentity

        html = """<div class="ui-pdp-outside_variations">
          <a href="/lavadora/p/MLB100?wid=MLB999">127V</a>
          <a href="https://other.test/lavadora/p/MLB300">220V</a>
          <a href="/lavadora/p/MLB200?wid=MLB999&amp;pdp_filters=item_id%3AMLB999&amp;picker=true">220V</a>
        </div>"""
        selected = voltage_variant_url(
            html, 'https://www.mercadolivre.com.br/lavadora/p/MLB100', ProductIdentity(sku='BWF15ABBNA'),
        )
        self.assertEqual(urlsplit(selected).path, '/lavadora/p/MLB200')
        self.assertEqual(parse_qs(urlsplit(selected).query), {'picker': ['true']})

    def test_fast_account_transport_requires_exact_postal_and_item_and_keeps_old_fallback(self):
        from unittest.mock import patch,MagicMock
        from services.mercado_livre_pages import _fast_account_calculator
        response=MagicMock(status_code=200,url='https://www.mercadolivre.com.br/shipping')
        response.text=self.quote()
        with patch('services.marketplace_intervention.enabled',return_value=True), \
             patch('services.mercado_livre_pages._account_cookie_snapshot',return_value=('dummy-session=value','Test browser')), \
             patch('curl_cffi.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value;session.get.return_value=response
            result=_fast_account_calculator('MLB123456','30130003',5)
            self.assertIsNotNone(result)
            self.assertIn('cp=30130003',session.get.call_args.kwargs['headers']['Cookie'])
            self.assertIsNone(_fast_account_calculator('MLB123456','01002020',5))
        from services import mercado_livre_pages
        mercado_livre_pages._fast_account_preferred_until=0
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)

    def pdp(self,**changes):
        event={'item_id':'MLB123456','seller_name':'Lojista real','has_stock':True,**changes}
        return html({'components':{'track':{'melidata_event':{'event_data':event}},'price':{'price':{'value':2999}}}},'<h1 class="ui-pdp-title">Geladeira Brastemp BRM46MB</h1>')

    def quote(self,postal='30130003',item='MLB123456',options=None):
        return html({'destinationValue':postal,'track':{'melidata_event':{'event_data':{'item_id':item,'shipping_promises':options or [{'type':'address','cost':15,'min_days':2,'max_days':4}]}}}})

    def test_nordic_primary_price_and_actual_seller(self):
        result=parse_primary(self.pdp(),'https://www.mercadolivre.com.br/p/MLB999?wid=MLB123456',ProductIdentity(sku='BRM46MB'))
        self.assertEqual(result['status'],'OK');self.assertEqual(result['item']['store'],'Lojista real');self.assertEqual(result['item']['price'],2999)

    def test_catalog_without_selected_item_is_not_mistaken_for_seller_item_id(self):
        self.assertIsNone(selected_item_id('https://www.mercadolivre.com.br/p/MLB999'))
        result=parse_primary(self.pdp(),'https://www.mercadolivre.com.br/p/MLB999',ProductIdentity(sku='BRM46MB'))
        self.assertIn('wid=MLB123456',result['item']['url'])

    def test_changed_seller_item_is_rejected(self):
        result=parse_primary(self.pdp(),'https://www.mercadolivre.com.br/p/MLB999?wid=MLB111111',ProductIdentity(sku='BRM46MB'))
        self.assertEqual(result['status'],'INVALID_MATCH')

    def test_stockout_is_preserved_without_price(self):
        result=parse_primary(self.pdp(has_stock=False),'https://www.mercadolivre.com.br/p/MLB999',ProductIdentity(sku='BRM46MB'))
        self.assertEqual(result['item']['availability'],'out_of_stock');self.assertIsNone(result['item']['price'])

    def test_exact_postal_code_and_item_are_mandatory(self):
        with self.assertRaises(ValueError):parse_calculator(self.quote(postal='01009902'),'MLB123456','01002020')
        with self.assertRaises(ValueError):parse_calculator(self.quote(item='MLB000000'),'MLB123456','30130003')

    def test_delivery_chooses_lowest_paid_cost_without_borrowing_other_estimate(self):
        options=[{'type':'address','cost':0,'min_days':8,'max_days':10},{'type':'address','cost':25,'min_days':1,'max_days':2},{'type':'address','cost':15,'min_days':3,'max_days':5},{'type':'agency','cost':1,'min_days':1,'max_days':1}]
        row=parse_calculator(self.quote(options=options),'MLB123456','30130003')
        self.assertEqual(row['shipping_cost'],15);self.assertEqual(row['delivery_max_days'],5);self.assertEqual(row['delivery_days_type'],'calendar')

    def test_explicit_zero_is_valid_when_there_is_no_paid_delivery(self):
        row=parse_calculator(self.quote(options=[{'type':'address','cost':0,'min_days':1,'max_days':2}]),'MLB123456','30130003')
        self.assertEqual(row['shipping_cost'],0)

    def test_missing_calculator_does_not_imply_free_shipping(self):
        with self.assertRaises(ValueError):parse_calculator('<html>Falha</html>','MLB123456','30130003')

    def test_calculator_uses_account_after_public_login_rejection_and_keeps_exact_destination(self):
        from unittest.mock import patch,MagicMock
        from services.mercado_livre_pages import quote_offer
        response=MagicMock(text='Para continuar acesse sua conta',url='https://www.mercadolivre.com.br/login',status_code=200)
        with patch('curl_cffi.requests.Session') as session,patch('services.marketplace_intervention.pending',return_value=None),patch('services.marketplace_intervention.enabled',return_value=True),patch('services.mercado_livre_pages._account_calculator',return_value=(self.quote(),'https://www.mercadolivre.com.br/shipping',200)) as account:
            session.return_value.__enter__.return_value.get.return_value=response
            result=quote_offer({'sku':'BRM46MB','url':'https://produto.mercadolivre.com.br/MLB-123456-teste'},'30130003')
        account.assert_called_once_with('MLB123456','30130003',12)
        self.assertEqual(result['shipping_cost'],15);self.assertEqual(result['destination_zip'],'30130003')

    def test_account_transport_failure_retries_without_requesting_login_or_free_shipping(self):
        from unittest.mock import patch,MagicMock
        from services.mercado_livre_pages import quote_offer
        response=MagicMock(text='Para continuar acesse sua conta',url='https://www.mercadolivre.com.br/login',status_code=200)
        with patch('curl_cffi.requests.Session') as session,patch('services.marketplace_intervention.pending',return_value=None),patch('services.marketplace_intervention.enabled',return_value=True),patch('services.mercado_livre_pages._account_calculator',return_value=None),patch('services.marketplace_intervention.require_access') as wait:
            session.return_value.__enter__.return_value.get.return_value=response
            result=quote_offer({'sku':'BRM46MB','url':'https://produto.mercadolivre.com.br/MLB-123456-teste','shipping_cost':0},'30130003')
        wait.assert_not_called();self.assertNotIn('shipping_cost',result);self.assertTrue(result['is_cached'])

    def test_account_calculator_requests_access_only_after_actual_account_refusal(self):
        from unittest.mock import patch,MagicMock
        from services.mercado_livre_pages import quote_offer
        response=MagicMock(text='Para continuar acesse sua conta',url='https://www.mercadolivre.com.br/login',status_code=200)
        with patch('curl_cffi.requests.Session') as session,patch('services.marketplace_intervention.pending',return_value=None),patch('services.marketplace_intervention.enabled',return_value=True),patch('services.mercado_livre_pages._account_calculator',return_value=(response.text,response.url,200)),patch('services.marketplace_intervention.require_access') as wait:
            session.return_value.__enter__.return_value.get.return_value=response
            result=quote_offer({'sku':'BRM46MB','url':'https://produto.mercadolivre.com.br/MLB-123456-teste'},'30130003')
        wait.assert_called_once();self.assertNotIn('shipping_cost',result)

    def test_search_dom_retains_shop_id_and_actual_visible_price(self):
        source='<a href="/Lava-loucas-BLF61AR-i.123.456"><img alt="Lava-louças Brastemp BLF61AR" src="https://image.test/1"><del>R$ 9.000,00</del><div>R$<span>3.356,93</span></div></a>'
        rows=search_cards(source);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['price'],3356.93);self.assertEqual(rows[0]['shop_id'],'123')


if __name__=='__main__':unittest.main()
