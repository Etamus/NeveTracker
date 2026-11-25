"""One fair I/O budget shared by all SKU collectors in this process.

Do not submit tasks that will occupy a worker waiting for another host's slot.
The dispatcher reserves capacity first and keeps every pending task in its queue.
"""
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import os
import threading
import time

DEFAULTS = {
    'precise_sku_workers': 8,
    'quick_sku_workers': 4,
    'quote_workers': 12,
    'concurrent_requests_per_domain': 2,
    'request_timeout_seconds': 15,
    'quick_timeout_seconds': 5,
    'discovery_stores_per_round': 3,
    'adaptive_parallelism': True,
    'hardware_acceleration': False,
    'reuse_public_browser': True,
    'browser_failure_cooldown_seconds': 30,
}
_settings = dict(DEFAULTS)
_condition = threading.Condition()
_active = Counter()
_local = threading.local()
_peak = 0
_hardware = {'cpu_percent': 0.0, 'available_memory_mb': 0, 'logical_cpus': os.cpu_count() or 1}
_sampled_at = 0.0
_cpu_times = None
_request_deadline = ContextVar('request_deadline', default=None)
_cancellation = ContextVar('extraction_cancellation', default=None)


class ExtractionCancelled(BaseException):
    """Internal control flow, distinct from a failed store request."""


@contextmanager
def cancellation_scope(event):
    token = _cancellation.set(event)
    try:
        yield
    finally:
        _cancellation.reset(token)


def check_cancelled():
    event = _cancellation.get()
    if event is not None and event.is_set():
        raise ExtractionCancelled()


def interruptible_sleep(seconds):
    event = _cancellation.get()
    if event is None:
        time.sleep(seconds)
    elif event.wait(seconds):
        raise ExtractionCancelled()


def submit_in_context(pool, function, *args, **kwargs):
    return pool.submit(copy_context().run, function, *args, **kwargs)


@contextmanager
def request_deadline(deadline):
    token = _request_deadline.set(deadline)
    try:
        yield
    finally:
        _request_deadline.reset(token)


def remaining_request_time():
    check_cancelled()
    deadline = _request_deadline.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Tempo da consulta rápida esgotado.')
    return remaining


class ProgressMessage(str):
    def __new__(cls, message, **details):
        value = super().__new__(cls, message)
        value.details = {**details, 'message': message}
        return value


def configure(control):
    with _condition:
        for field, default in DEFAULTS.items():
            value = getattr(control, field, default)
            _settings[field] = int(value) if isinstance(value, (int, float)) else default
        _condition.notify_all()


def settings():
    with _condition:
        return dict(_settings)


@contextmanager
def pooled_curl_session(factory):
    # One session per worker thread: keep TLS connections, never share mutable
    # cookies/handles across threads. Preserve injected transports in tests.
    if not getattr(factory, '__module__', '').startswith('curl_cffi'):
        with factory(impersonate='chrome') as session:
            yield session
        return
    sessions = getattr(_local, 'sessions', None)
    if sessions is None:
        sessions = _local.sessions = {}
    if factory not in sessions:
        sessions[factory] = factory(impersonate='chrome')
    yield sessions[factory]


def resource_key(url):
    from services.store_registry import find_target_store
    from urllib.parse import urlsplit
    store = find_target_store(None, None, url)
    return store['key'] if store else (urlsplit(url).hostname or str(url)).lower()


def _sample_hardware():
    global _sampled_at, _cpu_times
    now = time.monotonic()
    if now - _sampled_at < 2:
        return
    _sampled_at = now
    try:
        import psutil
        times = psutil.cpu_times()
        sample = (sum(times), times.idle + getattr(times, 'iowait', 0))
        if _cpu_times and sample[0] > _cpu_times[0]:
            _hardware['cpu_percent'] = round(100 * (1 - (sample[1]-_cpu_times[1])/(sample[0]-_cpu_times[0])), 1)
        _cpu_times = sample
        _hardware['available_memory_mb'] = int(psutil.virtual_memory().available / 1024**2)
    except (ImportError, OSError):
        pass  # Monitoring must never prevent checkpoint recovery/startup.


