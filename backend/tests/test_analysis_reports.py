from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, get_db
from models.product import OfferObservation, Product, ProductSource, PriceHistory, LeadTimeDestination, LeadTimeQuote
from routes.analysis import analysis_router
from routes.lead_time import lead_time_router
from services.auth_service import require_operator, get_current_user
from services.delivery_destinations import destinations, save_destinations
from services.lead_time_service import get_matrix, enqueue_quotes
from services.price_observations import record_observation, backfill_observations, history_report, compare_groups
from services.scraper_service import _save_result
from services.analysis_workbooks import history_workbook, lead_time_workbook


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name)/'analysis.db'), connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.item = dict(sku='W11199342', name='Filtro W11199342', brand='BRASTEM', category='Filtro', image_url=None,
            price=100, store='Parceiro A', source='Magazine Luiza', seller='Parceiro A',
            url='https://www.magazineluiza.com.br/filtro-w11199342/p/123/ep/filtro/?seller_id=a',
            shipping_cost=12, destination_zip='01001000', availability='in_stock')

    def tearDown(self):
        self.db.close(); self.engine.dispose(); self.temp.cleanup()

    def observe(self, stamp, **changes):
        row = record_observation(self.db, {**self.item, **changes}, datetime.fromisoformat(stamp))
        self.db.commit()
        return row

    def client(self):
        app = FastAPI()
        app.include_router(analysis_router, prefix='/analysis')
        app.include_router(lead_time_router, prefix='/products/lead-time')
        def database():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, username='READER', role='viewer', is_owner=False, is_admin=False)
        return app, TestClient(app)

    def test_real_unchanged_collections_append_and_restart_does_not_duplicate(self):
        _save_result(self.item, self.db); self.db.commit()
        _save_result(self.item, self.db); self.db.commit()
        _save_result({**self.item, 'price': 120}, self.db); self.db.commit()
        self.assertEqual(self.db.query(OfferObservation).count(), 3)
        self.assertEqual(self.db.query(PriceHistory).count(), 2)
        self.assertEqual(backfill_observations(self.db), 0)
        rows = self.db.query(OfferObservation).order_by(OfferObservation.id).all()
        self.assertEqual([r.price for r in rows], [100, 100, 120])
        self.assertEqual([r.changed for r in rows], [False, False, True])
        self.assertEqual(rows[0].brand, 'Brastemp')

    def test_cache_is_never_a_new_observation(self):
        _save_result(self.item, self.db); self.db.commit()
        _save_result({**self.item, 'is_cached': True, 'captured_at': '2020-01-01T00:00:00'}, self.db); self.db.commit()
        self.assertEqual(self.db.query(OfferObservation).count(), 1)

    def test_all_history_includes_older_years_and_exports_same_period(self):
        self.observe('2024-01-01T12:00:00')
        self.observe('2026-11-17T12:00:00', price=120)
        # An unrelated material must not expand the selected SKU's interval.
        self.observe('2020-01-01T12:00:00', sku='OTHER')
        _, client = self.client()
        query = '/analysis/history?skus=W11199342&all_dates=true'
        response = client.get(query)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report['series'][0]['date'], '2024-01-01')
        self.assertEqual(report['series'][-1]['date'], '2026-11-17')
        self.assertGreater(report['total'], 366)
        exported = client.get(query + '&export=true')
        self.assertEqual(exported.status_code, 200)
        workbook = load_workbook(BytesIO(exported.content), read_only=True)
        self.assertGreater(workbook.worksheets[0].max_row, 366)
        workbook.close()

    def test_history_pages_preserve_all_rows_and_daily_chart(self):
        self.observe('2026-11-01T12:00:00')
        self.observe('2026-11-02T12:00:00', price=125, shipping_cost=0)
        self.observe('2026-11-02T12:00:00', destination_zip='30130003')
        self.observe('2026-11-04T12:00:00', price=140, seller='Parceiro B')
        start, end = date(2026, 11, 1), date(2026, 11, 5)
        full = history_report(self.db, start, end, page_size=None)
        for page in range(1, 8):
            report = history_report(self.db, start, end, page=page, page_size=2)
            self.assertEqual(report['items'], full['items'][(page - 1) * 2:page * 2])
            self.assertEqual(report['total'], full['total'])
            self.assertEqual(report['series'], full['series'])

    def test_history_search_matches_whole_report_and_preserves_timeline(self):
        self.observe('2020-01-01T12:00:00', sku='OTHER')
        self.observe('2026-11-01T12:00:00')
        self.observe('2026-11-02T12:00:00', name='Sensor atual', price=125)
        self.observe('2026-11-04T12:00:00', name='Tampa corrigida', price=150)
        _, client = self.client()
        params = {'all_dates': True, 'search': 'SENSOR parceiro', 'page_size': 1}
        with client:
            first = client.get('/analysis/history', params=params).json()
            self.assertEqual(first['total'], 2)
            self.assertEqual(first['start'], '2026-11-01')
            self.assertEqual([day['offers'] for day in first['series']], [0, 1, 1, 0])
            self.assertTrue(first['items'][0]['carried'])
            second = client.get('/analysis/history', params={**params, 'page': 2}).json()
            self.assertEqual(second['items'][0]['previous_price'], 100)
            self.assertEqual(second['items'][0]['difference'], 25)
            response = client.get('/analysis/history', params={**params, 'export': True})
            with BytesIO(response.content) as data:
                workbook = load_workbook(data, read_only=True)
                self.assertEqual(workbook.worksheets[0].max_row, 3)
                workbook.close()

    def test_comparison_search_uses_latest_observation_and_literal_wildcards(self):
        self.observe('2026-11-01T12:00:00', name='Sensor antigo')
        self.observe('2026-11-02T12:00:00', name='Tampa corrigida')
        self.observe('2026-11-01T12:00:00', sku='SENSOR_2', name='Sensor precision 50%', price=45)
        _, client = self.client()
        params = {'left': 'Magazine Luiza', 'right': 'Brastemp', 'search': 'parceiro %'}
        with client:
            report = client.get('/analysis/comparison', params=params).json()
            self.assertEqual(report['groups'][0]['offers'], 1)
            self.assertEqual(report['groups'][0]['minimum'], 45)
            self.assertEqual([row['sku'] for row in report['items']], ['SENSOR_2'])
            obsolete = client.get('/analysis/comparison', params={**params, 'search': 'antigo'}).json()
            self.assertEqual(obsolete['items'], [])
            response = client.get('/analysis/comparison', params={**params, 'export': True})
            with BytesIO(response.content) as data:
                workbook = load_workbook(data, read_only=True)
                rows = list(workbook.worksheets[1].values)
                self.assertEqual([row[0] for row in rows[1:]], ['SENSOR_2'])
                workbook.close()

    def test_cached_price_later_confirmed_does_not_get_backfilled_twice(self):
        _save_result({**self.item, 'is_cached': True, 'captured_at': '2026-01-01T00:00:00'}, self.db); self.db.commit()
        self.assertEqual(self.db.query(OfferObservation).count(), 0)
        _save_result(self.item, self.db); self.db.commit()
        self.assertEqual(self.db.query(OfferObservation).count(), 1)
        self.assertEqual(backfill_observations(self.db), 0)


    def test_freight_and_stock_changes_are_immutable(self):
        self.observe('2026-11-01T12:00:00')
        changed = self.observe('2026-11-01T13:00:00', shipping_cost=20)
        stockout = self.observe('2026-11-01T14:00:00', price=None, availability='out_of_stock')
        self.assertTrue(changed.changed); self.assertTrue(stockout.changed)
        self.assertIn('12', self.db.query(OfferObservation).first().snapshot_json)
        report = history_report(self.db, date(2026,11,1), date(2026,11,1))
        self.assertIsNone(report['items'][0]['price'])
        self.assertEqual(report['items'][0]['checks'], 3)
        self.assertIn('shipping_cost', report['items'][0]['changed_fields'])
        self.assertIn('availability', report['items'][0]['changed_fields'])

    def test_daily_baseline_unchanged_days_and_sao_paulo_midnight(self):
        self.observe('2026-11-01T02:30:00')  # October 31 locally
        self.observe('2026-11-02T12:00:00', price=125)
        self.observe('2026-11-02T14:00:00', price=125)
        rows = {r['date']: r for r in history_report(self.db, date(2026,11,1), date(2026,11,3))['items']}
        self.assertTrue(rows['2026-11-01']['carried'])
        self.assertEqual(rows['2026-11-01']['checks'], 0)
        self.assertEqual(rows['2026-11-02']['difference'], 25)
        self.assertEqual(rows['2026-11-02']['checks'], 2)
        self.assertEqual(rows['2026-11-03']['previous_price'], 125)
        self.assertEqual(rows['2026-11-03']['difference'], 0)
        only = history_report(self.db, date(2026,11,1), date(2026,11,3), carry=False)
        self.assertEqual(len(only['items']), 1)
        self.assertEqual(only['items'][0]['date'], '2026-11-02')

    def test_stock_events_use_confirmed_observations_including_intraday_changes(self):
        self.observe('2026-11-01T12:00:00', availability='in_stock')
        self.observe('2026-11-02T12:00:00', price=None, availability='out_of_stock')
        self.observe('2026-11-02T16:00:00', availability='in_stock')
        self.observe('2026-11-04T12:00:00', availability=None)
        self.observe('2026-11-04T16:00:00', price=None, availability='out_of_stock')
        rows = {r['date']: r for r in history_report(self.db, date(2026,11,2), date(2026,11,4))['items']}
        self.assertEqual([e['kind'] for e in rows['2026-11-02']['stock_events']], ['rupture', 'restock'])
        self.assertEqual(rows['2026-11-02']['stock_events'][0]['previous_observed_at'], datetime(2026,11,1,12))
        self.assertFalse(rows['2026-11-03']['stock_events'])
        self.assertFalse(rows['2026-11-04']['stock_events'])

    def test_backfill_preserves_legacy_seller_and_is_idempotent(self):
        p = Product(sku='W11199342', name='Filtro', brand='BRASTEM')
        self.db.add(p); self.db.flush()
        self.db.add(PriceHistory(product_id=p.id, price=80, store='Parceiro antigo', source='Magazine Luiza', url=self.item['url'], timestamp=datetime(2026,10,1)))
        self.db.commit()
        self.assertEqual(backfill_observations(self.db), 1)
        self.assertEqual(backfill_observations(self.db), 0)
        self.assertEqual(self.db.query(OfferObservation).one().seller, 'Parceiro antigo')

    def test_comparison_period_uses_observations_inside_brasilia_dates(self):
        self.observe('2026-11-01T02:59:59',price=50)
        self.observe('2026-11-01T03:00:00',price=100)
        self.observe('2026-11-02T02:59:59',price=110)
        self.observe('2026-11-02T03:00:00',price=999)
        result=compare_groups(self.db,'source','Magazine Luiza','Brastemp',start=date(2026,11,1),end=date(2026,11,1))
        self.assertEqual(result['groups'][0]['minimum'],110)
        with self.assertRaises(ValueError):compare_groups(self.db,'source','Magazine Luiza','Brastemp',start=date(2026,11,2),end=date(2026,11,1))
        _,client=self.client()
        with client:
            response=client.get('/analysis/comparison?left=Magazine%20Luiza&right=Brastemp&start=2026-11-01&end=2026-11-01')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['groups'][0]['minimum'],110)
            self.assertEqual(client.get('/analysis/comparison?left=Magazine%20Luiza&right=Brastemp&start=2026-11-02&end=2026-11-01').status_code,400)

    def test_store_and_brand_comparisons_use_latest_available_sellers(self):
        self.observe('2026-11-01T12:00:00')
        self.observe('2026-11-02T12:00:00', price=110, destination_zip='30111000')
        self.observe('2026-11-02T13:00:00', source='Brastemp', store='Brastemp', seller='Brastemp', url='https://www.brastemp.com.br/filtro/p', price=90)
        self.observe('2026-11-02T13:00:00', sku='LEE18', brand='Electrolux', source='Electrolux', store='Electrolux', seller='Electrolux', url='https://loja.electrolux.com.br/lee18/p', price=1800)
        stores = compare_groups(self.db, 'source', 'Magazine Luiza', 'Brastemp')
        self.assertEqual(stores['groups'][0]['offers'], 1)
        self.assertEqual(stores['groups'][0]['minimum'], 110)
        self.assertEqual(stores['common_skus'], 1)
        self.assertEqual(stores['items'][0]['difference'], -20)
        brands = compare_groups(self.db, 'brand', 'Brastemp', 'Electrolux')
        self.assertEqual(brands['common_skus'], 0)
        self.assertEqual(len(brands['items']), 2)

    def test_custom_ceps_same_state_remain_distinct_in_queue_and_matrix(self):
        _save_result(self.item, self.db); self.db.commit()
        self.db.add_all([LeadTimeDestination(postal_code='01001000', uf='SP', city='São Paulo', position=0),
                         LeadTimeDestination(postal_code='13010000', uf='SP', city='Campinas', position=1)])
        self.db.commit()
        queued = enqueue_quotes(self.db, 'W11199342', ['SP'], postal_codes=['01001000', '13010000'])
        self.assertEqual(queued['queued'], 2)
        rows = self.db.query(LeadTimeQuote).order_by(LeadTimeQuote.postal_code).all()
        for index, row in enumerate(rows):
            row.status='quoted'; row.delivery_max_days=2+index; row.shipping_cost=15+index; row.captured_at=datetime.utcnow()
        self.db.commit()
        matrix = get_matrix(self.db, 'W11199342')
        store = next(s for s in matrix['stores'] if s['key']=='magalu')
        self.assertEqual(set(store['cells']), {'01001000', '13010000'})
        self.assertEqual(store['cells']['01001000']['delivery_max_days'], 2)
        self.assertEqual(store['cells']['13010000']['delivery_max_days'], 3)
        self.assertEqual(matrix['progress']['completed'], 2)
        book = load_workbook(lead_time_workbook(matrix))
        self.assertEqual(len(book.sheetnames), 2)
        self.assertEqual(book.worksheets[0].max_column, 5)
        self.assertTrue(any(cell.value == '13010000' for row in book.worksheets[1] for cell in row))

    def test_consolidated_listing_is_hidden_only_in_current_comparison(self):
        self.observe('2026-11-02T12:00:00')
        self.db.add(ProductSource(sku=self.item['sku'], store='magalu', seller=self.item['seller'],
                                  url=self.item['url'], validation_status='consolidated', active=False))
        self.db.commit()
        current = compare_groups(self.db, 'source', 'Magazine Luiza', 'Brastemp')
        self.assertEqual(current['groups'][0]['offers'], 0)
        historical = compare_groups(self.db, 'source', 'Magazine Luiza', 'Brastemp', end=date(2026, 11, 2))
        self.assertEqual(historical['groups'][0]['offers'], 1)
        self.assertEqual(history_report(self.db, date(2026, 11, 2), date(2026, 11, 2))['total'], 1)

    def test_comparison_does_not_resurrect_old_brand_after_correction(self):
        self.observe('2026-11-01T12:00:00', brand='Brastemp')
        self.observe('2026-11-02T12:00:00', brand='Electrolux')
        result=compare_groups(self.db, 'brand', 'Brastemp', 'Electrolux')
        self.assertEqual(result['groups'][0]['offers'], 0)
        self.assertEqual(result['groups'][1]['offers'], 1)

    @patch('services.delivery_destinations.resolve_cep')
    def test_invalid_cep_resolution_preserves_selection_and_reset_restores_capitals(self, resolve):
        save_destinations(self.db, [])
        resolve.return_value={'postal_code':'01001000','uf':'SP','city':'São Paulo'}
        save_destinations(self.db, ['01001-000', '01001000'])
        self.assertEqual(len(destinations(self.db)), 1)
        resolve.side_effect=ValueError('CEP inexistente')
        with self.assertRaises(ValueError): save_destinations(self.db, ['99999999'])
        self.assertEqual(destinations(self.db)[0]['postal_code'], '01001000')
        with self.assertRaises(ValueError): save_destinations(self.db, ['abc'])
        self.assertEqual(len(save_destinations(self.db, [])['destinations']), 27)

    def test_extraction_destinations_keep_valid_addresses_when_one_cep_is_unknown(self):
        from services.delivery_destinations import PostalCodeNotFound, first_quotable_cep, save_destinations
        from unittest.mock import patch
        def resolve(cep):
            if cep == '01000000':
                raise PostalCodeNotFound(cep)
            return {'postal_code': cep, 'uf': 'SP', 'city': 'São Paulo'}
        with patch('services.delivery_destinations.resolve_cep', side_effect=resolve):
            self.assertEqual(first_quotable_cep(['01000000', '01001000']), '01001000')
            result = save_destinations(self.db, ['01000000', '01001000', '02022010'], skip_unknown=True)
            self.assertEqual([row['postal_code'] for row in result['destinations']], ['01001000', '02022010'])
            self.assertEqual(result['unknown_postal_codes'], ['01000000'])
            with self.assertRaises(ValueError):
                save_destinations(self.db, ['01000000'], skip_unknown=True)

    def test_history_filter_pagination_and_all_rows_excel(self):
        self.observe('2026-11-01T12:00:00')
        report = history_report(self.db, date(2026,11,1), date(2026,11,3), page_size=1)
        self.assertEqual(report['total'], 3); self.assertEqual(len(report['items']), 1)
        self.assertEqual(history_report(self.db, date(2026,11,1), date(2026,11,3), skus=['OTHER'])['total'], 0)
        app, client = self.client()
        with client:
            params = 'start=2026-11-01&end=2026-11-03&page_size=1'
            self.assertEqual(client.get('/analysis/history?'+params).json()['total'], 3)
            response=client.get('/analysis/history?'+params+'&export=true')
            book=load_workbook(BytesIO(response.content))
            self.assertEqual(book.worksheets[0].max_row, 4)
            self.assertEqual(client.get('/analysis/history?start=2026-11-05&end=2026-11-01').status_code, 400)
            self.assertEqual(client.get('/analysis/comparison?dimension=brand&left=Brastemp&right=Electrolux&export=true').status_code, 200)

    def test_destination_routes_require_operator_and_static_route_wins(self):
        app, client = self.client()
        with client:
            self.assertIn('custom', client.get('/products/lead-time/destinations').json())
            self.assertIn(client.put('/products/lead-time/destinations',json={'postal_codes':[]}).status_code, [401,403])
            app.dependency_overrides[require_operator] = lambda: SimpleNamespace(role='operator')
            self.assertEqual(client.put('/products/lead-time/destinations',json={'postal_codes':['bad']}).status_code, 400)
            self.assertEqual(client.put('/products/lead-time/destinations',json={'postal_codes':[]}).status_code, 200)
            result=client.get('/products/lead-time/W11199342/export')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(load_workbook(BytesIO(result.content)).sheetnames), 2)

    def test_workbook_neutralizes_formula_from_untrusted_seller(self):
        self.observe('2026-11-01T12:00:00', seller='=HYPERLINK("bad")')
        report=history_report(self.db, date(2026,11,1), date(2026,11,1))
        sheet=load_workbook(history_workbook(report)).worksheets[0]
        self.assertEqual(sheet.cell(2,6).data_type, 's')
        self.assertTrue(sheet.cell(2,6).value.startswith("'="))

    def test_api_and_html_overlap_only_when_explicitly_always_enabled(self):
        from services.precise_extraction import _html_and_parallel_api
        from services.store_registry import find_target_store
        started=Event(); caller=get_ident()
        def api(*args): started.set(); return [{'api':True}]
        def html(*args, **kwargs):
            self.assertEqual(get_ident(), caller)
            self.assertTrue(started.wait(2), 'API must start before HTML completes')
            return [{'html':True}]
        with patch('services.precise_extraction.get_runtime_provider_settings', return_value={'enabled':True,'configured':True,'usage_mode':'always'}), \
             patch('services.precise_extraction._direct_candidates', side_effect=api) as direct, \
             patch('services.precise_extraction.discover_html_pages', side_effect=html):
            result=_html_and_parallel_api(self.db, SimpleNamespace(sku='W11199342'), find_target_store('mercado_livre'), SimpleNamespace(request_timeout_seconds=5), None, True)
            self.assertEqual(result, ([{'html':True}],[{'api':True}]))
            direct.assert_called_once()
        with patch('services.precise_extraction.get_runtime_provider_settings', return_value={'enabled':True,'configured':True,'usage_mode':'fallback'}), \
             patch('services.precise_extraction._direct_candidates') as direct, \
             patch('services.precise_extraction.discover_html_pages', return_value=[]):
            self.assertEqual(_html_and_parallel_api(self.db, None, find_target_store('mercado_livre'), SimpleNamespace(request_timeout_seconds=5), None, True), ([],None))
            direct.assert_not_called()
