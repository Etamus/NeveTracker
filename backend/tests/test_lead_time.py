from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from models.product import KnownSku, Product, PriceHistory, LeadTimeQuote
from routes.lead_time import lead_time_router
from services.lead_time_destinations import CAPITALS
from services.lead_time_service import enqueue_quotes, get_matrix, process_quote_batch, _best_cell


class LeadTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'test.db'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.db.add(KnownSku(sku='W11199342', model='W11199342'))
        p = Product(sku='W11199342', name='Filtro W11199342', brand='Consul')
        self.db.add(p); self.db.flush()
        self.price = PriceHistory(product_id=p.id, price=120, store='Parceiro', source='Magazine Luiza',
            url='https://www.magazineluiza.com.br/filtro-w11199342/p/123/ep/filtro/?seller_id=parceiro',
            availability='in_stock', destination_zip='01001000', shipping_cost=12, delivery_min_days=2,
            delivery_max_days=4, delivery_days_type='business', logistics_captured_at=datetime.utcnow())
        self.db.add(self.price); self.db.commit()

    def tearDown(self):
        self.db.close();self.engine.dispose();self.temp.cleanup()

    def test_all_states_and_capitals_have_distinct_reference_destinations(self):
        self.assertEqual(len(CAPITALS), 27)
        self.assertEqual(len({row['uf'] for row in CAPITALS}), 27)
        self.assertTrue(all(len(row['postal_code']) == 8 and row['postal_code'].isdigit() for row in CAPITALS))

    def test_appliance_lookup_keeps_declared_variants_and_rejects_spare_parts(self):
        from services.product_resolver import ProductIdentity, resolve_candidate
        self.assertTrue(resolve_candidate(ProductIdentity(sku='MD-RT411FGF01'), {'name':'Geladeira Midea MD-RT411FGF011 - 127v'}).accepted)
        self.assertFalse(resolve_candidate(ProductIdentity(sku='LEE18'), {'name':'Vareta suspensao lavadora LEE18'}).accepted)
        self.assertFalse(resolve_candidate(ProductIdentity(sku='LEE18'), {'name':'Agitador inferior LEE18'}).accepted)
        self.assertTrue(resolve_candidate(ProductIdentity(sku='LEE18'), {'name':'Maquina de lavar Electrolux LEE18'}).accepted)

    def test_sp_quote_cannot_fill_other_states(self):
        data = get_matrix(self.db, 'W11199342')
        magalu = next(row for row in data['stores'] if row['key'] == 'magalu')
        self.assertEqual(magalu['cells']['SP']['delivery_max_days'], 4)
        self.assertEqual(magalu['cells']['MG']['status'], 'not_queried')
        self.assertEqual(len(data['stores']), 15)
        self.assertEqual(get_matrix(self.db, 'W99999999')['has_offers'], False)

    def test_enqueue_is_deduplicated_and_does_not_copy_previous_logistics(self):
        self.assertEqual(enqueue_quotes(self.db, 'W11199342', ['SP', 'MG'])['queued'], 2)
        self.assertEqual(enqueue_quotes(self.db, 'W11199342', ['SP', 'MG'], True)['queued'], 0)
        rows = self.db.query(LeadTimeQuote).all()
        self.assertEqual(len(rows), 2)
        for row in rows:
            snapshot = json.loads(row.item_json)
            self.assertNotIn('shipping_cost', snapshot)
            self.assertNotIn('destination_zip', snapshot)

    @patch('services.lead_time_service._page_context', return_value=None)
    @patch('services.offer_enrichment._fetch_details_uncached')
    def test_real_worker_saves_each_destination_separately_without_overwriting_prices(self, quote, context):
        from models.product import CrawlerControl
        self.db.add(CrawlerControl(id=1, playwright_shipping_fallback=False));self.db.commit()
        enqueue_quotes(self.db, 'W11199342', ['SP', 'MG'])
        quote.side_effect = lambda url, price, cep, item: {'shipping_cost': 12 if cep == '01001000' else 25,
            'delivery_max_days': 4 if cep == '01001000' else 7, 'delivery_days_type': 'business', 'destination_zip': cep}
        with patch('services.lead_time_service.SessionLocal', self.sessions):
            self.assertTrue(process_quote_batch())
            self.assertFalse(process_quote_batch())
        self.db.expire_all()
        rows = {row.uf:row for row in self.db.query(LeadTimeQuote)}
        self.assertEqual(rows['SP'].delivery_max_days, 4)
        self.assertEqual(rows['MG'].delivery_max_days, 7)
        self.assertEqual(self.db.get(PriceHistory, self.price.id).destination_zip, '01001000')
        self.assertEqual(self.db.get(PriceHistory, self.price.id).delivery_max_days, 4)
        self.assertEqual(get_matrix(self.db, 'W11199342')['progress']['pending'], 0)

    def test_stockout_is_visible_and_not_quoted(self):
        self.price.availability='out_of_stock';self.price.price=None;self.db.commit()
        data=get_matrix(self.db, 'W11199342')
        magalu=next(row for row in data['stores'] if row['key']=='magalu')
        self.assertEqual(magalu['cells']['SP']['status'],'out_of_stock')
        self.assertEqual(enqueue_quotes(self.db,'W11199342',['SP'])['queued'],0)

    def test_mixed_day_types_are_not_ranked_as_equivalent(self):
        rows=[{'status':'quoted','delivery_max_days':3,'delivery_days_type':'calendar','seller':'A'},
              {'status':'quoted','delivery_max_days':4,'delivery_days_type':'business','seller':'B'}]
        self.assertEqual(_best_cell(rows)['status'],'mixed')

    def test_refresh_requires_permission_and_get_is_available(self):
        app=FastAPI();app.include_router(lead_time_router,prefix='/products/lead-time')
        def database():
            with self.sessions() as db:yield db
        app.dependency_overrides[get_db]=database
        with TestClient(app) as client:
            self.assertEqual(client.get('/products/lead-time/W11199342').status_code,200)
            self.assertIn(client.post('/products/lead-time/W11199342/refresh',json={'states':['SP']}).status_code,[401,403])
