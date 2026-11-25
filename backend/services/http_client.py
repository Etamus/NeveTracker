from __future__ import annotations

import atexit
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util import Timeout
from threading import BoundedSemaphore, Lock

_tls_slots = {}
_tls_slots_lock = Lock()
_adapter_lock = Lock()
_adapters = {}


RETRYABLE_STATUS_CODES = (408, 425, 500, 502, 503, 504)


class BoundedRetry(Retry):
    def is_retry(self, method, status_code, has_retry_after=False):
        # Let the persistent queue handle throttling; never sleep for a server's
        # potentially unbounded Retry-After inside a SKU's worker.
        if status_code == 429 or has_retry_after:
            return False
        return super().is_retry(method, status_code, has_retry_after)


class SharedBudgetAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        from services.extraction_resources import remaining_request_time, request_slot, resource_key
        with request_slot(resource_key(request.url)):
            remaining = remaining_request_time()
            if remaining is not None:
                kwargs["timeout"] = Timeout(total=remaining, connect=remaining, read=remaining)
                return HTTPAdapter.send(_quick_adapter, request, **kwargs)
            return super().send(request, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        with _adapter_lock:
            return super().proxy_manager_for(proxy, **proxy_kwargs)

    def close(self):
        # Sessions own their cookies and headers. The process owns the sockets.
        pass


_quick_adapter = SharedBudgetAdapter(max_retries=0, pool_connections=32, pool_maxsize=12)


def _new_adapter(pool_size):
    retry = BoundedRetry(
        total=2,
        connect=1,
        read=1,
        status=1,
        backoff_factor=0.5,
        status_forcelist=RETRYABLE_STATUS_CODES,
        allowed_methods=frozenset(("GET", "HEAD", "OPTIONS")),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    return SharedBudgetAdapter(
        max_retries=retry,
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )


def create_resilient_session(pool_size=12):
    session = Session()
    with _adapter_lock:
        adapter = _adapters.get(pool_size)
        if adapter is None:
            adapter = _adapters[pool_size] = _new_adapter(pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@atexit.register
def _close_adapters():
    with _adapter_lock:
        for adapter in _adapters.values():
            HTTPAdapter.close(adapter)
        HTTPAdapter.close(_quick_adapter)


def recover_public_html(response, url, timeout=15):
    """Try browser-compatible TLS once on a public HTTP 403, never on throttling.

    This is still a normal GET to the original storefront, with no proxy or paid
    service. Authentication and captcha responses remain unavailable.
    """
    from services.extraction_resources import remaining_request_time
    if remaining_request_time() is not None:
        return response
    if response.status_code != 403 or (getattr(response, "headers", None) or {}).get("Retry-After"):
        return response
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        return response
    try:
        from curl_cffi.requests import Session
        from services.magalu_public import public_storefront_url
        alternate = public_storefront_url(url)
        for target in [url] + ([alternate] if alternate else []):
            with _tls_slots_lock:
                slot = _tls_slots.setdefault(urlparse(target).hostname, BoundedSemaphore(2))
            with slot, Session(impersonate="chrome") as session:
                recovered = session.get(target, timeout=(5, timeout), headers={"Accept-Language": "pt-BR,pt;q=0.9"})
                if recovered.status_code == 200:
                    return recovered
    except Exception:
        pass
    return response
