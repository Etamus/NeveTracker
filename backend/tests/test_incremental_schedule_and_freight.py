import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import ExtractionJob, ExtractionSchedule, KnownSku, LeadTimeQuote, PriceHistory, Product, ProductSource, ScrapeCursor
from schemas.product import ExtractionScheduleRequest
from routes.products import get_extraction_schedule, update_extraction_schedule
from services import extraction_scheduler as scheduler
from services.lead_time_service import get_shipping_map
from fastapi import FastAPI
from fastapi.testclient import TestClient
from database import get_db
from routes.lead_time import lead_time_router
from services.auth_service import require_operator


class DatabaseFixture(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.db = self.sessions()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()


class IncrementalScheduleTests(DatabaseFixture):

    def observation(self, sku, hours=1, **kwargs):
        product = self.db.query(Product).filter_by(sku=sku).first()
        if not product:
            product = Product(sku=sku, name=sku)
            self.db.add_all([product, KnownSku(sku=sku, updated_at=self.now)])
            self.db.flush()
        price = PriceHistory(product_id=product.id, source='Dufrio', store='Dufrio',
                             url=kwargs.pop('url', 'https://www.dufrio.com.br/' + sku),
                             timestamp=self.now - timedelta(hours=hours), price=10,
                             destination_zip=kwargs.pop('destination_zip', '01001000'), **kwargs)
        self.db.add(price)
        self.db.commit()
        return price

    def stale(self, skus):
        return scheduler._stale_skus(self.db, skus, '01001-000', now=self.now)

    def test_all_offers_must_be_current_not_just_one(self):
        self.observation('W11199342')
        self.observation('W11199342', hours=9, url='https://www.dufrio.com.br/other-seller')
        self.assertEqual(self.stale(['W11199342']), ['W11199342'])

    def test_latest_unchanged_price_is_current_old_history_does_not_expire_it(self):
        self.observation('W11199342', hours=20)
        self.observation('W11199342', hours=1)
        self.assertEqual(self.stale(['W11199342']), [])

    def test_cached_metadata_edit_missing_data_and_wrong_cep_do_not_count(self):
        self.observation('W11199342', hours=9)
        self.observation('W10901034', is_cached=True)
        self.observation('W11198953', destination_zip='30111000')
        codes = ['W11199342', 'W10901034', 'W11198953', 'NEW123']
        self.assertEqual(self.stale(codes), codes)

    def test_failed_source_and_unfinished_discovery_remain_due(self):
        self.observation('W11199342')
        self.observation('W10901034')
        self.db.add(ProductSource(sku='W11199342', store='dufrio', url='https://www.dufrio.com.br/fail',
                                  validation_status='blocked', last_collected_at=self.now,
                                  last_attempted_at=self.now, failure_reason='HTTP 403'))
        self.db.add(ScrapeCursor(sku='W10901034', store='magalu', kind='search', key='query',
                                 next_url='https://www.magazineluiza.com.br/search', completed=False))
        self.db.commit()
        self.assertEqual(self.stale(['W11199342', 'W10901034']), ['W11199342', 'W10901034'])

    def test_recent_out_of_stock_observation_does_not_require_shipping(self):
        self.observation('W11199342', availability='OutOfStock', destination_zip=None)
        self.assertEqual(self.stale(['W11199342']), [])

    def test_toggle_roundtrip_default_off(self):
        self.assertFalse(ExtractionScheduleRequest().only_stale)
        update_extraction_schedule(ExtractionScheduleRequest(only_stale=True), db=self.db)
        self.assertTrue(get_extraction_schedule(db=self.db)['only_stale'])
        update_extraction_schedule(ExtractionScheduleRequest(only_stale=False), db=self.db)
        self.assertFalse(get_extraction_schedule(db=self.db)['only_stale'])

    def test_incremental_filters_queue_and_normal_mode_enqueues_everything(self):
        self.observation('W11199342')
        self.observation('W10901034', hours=9)
        with patch.object(scheduler, 'SessionLocal', self.sessions), patch.object(scheduler, 'enqueue_extraction_job') as enqueue:
            scheduler._run_cycle('01001000', 'slot', False, 'precise', only_stale=True)
            self.assertEqual(enqueue.call_args.args[0], ['W10901034'])
            self.assertTrue(enqueue.call_args.kwargs['force_refresh'])
            enqueue.reset_mock()
            scheduler._run_cycle('01001000', 'slot', True, 'precise', only_stale=False)
            self.assertEqual(enqueue.call_args.args[0], ['W10901034', 'W11199342'])

    def test_empty_scan_records_slot_and_does_not_enqueue(self):
        self.observation('W11199342')
        self.db.add(ExtractionSchedule(id=1, only_stale=True))
        self.db.commit()
        with patch.object(scheduler, 'SessionLocal', self.sessions), patch.object(scheduler, 'enqueue_extraction_job') as enqueue:
            scheduler._run_cycle('01001000', 'all-fresh-slot', True, only_stale=True)
            enqueue.assert_not_called()
        self.db.expire_all()
        self.assertEqual(self.db.get(ExtractionSchedule, 1).last_run_key, 'all-fresh-slot')

    def test_same_slot_active_job_is_not_duplicated_when_pending_sku_list_changes(self):
        self.db.add(ExtractionJob(id='active', request_key='unique', schedule_run_key='slot', status='running'))
        self.db.commit()
        with patch.object(scheduler, 'SessionLocal', self.sessions), patch.object(scheduler, '_known_skus') as scan:
            scheduler._run_cycle('01001000', 'slot', True, only_stale=True)
            scan.assert_not_called()


class FreightMapTests(DatabaseFixture):
    def offer(self, source='Midea', seller='Midea', **kwargs):
        return {'sku': 'MD-RT411FGF01', 'source': source, 'seller': seller,
                'url': 'https://www.midea.com.br/refrigerador', **kwargs}

    def quote(self, amount, uf='SP', status='quoted', seller='Midea', hours=1, **kwargs):
        row = LeadTimeQuote(quote_key=f'{seller}-{uf}-{amount}-{status}', sku='MD-RT411FGF01', source='Midea',
                            seller=seller, url='https://www.midea.com.br/refrigerador', uf=uf,
                            postal_code=kwargs.pop('postal_code', '01001000'), shipping_cost=amount,
                            captured_at=self.now-timedelta(hours=hours), status=status, item_json='{}', **kwargs)
        self.db.add(row); self.db.commit()

    def result(self, offers=None, source=None):
        with patch('services.lead_time_service.sku_offers', return_value=offers if offers is not None else [self.offer()]):
            return get_shipping_map(self.db, 'MD-RT411FGF01', source=source)

    def test_freight_without_lead_time_is_shown_and_never_leaks_to_other_states(self):
        self.quote(42, status='not_quoted')
        result = self.result()
        self.assertEqual(len(result['states']), 27)
        sp = next(state for state in result['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 42)
        self.assertTrue(all(state['quote'] is None for state in result['states'] if state['uf'] != 'SP'))

    def test_choose_cheapest_paid_shipping_keep_zero_when_only_confirmed_option(self):
        self.quote(0, seller='Free')
        self.quote(25, seller='Paid')
        offers = [self.offer(seller='Free'), self.offer(seller='Paid')]
        sp = next(state for state in self.result(offers)['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 25)
        sp = next(state for state in self.result([offers[0]])['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 0)

    def test_wrong_cep_failed_quote_out_of_stock_and_other_source_are_excluded(self):
        self.quote(5, postal_code='30111000')
        self.quote(6, status='failed')
        self.assertTrue(all(state['quote'] is None for state in self.result()['states']))
        self.quote(10, seller='Available')
        offers = [self.offer(seller='Available', availability='OutOfStock')]
        self.assertTrue(all(state['quote'] is None for state in self.result(offers)['states']))
        self.assertTrue(all(state['quote'] is None for state in self.result([self.offer(seller='Available')], source='Consul')['states']))

    def test_price_history_freight_does_not_require_lead_time_and_stale_is_explicit(self):
        offers = [self.offer(destination_zip='01001000', shipping_cost=15, _logistics_date=(self.now-timedelta(hours=30)).isoformat())]
        sp = next(state for state in self.result(offers)['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 15)
        self.assertTrue(sp['quote']['stale'])

    def test_fresh_quote_precedes_cheaper_expired_quote(self):
        self.quote(10, seller='Old', hours=30)
        self.quote(20, seller='New')
        sp = next(state for state in self.result([self.offer(seller='Old'), self.offer(seller='New')])['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 20)

    def test_latest_observation_for_same_offer_wins_across_quote_and_price_tables(self):
        self.quote(10, hours=3)
        offers = [self.offer(destination_zip='01001000', shipping_cost=25, _logistics_date=self.now.isoformat())]
        sp = next(state for state in self.result(offers)['states'] if state['uf'] == 'SP')
        self.assertEqual(sp['quote']['shipping_cost'], 25)
        self.assertEqual(sp['quote_count'], 1)

    def test_analytics_multi_store_filter_applies_to_map_and_quote_queue(self):
        offers = [self.offer(source=source, seller=source) for source in ['Midea', 'Dufrio', 'Consul']]
        for index, item in enumerate(offers):
            self.db.add(LeadTimeQuote(quote_key=f'filter-{index}', sku='MD-RT411FGF01',
                source=item['source'], seller=item['seller'], url=item['url'], uf='SP', postal_code='01001000',
                shipping_cost=[30, 40, 1][index], status='quoted', captured_at=self.now, item_json='{}'))
        self.db.commit()
        app = FastAPI()
        app.include_router(lead_time_router, prefix='/products/lead-time')
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[require_operator] = lambda: object()
        with patch('services.lead_time_service.sku_offers', return_value=offers), TestClient(app) as client:
            response = client.get('/products/lead-time/MD-RT411FGF01/shipping', params=[('source', 'Midea'), ('source', 'Duofrio')])
            self.assertEqual(response.status_code, 200)
            sp = next(state for state in response.json()['states'] if state['uf'] == 'SP')
            self.assertEqual(sp['quote']['shipping_cost'], 30)
            self.assertEqual(sp['quote_count'], 2)
            result = client.post('/products/lead-time/MD-RT411FGF01/refresh', json={'states': ['MG'], 'sources': ['Midea', 'Duofrio']})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['queued'], 2)
            self.assertEqual({row.source for row in self.db.query(LeadTimeQuote).filter_by(uf='MG')}, {'Midea', 'Dufrio'})


if __name__ == '__main__':
    unittest.main()
