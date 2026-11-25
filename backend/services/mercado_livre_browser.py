"""Anonymous, persistent public Mercado Livre transport. No tokens or paid proxies."""

from pathlib import Path
import re
import sys
from threading import BoundedSemaphore
from urllib.parse import urlparse
import time

from bs4 import BeautifulSoup


PROFILE_DIR = Path(__file__).resolve().parents[2] / ".tools" / "browser" / "mercado-livre"
BROWSER_SLOT = BoundedSemaphore(1)
HTTP_SLOT = BoundedSemaphore(1)
_http_session = None


def _hide_owned_windows(driver_pid):
    """Hide only windows belonging to the driver we just launched on Windows."""
    if sys.platform != 'win32' or not isinstance(driver_pid, int):
        return
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('exe', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return
    parents = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            parents[entry.pid] = entry.parent
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {driver_pid}
    while True:
        children = {pid for pid, parent in parents.items() if parent in owned}
        if children.issubset(owned):
            break
        owned.update(children)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def hide(window, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in owned:
            user.ShowWindow(window, 0)
        return True
    user.EnumWindows(hide, 0)


def allowed_url(url):
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br"))


def public_http_available(url):
    from services.browser_health import browser_available
    family = 'catalog' if re.search(r'/(?:p|up)/', urlparse(url).path) else 'listing'
    return browser_available(url, 'http_' + family)


def page_status(html, final_url, status_code=200):
    if not allowed_url(final_url):
        return "BLOCKED", "Mercado Livre redirecionou para outro dominio"
    if status_code in {404, 410}:
        return "NOT_FOUND", f"HTTP {status_code}"
    if status_code in {401, 403, 429}:
        return "BLOCKED", f"HTTP {status_code}"
    if status_code != 200:
        return "HTTP_ERROR", f"HTTP {status_code}"
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    if re.search(r"/gz/account-verification|/registration|/login(?:[/?]|$)", final_url, re.I) or re.search(
        r"para continuar.{0,30}(?:acesse|entre|ingress|ingres).{0,15}(?:conta|cuenta)", text, re.I,
    ):
        return "LOGIN_REQUIRED", "Mercado Livre solicitou acesso a uma conta nesta sessao"
    if re.search(r"captcha|robot check|access denied|acesso negado|suspicious.traffic", text, re.I):
        return "BLOCKED", "Mercado Livre retornou verificacao em vez dos anuncios"
    if re.search(r"this page requires javascript|enable javascript|continuar.{0,20}javascript", text, re.I):
        return "JS_REQUIRED", "Mercado Livre retornou uma verificacao JavaScript"
    if re.search(r"hubo un error accediendo|erro ao acessar esta p[aá]gina", text, re.I):
        return "BLOCKED", "Mercado Livre retornou uma pagina de erro de acesso"
    return "OK", None


def fetch_public_html(url, timeout=15, use_browser=False, page_handler=None):
    if not allowed_url(url):
        return {"status": "INVALID_MATCH", "reason": "URL fora do Mercado Livre Brasil"}
    if use_browser:
        return _render_public_html(url, timeout, page_handler)
    from services.mercado_livre_mobile import fetch_mobile_html
    started=time.monotonic()
    mobile=fetch_mobile_html(url,timeout=min(10,max(1,timeout*.6)))
    if mobile.get('status') in {'OK','NOT_FOUND','INVALID_MATCH'}:return mobile
    timeout=max(1,timeout-(time.monotonic()-started))
    from services.browser_health import browser_available, record_browser_result
    family = 'catalog' if re.search(r'/(?:p|up)/', urlparse(url).path) else 'listing'
    channel = 'http_' + family
    if not browser_available(url, channel):
        return {'status': 'LOGIN_REQUIRED', 'reason': 'HTTP em pausa temporaria nesta familia de paginas apos duas exigencias de login; nenhuma nova pagina ou preco confirmado.'}
    global _http_session
    try:
        from curl_cffi.requests import Session
        from services.extraction_resources import remaining_request_time
        with HTTP_SLOT:
            remaining = remaining_request_time()
            if _http_session is None:
                _http_session = Session(impersonate="chrome")
            response = _http_session.get(url, timeout=min(timeout, remaining) if remaining is not None else timeout)
        status, reason = page_status(response.text, response.url, response.status_code)
        if status in {'LOGIN_REQUIRED', 'OK'}:
            record_browser_result(url, status, channel)
        return {"status": status, "reason": reason, "html": response.text, "url": response.url}
    except Exception as exc:
        return {"status": "TIMEOUT" if "timeout" in str(exc).lower() else "HTTP_ERROR", "reason": str(exc)[:200]}


def _render_public_html(url, timeout, page_handler):
    from services.marketplace_intervention import suspension as pending,enabled
    access=pending('mercado_livre')
    if access:return {'status':'ACCESS_REQUIRED' if access['status']=='waiting' else 'RETRY_LATER','reason':access['reason']}
    from patchright.sync_api import TimeoutError as BrowserTimeout, sync_playwright
    from services.mercado_livre_session import session_status
    from services.browser_health import browser_available, record_browser_result
    if not browser_available(url):
        return {'status': 'RETRY_LATER', 'reason': 'Navegador em pausa temporaria neste host apos bloqueios confirmados; HTTP continua disponivel.'}
    if enabled('mercado_livre') and session_status()['status'] in {'opening', 'waiting'}:
        return {'status': 'BROWSER_ERROR', 'reason': 'Conclua a validacao na janela do Mercado Livre e atualize novamente.'}

    with BROWSER_SLOT:
        try:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as runtime:
                from services.marketplace_session import account_context
                manager=account_context(runtime,'mercado_livre',visible=sys.platform=='win32')
                context=manager.__enter__()
                try:
                    _hide_owned_windows(runtime._impl_obj._connection._transport._proc.pid)
                    page = context.new_page()
                    page.set_default_timeout(timeout * 1000)
                    # Keep scripts and styles intact: the public access check needs JS.
                    page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "font", "media"} else route.continue_())
                    if not context.cookies("https://www.mercadolivre.com.br/"):
                        try:
                            page.goto("https://www.mercadolivre.com.br/", wait_until="domcontentloaded", timeout=min(timeout * 1000, 8000))
                            page.locator('.nav-search-input').first.wait_for(timeout=min(timeout * 1000, 3000))
                        except BrowserTimeout:
                            pass
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                    code = response.status if response else 0
                    status, reason = page_status(page.content(), page.url, code)
                    if status in {"JS_REQUIRED", "OK"}:
                        try:
                            page.wait_for_function(r"""() => !!document.querySelector('.poly-card, .ui-search-layout__item, .ui-pdp-title, .ui-pdp-buybox, #__PRELOADED_STATE__, #__NEXT_DATA__') || /nenhum resultado|n[aã]o encontramos|para continuar.{0,30}acesse/i.test(document.body?.innerText || '')""", timeout=min(timeout * 1000, 10000))
                        except BrowserTimeout:
                            pass
                        code = 200 if allowed_url(page.url) and page.locator('.poly-card, .ui-pdp-title').count() else code
                        status, reason = page_status(page.content(), page.url, code)
                    if status == "OK" and page_handler:
                        record_browser_result(url, status)
                        return page_handler(page)
                    record_browser_result(url, status)
                    return {"status": status, "reason": reason, "html": page.content(), "url": page.url}
                finally:
                    if not page.is_closed():page.close()
                    manager.__exit__(None,None,None)
        except Exception as exc:
            return {"status": "BROWSER_ERROR", "reason": str(exc)[:300]}


