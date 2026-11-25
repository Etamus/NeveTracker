"""Anonymous mobile HTML transport; desktop/account transport remains fallback.

The public webview route was studied in omkarcloud/mercado-libre-scraper
(MIT), mercado_libre/fetch.py. No proxy, account, API key or CAPTCHA service.
"""
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from curl_cffi.requests import Session

_local = threading.local()
_slots = threading.BoundedSemaphore(2)
APP_USER_AGENT = 'MercadoLibre-Android/10.300.1 (Linux; U; Android 13; Pixel 7 Build/TQ3A)'


def public_url(url):
    try:
        parts = urlsplit(str(url or ''))
        host = parts.hostname or ''
        return (parts.scheme == 'https' and not parts.username and not parts.password
                and (host == 'mercadolivre.com.br' or host.endswith('.mercadolivre.com.br'))
                and parts.port in {None, 443})
    except ValueError:return False


def mobile_url(url):
    if not public_url(url):raise ValueError('URL fora do Mercado Livre Brasil.')
    parts = urlsplit(url)
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != 'skipwebview']
    params.append(('skipWebview', 'true'))
    return urlunsplit(parts._replace(query=urlencode(params), fragment=''))


def _session():
    now = time.monotonic()
    if getattr(_local, 'session', None) is not None and (now - _local.created > 120 or _local.requests >= 40):
        _drop_session()
    if getattr(_local, 'session', None) is None:
        _local.session = Session(impersonate='chrome')
        _local.created, _local.requests = now, 0
    _local.requests += 1
    # This transport deliberately carries no account or destination cookies.
    _local.session.cookies.clear()
    return _local.session


def _drop_session():
    session = getattr(_local, 'session', None)
    if session is not None:session.close()
    _local.session = None


def fetch_mobile_html(url, timeout=8):
    from services.mercado_livre_browser import page_status
    from services.mercado_livre_pages import initial_state
    if not public_url(url):return {'status':'INVALID_MATCH', 'reason':'URL fora do Mercado Livre Brasil.'}
    deadline = time.monotonic() + timeout
    with _slots:
        for attempt in range(2):
            target = url
            try:
                for _ in range(4):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:return {'status':'TIMEOUT', 'reason':'Tempo limite do HTML público móvel.'}
                    target = mobile_url(target)
                    response = _session().get(target, headers={
                        'User-Agent':APP_USER_AGENT, 'Accept-Language':'pt-BR,pt;q=0.9,en;q=0.7',
                        'Accept':'text/html,application/xhtml+xml',
                    }, timeout=remaining, allow_redirects=False)
                    if response.status_code in {301,302,303,307,308}:
                        location = response.headers.get('location')
                        if not location:return {'status':'HTTP_ERROR', 'reason':'Redirecionamento sem destino.'}
                        target = urljoin(target, location)
                        if not public_url(target):return {'status':'BLOCKED', 'reason':'Redirecionamento fora do Mercado Livre Brasil.'}
                        if '/login' in urlsplit(target).path or '/account-verification' in urlsplit(target).path:
                            return {'status':'LOGIN_REQUIRED', 'reason':'A página pública solicitou acesso.'}
                        continue
                    status, reason = page_status(response.text, str(response.url), response.status_code)
                    if response.status_code >= 500 and attempt == 0:break
                    if status == 'OK' and not initial_state(response.text):
                        status, reason = 'PARSER_ERROR', 'A página móvel não publicou o estado do produto ou da busca.'
                    parts=urlsplit(str(response.url))
                    clean_url=urlunsplit(parts._replace(query=urlencode([(key,value) for key,value in parse_qsl(parts.query,keep_blank_values=True) if key.lower()!='skipwebview'])))
                    return {'status':status, 'reason':reason, 'html':response.text,
                            'url':clean_url, 'transport':'mercado_livre_public_mobile'}
                else:return {'status':'HTTP_ERROR', 'reason':'Limite de redirecionamentos da página pública.'}
            except Exception as exc:
                if attempt == 1 or time.monotonic() >= deadline:
                    return {'status':'TIMEOUT' if 'timeout' in str(exc).lower() else 'HTTP_ERROR',
                            'reason':f'Falha do HTML público móvel: {type(exc).__name__}.'}
            _drop_session()
    return {'status':'HTTP_ERROR', 'reason':'A página pública não respondeu.'}
