import json,unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from database import Base,get_db
from models.product import KnownSku,Product,PriceHistory,ProductSource,ScrapeCursor,LeadTimeQuote,OfferObservation,ExtractionJob,ExtractionJobItem
from routes.products import product_router
from services.auth_service import get_current_user,require_developer,get_optional_current_user
from services.scraper_service import _normalize_result


class MaterialManagementTests(unittest.TestCase):
 def setUp(self):
  self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
  Base.metadata.create_all(self.engine);self.db=sessionmaker(bind=self.engine)()
  self.addCleanup(self.engine.dispose);self.addCleanup(self.db.close)
  app=FastAPI();app.include_router(product_router,prefix='/products')
  self.actor=SimpleNamespace(id=1,role='developer',logistics_postal_code='99999999')
  app.dependency_overrides[get_db]=lambda:self.db
  for dependency in [get_current_user,require_developer,get_optional_current_user]:app.dependency_overrides[dependency]=lambda:self.actor
  self.client=TestClient(app);self.addCleanup(self.client.close)

 def product(self,sku,name=None):
  row=Product(sku=sku,name=name or f'Peça {sku}',brand='Brastemp',category='Peças')
  self.db.add(row);self.db.flush()
  self.db.add(PriceHistory(product_id=row.id,price=100,store='Loja A',source='Brastemp',url='https://example.test/'+sku))
  self.db.commit();return row

 def test_management_paginates_union_and_does_not_infer_materials_from_title_numbers(self):
  for index in range(120):self.db.add(KnownSku(sku=f'W{10000000+index}'))
  self.product('BLF61AR','Lava Louças BLF61AR anúncio 987654321012')
  self.db.commit()
  first=self.client.get('/products/skus/status',params={'page':1,'page_size':50}).json()
  second=self.client.get('/products/skus/status',params={'page':2,'page_size':50}).json()
  self.assertEqual(first['total'],121);self.assertEqual(len(first['items']),50)
  self.assertFalse({row['sku'] for row in first['items']}&{row['sku'] for row in second['items']})
  self.assertNotIn('987654321012',[row['sku'] for row in self.client.get('/products/skus/status').json()['items']])
  found=self.client.get('/products/skus/status',params={'q':'Lava Louças','page':1}).json()
  self.assertEqual([row['sku'] for row in found['items']],['BLF61AR'])

 def test_bulk_selection_lists_matching_keys_without_offer_statistics(self):
  self.db.add_all(KnownSku(sku=f'W10000{index:03}') for index in range(70))
  self.product('BLF61AR')
  with patch('services.material_index.material_status_map', side_effect=AssertionError('Unexpected offer query')):
   response=self.client.get('/products/skus/keys',params={'q':'W10000'})
   self.assertEqual(response.status_code,200)
   self.assertEqual(response.json()['skus'],[f'W10000{index:03}' for index in range(70)])

 def test_delete_removes_every_saved_view_and_preserves_another_sku_mentioned_as_compatible(self):
  sku='W10531315';other='W11199342'
  self.db.add(KnownSku(sku=sku));a=self.product(sku)
  b=self.product(other,f'Peça {other} compatível {sku}')
  self.db.add(KnownSku(sku=other,description=f'Peça {other} compatível {sku}'))
  self.db.add_all([ProductSource(product_id=a.id,sku=sku,store='brastemp',seller='Loja A',url='https://example.test/'+sku),
   ScrapeCursor(sku=sku,store='brastemp',kind='search',key=sku,next_url='https://example.test/'),
   LeadTimeQuote(quote_key='quote',sku=sku,source='Brastemp',seller='Loja A',url='https://example.test/'+sku,uf='SP',postal_code='01001000',item_json='{}'),
   OfferObservation(offer_key='observation',sku=sku,source='Brastemp',seller='Loja A',url='https://example.test/'+sku,snapshot_json='{}',observed_at=datetime.utcnow())])
  job=ExtractionJob(id='done',request_key='done',status='completed',total_skus=2)
  job.items=[ExtractionJobItem(sku=sku,position=0,status='completed',results_json=json.dumps([{'sku':sku}])),
   ExtractionJobItem(sku=other,position=1,status='completed',results_json=json.dumps([{'sku':sku},{'sku':other}]))]
  self.db.add(job);self.db.commit()
  response=self.client.delete('/products/skus/'+sku)
  self.assertEqual(response.status_code,200,response.text)
  for model in [KnownSku,Product,ProductSource,ScrapeCursor,LeadTimeQuote,OfferObservation,ExtractionJobItem]:
   self.assertEqual(self.db.query(model).filter_by(sku=sku).count(),0,model.__name__)
  self.assertIsNotNone(self.db.get(Product,b.id))
  retained=self.db.query(ExtractionJobItem).filter_by(sku=other).one()
  self.assertEqual(json.loads(retained.results_json),[{'sku':other}])
  for path in ['/products/skus/status?q='+sku,'/products/analytics/sheet?skus='+sku,'/products/analytics/sheet/summary?skus='+sku,'/products/analytics/sheet/summary?search='+sku,'/products/analytics/sheet?search='+sku]:
   self.assertEqual(self.client.get(path).json()['items'],[],path)

 def test_quote_only_material_is_listed_and_can_be_deleted(self):
  self.db.add(LeadTimeQuote(quote_key='only',sku='BLF61AR',source='Brastemp',seller='Loja A',url='https://example.test/',uf='SP',postal_code='01001000',item_json='{}'))
  self.db.commit()
  self.assertEqual(self.client.get('/products/skus/status').json()['items'][0]['sku'],'BLF61AR')
  self.assertEqual(self.client.delete('/products/skus/BLF61AR').status_code,200)

 def test_same_input_searches_material_code_and_product_description(self):
  self.product('BLF61AR','Lava Louças Brastemp BLF61AR')
  for search in ['BLF61','Brastemp Lava','Louças']:
   for endpoint in ['sheet','sheet/summary']:
    body=self.client.get('/products/analytics/'+endpoint,params={'search':search}).json()
    self.assertEqual([row['sku'] for row in body['items']],['BLF61AR'])

 def test_exact_extraction_retains_requested_model_instead_of_provider_id(self):
  for sku in ['BLF61AR','TF71S','RE31','MDRX150FGGDX01','MA512W150A','360480']:
   kinds={'BLF61AR':'Lava louças Brastemp','TF71S':'Geladeira Electrolux','RE31':'Geladeira Electrolux',
       'MDRX150FGGDX01':'Cervejeira Midea','MA512W150A':'Máquina de lavar Midea','360480':'Compressor original'}
   item={'name':kinds[sku]+' '+sku,'url':'https://shopee.com.br/product/123456789/987654321','price':1000,'store':'Loja A','source':'Shopee'}
   normalized=_normalize_result(item,sku,'Shopee')
   self.assertIsNotNone(normalized,sku);self.assertEqual(normalized['sku'],sku)
   self.assertIsNone(_normalize_result({**item,'name':'Produto IF55'},sku,'Shopee'))

 def test_default_search_ignores_old_reference_preference_and_custom_postals_are_exact(self):
  summary={'results':[],'stores':[],'comparison':[],'fresh_count':0,'processed_count':0}
  with patch('routes.products.get_active_extraction_job',return_value=None),patch('routes.products.run_all_scrapers',return_value=summary) as collect:
   self.assertEqual(self.client.get('/products/search',params={'q':'BLF61AR'}).status_code,200)
   self.assertEqual(collect.call_args.kwargs['postal_code'],'01001000')
  with patch('routes.products.get_active_extraction_job',return_value=None),patch('routes.products.run_all_scrapers',return_value=summary),patch('services.multi_postal_extraction.collect_destinations',return_value=[]) as quote:
   response=self.client.get('/products/search',params=[('q','BLF61AR'),('postal_codes','30130-003'),('postal_codes','80530-908')])
   self.assertEqual(response.status_code,200,response.text);self.assertEqual(quote.call_args.args[3],['30130003','80530908'])
  self.assertEqual(self.client.get('/products/search',params={'q':'BLF61AR','postal_codes':'123'}).status_code,422)

 def test_metadata_initialization_does_not_recreate_deleted_materials(self):
  from services.sku_metadata_seed import seed_sku_business_metadata
  seed_sku_business_metadata(self.db,create_missing=False)
  self.assertEqual(self.db.query(KnownSku).count(),0)

 def test_summary_query_count_is_bounded_instead_of_one_query_per_row(self):
  for index in range(40):self.product(f'W{10000000+index}')
  calls=[]
  def counted(*args):calls.append(1)
  event.listen(self.engine,'before_cursor_execute',counted)
  try:
   body=self.client.get('/products/analytics/sheet/summary').json()
   self.assertEqual(len(body['items']),40);self.assertLessEqual(len(calls),5)
  finally:event.remove(self.engine,'before_cursor_execute',counted)


if __name__=='__main__':unittest.main()
