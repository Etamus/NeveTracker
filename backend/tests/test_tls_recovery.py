import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scrapy.http import HtmlResponse
from scrapy.settings import Settings
from crawlers.scrapy_engine import ProductSourceSpider
from services.http_client import recover_public_html
from services.product_resolver import ProductIdentity


class TlsRecoveryTests(unittest.TestCase):
    def test_scrapy_downloads_identical_url_once_and_validates_each_seller(self):
        import asyncio

        url = "https://www.dufrio.com.br/sensor-w10531315"
        sources = [SimpleNamespace(id=index, url=url, store="dufrio", seller=seller)
                   for index, seller in enumerate(("Loja A", "Loja B"), 1)]
        output = {}
        spider = ProductSourceSpider(sources, ProductIdentity(sku="W10531315"), output)

        async def collect_requests():
            return [request async for request in spider.start()]

        requests = asyncio.run(collect_requests())
        self.assertEqual(len(requests), 1)
        with patch("crawlers.scrapy_engine.parse_product_response", side_effect=[
            ValueError("Falha de parser na loja A"), {"status": "OK", "item": {"seller": "Loja B"}},
        ]) as parse:
            spider.parse_source(HtmlResponse(url=url), **requests[0].cb_kwargs)
        self.assertEqual([call.args[1].seller for call in parse.call_args_list], ["Loja A", "Loja B"])
        self.assertEqual(output[1]["status"], "PARSER_ERROR")
        self.assertEqual(output[2]["item"]["seller"], "Loja B")

    def test_magalu_storefront_keeps_product_and_seller_without_redirecting_other_sites(self):
        from services.magalu_public import public_storefront_url
        source = "https://www.magazineluiza.com.br/compressor/p/abc123/ed/acrf/?seller_id=lojista"
        mirror = public_storefront_url(source)
        self.assertIn('/magazineoficialesbr/compressor/p/abc123/', mirror)
        self.assertTrue(mirror.endswith('?seller_id=lojista'))
        self.assertIsNone(public_storefront_url("https://evil.test/compressor/p/abc123/"))
        self.assertIsNone(public_storefront_url("https://www.magazineluiza.com.br/login/"))

    def test_embedded_magalu_prices_and_sellers_remain_individual(self):
        import json
        from services.magalu_public import embedded_seller_offers
        product = {'title': 'Compressor W11677211', 'available': True, 'seller': {'id': 'a'}, 'offers': [
            {'seller': {'id': 'a'}, 'price': {'bestPrice': '100', 'price': '120', 'currency': 'BRL'}},
            {'seller': {'id': 'b'}, 'price': {'bestPrice': '130', 'price': '150', 'currency': 'BRL'}},
        ]}
        data = {'props': {'pageProps': {'data': {'product': product}}}}
        response = HtmlResponse(url='https://www.magazinevoce.com.br/shop/compressor/p/abc123/',
            body=('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>').encode(), encoding='utf-8')
        offers = embedded_seller_offers(response, ProductIdentity(sku='W11677211'))
        self.assertEqual([x['price'] for x in offers], [100, 130])
        self.assertEqual([x['seller']['name'] for x in offers], ['a-Magalu', 'b-Magalu'])
        self.assertTrue(offers[1]['url'].endswith('seller_id=b'))
        self.assertEqual(offers[0]['installment_price'], 120)
        self.assertEqual(offers[0]['payment_difference'], 20)
        self.assertEqual(embedded_seller_offers(response, ProductIdentity(sku='W11677210')), [])
    @patch("curl_cffi.requests.Session")
    def test_tls_fallback_recovers_public_html_without_changing_existing_responses(self, session):
        recovered = SimpleNamespace(status_code=200)
        session.return_value.__enter__.return_value.get.return_value = recovered
        blocked = SimpleNamespace(status_code=403, headers={})
        self.assertIs(recover_public_html(blocked, "https://example.test/product", 5), recovered)
        self.assertEqual(session.call_args.kwargs["impersonate"], "chrome")
        session.reset_mock()
        for status, headers in [(200, {}), (401, {}), (429, {}), (403, {"Retry-After": "300"})]:
            original = SimpleNamespace(status_code=status, headers=headers)
            self.assertIs(recover_public_html(original, "https://example.test/product"), original)
        session.assert_not_called()

    @patch("crawlers.scrapy_engine.recover_public_html")
    def test_scrapy_recovery_uses_existing_product_validation_and_individual_offers(self, recover):
        source = SimpleNamespace(id=1, store="dufrio", seller="Loja Teste",
                                 url="https://www.dufrio.com.br/sensor-w10531315", external_product_id=None,
                                 asin=None, item_id=None, shop_id=None)
        output = {}
        spider = ProductSourceSpider([source], ProductIdentity(sku="W10531315"), output)
        spider.crawler = SimpleNamespace(settings=Settings({"DOWNLOAD_TIMEOUT": 5}))
        recover.return_value = SimpleNamespace(status_code=200, url=source.url,
            content=(Path(__file__).parent / "fixtures/product_pdp.html").read_bytes(), encoding="utf-8")
        spider.recover_source(HtmlResponse(url=source.url, status=403), 1)
        self.assertEqual(output[1]["status"], "OK")
        self.assertTrue(output[1]["items"])
        recover.return_value = SimpleNamespace(status_code=403)
        spider.recover_source(HtmlResponse(url=source.url, status=403), 1)
        self.assertEqual(output[1]["status"], "BLOCKED")
