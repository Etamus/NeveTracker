from __future__ import annotations

import math
import threading
from types import SimpleNamespace
from collections import defaultdict, Counter
import time
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerRunner
from scrapy.settings import Settings
from twisted.internet import reactor

from crawlers.base import parse_product_response
from crawlers.profiles import DEFAULT_PROFILE, STORE_PROFILES
from services.http_client import recover_public_html
from services.extraction_resources import ExtractionCancelled, check_cancelled


CACHE_DIR = Path(__file__).resolve().parents[1] / ".scrapy-cache"
_reactor_lock = threading.Lock()
_reactor_thread = None


class ProductSourceSpider(scrapy.Spider):
    name = "nevetracker_product_sources"

    def __init__(self, sources, identity, output, stop_requested=None, **kwargs):
        super().__init__(**kwargs)
        self.sources = {source.id: source for source in sources}
        self.identity = identity
        self.output = output
        self.stop_requested = stop_requested

    async def start(self):
        by_page = defaultdict(list)
        for source in self.sources.values():
            by_page[source.url].append(source.id)
        for url, source_ids in by_page.items():
            if self.stop_requested is not None and self.stop_requested.is_set():
                return
            yield scrapy.Request(
                url,
                callback=self.parse_source,
                errback=self.parse_error,
                cb_kwargs={"source_id": source_ids},
                meta={
                    "source_id": source_ids,
                    "handle_httpstatus_list": [401, 403, 404, 410, 429],
                },
                dont_filter=True,
                headers={"Accept-Language": "pt-BR,pt;q=0.9"},
            )

    def _record(self, source_ids, status, reason, **details):
        for source_id in source_ids:
            self.output[source_id] = {"status": status, "reason": reason, **details}

    def _parse_page(self, response, source_ids):
        for source_id in source_ids:
            source = self.sources[source_id]
            try:
                self.output[source_id] = parse_product_response(
                    response, source, self.identity, STORE_PROFILES.get(source.store, DEFAULT_PROFILE),
                )
            except Exception as exc:
                self.output[source_id] = {"status": "PARSER_ERROR", "reason": str(exc)}

    def parse_source(self, response, source_id):
        if self.stop_requested is not None and self.stop_requested.is_set():
            return
        ids = source_id if isinstance(source_id, list) else [source_id]
        if response.status in {404, 410}:
            self._record(ids, "NOT_FOUND", f"HTTP {response.status}")
            return
        if response.status == 403 and not response.headers.get("Retry-After"):
            from twisted.internet.threads import deferToThread
            return deferToThread(self.recover_source, response, ids)
        if response.status in {401, 403, 429}:
            self._record(ids, "BLOCKED", f"HTTP {response.status}", _retry_after=response.headers.get("Retry-After"))
            return
        if response.status != 200:
            self._record(ids, "PARSER_ERROR", f"HTTP {response.status}")
            return
        self._parse_page(response, ids)

    def recover_source(self, response, source_id):
        if self.stop_requested is not None and self.stop_requested.is_set():
            return
        from scrapy.http import HtmlResponse
        ids = source_id if isinstance(source_id, list) else [source_id]
        recovered = recover_public_html(SimpleNamespace(status_code=403), response.url,
                                        self.crawler.settings.getfloat("DOWNLOAD_TIMEOUT", 15))
        if recovered.status_code != 200:
            self._record(ids, "BLOCKED", "HTTP 403; transporte compativel tambem indisponivel")
            return
        page = HtmlResponse(url=recovered.url, body=recovered.content, encoding=recovered.encoding or "utf-8")
        self._parse_page(page, ids)
        from services.magalu_public import is_public_storefront
        if is_public_storefront(recovered.url):
            for source_id in ids:
                if self.sources[source_id].store == "magalu":
                    for item in self.output[source_id].get("items", []):
                        item["discovery_source"] = "magalu_public_storefront"

    def parse_error(self, failure):
        source_id = failure.request.meta.get("source_id")
        reason = failure.getErrorMessage() or failure.type.__name__
        status = "TIMEOUT" if "timeout" in reason.lower() else "PARSER_ERROR"
        self._record(source_id if isinstance(source_id, list) else [source_id], status, reason)


