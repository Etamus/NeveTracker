import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import marketplace_intervention as access
from services import marketplace_session as sessions
from services.marketplace_validation import evidence


class MarketplaceInterventionTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        for target in [
            patch.object(access,'_path',side_effect=lambda store,kind:self.root/f'{store}-{kind}.json'),
            patch.object(sessions,'metadata_file',side_effect=lambda store:self.root/f'{store}-validation.json'),
            patch('services.marketplace_access.path',side_effect=lambda store:self.root/f'{store}-backoff.json'),
            patch('services.browser_health.record_browser_result'),
        ]:
            target.start();self.addCleanup(target.stop)

    def test_first_pending_sku_is_stable_and_release_is_store_specific_and_durable(self):
        first=access.require_access('shopee','BLF61AR','Verificação')
        self.assertEqual(access.require_access('shopee','BRE66AK','Outra falha'),first)
        access.require_access('mercado_livre','BRE66AK','Login')
        self.assertEqual(access.waiting_stores(['shopee','mercado_livre']),['mercado_livre','shopee'])
        self.assertFalse(access.ready('shopee'))
        access.release('shopee','visible-search')
        self.assertTrue(access.ready('shopee'));self.assertFalse(access.ready('mercado_livre'))
        self.assertIsNone(access.pending('shopee'))
        self.assertEqual(json.loads(sessions.metadata_file('shopee').read_text())['search_target_id'],'visible-search')
        self.assertTrue(access.released_after('shopee',datetime.utcnow()-timedelta(minutes=1)))
        access.require_access('shopee','BRE66AK','Novo bloqueio')
        self.assertFalse(access.ready('shopee'))

    def test_disabled_account_never_creates_access_wait_or_connects_to_saved_profile(self):
        access.configure('shopee',False)
        self.assertFalse(access.enabled('shopee'));self.assertIsNone(access.require_access('shopee','BLF61AR','Login'))
        self.assertIsNone(sessions.ensure_account_browser('shopee'))
        with self.assertRaises(ValueError):sessions.start_validation('shopee')
        runtime=MagicMock()
        with patch.object(sessions,'browser_endpoint') as endpoint, patch.object(sessions,'browser_options',return_value={'headless':True}):
            with sessions.account_context(runtime,'shopee'):pass
        endpoint.assert_not_called();runtime.chromium.connect_over_cdp.assert_not_called()
        runtime.chromium.launch_persistent_context.assert_not_called()
        runtime.chromium.launch.return_value.new_context.assert_called_once()
        runtime.chromium.launch.return_value.close.assert_called_once()

    def test_transport_recovery_keeps_real_access_checks_and_active_cooldowns(self):
        access.require_access('shopee', 'BLF61AR', 'Login')
        access.record_transport_recovery('shopee')
        self.assertFalse(access.ready('shopee'))
        path = access._path('shopee', 'access-request')
        state = {'status': 'retrying', 'attempts': access.MAX_TRANSPORT_RETRIES}
        access._write(path, {**state, 'retry_at': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})
        access.record_transport_recovery('shopee')
        self.assertTrue(access.transport_retries_exhausted('shopee'))
        access._write(path, {**state, 'retry_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
        access.record_transport_recovery('shopee')
        self.assertTrue(access.ready('shopee'))
        self.assertFalse(access.transport_retries_exhausted('shopee'))
        self.assertTrue(access.released_after('shopee', datetime.utcnow() - timedelta(minutes=1)))
        self.assertFalse(sessions.metadata_file('shopee').exists())

    def test_shopee_only_releases_exact_fresh_search_with_results_or_explicit_empty_state(self):
        now=datetime.now(timezone.utc);since=now.isoformat()
        state=dict(url='https://shopee.com.br/search?keyword=BLF61AR',observed_at=now.timestamp()*1000+1,rows=[{'item_basic':{'itemid':1,'name':'Lava Louças Brastemp BLF61AR'}}])
        with patch('services.shopee_live_search.read_state',return_value=state):
            self.assertTrue(evidence('shopee',None,'BLF61AR',since=since))
            self.assertFalse(evidence('shopee',None,'BRE66AK',since=since))
            state['blocked']=True;self.assertFalse(evidence('shopee',None,'BLF61AR',since=since))
            state['blocked']=False;state['observed_at']=0
            self.assertFalse(evidence('shopee',None,'BLF61AR',since=since))
            state['observed_at']=now.timestamp()*1000+1;state['rows']=[]
            self.assertFalse(evidence('shopee',None,'BLF61AR',since=since))
            state['empty']=True;self.assertTrue(evidence('shopee',None,'BLF61AR',since=since))

    def test_mercado_livre_rejects_login_pages_wrong_sku_and_stale_search(self):
        now=datetime.now(timezone.utc)
        state=dict(url='https://lista.mercadolivre.com.br/blf61ar',observed_at=now.timestamp()*1000+1,cards=8,text='Anúncios',html='<html>anúncios</html>')
        page=MagicMock();page.call.side_effect=lambda *args,**kwargs:{'result':{'value':json.dumps(state)}}
        with patch('services.mercado_livre_browser.page_status',return_value=('OK',None)):
            self.assertTrue(evidence('mercado_livre',page,'BLF61AR',since=now.isoformat()))
            self.assertFalse(evidence('mercado_livre',page,'BRE66AK',since=now.isoformat()))
            state['observed_at']=0;self.assertFalse(evidence('mercado_livre',page,'BLF61AR',since=now.isoformat()))
            state['observed_at']=now.timestamp()*1000+1
            state['url']='https://login.mercadolivre.com.br/login';state['cards']=0
            self.assertFalse(evidence('mercado_livre',page,'BLF61AR',since=now.isoformat()))

    def test_active_search_tab_preserves_user_tab_and_uses_normal_search_instead_of_new_navigation(self):
        from services.shopee_live_search import search_tab
        page=MagicMock()
        initial=dict(url='https://shopee.com.br/search?keyword=BLF61AR',observed_at=1,current='1',rows=[{}])
        fresh=dict(url='https://shopee.com.br/search?keyword=BRE66AK',observed_at=2,current='1',total='2',rows=[{'item_data':{'shopid':2,'itemid':3}}])
        with patch('services.shopee_live_search.read_state',side_effect=[initial,*[fresh]*7]),patch('services.shopee_live_search.time.sleep'):
            result=search_tab(page,'https://shopee.com.br/search?keyword=BRE66AK',5)
        self.assertEqual(len(result['rows']),1);self.assertTrue(result['more'])
        self.assertTrue(any(call.args[0]=='Input.insertText' and call.kwargs['text']=='BRE66AK' for call in page.call.call_args_list))
        self.assertFalse(any(call.args[0] in {'Runtime.enable','Page.navigate','Target.closeTarget'} for call in page.call.call_args_list))

    def test_blocked_discovery_is_fast_and_never_calls_network_until_released(self):
        from crawlers.paged_discovery import fetch_search_page
        access.require_access('shopee','BLF61AR','CAPTCHA')
        diagnostics={}
        with patch('services.shopee_session_search.search') as search:
            rows,next_url,success=fetch_search_page('shopee','https://shopee.com.br/search?keyword=BLF61AR',5,diagnostics=diagnostics)
        search.assert_not_called();self.assertFalse(success);self.assertEqual(rows,[])
        self.assertEqual(diagnostics['status'],'ACCESS_REQUIRED')

    def test_validation_waits_for_real_evidence_and_never_closes_the_user_tab(self):
        access.require_access('shopee','BLF61AR','Verifique o acesso')
        from services.marketplace_validation import search_url
        tab=dict(id='user-tab',type='page',url=search_url('shopee','BLF61AR'),webSocketDebuggerUrl='ws://page')
        browser=MagicMock();page=MagicMock();slot=MagicMock()
        with patch.object(sessions,'configuration',return_value=(self.root/'shopee',slot,'Shopee','')),patch.object(sessions,'launch_account_browser',return_value='http://127.0.0.1:9472'),patch.object(sessions,'set_status') as status,patch('services.marketplace_validation.requests.get') as get,patch('services.shopee_page_transport.CDP',side_effect=[browser,page]),patch('playwright.sync_api.sync_playwright'),patch('services.marketplace_validation.evidence',side_effect=[False,True]) as verified,patch('services.marketplace_validation.time.sleep'),patch('services.marketplace_validation._show_window'),patch.object(access,'release') as release:
            get.return_value.json.side_effect=[{'webSocketDebuggerUrl':'ws://browser'},[tab],[tab]]
            sessions._validate('shopee')
        self.assertEqual(verified.call_count,2);release.assert_called_once_with('shopee','user-tab')
        self.assertEqual(status.call_args.args[1],'connected');slot.release.assert_called_once()
        self.assertFalse(any(call.args[0] in {'Target.closeTarget','Browser.close','Runtime.enable'} for call in browser.call.call_args_list+page.call.call_args_list))

    def test_validation_finds_a_new_store_tab_opened_by_the_user(self):
        from services.marketplace_validation import _verified_other_tab
        tabs=[dict(id='other-site',type='page',url='https://evilshopee.com.br/search',webSocketDebuggerUrl='ws://foreign'),dict(id='human-search',type='page',url='https://shopee.com.br/search?keyword=BLF61AR',webSocketDebuggerUrl='ws://human')]
        with patch('services.marketplace_validation.requests.get') as get,patch('services.shopee_page_transport.CDP') as cdp,patch('services.marketplace_validation.evidence',return_value=True):
            get.return_value.json.return_value=tabs
            target=_verified_other_tab('http://127.0.0.1:9472','old-tab','shopee','BLF61AR',None,None)
        self.assertEqual(target,'human-search');cdp.assert_called_once_with('ws://human');cdp.return_value.close.assert_called_once()

    def test_quick_search_requests_human_access_when_saved_account_search_is_blocked(self):
        from services.scraper_service import _run_store_scrapers
        sessions.metadata_file('shopee').touch()
        def blocked(*args,**kwargs):
            kwargs['diagnostics'].update(status='BLOCKED',reason='Verificação solicitada')
            return [],None,False
        config=dict(key='shopee',label='Shopee',direct_scrapers=[lambda query:[]])
        with patch('crawlers.paged_discovery.fetch_search_page',side_effect=blocked):
            self.assertEqual(_run_store_scrapers(config,'BLF61AR'),[])
        self.assertEqual(access.pending('shopee')['sku'],'BLF61AR')
        with patch('crawlers.paged_discovery.fetch_search_page') as fetch:
            self.assertEqual(_run_store_scrapers(config,'BRE66AK'),[])
        fetch.assert_not_called()

    def test_quick_search_never_uses_the_account_when_disabled(self):
        from services.scraper_service import _run_store_scrapers
        access.configure('shopee',False)
        sessions.metadata_file('shopee').touch()
        with patch('crawlers.paged_discovery.fetch_search_page') as fetch:
            self.assertEqual(_run_store_scrapers(dict(key='shopee',label='Shopee',direct_scrapers=[lambda query:[]]),'BLF61AR'),[])
        fetch.assert_not_called();self.assertIsNone(access.pending('shopee'))

    def test_changed_search_url_cannot_confirm_stale_cards_from_previous_sku(self):
        from services.shopee_live_search import search_tab
        rows=[{'item_data':{'shopid':1,'itemid':2}}]
        previous=dict(url='https://shopee.com.br/search?keyword=BLF61AR',observed_at=1,rows=rows)
        stale=dict(url='https://shopee.com.br/search?keyword=BRE66AK',observed_at=2,rows=rows)
        with patch('services.shopee_live_search.read_state',side_effect=[previous,stale]),patch('services.shopee_live_search.time.monotonic',side_effect=[0,1,21]),patch('services.shopee_live_search.time.sleep'):
            result=search_tab(MagicMock(),'https://shopee.com.br/search?keyword=BRE66AK',20)
        self.assertEqual(result['status'],'TIMEOUT');self.assertFalse(result.get('blocked',False))

    def test_transient_wait_retries_automatically_without_login_request(self):
        first=access.defer_access('shopee','BLF61AR','Aba ocupada')
        self.assertEqual(first['status'],'retrying');self.assertIsNone(access.pending('shopee'))
        self.assertEqual(access.suspension('shopee')['status'],'retrying')
        self.assertEqual(access.waiting_stores(['shopee']),['shopee']);self.assertFalse(access.ready('shopee'))
        self.assertEqual(access.defer_access('shopee','BRE66AK','Outra falha'),first)
        first['retry_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        access._write(access._path('shopee','access-request'),first)
        self.assertTrue(access.ready('shopee'));self.assertIsNone(access.suspension('shopee'))
        self.assertTrue(access.released_after('shopee',datetime.utcnow()-timedelta(minutes=1)))

    def test_temporary_failure_cannot_replace_a_confirmed_human_access_wait(self):
        pending=access.require_access('shopee','BLF61AR','CAPTCHA confirmado')
        self.assertEqual(access.defer_access('shopee','BRE66AK','Timeout'),pending)
        self.assertEqual(access.pending('shopee'),pending)

    def test_product_login_already_open_and_busy_transport_do_not_request_manual_login(self):
        from services.shopee_public import collect_account_product
        for status in ['LOGIN_PENDING','BROWSER_BUSY','BROWSER_ERROR','TIMEOUT','RETRY_LATER']:
            access._path('shopee','access-request').unlink(missing_ok=True)
            with patch('services.shopee_public.collect_browser',return_value={'status':status,'reason':'Falha temporária'}):
                collect_account_product(SimpleNamespace(url='https://shopee.com.br/product/1/2'),SimpleNamespace(sku='BLF61AR'))
            self.assertIsNone(access.pending('shopee'));self.assertEqual(access.suspension('shopee')['status'],'retrying')

    def test_browser_search_timeout_falls_back_before_asking_user_and_empty_search_is_valid(self):
        from services.shopee_session_search import search
        def timeout(url,seconds,diagnostics):
            diagnostics.update(status='TIMEOUT',reason='Demora na aba')
            return [],False,False
        fetched={'status':'OK','html':'','payloads':[{'url':'https://shopee.com.br/api/v4/search/search_items?keyword=BLF61AR','data':{'items':[],'nomore':True}}]}
        with patch('services.shopee_session_search._validated_tab_search',side_effect=timeout),patch('services.shopee_page_transport.read_page',return_value=fetched) as fallback:
            diagnostics={};rows,next_url,success=search('https://shopee.com.br/search?keyword=BLF61AR',diagnostics=diagnostics)
        fallback.assert_called_once();self.assertTrue(success);self.assertEqual(rows,[])
        self.assertEqual(diagnostics['status'],'OK');self.assertIsNone(access.pending('shopee'))

    def test_confirmed_product_refusal_still_requests_user_access(self):
        from services.shopee_public import collect_account_product
        with patch('services.marketplace_session.ensure_account_browser',return_value='http://127.0.0.1:9472'),patch('services.marketplace_access.limited',return_value=None),patch('services.shopee_page_transport.read_page',return_value={'status':'OK','html':'<html>Sem informações públicas</html>','payloads':[{'data':{'error':90309999}}]}):
            result=collect_account_product(SimpleNamespace(url='https://shopee.com.br/product/1/2'),SimpleNamespace(sku='BLF61AR'))
        self.assertEqual(result['status'],'BLOCKED');self.assertIsNotNone(access.pending('shopee'))

    def test_confirmed_product_with_refused_delivery_retries_without_another_login(self):
        from services.shopee_public import collect_account_product
        verified={'status':'OK','item':{'price':100,'_access_required':True,'_logistics_error':'Entrega recusada (90309999).'}}
        with patch('services.shopee_public.collect_browser',return_value=verified):
            result=collect_account_product(SimpleNamespace(url='https://shopee.com.br/product/1/2'),SimpleNamespace(sku='BLF61AR'))
        self.assertEqual(result,verified)
        self.assertIsNone(access.pending('shopee'))
        self.assertEqual(access.suspension('shopee')['status'],'retrying')


if __name__=='__main__':unittest.main()
