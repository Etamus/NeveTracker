import unittest
from unittest.mock import patch, MagicMock
from collections import deque
from services.shopee_session_search import parse_search, search
from services.shopee_page_transport import read_page, _read_page


class ShopeeSessionSearchTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)

    def test_modern_search_does_not_publish_account_voucher_price(self):
        rows, more = parse_search({'items': [{'item_basic': None,
            'item_card_displayed_asset': {'name': 'Geladeira BRM46MB'},
            'item_data': {'shopid': 12, 'itemid': 34, 'item_card_display_price': {
                'price': 254748000, 'applied_product_promo_price': 286900000,
                'recommended_platform_voucher_info': {'voucher_discount': 10000000}}}}], 'nomore': False})
        self.assertEqual(rows[0]['price'], 2869)
        self.assertNotIn('cash_price', rows[0])
        self.assertEqual(rows[0]['url'], 'https://shopee.com.br/product/12/34')
        self.assertTrue(more)

    def test_legacy_stockout_remains_visible_without_price(self):
        rows, more = parse_search({'items': [{'item_basic': {'shopid': 12, 'itemid': 34,
            'name': 'BRM46MB', 'stock': 0, 'price': 100000}}], 'nomore': True})
        self.assertEqual(rows[0]['availability'], 'out_of_stock')
        self.assertIsNone(rows[0]['price'])
        self.assertFalse(more)

    def test_foreign_page_cannot_use_account_browser(self):
        with patch('services.shopee_page_transport.ensure_account_browser') as endpoint:
            with self.assertRaises(ValueError):
                read_page('https://untrusted.example/product/1/2')
            endpoint.assert_not_called()

    def test_session_validation_does_not_accept_unrelated_recommendations(self):
        from services.marketplace_validation import evidence
        state={'url':'https://shopee.com.br/search?keyword=BLF61AR','blocked':False,'empty':False,
               'rows':[{'item_basic':{'name':'Gabinete Gamer Preto'}}]}
        with patch('services.shopee_live_search.read_state',return_value=state):
            self.assertFalse(evidence('shopee',MagicMock(),'BLF61AR'))
            state['rows']=[{'item_basic':{'name':'Lava Louças Brastemp BLF61AR'}}]
            self.assertTrue(evidence('shopee',MagicMock(),'BLF61AR'))
            state.update(rows=[],empty=True)
            self.assertTrue(evidence('shopee',MagicMock(),'BLF61AR'))
            state['blocked']=True
            self.assertFalse(evidence('shopee',MagicMock(),'BLF61AR'))

    def test_visible_empty_search_phrase_is_recognized_by_browser_reader(self):
        from playwright.sync_api import sync_playwright
        from services.shopee_live_search import read_state
        with sync_playwright() as runtime:
            browser=runtime.chromium.launch(headless=True)
            try:
                page=browser.new_page();page.set_content('<html><body>Nenhum resultado foi encontrado</body></html>')
                class Adapter:
                    def call(self,method,**kwargs):return {'result':{'value':page.evaluate(kwargs['expression'])}}
                state=read_state(Adapter())
                self.assertTrue(state['empty']);self.assertFalse(state['blocked'])
            finally:browser.close()

    @patch('services.shopee_page_transport.ensure_account_browser',return_value='http://127.0.0.1:9472')
    @patch('services.shopee_page_transport.requests.get')
    @patch('services.shopee_page_transport.CDP')
    @patch('services.shopee_page_transport.time.monotonic',side_effect=[0,1,4])
    def test_captcha_redirect_is_blocked_instead_of_successful_parser_error(self,clock,cdp,get,endpoint):
        browser,page=MagicMock(),MagicMock();cdp.side_effect=[browser,page]
        browser.call.return_value={'targetId':'owned'}
        get.side_effect=[MagicMock(json=lambda:{'webSocketDebuggerUrl':'ws://browser'}),MagicMock(json=lambda:[{'id':'owned','webSocketDebuggerUrl':'ws://page'}])]
        page.events=deque();page.ws.recv.side_effect=TimeoutError
        page.call.return_value={'frameTree':{'frame':{'url':'https://shopee.com.br/verify/captcha'}}}
        result=_read_page('https://shopee.com.br/search?keyword=W10531315',3,('/api/v4/search/search_items',))
        self.assertEqual(result['status'],'BLOCKED')
        self.assertIn('CAPTCHA',result['reason'])
        browser.call.assert_any_call('Target.closeTarget',targetId='owned')

    @patch('services.marketplace_session.session_status', return_value={'status': 'waiting'})
    def test_login_in_progress_never_holds_extraction_waiting_for_login_lock(self, status):
        with patch('services.shopee_page_transport.ensure_account_browser') as endpoint:
            self.assertEqual(read_page('https://shopee.com.br/search?keyword=BRM46MB')['status'], 'LOGIN_PENDING')
            endpoint.assert_not_called()

    @patch('services.shopee_page_transport.ensure_account_browser', return_value='http://127.0.0.1:9472')
    @patch('services.shopee_page_transport.requests.get')
    @patch('services.shopee_page_transport.CDP')
    @patch('services.shopee_page_transport.time.monotonic', side_effect=[0, 1, 4])
    def test_reader_closes_only_its_owned_tab_without_runtime_instrumentation(self, clock, cdp, get, endpoint):
        browser, page = MagicMock(), MagicMock()
        cdp.side_effect = [browser, page]
        browser.call.return_value = {'targetId': 'owned'}
        get.side_effect = [MagicMock(json=lambda: {'webSocketDebuggerUrl': 'ws://127.0.0.1/browser'}),
                           MagicMock(json=lambda: [{'id': 'owned', 'webSocketDebuggerUrl': 'ws://127.0.0.1/page'}])]
        page.events = deque()
        page.ws.recv.side_effect = TimeoutError
        page.call.side_effect = lambda method, **kwargs: {'root': {'nodeId': 1}} if method == 'DOM.getDocument' else {'outerHTML': '<html></html>'}
        result = _read_page('https://shopee.com.br/search?keyword=BRM46MB', 3, ('/api/v4/search/search_items',))
        self.assertEqual(result['status'], 'OK')
        browser.call.assert_any_call('Target.closeTarget', targetId='owned')
        self.assertFalse(any(call.args[0] == 'Runtime.enable' for call in page.call.call_args_list))
        self.assertFalse(any(call.args[0] == 'Browser.close' for call in browser.call.call_args_list))

    @patch('services.shopee_page_transport.ensure_account_browser',return_value='http://127.0.0.1:9472')
    @patch('services.shopee_page_transport.requests.get')
    @patch('services.shopee_page_transport.CDP')
    @patch('services.shopee_page_transport.time.monotonic',side_effect=[0,1,4])
    def test_postal_batch_opens_one_owned_tab_for_five_destinations(self,clock,cdp,get,endpoint):
        browser,page=MagicMock(),MagicMock();cdp.side_effect=[browser,page]
        browser.call.return_value={'targetId':'owned'}
        get.side_effect=[MagicMock(json=lambda:{'webSocketDebuggerUrl':'ws://browser'}),MagicMock(json=lambda:[{'id':'owned','webSocketDebuggerUrl':'ws://page'}])]
        page.events=deque();page.ws.recv.side_effect=TimeoutError
        page.call.side_effect=lambda method,**kwargs:{'root':{'nodeId':1}} if method=='DOM.getDocument' else {'outerHTML':'<html></html>'}
        ceps=['30130003','80530908','20211110','90010170','01002020']
        with patch('services.shopee_shipping.request_postal_payload',side_effect=lambda page,url,cep:{'url':url,'data':{'data':{'product_shipping':{'delivery_address':{'zip_code':cep}}}}}) as quote,patch('services.shopee_page_transport.time.sleep') as pace:
            result=_read_page('https://shopee.com.br/product/1/2',3,('/api/v4/pdp/get_pc',),postal_codes=ceps)
        self.assertEqual(quote.call_count,5)
        self.assertEqual([call.args[0] for call in pace.call_args_list],[2.0]*4)
        self.assertEqual([call.args[0] for call in browser.call.call_args_list].count('Target.createTarget'),1)
        self.assertEqual({payload['requested_postal_code'] for payload in result['payloads']},set(ceps))
        browser.call.assert_any_call('Target.closeTarget',targetId='owned')

    @patch('services.shopee_page_transport.ensure_account_browser', return_value='http://127.0.0.1:9472')
    @patch('services.shopee_page_transport.requests.get')
    @patch('services.shopee_page_transport.CDP')
    @patch('services.shopee_page_transport.time.monotonic', side_effect=[0, 1, 2, 2, 4])
    def test_refused_product_does_not_attempt_any_destination(self, clock, cdp, get, endpoint):
        browser, page = MagicMock(), MagicMock()
        cdp.side_effect = [browser, page]
        browser.call.return_value = {'targetId': 'owned'}
        get.side_effect = [MagicMock(json=lambda: {'webSocketDebuggerUrl': 'ws://browser'}),
                           MagicMock(json=lambda: [{'id': 'owned', 'webSocketDebuggerUrl': 'ws://page'}])]
        page.events = deque([
            {'method': 'Network.responseReceived', 'params': {'requestId': 'pdp', 'response': {'url': 'https://shopee.com.br/api/v4/pdp/get_pc'}}},
            {'method': 'Network.loadingFinished', 'params': {'requestId': 'pdp'}},
        ])
        replies = {'Network.getResponseBody': {'body': '{"error": 90309999}'},
                   'DOM.getDocument': {'root': {'nodeId': 1}}, 'DOM.getOuterHTML': {'outerHTML': '<html></html>'}}
        page.call.side_effect = lambda method, **kwargs: replies.get(method, {})
        ceps = ['30130003', '80530908']
        with patch('services.shopee_shipping.request_postal_payload') as quote:
            result = _read_page('https://shopee.com.br/product/1/2', 3, ('/api/v4/pdp/get_pc',), postal_codes=ceps)
        quote.assert_not_called()
        self.assertEqual(set(result['postal_errors']), set(ceps))
        self.assertEqual(result['payloads'][0]['data']['error'], 90309999)

    @patch('services.shopee_page_transport.ensure_account_browser',return_value='http://127.0.0.1:9472')
    @patch('services.shopee_page_transport.requests.get')
    @patch('services.shopee_page_transport.CDP')
    @patch('services.shopee_page_transport.time.monotonic',side_effect=[0,1,4])
    def test_silent_delivery_transport_defers_remaining_postals_after_two_empty_responses(self,clock,cdp,get,endpoint):
        browser,page=MagicMock(),MagicMock();cdp.side_effect=[browser,page]
        browser.call.return_value={'targetId':'owned'}
        get.side_effect=[MagicMock(json=lambda:{'webSocketDebuggerUrl':'ws://browser'}),MagicMock(json=lambda:[{'id':'owned','webSocketDebuggerUrl':'ws://page'}])]
        page.events=deque();page.ws.recv.side_effect=TimeoutError
        page.call.side_effect=lambda method,**kwargs:{'root':{'nodeId':1}} if method=='DOM.getDocument' else {'outerHTML':'<html></html>'}
        ceps=['30130003','80530908','20211110','90010170','01002020']
        with patch('services.shopee_shipping.request_postal_payload',return_value={'data':{}}) as quote,patch('services.shopee_page_transport.time.sleep'):
            result=_read_page('https://shopee.com.br/product/1/2',3,('/api/v4/pdp/get_pc',),postal_codes=ceps)
        self.assertEqual(quote.call_count,2)
        self.assertTrue(result['postal_retry_required'])
        self.assertTrue(set(ceps[2:])<=set(result['postal_errors']))
        browser.call.assert_any_call('Target.closeTarget',targetId='owned')

    @patch('services.marketplace_access.limited', return_value=None)
    @patch('services.shopee_page_transport.read_page')
    def test_successful_empty_search_is_not_reported_as_login_failure(self, page, limit):
        page.return_value = {'status': 'OK', 'html': '', 'payloads': [{'url': 'https://shopee.com.br/api/v4/search/search_items?keyword=BRM46MB', 'data': {'items': [], 'nomore': True}}]}
        diagnostics = {}
        self.assertEqual(search('https://shopee.com.br/search?keyword=BRM46MB', diagnostics=diagnostics), ([], None, True))
        self.assertEqual(diagnostics['status'], 'OK')

    @patch('services.marketplace_access.limited', return_value=None)
    @patch('services.shopee_page_transport.read_page')
    def test_other_keyword_response_is_not_attributed_to_request(self, page, limit):
        page.return_value = {'status': 'OK', 'html': '', 'payloads': [{'url': 'https://shopee.com.br/api/v4/search/search_items?keyword=OTHER', 'data': {'items': [{'item_basic': {'name': 'OTHER', 'shopid': 1, 'itemid': 2, 'price': 100000}}]}}]}
        self.assertEqual(search('https://shopee.com.br/search?keyword=BRM46MB')[0], [])
