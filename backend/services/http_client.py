from __future__ import annotations

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


RETRYABLE_STATUS_CODES = (408, 425, 429, 500, 502, 503, 504)


def create_resilient_session(pool_size=12):
    session = Session()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=0.5,
        status_forcelist=RETRYABLE_STATUS_CODES,
        allowed_methods=frozenset(("GET", "HEAD", "OPTIONS", "POST")),
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
