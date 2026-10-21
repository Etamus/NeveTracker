from __future__ import annotations

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from threading import BoundedSemaphore, Lock

_tls_slots = {}
_tls_slots_lock = Lock()


RETRYABLE_STATUS_CODES = (408, 425, 500, 502, 503, 504)


class BoundedRetry(Retry):
    def is_retry(self, method, status_code, has_retry_after=False):
        # Let the persistent queue handle throttling; never sleep for a server's
        # potentially unbounded Retry-After inside a SKU's worker.
        if status_code == 429 or has_retry_after:
            return False
        return super().is_retry(method, status_code, has_retry_after)


def create_resilient_session(pool_size=12):
    session = Session()
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
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def recover_public_html(response, url, timeout=15):
    """Try browser-compatible TLS once on a public HTTP 403, never on throttling.

    This is still a normal GET to the original storefront, with no proxy or paid
    service. Authentication and captcha responses remain unavailable.
    """
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
