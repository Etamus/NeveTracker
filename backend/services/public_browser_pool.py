"""Reuse browser processes; isolate every public page in a fresh context.

Account-backed Mercado Livre/Shopee profiles are deliberately managed elsewhere.
Each Playwright runtime stays exclusively on its owning worker thread.
"""
import atexit
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import copy_context
from functools import wraps
from queue import Queue, Empty
import threading
import time
from types import SimpleNamespace
from services.extraction_resources import ExtractionCancelled, check_cancelled

_tasks = Queue()
_lock = threading.Lock()
_threads = []
_local = threading.local()
_closing = False


def _reset():
    for name, action in [('browser', 'close'), ('runtime', 'stop')]:
        value = getattr(_local, name, None)
        if value:
            try:getattr(value, action)()
            except Exception:pass
            setattr(_local, name, None)


def _worker():
    _local.worker = True
    try:
        while True:
            task = _tasks.get()
            if task is None:
                return
            future, function, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:future.set_result(function(*args, **kwargs))
            except BaseException as exc:future.set_exception(exc)
    finally:
        _reset()


def public_render(function):
    @wraps(function)
    def render(*args, **kwargs):
        from playwright.sync_api import sync_playwright
        check_cancelled()
        # Injected transports remain synchronous and deterministic in tests.
        if getattr(_local, 'worker', False) or not getattr(sync_playwright, '__module__', '').startswith('playwright'):
            return function(*args, **kwargs)
        with _lock:
            if _closing:
                raise RuntimeError('O navegador público está encerrando; retome pelo checkpoint.')
            if not _threads:
                for index in range(2):
                    thread = threading.Thread(target=_worker, name=f'public-browser-{index}', daemon=True)
                    _threads.append(thread);thread.start()
        future = Future()
        context = copy_context()

        def run():
            check_cancelled()
            return function(*args, **kwargs)

        _tasks.put((future, context.run, (run,), {}))
        while True:
            try:
                result = future.result(timeout=.1)
            except TimeoutError:
                if future.done():
                    return future.result()
                try:
                    check_cancelled()
                except ExtractionCancelled:
                    if not future.cancel():
                        # An active lease must finish cleanup on its worker.
                        try:
                            future.result()
                        except Exception:
                            pass
                    raise
            else:
                check_cancelled()
                return result
    return render


class _Lease:
    def __init__(self, browser):
        self.browser = browser
        self.contexts = []

    def new_page(self, **kwargs):
        context = self.browser.new_context(**kwargs)
        self.contexts.append(context)
        return context.new_page()

    def close(self):
        for context in self.contexts:
            try:context.close()
            except Exception:pass
        self.contexts.clear()


@contextmanager
def public_runtime():
    from playwright.sync_api import sync_playwright
    from services.extraction_resources import settings
    if not settings()['reuse_public_browser'] or not getattr(_local, 'worker', False):
        with sync_playwright() as runtime:
            yield runtime
        return
    browser = getattr(_local, 'browser', None)
    if browser and (not browser.is_connected() or getattr(_local,'uses',0)>=40 or time.monotonic()-_local.created>600):
        _reset();browser=None
    if browser is None:
        _local.runtime = sync_playwright().start()
        try:_local.browser = _local.runtime.chromium.launch(headless=True, timeout=10000)
        except BaseException:
            _reset();raise
        browser=_local.browser;_local.created=time.monotonic();_local.uses=0
    lease=_Lease(browser)
    _local.uses+=1
    try:
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs:lease))
    finally:
        lease.close()


def shutdown():
    global _closing
    with _lock:
        _closing=True
        threads=list(_threads)
        while True:
            try:pending=_tasks.get_nowait()
            except Empty:break
            if pending is not None:pending[0].cancel()
        for _ in threads:_tasks.put(None)
    for thread in threads:
        thread.join(timeout=10)


atexit.register(shutdown)