def hardware_workers(configured):
    with _condition:
        _sample_hardware()
        if not _settings['adaptive_parallelism']:
            return configured
        memory = _hardware['available_memory_mb']
        limit = max(1, (memory-512)//192) if memory else configured
        if _hardware['cpu_percent'] >= 90:
            limit = min(limit, max(1, configured//2))
        return max(1, min(configured, limit))


def effective_workers(field):
    """Scale independent work while preserving each storefront's request limit."""
    with _condition:
        _sample_hardware()
        configured = _settings[field]
        if _settings['hardware_acceleration'] and _hardware['cpu_percent'] < 75:
            cpus = _hardware['logical_cpus']
            memory = max(0, _hardware['available_memory_mb'] - 1024)
            if field == 'precise_sku_workers':
                configured = max(configured, min(24, cpus * 2, memory // 384))
            elif field == 'quote_workers':
                configured = max(configured, min(64, cpus * 4, memory // 192))
        return hardware_workers(configured)


def _reserve(key):
    global _peak
    # Account-backed browser sessions must remain serial; unrelated shops can run.
    limit = 1 if key == 'shopee' else _settings['concurrent_requests_per_domain']
    budget = effective_workers('quote_workers')
    if sum(_active.values()) >= budget or _active[key] >= limit:
        return False
    _active[key] += 1
    _peak = max(_peak, sum(_active.values()))
    return True


def _release(key):
    with _condition:
        _active[key] -= 1
        if not _active[key]:
            del _active[key]
        _condition.notify_all()


@contextmanager
def request_slot(key):
    # A quoted task may call an HTTP transport; that nested call already owns a slot.
    remaining_request_time()
    owned = getattr(_local, 'keys', set())
    if owned:
        yield
        return
    with _condition:
        while not _reserve(key):
            remaining = remaining_request_time()
            _condition.wait(min(.25, remaining) if remaining is not None else .25)
    _local.keys = owned | {key}
    try:
        check_cancelled()
        yield
    finally:
        _local.keys = owned
        _release(key)


def fair_map(function, values, key, workers=None):
    """Yield (input, result) on completion; never drop failed or queued work."""
    queues = {}
    for value in values:
        check_cancelled()
        resource = key(value)
        queues.setdefault(resource, deque()).append(value)
    pending = deque(queues)
    budget = effective_workers('quote_workers')
    count = max(1, min(workers or budget, budget))
    def run(value, resource):
        owned = getattr(_local, 'keys', set())
        _local.keys = owned | {resource}
        try:
            check_cancelled()
            return function(value)
        finally:
            _local.keys = owned
            _release(resource)
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix='quote-io') as pool:
        running = {}
        while pending or running:
            check_cancelled()
            stalled = 0
            while pending and stalled < len(pending):
                check_cancelled()
                if len(running) >= count:
                    break
                resource = pending.popleft()
                with _condition:
                    reserved = _reserve(resource)
                if reserved:
                    queue = queues[resource]
                    value = queue.popleft()
                    if queue:
                        pending.append(resource)
                    stalled = 0
                    try:
                        running[submit_in_context(pool, run, value, resource)] = value
                    except BaseException:
                        _release(resource)
                        raise
                else:
                    pending.append(resource)
                    stalled += 1
            if not running:
                with _condition:
                    _condition.wait(.05)
                continue
            completed, _ = wait(running, timeout=.1, return_when=FIRST_COMPLETED)
            for future in completed:
                check_cancelled()
                value = running.pop(future)
                yield value, future.result()


def snapshot():
    with _condition:
        _sample_hardware()
        return {'active_requests': sum(_active.values()), 'active_by_store': dict(_active),
                'peak_requests': _peak, 'io_budget': effective_workers('quote_workers'),
                'precise_sku_workers': effective_workers('precise_sku_workers'), **_hardware}


class SharedScrapyBudget:
    """Reserve without blocking the reactor or its thread pool."""
    def process_request(self, request, spider=None):
        from scrapy.exceptions import IgnoreRequest
        from twisted.internet import reactor
        from twisted.internet.defer import Deferred
        key = resource_key(request.url)
        scheduled = []
        def cancel(_):
            if scheduled and scheduled[0].active():
                scheduled[0].cancel()
        result = Deferred(cancel)
        def attempt():
            if result.called:
                return
            current_spider = spider if spider is not None else getattr(getattr(self, 'crawler', None), 'spider', None)
            stop_requested = getattr(current_spider, 'stop_requested', None)
            if stop_requested is not None and stop_requested.is_set():
                result.errback(IgnoreRequest('Coleta interrompida.'))
                return
            with _condition:
                reserved = _reserve(key)
            if reserved:
                request.meta['_shared_budget_key'] = key
                result.callback(None)
            else:
                scheduled[:] = [reactor.callLater(.05, attempt)]
        attempt()
        return result

    @classmethod
    def from_crawler(cls, crawler):
        middleware = cls()
        middleware.crawler = crawler
        return middleware

    def _done(self, request):
        key = request.meta.pop('_shared_budget_key', None)
        if key is not None:
            _release(key)

    def process_response(self, request, response, spider=None):
        self._done(request)
        return response

    def process_exception(self, request, exception, spider=None):
        self._done(request)
