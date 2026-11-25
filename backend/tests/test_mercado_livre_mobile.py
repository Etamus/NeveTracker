import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from urllib.parse import parse_qs,urlsplit

from services import mercado_livre_mobile as mobile
from services.mercado_livre_browser import fetch_public_html
from crawlers.mercado_livre_public import parse_search_html,collect_public_product
from services.product_resolver import ProductIdentity


def nordic(state):
    return '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r='+json.dumps({'appProps':{'pageProps':{'initialState':state}}})+';</script>'


def card(item,seller):
    return {'id':'POLYCARD','polycard':{'metadata':{'id':item,'url':'www.mercadolivre.com.br/refil/up/MLBU777','url_fragments':'#wid='+item},'components':[
        {'id':'title','type':'title','title':{'text':'Filtro W11199342'}},
        {'id':'seller','type':'seller','seller':{'text':seller+' {icon_cockade}','values':[{'key':'icon_cockade','type':'icon'}]}},
        {'id':'price_v2','type':'price','price':{'current_price':{'value':88.99,'currency':'BRL'}}}]}}


class MercadoLivreMobileTests(unittest.TestCase):
    def response(self,code=200,url='https://lista.mercadolivre.com.br/w11199342?skipWebview=true',html=None,headers=None):
        return SimpleNamespace(status_code=code,url=url,text=html if html is not None else nordic({'results':[card('MLB123','Loja A')]}),headers=headers or {})

    def test_mobile_url_preserves_seller_filters_and_has_one_transport_parameter(self):
        url=mobile.mobile_url('https://www.mercadolivre.com.br/p/MLB99?wid=MLB123&skipWebview=false&pdp_filters=item_id%3AMLB123')
        query=parse_qs(urlsplit(url).query)
        self.assertEqual(query['wid'],['MLB123']);self.assertEqual(query['skipWebview'],['true'])
        self.assertEqual(query['pdp_filters'],['item_id:MLB123'])

    def test_foreign_urls_never_open_a_session(self):
        with patch.object(mobile,'_session') as session:
            for url in ['https://mercadolivre.com.br.evil.test/x','http://mercadolivre.com.br/x','https://user:pass@mercadolivre.com.br/x','https://mercadolivre.com.br:999/x','https://mercadolivre.com.br:invalid/x']:
                self.assertEqual(mobile.fetch_mobile_html(url)['status'],'INVALID_MATCH')
        session.assert_not_called()

    def test_redirect_reapplies_mobile_flag_and_does_not_persist_it_in_offer_url(self):
        session=MagicMock();session.get.side_effect=[self.response(302,headers={'location':'https://www.mercadolivre.com.br/p/MLB99?wid=MLB123'}),self.response(url='https://www.mercadolivre.com.br/p/MLB99?wid=MLB123&skipWebview=true')]
        with patch.object(mobile,'_session',return_value=session):result=mobile.fetch_mobile_html('https://lista.mercadolivre.com.br/w11199342')
        self.assertEqual(result['status'],'OK');self.assertNotIn('skipWebview',result['url']);self.assertIn('wid=MLB123',result['url'])
        self.assertTrue(all('skipWebview=true' in call.args[0] for call in session.get.call_args_list))
        self.assertFalse(any('Cookie' in call.kwargs['headers'] for call in session.get.call_args_list))

    def test_redirect_to_foreign_host_is_rejected_before_next_request(self):
        session=MagicMock();session.get.return_value=self.response(302,headers={'location':'https://untrusted.example/'})
        with patch.object(mobile,'_session',return_value=session):result=mobile.fetch_mobile_html('https://lista.mercadolivre.com.br/w11199342')
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(session.get.call_count,1)

    def test_404_is_not_retried_but_server_error_gets_one_retry(self):
        session=MagicMock()
        with patch.object(mobile,'_session',return_value=session),patch.object(mobile,'_drop_session'):
            session.get.return_value=self.response(404,html='Não encontrado')
            self.assertEqual(mobile.fetch_mobile_html('https://lista.mercadolivre.com.br/w11199342')['status'],'NOT_FOUND')
            session.get.assert_called_once();session.get.reset_mock()
            session.get.side_effect=[self.response(503),self.response()]
            self.assertEqual(mobile.fetch_mobile_html('https://lista.mercadolivre.com.br/w11199342')['status'],'OK')
            self.assertEqual(session.get.call_count,2)

    def test_empty_shell_is_not_successful_extraction(self):
        with patch.object(mobile,'_session') as session:
            session.return_value.get.return_value=self.response(html='<html>Carregando...</html>')
            self.assertEqual(mobile.fetch_mobile_html('https://lista.mercadolivre.com.br/w11199342')['status'],'PARSER_ERROR')

    def test_mobile_is_primary_and_desktop_remains_fallback(self):
        with patch.object(mobile,'fetch_mobile_html',return_value={'status':'OK','html':'real state','url':'https://lista.mercadolivre.com.br/w11199342'}) as primary,patch('services.mercado_livre_browser._http_session') as desktop:
            self.assertEqual(fetch_public_html('https://lista.mercadolivre.com.br/w11199342')['status'],'OK');desktop.get.assert_not_called()
        with patch.object(mobile,'fetch_mobile_html',return_value={'status':'BLOCKED'}),patch('services.browser_health.browser_available',return_value=True),patch('services.mercado_livre_browser._http_session') as desktop:
            desktop.get.return_value=self.response(html='<html>Dados públicos</html>')
            self.assertEqual(fetch_public_html('https://lista.mercadolivre.com.br/w11199342')['status'],'OK');desktop.get.assert_called_once()

    def test_nordic_pagination_and_sellers_do_not_collapse_same_catalog(self):
        records=[card('MLB'+str(100+i),'Loja '+str(i)) for i in range(125)]
        next_url='https://lista.mercadolivre.com.br/w11199342_Desde_126_NoIndex_True'
        html=nordic({'results':records,'pagination':{'next_page':{'url':next_url,'show':True}}})
        rows,nxt=parse_search_html(html,'https://lista.mercadolivre.com.br/w11199342')
        self.assertEqual(len(rows),125);self.assertEqual(len({row['store'] for row in rows}),125)
        self.assertTrue(all(row['price']==88.99 and '{' not in row['seller'] for row in rows))
        self.assertTrue(all(row['item_id'] in row['url'] for row in rows));self.assertEqual(nxt,next_url)

    def test_changed_seller_after_redirect_is_not_assigned_to_original_listing(self):
        source=SimpleNamespace(url='https://www.mercadolivre.com.br/p/MLB99?wid=MLB123')
        parsed={'status':'OK','item':{'item_id':'MLB456'},'items':[{'item_id':'MLB456'}]}
        with patch('crawlers.mercado_livre_public.fetch_public_html',return_value={'status':'OK','html':'','url':'https://www.mercadolivre.com.br/p/MLB99'}),patch('crawlers.mercado_livre_public.parse_product_html',return_value=parsed):
            result=collect_public_product(source,ProductIdentity(sku='W11199342'))
        self.assertEqual(result['status'],'INVALID_MATCH')

    def test_sessions_are_reused_within_a_thread_and_isolated_between_threads(self):
        mobile._drop_session()
        with patch.object(mobile,'Session') as factory:
            first=MagicMock();second=MagicMock();factory.side_effect=[first,second]
            self.assertIs(mobile._session(),first);self.assertIs(mobile._session(),first)
            observed=[]
            def other():
                observed.append(mobile._session());mobile._drop_session()
            worker=threading.Thread(target=other);worker.start();worker.join()
            self.assertEqual(observed,[second]);self.assertEqual(factory.call_count,2)
            mobile._drop_session()


if __name__=='__main__':unittest.main()
