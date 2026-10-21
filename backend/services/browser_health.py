"""Short host-specific circuit breaker for confirmed browser access blocks.

HTTP discovery remains available. A login wall on product pages must not suppress
search pages on a different host or turn missing results into successful coverage.
"""
import time
from threading import Lock
from urllib.parse import urlparse

_lock = Lock()
_states = {}
_shipping_states = {}
COOLDOWN_SECONDS = 600
BLOCKED_STATUSES = {"BLOCKED", "LOGIN_REQUIRED"}


def browser_available(url, channel="browser"):
    host = (urlparse(str(url or "")).hostname or "").lower()
    host = host if channel == 'browser' else channel + '|' + host
    with _lock:
        return time.monotonic() >= _states.get(host, (0, 0))[1]


def record_browser_result(url, status, channel="browser"):
    host = (urlparse(str(url or "")).hostname or "").lower()
    host = host if channel == 'browser' else channel + '|' + host
    with _lock:
        failures, deadline = _states.get(host, (0, 0))
        if status == "OK":
            _states.pop(host, None)
        elif status in BLOCKED_STATUSES:
            failures += 1
            _states[host] = (failures, time.monotonic() + COOLDOWN_SECONDS if failures >= 2 else deadline)


def reset_browser_health():
    with _lock:
        _states.clear()
        _shipping_states.clear()


def shipping_form_available(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    with _lock:
        return time.monotonic() >= _shipping_states.get(host, (0, 0))[1]


def record_missing_shipping_form(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    with _lock:
        count, _ = _shipping_states.get(host, (0, 0))
        _shipping_states[host] = (count + 1, time.monotonic() + COOLDOWN_SECONDS if count >= 1 else 0)


def record_shipping_form_found(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    with _lock:
        _shipping_states.pop(host, None)
