import unittest
from unittest.mock import patch,MagicMock
from tempfile import TemporaryDirectory
from pathlib import Path
from datetime import datetime,timedelta,timezone
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from models.auth import AuthUser
from routes.integrations import integration_router
from services.auth_service import get_current_user
from services import marketplace_session as sessions


class MarketplaceAccountsTests(unittest.TestCase):
    def setUp(self):
        self.folder=TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        control=patch('services.marketplace_intervention._path',side_effect=lambda store,kind: Path(self.folder.name)/f'{store}-{kind}.json')
        control.start();self.addCleanup(control.stop)
        self.app=FastAPI();self.app.include_router(integration_router,prefix='/integrations');self.client=TestClient(self.app)

    def actor(self,role):
        self.app.dependency_overrides[get_current_user]=lambda:AuthUser(username='ACCOUNTTEST',role=role,is_owner=role=='developer')

    @patch('services.marketplace_session.start_validation',return_value={'status':'opening','message':'Abrindo janela'})
    def test_only_full_administrators_can_connect_accounts(self,start):
        for role in ['viewer','governor']:
            self.actor(role);self.assertEqual(self.client.post('/integrations/accounts/shopee/connect').status_code,403)
        start.assert_not_called()
        self.actor('administrator');self.assertEqual(self.client.post('/integrations/accounts/shopee/connect').status_code,200)
        start.assert_called_once_with('shopee')

    def test_guest_cannot_read_session_state(self):
        self.assertEqual(self.client.get('/integrations/accounts/mercado_livre').status_code,401)

    @patch('services.marketplace_session.session_status',return_value={'status':'connected','message':'Acesso validado'})
    def test_status_response_has_no_cookies_credentials_or_browser_endpoint(self,status):
        self.actor('administrator');r=self.client.get('/integrations/accounts/mercado_livre')
        self.assertEqual(set(r.json()),{'status','message','enabled','intervention'})

    def test_unregistered_store_is_rejected(self):
        self.actor('administrator');self.assertEqual(self.client.get('/integrations/accounts/untrusted').status_code,400)

    @patch('services.marketplace_session.session_status',return_value={'status':'idle','message':'Conta'})
    def test_account_toggle_is_persistent_and_admin_only(self,status):
        for store in ['shopee','mercado_livre']:
            self.actor('administrator')
            self.assertFalse(self.client.put('/integrations/accounts/'+store,json={'enabled':False}).json()['enabled'])
            self.assertFalse(self.client.get('/integrations/accounts/'+store).json()['enabled'])
            self.actor('governor')
            self.assertEqual(self.client.put('/integrations/accounts/'+store,json={'enabled':True}).status_code,403)
            self.actor('administrator')
            self.assertTrue(self.client.put('/integrations/accounts/'+store,json={'enabled':True}).json()['enabled'])

    def test_saved_profile_uses_newest_available_bundled_browser_revision(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            for revision in [1187,1243]:
                executable=root/'ms-playwright'/f'chromium-{revision}'/'chrome-win'/'chrome.exe'
                executable.parent.mkdir(parents=True);executable.touch()
            with patch.dict('os.environ',{'LOCALAPPDATA':folder},clear=True):
                options=sessions.browser_options()
            self.assertEqual(Path(options['executable_path']),root/'ms-playwright'/'chromium-1243'/'chrome-win'/'chrome.exe')

    @patch('services.marketplace_session.browser_endpoint',return_value='http://127.0.0.1:9471')
    def test_collection_does_not_close_interactive_browser_or_user_tabs(self,endpoint):
        runtime=MagicMock();context=MagicMock();browser=runtime.chromium.connect_over_cdp.return_value;browser.contexts=[context]
        with sessions.account_context(runtime,'mercado_livre') as actual:self.assertIs(actual,context)
        browser.close.assert_not_called();context.close.assert_not_called();context.pages[0].close.assert_not_called()

    def test_renderer_rejects_foreign_urls_before_opening_browser(self):
        result=sessions.render_page('shopee','https://untrusted.example/product/1')
        self.assertEqual(result['status'],'INVALID_MATCH')

    def test_validation_metadata_releases_old_blocked_sources_only_after_success(self):
        with TemporaryDirectory() as folder:
            file=Path(folder)/'validation.json';stamp=datetime.now(timezone.utc)
            file.write_text(json.dumps({'verified_at':stamp.isoformat()}))
            with patch.object(sessions,'metadata_file',return_value=file):
                self.assertTrue(sessions.validated_after('shopee',stamp.replace(tzinfo=None)-timedelta(minutes=1)))
                self.assertFalse(sessions.validated_after('shopee',stamp.replace(tzinfo=None)+timedelta(minutes=1)))

    @patch('services.marketplace_session._interactive_state',return_value={'status':'waiting','message':'Entre'})
    def test_confirm_command_reaches_login_opened_in_another_process(self,state):
        with TemporaryDirectory() as folder:
            profile=Path(folder)/'shopee'
            with patch.object(sessions,'configuration',return_value=(profile,MagicMock(),'Shopee','https://shopee.com.br/')):
                result=sessions.finish_validation('shopee')
                self.assertEqual(result['status'],'waiting');self.assertTrue((profile.parent/'shopee-confirm.request').exists())


if __name__=='__main__':unittest.main()
