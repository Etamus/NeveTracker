"""Serve the built interface and a same-origin proxy to the local API.

Reusing the loopback API avoids a second extraction worker against the same DB.
Only dist/ is exposed as static files; the workspace and database stay private.
"""
from contextlib import asynccontextmanager
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


class InterfaceFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            accept = dict(scope.get('headers', [])).get(b'accept', b'').decode()
            if exc.status_code == 404 and '.' not in Path(path).name and 'text/html' in accept:
                return await super().get_response('index.html', scope)
            raise


def create_lan_app(directory, backend_url, transport=None):
    @asynccontextmanager
    async def lifespan(application):
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5), trust_env=False,
                                     follow_redirects=False, transport=transport) as client:
            application.state.backend_client = client
            yield

    application = FastAPI(title='NeveTracker LAN', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.get('/server-health')
    async def health():
        return {'service': 'NeveTracker LAN', 'api_url': '/api'}

    @application.api_route('/api', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
    @application.api_route('/api/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
    async def proxy(request: Request, path=''):
        # A fixed upstream and absolute path prevent user-controlled host routing.
        target = httpx.URL(backend_url).copy_with(path='/' + path.lstrip('/'), query=request.url.query.encode())
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_HEADERS]
        client = request.app.state.backend_client
        try:
            upstream = await client.send(client.build_request(request.method, target, headers=headers,
                                                              content=await request.body()), stream=True)
        except httpx.RequestError:
            return JSONResponse({'detail': 'Backend indisponivel. Consulte os logs do servidor.'}, status_code=502)
        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
        location = response_headers.get('location')
        if location and location.startswith(backend_url.rstrip('/') + '/'):
            response_headers['location'] = '/api' + location[len(backend_url.rstrip('/')):]
        elif location and location.startswith('/') and not location.startswith('//'):
            response_headers['location'] = '/api' + location
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=response_headers, background=BackgroundTask(upstream.aclose))

    application.mount('/', InterfaceFiles(directory=str(directory), html=True, check_dir=False), name='interface')
    return application


app = create_lan_app(Path(__file__).resolve().parents[1] / 'dist',
                     os.getenv('NEVETRACKER_BACKEND_URL', 'http://127.0.0.1:8000'))
