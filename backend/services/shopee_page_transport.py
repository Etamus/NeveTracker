"""Read the account page's own responses; inspired by Unayung/shopee-cli (MIT).

Only Network/Page domains are enabled. Each call owns one background tab,
and never closes the account browser or existing user tabs.
"""
import base64
import json
import time
import re
from collections import deque
from urllib.parse import urlsplit

from services.extraction_resources import check_cancelled, interruptible_sleep

import requests
from websockets.sync.client import connect
from services.marketplace_session import ensure_account_browser

_last_read = 0.0
POSTAL_INTERVAL_SECONDS = 2.0


class CDP:
    def __init__(self, url):
        self.connection = connect(url, open_timeout=10, close_timeout=2, max_size=24_000_000)
        self.ws = self.connection.__enter__()
        self.sequence = 0
        self.events = deque()

    def close(self):
        self.connection.__exit__(None, None, None)

    def call(self, method, **params):
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
        deadline = time.monotonic() + getattr(self, 'command_timeout', 15)
        while time.monotonic() < deadline:
            check_cancelled()
            value = json.loads(self.ws.recv(timeout=max(.1, deadline-time.monotonic())))
            if value.get('id') == request_id:
                if value.get('error'):
                    raise RuntimeError(value['error'].get('message', 'CDP error'))
                return value.get('result', {})
            if 'method' in value:
                self.events.append(value)
        raise TimeoutError(method)


def read_page(url, timeout=15, response_paths=('/api/v4/search/search_items',), postal_code=None, postal_codes=None):
    global _last_read
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'shopee.com.br', 'www.shopee.com.br'}:
        raise ValueError('Somente páginas da Shopee Brasil são permitidas.')
    if postal_code is not None and not re.fullmatch(r'\d{8}',postal_code):raise ValueError('CEP inválido.')
    if postal_codes is not None and (not isinstance(postal_codes,list) or len(postal_codes)>200 or any(not re.fullmatch(r'\d{8}',str(cep)) for cep in postal_codes)):
        raise ValueError('Lista de CEPs inválida.')
    from services.marketplace_access import limited
    from services.marketplace_intervention import enabled,suspension as pending
    if not enabled('shopee'):return {'status':'DISABLED','reason':'Uso da conta Shopee desativado.','payloads':[]}
    access=pending('shopee')
    if access:return {'status':'ACCESS_REQUIRED' if access['status']=='waiting' else 'RETRY_LATER','reason':access['reason'],'payloads':[]}
    from services.marketplace_session import configuration, session_status
    if session_status('shopee')['status'] in {'opening', 'waiting'}:
        return {'status': 'LOGIN_PENDING', 'reason': 'Conclua o login na janela da Shopee.', 'payloads': []}
    _, slot, _, _ = configuration('shopee')
    if not slot.acquire(timeout=min(2, max(0, timeout))):
        return {'status': 'BROWSER_BUSY', 'reason': 'A sessão da Shopee está em uso. A coleta deve ser retomada.', 'payloads': []}
    try:
        blocked = limited('shopee')
        if blocked:
            return {**blocked, 'payloads': []}
        wait = 9-(time.monotonic()-_last_read)
        if wait > 0:
            interruptible_sleep(wait)
        _last_read = time.monotonic()
        if postal_codes is not None:return _read_page(url, timeout, response_paths, postal_code, postal_codes=postal_codes)
        return _read_page(url, timeout, response_paths, postal_code)
    finally:
        slot.release()


