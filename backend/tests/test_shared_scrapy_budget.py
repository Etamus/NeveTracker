import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from crawlers.scrapy_engine import collect_sources_with_scrapy
from services.product_resolver import ProductIdentity
from services import extraction_resources as resources


class SharedScrapyBudgetTests(unittest.TestCase):
    def test_cancel_stops_waiting_for_scrapy_and_prevents_new_downloads(self):
        started = threading.Event()
        release = threading.Event()
        cancellation = threading.Event()
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                started.set()
                release.wait(10)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            sources = [SimpleNamespace(id=index, store='dufrio',
                       url=f'http://127.0.0.1:{server.server_port}/cancel-test/{index}')
                       for index in range(12)]
            resources.configure(SimpleNamespace(**{
                **resources.DEFAULTS, 'concurrent_requests_per_domain': 1, 'adaptive_parallelism': False,
            }))
            self.addCleanup(lambda: resources.configure(SimpleNamespace(**resources.DEFAULTS)))

            def collect():
                with resources.cancellation_scope(cancellation):
                    return collect_sources_with_scrapy(sources, ProductIdentity(sku='W10531315'), 1, 15)

            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(collect)
                    try:
                        self.assertTrue(started.wait(4))
                        cancellation.set()
                        with self.assertRaises(resources.ExtractionCancelled):
                            result.result(timeout=2)
                    finally:
                        cancellation.set()
                        release.set()
                deadline = time.monotonic() + 3
                while resources.snapshot()['active_requests'] and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(resources.snapshot()['active_requests'], 0)
                self.assertEqual(len(paths), 1)
            finally:
                server.shutdown()
                thread.join()

    def test_multiple_crawlers_share_capacity_and_release_every_token(self):
        active=peak=0;lock=threading.Lock()
        body=('<title>Sensor W10531315</title><script type="application/ld+json">'+json.dumps({
            '@context':'https://schema.org','@type':'Product','name':'Sensor W10531315','sku':'W10531315',
            'offers':{'@type':'Offer','price':20,'priceCurrency':'BRL'}})+'</script>').encode()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal active,peak
                with lock:active+=1;peak=max(peak,active)
                try:
                    time.sleep(.03)
                    self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
                finally:
                    with lock:active-=1
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        resources.configure(SimpleNamespace(**{**resources.DEFAULTS,'quote_workers':8,'adaptive_parallelism':False}))
        self.addCleanup(lambda:resources.configure(SimpleNamespace(**resources.DEFAULTS)))
        batches=[[SimpleNamespace(id=index,store='dufrio',sku='W10531315',seller='Dufrio',brand='Brastemp',
            external_product_id=None,asin=None,item_id=None,shop_id=None,
            url=f'http://127.0.0.1:{server.server_port}/sensor-w10531315/{index}')
            for index in range(offset,offset+4)] for offset in (1,5)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda batch:collect_sources_with_scrapy(batch,ProductIdentity(sku='W10531315'),2,5),batches))
        self.assertEqual(sum(len(result) for result in results),8)
        for result in results:
            self.assertTrue(all(value['status']=='OK' for value in result.values()),result)
        self.assertLessEqual(peak,2)
        self.assertEqual(resources.snapshot()['active_requests'],0)