def _ensure_reactor():
    global _reactor_thread
    with _reactor_lock:
        if reactor.running:
            return
        if _reactor_thread and _reactor_thread.is_alive():
            pass
        else:
            _reactor_thread = threading.Thread(
                target=lambda: reactor.run(installSignalHandlers=False),
                name="nevetracker-scrapy-reactor",
                daemon=True,
            )
            _reactor_thread.start()
    deadline = time.monotonic() + 5
    while not reactor.running and time.monotonic() < deadline:
        time.sleep(0.02)
    if not reactor.running:
        raise RuntimeError("O reactor do Scrapy nao iniciou.")


def _settings(per_domain, timeout):
    from services.extraction_resources import effective_workers, settings

    concurrent = min(12, max(2, per_domain * 4))
    if settings()['hardware_acceleration']:
        concurrent = max(concurrent, min(32, effective_workers('quote_workers')))
    return Settings(
        {
            "USER_AGENT": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
            "ROBOTSTXT_OBEY": False,
            "COOKIES_ENABLED": True,
            "CONCURRENT_REQUESTS": concurrent,
            "CONCURRENT_REQUESTS_PER_DOMAIN": per_domain,
            "DOWNLOADER_MIDDLEWARES": {"services.extraction_resources.SharedScrapyBudget": 900},
            "DOWNLOAD_TIMEOUT": timeout,
            "DOWNLOAD_DELAY": 0.15,
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "RETRY_ENABLED": True,
            "RETRY_TIMES": 2,
            "RETRY_HTTP_CODES": [408, 425, 500, 502, 503, 504],
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.25,
            "AUTOTHROTTLE_MAX_DELAY": 5,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": float(per_domain),
            "HTTPCACHE_ENABLED": False,
            "HTTPCACHE_EXPIRATION_SECS": 900,
            "HTTPCACHE_DIR": str(CACHE_DIR),
            "LOG_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
            "TWISTED_REACTOR": "twisted.internet.selectreactor.SelectReactor",
        }
    )


def collect_sources_with_scrapy(sources, identity, per_domain=2, timeout=15):
    check_cancelled()
    sources = list(sources)
    if not sources:
        return {}
    _ensure_reactor()
    output = {}
    completed = threading.Event()
    timed_out = threading.Event()
    failure = []
    pending = []

    def run():
        if timed_out.is_set():
            return
        try:
            runner = CrawlerRunner(_settings(per_domain, timeout))
            crawler = runner.create_crawler(ProductSourceSpider)
            pending.append(crawler)
            if timed_out.is_set():
                return
            deferred = runner.crawl(
                crawler,
                sources=sources,
                identity=identity,
                output=output,
                stop_requested=timed_out,
            )
        except Exception as exc:
            failure.append(exc)
            completed.set()
            return

        def finish(result):
            if hasattr(result, "value"):
                failure.append(result.value)
            completed.set()
            return None

        deferred.addBoth(finish)

    def stop():
        for crawler in pending:
            crawler.stop()

    reactor.callFromThread(run)
    from urllib.parse import urlparse
    domain_counts = Counter(urlparse(source.url).hostname for source in sources)
    batches = max(math.ceil(count / max(1, per_domain)) for count in domain_counts.values())
    wait_seconds = min(90, max(20, timeout * batches + 10))
    deadline = time.monotonic() + wait_seconds
    try:
        while not completed.wait(max(0, min(.1, deadline - time.monotonic()))):
            check_cancelled()
            if time.monotonic() >= deadline:
                break
        check_cancelled()
    except ExtractionCancelled:
        timed_out.set()
        reactor.callFromThread(stop)
        raise
    if not completed.is_set():
        timed_out.set()
        reactor.callFromThread(stop)
        partial = dict(output)
        for source in sources:
            partial.setdefault(
                source.id,
                {"status": "TIMEOUT", "reason": "Tempo global do Scrapy excedido"},
            )
        return partial
    if failure:
        print(f"[COLLECTION][SCRAPY] PARSER_ERROR: {failure[0]}")
    for source in sources:
        output.setdefault(
            source.id,
            {"status": "PARSER_ERROR", "reason": str(failure[0])[:300] if failure else "Spider terminou sem resposta"},
        )
    return output
