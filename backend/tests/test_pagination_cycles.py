import json
import unittest
from models.product import ScrapeCursor
from services.scrape_pagination import _advance,pagination_key


class PaginationCycleTests(unittest.TestCase):
    def test_search_redirect_to_full_storefront_is_not_crawled_as_material_results(self):
        from bs4 import BeautifulSoup
        from unittest.mock import patch
        from crawlers.paged_discovery import fetch_search_page
        first='https://www.refrigeracaomota.com.br/?s=MAGISTER+4B+INOX+PTO&post_type=product'
        soup=BeautifulSoup('<title>Produtos - Refrigeração Mota</title><input name="s" value=""><a rel="next" href="/page/2/">Próxima</a>','html.parser')
        diagnostics={}
        with patch('crawlers.paged_discovery._fetch_html_result',return_value=((soup,'https://www.refrigeracaomota.com.br'),'OK',None)):
            items,following,success=fetch_search_page('refrigeracao_mota',first,diagnostics=diagnostics)
        self.assertFalse(success);self.assertEqual(items,[]);self.assertIsNone(following)
        self.assertEqual(diagnostics['status'],'SEARCH_MISMATCH')

    def test_query_to_canonical_search_path_redirect_keeps_coverage(self):
        from bs4 import BeautifulSoup
        from crawlers.paged_discovery import search_scope_matches
        soup=BeautifulSoup('<title>Resultados W10531315</title>','html.parser')
        self.assertTrue(search_scope_matches('https://shop.test/?q=W10531315','https://shop.test/busca/w10531315/',soup))

    def test_wordpress_catalog_cannot_masquerade_as_search_on_unchanged_url(self):
        from bs4 import BeautifulSoup
        from crawlers.paged_discovery import search_scope_matches
        soup=BeautifulSoup('<body class="home archive post-type-archive-product woocommerce"><input name="s" value=""></body>','html.parser')
        url='https://shop.test/?s=W10531315&post_type=product'
        self.assertFalse(search_scope_matches(url,url,soup))

    def test_woocommerce_pagination_retains_material_query(self):
        from bs4 import BeautifulSoup
        from crawlers.paged_discovery import _next_page
        first='https://www.refrigeracaomota.com.br/?s=W10531315&post_type=product'
        soup=BeautifulSoup('<a rel="next" href="/page/2/">Próxima</a>','html.parser')
        following=_next_page(soup,first,'refrigeracaomota.com.br')
        from urllib.parse import urlsplit,parse_qs
        self.assertEqual(urlsplit(following).path,'/page/2/')
        self.assertEqual(parse_qs(urlsplit(following).query),{'s':['W10531315'],'post_type':['product']})

    def test_old_checkpoint_that_escaped_search_restarts_scoped_first_page(self):
        first='https://www.refrigeracaomota.com.br/?s=W10531315&post_type=product'
        cursor=ScrapeCursor(sku='W10531315',store='refrigeracao_mota',kind='search',key='W10531315',
            initial_url=first,next_url='https://www.refrigeracaomota.com.br/page/12/',
            completed=False,last_status='OK',visited_urls='[]')
        fetched=[]
        def fetch(url,**kwargs):fetched.append(url);return [],None,True
        _advance(cursor,first,fetch,{})
        self.assertEqual(fetched,[first]);self.assertTrue(cursor.completed)

    def test_scoped_search_keeps_all_150_distinct_pages_without_a_total_cap(self):
        from urllib.parse import urlsplit,parse_qs
        first='https://shop.test/?s=W10531315&post_type=product'
        cursor=ScrapeCursor(sku='W10531315',store='refrigeracao_mota',kind='search',key='W10531315',
            initial_url=first,next_url=first,completed=False,last_status='OK',visited_urls='[]')
        pages=[]
        def fetch(url,**kwargs):
            self.assertEqual(parse_qs(urlsplit(url).query)['s'],['W10531315'])
            page=int(urlsplit(url).path.strip('/').split('/')[-1]) if '/page/' in url else 1
            pages.append(page)
            return [{'url':f'https://shop.test/product-{page}'}],f'https://shop.test/page/{page+1}/' if page<150 else None,True
        for _ in range(150):
            _advance(cursor,first,fetch,{})
        self.assertTrue(cursor.completed);self.assertEqual(pages,list(range(1,151)))

    def test_tracking_only_changes_stop_repeated_bing_offset(self):
        old='https://www.bing.com/search?q=W10531315&first=11&FPIG=old&FORM=PORE'
        current='https://www.bing.com/search?q=W10531315&first=11&FPIG=current&FORM=PORE'
        following='https://www.bing.com/search?q=W10531315&FPIG=current%2Cold&first=11&FORM=PORE'
        cursor=ScrapeCursor(sku='W10531315',store='consul',kind='indexed_search',key='bing',
            initial_url=old,next_url=current,completed=False,last_status='OK',visited_urls=json.dumps([old]))
        values=_advance(cursor,old,lambda url,**kwargs:([{'url':'https://www.consul.com.br/sensor/p'}],following,True),{})
        self.assertEqual(len(values),1);self.assertTrue(cursor.completed)

    def test_actual_next_page_and_seller_change_are_preserved(self):
        first='https://www.bing.com/search?q=W10531315&first=11&FPIG=old'
        second='https://www.bing.com/search?q=W10531315&first=21&FPIG=new'
        self.assertNotEqual(pagination_key(first),pagination_key(second))
        self.assertNotEqual(pagination_key('https://store.test/p?seller_id=1'),pagination_key('https://store.test/p?seller_id=2'))
        cursor=ScrapeCursor(sku='W10531315',store='consul',kind='indexed_search',key='bing',
            initial_url=first,next_url=first,completed=False,last_status='OK',visited_urls='[]')
        _advance(cursor,first,lambda url,**kwargs:([],second,True),{})
        self.assertFalse(cursor.completed);self.assertEqual(cursor.next_url,second)

    def test_repeated_offers_stop_a_server_that_ignores_the_requested_offset(self):
        first='https://www.bing.com/search?q=W10531315&first=1'
        second='https://www.bing.com/search?q=W10531315&first=11'
        third='https://www.bing.com/search?q=W10531315&first=21'
        cursor=ScrapeCursor(sku='W10531315',store='consul',kind='indexed_search',key='bing',
            initial_url=first,next_url=first,completed=False,last_status='OK',visited_urls='[]')
        offers=[{'url':'https://www.consul.com.br/sensor/p','seller':'Consul'}]
        _advance(cursor,first,lambda url,**kwargs:(offers,second,True),{})
        self.assertFalse(cursor.completed)
        _advance(cursor,first,lambda url,**kwargs:(offers,third,True),{})
        self.assertTrue(cursor.completed)
