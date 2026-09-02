from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import scrapy
from scrapy.crawler import CrawlerRunner
from scrapy.settings import Settings
from twisted.internet import reactor

from crawlers.base import parse_product_response
from crawlers.profiles import DEFAULT_PROFILE, STORE_PROFILES


CACHE_DIR = Path(__file__).resolve().parents[1] / ".scrapy-cache"
_reactor_lock = threading.Lock()
_reactor_thread = None


class ProductSourceSpider(scrapy.Spider):
    name = "nevetracker_product_sources"

    def __init__(self, sources, identity, output, **kwargs):
        super().__init__(**kwargs)
        self.sources = {source.id: source for source in sources}
        self.identity = identity
        self.output = output

    async def start(self):
        for source in self.sources.values():
            yield scrapy.Request(
                source.url,
                callback=self.parse_source,
                errback=self.parse_error,
                cb_kwargs={"source_id": source.id},
                meta={
                    "source_id": source.id,
                    "handle_httpstatus_list": [401, 403, 404, 410, 429],
                },
                dont_filter=True,
                headers={"Accept-Language": "pt-BR,pt;q=0.9"},
            )

    def parse_source(self, response, source_id):
        source = self.sources[source_id]
        if response.status in {404, 410}:
            self.output[source_id] = {"status": "NOT_FOUND", "reason": f"HTTP {response.status}"}
            return
        if response.status in {401, 403, 429}:
            self.output[source_id] = {"status": "BLOCKED", "reason": f"HTTP {response.status}"}
            return
        if response.status != 200:
            self.output[source_id] = {"status": "PARSER_ERROR", "reason": f"HTTP {response.status}"}
            return
        profile = STORE_PROFILES.get(source.store, DEFAULT_PROFILE)
        self.output[source_id] = parse_product_response(
            response,
            source,
            self.identity,
            profile,
        )

    def parse_error(self, failure):
        source_id = failure.request.meta.get("source_id")
        reason = failure.getErrorMessage() or failure.type.__name__
        status = "TIMEOUT" if "timeout" in reason.lower() else "PARSER_ERROR"
        self.output[source_id] = {"status": status, "reason": reason}


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
    return Settings(
        {
            "USER_AGENT": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
            "ROBOTSTXT_OBEY": False,
            "COOKIES_ENABLED": True,
            "CONCURRENT_REQUESTS": min(12, max(2, per_domain * 4)),
            "CONCURRENT_REQUESTS_PER_DOMAIN": per_domain,
            "DOWNLOAD_TIMEOUT": timeout,
            "DOWNLOAD_DELAY": 0.15,
            "RANDOMIZE_DOWNLOAD_DELAY": True,
            "RETRY_ENABLED": True,
            "RETRY_TIMES": 2,
            "RETRY_HTTP_CODES": [408, 425, 429, 500, 502, 503, 504],
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 0.25,
            "AUTOTHROTTLE_MAX_DELAY": 5,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": float(per_domain),
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_EXPIRATION_SECS": 900,
            "HTTPCACHE_DIR": str(CACHE_DIR),
            "LOG_ENABLED": False,
            "TELNETCONSOLE_ENABLED": False,
            "TWISTED_REACTOR": "twisted.internet.selectreactor.SelectReactor",
        }
    )


def collect_sources_with_scrapy(sources, identity, per_domain=2, timeout=15):
    sources = list(sources)
    if not sources:
        return {}
    _ensure_reactor()
    output = {}
    completed = threading.Event()
    failure = []

    def run():
        runner = CrawlerRunner(_settings(per_domain, timeout))
        deferred = runner.crawl(
            ProductSourceSpider,
            sources=sources,
            identity=identity,
            output=output,
        )

        def finish(result):
            if hasattr(result, "value"):
                failure.append(result.value)
            completed.set()
            return None

        deferred.addBoth(finish)

    reactor.callFromThread(run)
    batches = math.ceil(len(sources) / max(1, per_domain))
    wait_seconds = min(300, max(20, timeout * batches + 10))
    if not completed.wait(wait_seconds):
        return {
            source.id: {"status": "TIMEOUT", "reason": "Tempo global do Scrapy excedido"}
            for source in sources
        }
    if failure:
        print(f"[COLLECTION][SCRAPY] PARSER_ERROR: {failure[0]}")
    for source in sources:
        output.setdefault(
            source.id,
            {"status": "PARSER_ERROR", "reason": "Spider terminou sem resposta"},
        )
    return output
