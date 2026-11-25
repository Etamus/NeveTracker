import json
import unittest
from datetime import date,datetime
from types import SimpleNamespace
from unittest.mock import patch
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import ValidationError

from database import Base
from models.product import ExtractionJob,ExtractionJobItem,LeadTimeQuote,PriceHistory,ProductSource
from schemas.product import SkuBatchRequest
from services.seller_selection import seller_matches
from services.price_observations import record_observation
from services.complete_export import complete_workbook
from services.quick_extraction import run_quick_extraction


class SellerAndDateTests(unittest.TestCase):
    def setUp(self):
        from tests.marketplace_fixture import isolate_marketplace_control
        isolate_marketplace_control(self)
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions=sessionmaker(bind=self.engine,autoflush=False)
        self.db=self.sessions()
        self.addCleanup(self.engine.dispose);self.addCleanup(self.db.close)
        self.offer=dict(sku='W10531315',name='Sensor W10531315',brand='Brastemp',category='Sensor',image_url=None,
            source='Magazine Luiza',store='Loja A',seller='Loja A',url='https://www.magazineluiza.com.br/sensor/p/123',price=100.0,
            availability='in_stock',destination_zip='01001000',shipping_cost=12.5,delivery_max_days=3)

    def test_validates_store_scoped_custom_names_and_deduplicates_platform_suffix(self):
        payload=SkuBatchRequest(skus=['W10531315'],sellers={'magalu':[' Peças Ágil ','pecas agil-Magalu'],'shopee':['Loja A']})
        self.assertEqual(payload.sellers,{'magalu':['Peças Ágil'],'shopee':['Loja A']})
        for sellers in ({'bad':['Name']},{'magalu':[' ']},{'magalu':['x'*161]},{'shopee':['A\nB']}):
            with self.assertRaises(ValidationError): SkuBatchRequest(skus=['W10531315'],sellers=sellers)

    def test_exact_seller_names_are_scoped_to_their_platform_and_unknown_is_not_saved(self):
        selection={'magalu':['Peças Ágil']}
        self.assertTrue(seller_matches(selection,{'source':'Magazine Luiza','store':'pecas agil-Magalu'}))
        self.assertFalse(seller_matches(selection,{'source':'Magazine Luiza','store':'Peças Ágil Plus'}))
        self.assertTrue(seller_matches(selection,{'source':'Shopee','store':'Outro'}))
        unknown=SimpleNamespace(store='magalu',seller=None)
        self.assertTrue(seller_matches(selection,unknown,allow_unknown=True))
        self.assertFalse(seller_matches(selection,unknown))
        self.assertTrue(seller_matches(selection,{'source':'Magazine Luiza','store':'Magazine Luiza','_seller_unknown':True},allow_unknown=True))
        self.assertFalse(seller_matches(selection,{'source':'Magazine Luiza','store':'Magazine Luiza','_seller_unknown':True}))

    def test_coverage_does_not_attribute_other_sellers_failures_to_the_selected_one(self):
        from services.scrape_pagination import coverage_by_store
        self.db.add_all([ProductSource(sku='W10531315',store='magalu',seller='Loja A',url='https://example.test/a',active=True,validation_status='ok'),ProductSource(sku='W10531315',store='magalu',seller='Loja B',url='https://example.test/b',active=True,validation_status='blocked')])
        self.db.commit()
        result=coverage_by_store(self.db,'W10531315',[{'key':'magalu','label':'Magazine Luiza'}],sellers={'magalu':['Loja A']})
        self.assertEqual(result[0]['sources'],1)
        self.assertEqual(result[0]['failed_sources'],0)

    @patch('services.quick_extraction.first_page')
    def test_quick_does_not_save_or_return_unselected_sellers(self,search):
        search.return_value=[self.offer,{**self.offer,'seller':'Loja B','store':'Loja B','url':'https://www.magazineluiza.com.br/sensor/p/456'}]
        result=run_quick_extraction('W10531315',self.db,stores=['magalu'],sellers={'magalu':['Loja A']})
        self.assertEqual(result['fresh_count'],1)
        self.assertEqual(self.db.query(PriceHistory).count(),1)
        self.assertTrue(all(seller_matches({'magalu':['Loja A']},item) for item in result['results']))

    @patch('services.marketplace_intervention.suspension',return_value=None)
    @patch('services.precise_extraction.collect_sources_with_scrapy')
    def test_precise_skips_other_known_sellers_before_requesting_pages(self,scrape,suspension):
        from services.precise_extraction import _collect_sources
        from services.product_resolver import ProductIdentity
        source=SimpleNamespace(store='magalu',seller='Loja B')
        result=_collect_sources(self.db,ProductIdentity(sku='W10531315'),[source],None,SimpleNamespace(sellers={'magalu':['Loja A']}))
        self.assertEqual(result,([],set()));scrape.assert_not_called()

    def test_queue_persists_seller_selection_and_passes_it_to_resumed_worker(self):
        from services import extraction_queue as queue
        with patch.object(queue,'SessionLocal',self.sessions):
            job=queue.enqueue_extraction_job(['W10531315'],mode='precise',stores=['magalu'],sellers={'magalu':['Loja A']})
            self.assertEqual(job['sellers'],{'magalu':['Loja A']})
            same=queue.enqueue_extraction_job(['W10531315'],mode='precise',stores=['magalu'],sellers={'magalu':['loja a-Magalu']})
            self.assertEqual(same['id'],job['id'])
            other=queue.enqueue_extraction_job(['W10531315'],mode='precise',stores=['magalu'],sellers={'magalu':['Loja B']})
            self.assertNotEqual(other['id'],job['id'])
            with self.sessions() as db:
                item=db.query(ExtractionJobItem).filter_by(job_id=job['id']).one()
                item_id=item.id
                self.assertEqual(json.loads(db.get(ExtractionJob,job['id']).sellers_json),{'magalu':['Loja A']})
            options=queue._mark_item_running(job['id'],item_id)
            self.assertEqual(options['sellers'],{'magalu':['Loja A']})

    def observe(self,stamp,**values):
        record_observation(self.db,{**self.offer,**values},stamp)
        self.db.commit()

    def test_progress_counts_only_requested_sellers(self):
        from services.extraction_queue import serialize_job
        stamp=datetime(2026,11,17,12)
        job=ExtractionJob(id='progress',request_key='progress',status='running',mode='precise',scope='all',total_skus=1,created_at=datetime(2026,11,17,10),stores_json='["magalu"]',sellers_json='{"magalu":["Loja A"]}',postal_codes_json='["01001000"]')
        job.items=[ExtractionJobItem(sku='W10531315',position=0)]
        self.db.add(job)
        for seller in ['Loja A','Loja B']:
            self.observe(stamp,seller=seller,store=seller)
            self.db.add(LeadTimeQuote(quote_key=seller,sku='W10531315',source='Magazine Luiza',seller=seller,url=self.offer['url'],uf='SP',postal_code='01001000',status='quoted',shipping_cost=12,captured_at=stamp,item_json='{}'))
        self.db.commit()
        result=serialize_job(job)
        self.assertEqual(result['quotes_checked'],1)
        self.assertEqual(result['quotes_complete'],1)
        self.assertEqual(result['observations_saved'],1)
        self.assertEqual(result['observed_skus'],1)

    def test_date_uses_brasilia_boundaries_and_preserves_old_freight_after_quote_overwrite(self):
        self.observe(datetime(2026,11,17,2,59,59),price=1)
        self.observe(datetime(2026,11,17,3),price=11,shipping_cost=1)
        self.observe(datetime(2026,11,18,2,59,59),price=22,shipping_cost=2)
        self.observe(datetime(2026,11,18,3),price=33,shipping_cost=3)
        self.db.add(LeadTimeQuote(quote_key='later',sku='W10531315',source='Magazine Luiza',seller='Loja A',url=self.offer['url'],uf='SP',postal_code='01001000',status='quoted',shipping_cost=99,delivery_max_days=99,captured_at=datetime(2026,11,18,4),item_json='{}'))
        self.db.commit()
        book=load_workbook(complete_workbook(self.db,date_from=date(2026,11,17),date_to=date(2026,11,17)))
        rows=list(book['Database'].iter_rows(min_row=2,values_only=True))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0][5],22);self.assertEqual(rows[0][10],2)
        history=list(book['Histórico de preços'].iter_rows(min_row=2,values_only=True))
        self.assertEqual([row[5] for row in history],[11,22])
        freight=list(book['Frete por estado'].iter_rows(min_row=2,values_only=True))
        self.assertEqual(len(freight),1);self.assertEqual(freight[0][5],2)
        self.assertEqual(freight[0][1],'SP')

    def test_dated_job_export_filters_sellers_and_keeps_stockout_rows(self):
        self.observe(datetime(2026,11,17,12),price=None,availability='out_of_stock')
        self.observe(datetime(2026,11,17,12),store='Loja B',seller='Loja B',price=200)
        job=ExtractionJob(id='scoped',request_key='scoped',status='completed',stores_json='["magalu"]',sellers_json='{"magalu":["Loja A"]}',postal_codes_json='["01001000"]')
        job.items=[ExtractionJobItem(sku='W10531315',position=0)];self.db.add(job);self.db.commit()
        book=load_workbook(complete_workbook(self.db,'scoped',date_from=date(2026,11,17)))
        self.assertEqual(book['Database'].max_row,2)
        self.assertEqual(book['Database'].cell(2,6).value,'Sem estoque')
        self.assertEqual(book['Histórico de preços'].max_row,2)
        self.assertEqual(book['Lead Time detalhes'].cell(2,6).value,'Sem estoque')

    def test_empty_date_is_not_backfilled_with_another_days_data(self):
        self.observe(datetime(2026,11,17,12))
        book=load_workbook(complete_workbook(self.db,date_from=date(2026,11,16)))
        self.assertTrue(all(sheet.max_row==1 for sheet in book))


if __name__=='__main__':unittest.main()
