from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from models.auth import AuthUser
from models.product import CrawlerControl, ExtractionJob, ExtractionSchedule, KnownSku, Product, PriceHistory
from routes.auth import auth_router
from routes.products import product_router, process_extraction_queue_item
from routes.sku_metadata import sku_metadata_router
from services import extraction_queue as queue, extraction_scheduler as scheduler
from services.auth_service import create_session, set_password
from services.precise_extraction import _html_and_parallel_api, _discover_store, _collect_sources
from services.product_resolver import ProductIdentity
from services.store_registry import find_target_store


class ExtractionAccessTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.temp=TemporaryDirectory()
        self.engine=create_engine('sqlite:///'+str(Path(self.temp.name)/'access.db'), connect_args={'check_same_thread':False})
        Base.metadata.create_all(self.engine)
        self.sessions=sessionmaker(bind=self.engine)
        self.tokens={}
        with self.sessions() as db:
            for username,role in [('ALICE','governor'),('BOB','governor'),('VIEWER','viewer'),
                                  ('ADMIN_A','administrator'),('ADMIN_B','administrator')]:
                user=AuthUser(username=username,role=role,is_owner=username=='ALICE');set_password(user,'test-password')
                db.add(user);db.flush()
                token,_=create_session(db,user);self.tokens[username]=(user.id,token)
            db.add(KnownSku(sku='W11509387'));db.add(CrawlerControl(id=1))
            db.commit()
        app=FastAPI();app.include_router(product_router,prefix='/products');app.include_router(auth_router,prefix='/auth');app.include_router(sku_metadata_router,prefix='/products/skus/metadata')
        def database():
            with self.sessions() as db: yield db
        app.dependency_overrides[get_db]=database
        self.client=TestClient(app)
        self.patch=patch.object(queue,'SessionLocal',self.sessions);self.patch.start()

    def tearDown(self):
        self.client.close();self.patch.stop();self.engine.dispose();self.temp.cleanup()

    def headers(self,name):return {'Authorization':'Bearer '+self.tokens[name][1]}

    def test_public_and_viewer_cannot_observe_jobs_or_start_extraction(self):
        for headers in [{},self.headers('VIEWER')]:
            self.assertIn(self.client.get('/products/extraction-jobs/active',headers=headers).status_code,[401,403])
            self.assertIn(self.client.post('/products/extraction-jobs',headers=headers,json={'skus':['W11509387']}).status_code,[401,403])
            self.assertIn(self.client.post('/products/batch-search',headers=headers,json={'skus':['W11509387']}).status_code,[401,403])
        with patch('routes.products.run_all_scrapers') as scrape:
            self.assertEqual(self.client.get('/products/search?q=W11509387').status_code,200)
            scrape.assert_not_called()
            self.assertEqual(self.client.post('/products/search/batch',json={'skus':['W11509387']}).status_code,409)

    def test_governor_cannot_cancel_another_users_extraction(self):
        a=self.client.post('/products/extraction-jobs',headers=self.headers('ALICE'),json={'skus':['W11509387']})
        self.assertEqual(a.status_code,200,a.text);job=a.json()
        self.assertEqual(job['owner_user_id'],self.tokens['ALICE'][0])
        self.assertEqual(self.client.post('/products/extraction-jobs/'+job['id']+'/cancel',headers=self.headers('BOB')).status_code,403)
        busy=self.client.post('/products/extraction-jobs',headers=self.headers('BOB'),json={'skus':['W11199342']})
        self.assertEqual(busy.status_code,409,busy.text)
        with self.sessions() as db:
            self.assertIsNone(db.query(KnownSku).filter_by(sku='W11199342').first())
        self.assertIn('Aguarde',busy.json()['detail'])
        cancelled = self.client.post('/products/extraction-jobs/'+job['id']+'/cancel',headers=self.headers('ALICE'))
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()['status'], 'cancelled')
        # A queued job has no active task to drain.
        self.assertEqual(self.client.post('/products/extraction-jobs',headers=self.headers('BOB'),json={'skus':['W11199342']}).status_code,200)

    def test_administrator_can_cancel_another_administrators_extraction(self):
        url = '/products/extraction-jobs'
        for creator, actor in [('ADMIN_A', 'ADMIN_B'), ('ADMIN_B', 'ADMIN_A'), ('BOB', 'ADMIN_A'), ('ALICE', 'ADMIN_B')]:
            with self.subTest(creator=creator, actor=actor):
                created = self.client.post(url, headers=self.headers(creator), json={'skus': ['W11509387']})
                self.assertEqual(created.status_code, 200, created.text)
                job = created.json()
                cancel_url = f"{url}/{job['id']}/cancel"
                self.assertEqual(self.client.post(cancel_url, headers=self.headers('VIEWER')).status_code, 403)
                response = self.client.post(cancel_url, headers=self.headers(actor))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['cancel_requested'])
                self.assertEqual(response.json()['owner_user_id'], self.tokens[creator][0])
                queue._finish_or_requeue_job(job['id'])

    def test_governor_can_cancel_own_extraction(self):
        url = '/products/extraction-jobs'
        created = self.client.post(url, headers=self.headers('BOB'), json={'skus': ['W11509387']})
        self.assertEqual(created.status_code, 200, created.text)
        response = self.client.post(f"{url}/{created.json()['id']}/cancel", headers=self.headers('BOB'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['cancel_requested'])

    def test_simultaneous_requests_from_different_users_reserve_one_owner(self):
        def enqueue(owner):
            try:return queue.enqueue_extraction_job(['W11509387'],owner_user_id=owner)['id']
            except HTTPException as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(enqueue,[self.tokens['ALICE'][0],self.tokens['BOB'][0]]))
        self.assertEqual(results.count(409),1)
        with self.sessions() as db:self.assertEqual(db.query(ExtractionJob).count(),1)

    def test_cep_is_saved_only_for_authenticated_user_and_default_is_maintained(self):
        response=self.client.put('/auth/me/preferences',headers=self.headers('ALICE'),json={'logistics_postal_code':'30111-000'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/auth/me',headers=self.headers('ALICE')).json()['logistics_postal_code'],'30111000')
        self.assertEqual(self.client.get('/auth/me',headers=self.headers('BOB')).json()['logistics_postal_code'],'01001000')
        self.assertEqual(self.client.put('/auth/me/preferences',headers=self.headers('ALICE'),json={'logistics_postal_code':'bad'}).status_code,422)
        self.assertEqual(self.client.put('/auth/me/preferences',json={'logistics_postal_code':'30111000'}).status_code,401)

    def test_metadata_manual_bulk_atomic_validation_and_no_collection_timestamp(self):
        with self.sessions() as db:
            db.add(KnownSku(sku='W11199342'));db.commit()
        items=[{'sku':'w11509387','category':'PURIFICADORES','cluster':'CONSUMÍVEIS & ACESSÓRIOS','subfamily':'Filtros','market_type':'Cativo'}, {'sku':'W11199342','category':'Peças','market_type':'Competitivo'}]
        self.assertEqual(self.client.put('/products/skus/metadata',headers=self.headers('ALICE'),json={'items':items}).status_code,200)
        with self.sessions() as db:
            row=db.query(KnownSku).filter_by(sku='W11509387').one();self.assertEqual(row.subfamily,'Filtros');self.assertEqual(row.category,'PURIFICADORES')
        invalid=[{'sku':'W11509387','category':'MUST NOT CHANGE'}, {'sku':'INVALID SPACE','category':'bad'}]
        self.assertEqual(self.client.put('/products/skus/metadata',headers=self.headers('ALICE'),json={'items':invalid}).status_code,422)
        with self.sessions() as db:self.assertEqual(db.query(KnownSku).filter_by(sku='W11509387').one().category,'PURIFICADORES')
        self.assertEqual(self.client.put('/products/skus/metadata',headers=self.headers('VIEWER'),json={'items':items}).status_code,403)

    def test_api_toggle_is_snapshotted_and_quick_mode_always_enabled(self):
        with self.sessions() as db:
            control=db.get(CrawlerControl,1);control.api_with_scraping=False;db.commit()
        precise=self.client.post('/products/extraction-jobs',headers=self.headers('ALICE'),json={'skus':['W11509387'],'mode':'precise'}).json()
        quick=self.client.post('/products/extraction-jobs',headers=self.headers('ALICE'),json={'skus':['W11199342'],'mode':'quick'}).json()
        self.assertFalse(precise['api_with_scraping']);self.assertTrue(quick['api_with_scraping'])
        identity=ProductIdentity(sku='W11509387')
        control=SimpleNamespace(api_with_scraping=False,request_timeout_seconds=5)
        with patch('services.precise_extraction.get_runtime_provider_settings',return_value={'enabled':True,'configured':True,'usage_mode':'always'}), patch('services.precise_extraction.discover_html_pages',return_value=[]) as html, patch('services.precise_extraction._direct_candidates') as api:
            with self.sessions() as db:
                self.assertEqual(_html_and_parallel_api(db,identity,find_target_store('mercado_livre'),control,None,True),([],None))
            api.assert_not_called();html.assert_called_once()

    def test_metadata_template_roundtrip_requires_admin_and_existing_skus(self):
        from io import BytesIO
        from openpyxl import load_workbook
        url='/products/skus/metadata'
        self.assertEqual(self.client.get(url+'/template').status_code,401)
        self.assertEqual(self.client.get(url+'/template',headers=self.headers('BOB')).status_code,403)
        response=self.client.get(url+'/template',headers=self.headers('ALICE'))
        self.assertEqual(response.status_code,200,response.text)
        book=load_workbook(BytesIO(response.content));sheet=book.active
        self.assertEqual([cell.value for cell in sheet[1]],['SKU','Categoria','Cluster','Subfamilia','Mercado'])
        self.assertEqual(sheet['A2'].value,'W11509387')
        for col,value in zip('BCDE',['PURIFICADORES','CONSUMÍVEIS & ACESSÓRIOS','Filtros','Competitivo']):sheet[f'{col}2']=value
        output=BytesIO();book.save(output)
        response=self.client.post(url+'/import',headers=self.headers('ALICE'),files={'file':('modelo.xlsx',output.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        self.assertEqual(response.status_code,200,response.text)
        with self.sessions() as db:
            row=db.query(KnownSku).filter_by(sku='W11509387').one()
            self.assertEqual((row.category,row.cluster,row.subfamily,row.market_type),('PURIFICADORES','CONSUMÍVEIS & ACESSÓRIOS','Filtros','Competitivo'))
        self.assertEqual(self.client.put(url,headers=self.headers('BOB'),json={'items':[{'sku':'W11509387','category':'forbidden'}]}).status_code,403)
        response=self.client.put(url,headers=self.headers('ALICE'),json={'items':[{'sku':'UNKNOWN123','category':'new'}]})
        self.assertEqual(response.status_code,404)
        with self.sessions() as db:self.assertIsNone(db.query(KnownSku).filter_by(sku='UNKNOWN123').first())
        broken=self.client.post(url+'/import',headers=self.headers('ALICE'),files={'file':('corrompido.xlsx',b'not a workbook','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        self.assertEqual(broken.status_code,400)

    def test_guest_search_requires_saved_content_and_does_not_scrape(self):
        with self.sessions() as db:
            product=Product(sku='W11509387',name='Filtro Purificador W11509387',brand='Brastemp',category='Filtros')
            db.add(product);db.flush();db.add(PriceHistory(product_id=product.id,price=100,store='Dufrio',source='Dufrio',url='https://www.dufrio.com.br/filtro-w11509387.html'));db.commit()
        with patch('routes.products.run_all_scrapers') as scrape:
            self.assertEqual(len(self.client.get('/products/search?q=W11509387').json()['results']),1)
            self.assertEqual(self.client.get('/products/search?q=W1150938700').json()['results'],[])
            self.assertEqual(self.client.get('/products/search?q=Filtro+UnicornioInexistente').json()['results'],[])
            self.assertEqual(self.client.get('/products/search?q=UnicornioInexistente').json()['results'],[])
            self.assertEqual(self.client.get('/products/search',params={'q':'%%'}).json()['results'],[])
            self.assertEqual(self.client.get('/products/search',params={'q':'x y'}).json()['results'],[])
            scrape.assert_not_called()
        for path in ['/products/analytics/sheet','/products/analytics/sheet/options','/products/analytics/sheet/summary','/products/analytics/sheet/export']:
            self.assertEqual(self.client.get(path).status_code,401)
            self.assertEqual(self.client.get(path,headers=self.headers('VIEWER')).status_code,200)

    def test_logged_in_users_can_search_new_content_but_guests_cannot(self):
        result={'results':[{'sku':None,'name':'Produto novo','store':'Dufrio','source':'Dufrio','url':'https://www.dufrio.com.br/novo.html','current_price':123}], 'comparison':[], 'stores':[]}
        for name in ('ALICE','BOB','VIEWER'):
            with patch('routes.products.run_all_scrapers',return_value=result) as scrape:
                response=self.client.get('/products/search',params={'q':'Produto fora do banco'},headers=self.headers(name))
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['results'][0]['current_price'],123)
                scrape.assert_called_once()
                self.assertEqual(scrape.call_args.kwargs['postal_code'],'01001000')
                self.assertFalse(scrape.call_args.kwargs['persist'])
        with patch('routes.products.run_all_scrapers') as scrape:
            self.assertEqual(self.client.get('/products/search',params={'q':'Produto fora do banco'}).json()['results'],[])
            self.assertEqual(self.client.get('/products/search',params={'q':'Produto fora do banco'},headers={'Authorization':'Bearer invalid'}).status_code,401)
            scrape.assert_not_called()

    def test_metadata_does_not_register_codes_mentioned_in_product_names(self):
        with self.sessions() as db:
            db.add(Product(sku='W11509387', name='Refil W11509387 compatível com W12345678'))
            db.commit()
        response = self.client.put('/products/skus/metadata', headers=self.headers('ALICE'),
                                   json={'items': [{'sku': 'W12345678', 'category': 'Outros'}]})
        self.assertEqual(response.status_code, 404, response.text)
        with self.sessions() as db:
            self.assertIsNone(db.query(KnownSku).filter_by(sku='W12345678').first())

    def test_live_exact_sku_search_does_not_register_material(self):
        with patch('routes.products.run_all_scrapers', return_value={'results': [], 'comparison': [], 'stores': []}) as scrape:
            response = self.client.get('/products/search?q=W12345678', headers=self.headers('ALICE'))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(scrape.call_args.kwargs['persist'])
        with self.sessions() as db:
            self.assertIsNone(db.query(KnownSku).filter_by(sku='W12345678').first())

    def test_live_navbar_search_respects_other_users_extraction(self):
        queue.enqueue_extraction_job(['W11509387'],owner_user_id=self.tokens['ALICE'][0])
        with patch('routes.products.run_all_scrapers') as scrape:
            response=self.client.get('/products/search?q=ProdutoNovo',headers=self.headers('BOB'))
            self.assertEqual(response.status_code,409)
            scrape.assert_not_called()

    def test_scheduler_runs_at_local_time_once_and_processed_by_real_queue(self):
        with self.sessions() as db:
            db.add(ExtractionSchedule(id=1,enabled=True,owner_user_id=self.tokens['ALICE'][0],days='2',start_time='09:00',end_time='18:00'));db.commit()
        # Tuesday, November 10: 12:00 UTC is 09:00 in São Paulo.
        now=datetime(2026,11,10,11,59)
        with patch.object(scheduler,'SessionLocal',self.sessions):
            schedule=scheduler._schedule_snapshot()
            self.assertIsNone(scheduler._due_run_key(schedule,now))
            key=scheduler._due_run_key(schedule,datetime(2026,11,10,12,0))
            self.assertEqual(key,'2026-11-10-0900')
            calls=[]
            previous=queue._processor
            queue.configure_extraction_processor(lambda sku,options:(calls.append((sku,options)) or {'status':'updated'}))
            try:
                job=scheduler._run_cycle(schedule['postal_code'],key,True,owner_user_id=schedule['owner_user_id'])
                self.assertEqual(queue._process_job_batch(job['id']),'queued')
                self.assertEqual(calls[0][1]['store_phase'],'general')
                self.assertNotIn('shopee',calls[0][1]['stores'])
                self.assertEqual(queue._process_job_batch(job['id']),'completed')
                self.assertEqual(calls[1][1]['store_phase'],'marketplaces')
                self.assertEqual(set(calls[1][1]['stores']),{'mercado_livre','shopee'})
                self.assertEqual(calls[0][0],'W11509387')
                self.assertEqual(scheduler._schedule_snapshot()['last_run_key'],key)
                with patch.object(scheduler,'_due_run_key',return_value=key),patch.object(scheduler,'_run_cycle') as again:
                    scheduler._tick();again.assert_not_called()
            finally:queue.configure_extraction_processor(previous)

    def test_busy_schedule_retries_slot_when_other_owner_finishes(self):
        job=queue.enqueue_extraction_job(['W11199342'],owner_user_id=self.tokens['BOB'][0])
        with patch.object(scheduler,'SessionLocal',self.sessions):
            with self.assertRaises(HTTPException):scheduler._run_cycle('01001000','slot',True,owner_user_id=self.tokens['ALICE'][0])
            with self.sessions() as db:
                row=db.get(ExtractionJob,job['id']);row.status='completed';db.commit()
            self.assertEqual(scheduler._run_cycle('01001000','slot',True,owner_user_id=self.tokens['ALICE'][0])['owner_user_id'],self.tokens['ALICE'][0])
