from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from models.auth import AuthUser
from routes.integrations import integration_router
from services.auth_service import get_current_user
from services.precise_extraction import _source_due
from services.scrape_pagination import browser_retry_due


class MercadoLivreSessionTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.app = FastAPI()
        self.app.include_router(integration_router, prefix='/integrations')
        self.client = TestClient(self.app)

    @patch('services.mercado_livre_session.start_validation')
    def test_governor_cannot_open_shared_browser_session(self, start):
        self.app.dependency_overrides[get_current_user] = lambda: AuthUser(username='governor', role='governor', is_admin=True, is_owner=False)
        self.assertEqual(self.client.post('/integrations/mercado-livre/session').status_code, 403)
        start.assert_not_called()

    @patch('services.mercado_livre_session.start_validation', return_value={'status': 'opening', 'message': 'Abrindo'})
    def test_developer_starts_validation_without_returning_credentials(self, start):
        self.app.dependency_overrides[get_current_user] = lambda: AuthUser(username='developer', role='developer', is_owner=True)
        response = self.client.post('/integrations/mercado-livre/session')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'status', 'message'})
        start.assert_called_once()

    @patch('services.mercado_livre_session.validated_after', return_value=True)
    def test_new_validation_retries_sources_and_cursors_blocked_before_it(self, verified):
        attempted = datetime.utcnow() - timedelta(minutes=1)
        source = SimpleNamespace(store='mercado_livre', last_attempted_at=attempted, validation_status='blocked')
        self.assertTrue(_source_due(source, None, datetime.utcnow()))
        cursor = SimpleNamespace(store='mercado_livre', completed=False, last_status='LOGIN_REQUIRED',
                                 browser_attempted_at=attempted, last_attempted_at=attempted)
        self.assertTrue(browser_retry_due(cursor))
        self.assertFalse(_source_due(source, attempted - timedelta(seconds=1), datetime.utcnow()))