def _read_page(url, timeout, response_paths, postal_code=None, postal_codes=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'shopee.com.br', 'www.shopee.com.br'}:
        raise ValueError('Somente páginas da Shopee Brasil são permitidas.')
    endpoint = ensure_account_browser('shopee')
    if not endpoint:
        return {'status': 'BROWSER_ERROR', 'reason': 'O navegador da conta Shopee ainda não está disponível.', 'payloads': []}
    browser = page = None
    target = None
    try:
        version = requests.get(endpoint+'/json/version', timeout=3).json()
        browser = CDP(version['webSocketDebuggerUrl'])
        target = browser.call('Target.createTarget', url='about:blank', background=True)['targetId']
        tabs = requests.get(endpoint+'/json/list', timeout=3).json()
        tab = next(row for row in tabs if row['id'] == target)
        page = CDP(tab['webSocketDebuggerUrl'])
        page.call('Network.enable', maxTotalBufferSize=24_000_000, maxResourceBufferSize=12_000_000)
        page.call('Page.enable')
        page.call('Page.navigate', url=url)
        pending, finished, payloads = {}, set(), []
        deadline = time.monotonic() + max(3, min(timeout, 45))
        received_at = None
        while time.monotonic() < deadline:
            # Analytics can keep the socket busy forever. Completion depends on
            # relevant product responses settling, not on all browser traffic.
            if received_at and not pending and time.monotonic()-received_at>1.5:
                break
            if page.events:
                event = page.events.popleft()
            else:
                try:
                    event = json.loads(page.ws.recv(timeout=.3))
                except TimeoutError:
                    if received_at and time.monotonic()-received_at > 1.5:
                        break
                    continue
            params = event.get('params', {})
            if event.get('method') == 'Network.responseReceived':
                response = params.get('response', {})
                address = urlsplit(response.get('url', ''))
                if address.hostname in {'shopee.com.br', 'www.shopee.com.br'} and response.get('status') == 429:
                    from services.marketplace_access import record_limit
                    record_limit('shopee', 600)
                if address.hostname in {'shopee.com.br', 'www.shopee.com.br'} and address.path in response_paths:
                    pending[params['requestId']] = response.get('url')
            elif event.get('method') == 'Network.loadingFinished':
                finished.add(params.get('requestId'))
            for request_id in list(pending):
                if request_id not in finished:
                    continue
                address = pending.pop(request_id)
                try:
                    body = page.call('Network.getResponseBody', requestId=request_id)
                    raw = base64.b64decode(body['body']).decode() if body.get('base64Encoded') else body['body']
                    payload = json.loads(raw)
                    payloads.append({'url': address, 'data': payload})
                    if payload.get('error') == 90309999:
                        from services.marketplace_access import record_limit
                        record_limit('shopee')
                    received_at = time.monotonic()
                except (ValueError, RuntimeError, KeyError):
                    pass
        frame=page.call('Page.getFrameTree')
        final_url=(frame.get('frameTree') or {}).get('frame',{}).get('url') or url
        final_path=urlsplit(final_url).path
        if final_path.startswith('/verify/') or final_path.startswith('/buyer/login'):
            return {'status':'LOGIN_REQUIRED' if final_path.startswith('/buyer/login') else 'BLOCKED',
                    'reason':'A Shopee solicitou login para continuar.' if final_path.startswith('/buyer/login') else 'A Shopee solicitou CAPTCHA/verificação para liberar esta consulta.',
                    'payloads':payloads,'url':final_url,'html':''}
        postal_error=None;postal_errors={};postal_retry_required=False;empty_responses=0
        destinations=list(dict.fromkeys(postal_codes or ([postal_code] if postal_code else [])))
        refused_product = any((response.get('data') or {}).get('error') == 90309999 for response in payloads)
        if destinations and refused_product:
            postal_errors = dict.fromkeys(destinations, 'A Shopee recusou a consulta do anúncio (90309999).')
            postal_error = postal_errors.get(postal_code)
        elif destinations:
            from services.shopee_shipping import request_postal_payload
            for index,cep in enumerate(destinations):
                check_cancelled()
                try:
                    # Bursts produced valid first CEPs then delivery refusals.
                    # Two-second pacing completed the missing destinations in
                    # live comparisons without adding pages or dropping CEPs.
                    if index:interruptible_sleep(POSTAL_INTERVAL_SECONDS)
                    response=request_postal_payload(page,url,cep)
                    empty_responses=0 if response and response.get('data') else empty_responses+1
                    if response:
                        payloads.insert(0,{**response,'requested_postal_code':cep})
                        if (response.get('data') or {}).get('error')==90309999:
                            for remaining in destinations[index:]:postal_errors[remaining]='A Shopee recusou a consulta de entrega (90309999).'
                            break
                except Exception as exc:
                    postal_errors[cep]=str(exc)[:250];empty_responses+=1
                if empty_responses>=2:
                    # A silent transport must not consume 27 consecutive XHR
                    # timeouts. Preserve every pending CEP for an automatic retry.
                    postal_retry_required=True
                    for remaining in destinations[index:]:
                        postal_errors.setdefault(remaining,'A Shopee não respondeu à consulta; nova tentativa necessária.')
                    break
            postal_error=postal_errors.get(postal_code)
        document = page.call('DOM.getDocument')
        html = page.call('DOM.getOuterHTML', nodeId=document['root']['nodeId'])['outerHTML']
        return {'status': 'OK', 'html': html, 'payloads': payloads, 'postal_error':postal_error,'postal_errors':postal_errors,'postal_retry_required':postal_retry_required,'url':final_url}
    finally:
        try:
            if page:
                page.close()
        finally:
            if browser:
                try:
                    if target:
                        browser.call('Target.closeTarget', targetId=target)
                finally:
                    browser.close()
