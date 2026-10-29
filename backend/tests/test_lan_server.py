from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import RedirectResponse, Response
from lan_server import create_lan_app


class LanServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        (root / 'index.html').write_text('<title>NeveTracker</title>', encoding='utf-8')
        (root / 'assets').mkdir()
        (root / 'assets' / 'app.js').write_text('const api="/api";')
        api = FastAPI()

        @api.api_route('/echo', methods=['GET', 'POST', 'DELETE'])
        async def echo(request: Request):
            return {'path': request.url.path, 'query': request.url.query,
                    'auth': request.headers.get('authorization'), 'body': (await request.body()).decode()}

        @api.get('/download')
        async def download():
            return Response(b'excel-bytes', headers={'Content-Disposition': 'attachment; filename=result.xlsx'})

        @api.get('/redirect')
        async def redirect():
            return RedirectResponse('http://127.0.0.1:8000/echo')

        self.client = TestClient(create_lan_app(root, 'http://127.0.0.1:8000', transport=httpx.ASGITransport(app=api)))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_interface_and_relative_api_work_with_lan_host(self):
        self.assertIn('NeveTracker', self.client.get('/', headers={'Host': '192.168.1.5:8800'}).text)
        response = self.client.post('/api/echo?sku=A&sku=B', content='payload', headers={'Authorization': 'Bearer secret'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'path': '/echo', 'query': 'sku=A&sku=B', 'auth': 'Bearer secret', 'body': 'payload'})

    def test_download_and_api_status_are_preserved(self):
        response = self.client.get('/api/download')
        self.assertEqual(response.content, b'excel-bytes')
        self.assertIn('result.xlsx', response.headers['content-disposition'])
        self.assertEqual(self.client.get('/api/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/redirect', follow_redirects=False).headers['location'], '/api/echo')

    def test_spa_fallback_does_not_expose_private_or_missing_assets(self):
        self.assertEqual(self.client.get('/database', headers={'Accept': 'text/html'}).status_code, 200)
        for path in ['/backend/precos.db', '/.env', '/assets/missing.js']:
            self.assertEqual(self.client.get(path).status_code, 404)
