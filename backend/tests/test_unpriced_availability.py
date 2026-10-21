import json
import unittest
import asyncio
from io import BytesIO
from openpyxl import load_workbook
from types import SimpleNamespace

from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import sessionmaker
from scrapy.http import HtmlResponse

from database import Base
from models.product import PriceHistory
from crawlers.base import parse_product_response
from crawlers.profiles import DEFAULT_PROFILE
from services.availability_migration import ensure_nullable_price
from services.product_resolver import ProductIdentity
from services.precise_extraction import _valid_offer
from services.scraper_service import _save_result, _cached_sku_observations, build_summary_from_cached_results, _build_comparison_rows
from routes.products import _latest_products_query, _serialize_results, export_analytics_sheet, analytics_sheet_summary


class UnpricedAvailabilityTests(unittest.TestCase):
    def offer(self, price=None, availability='out_of_stock'):
        return {'name': 'Sensor W10531315', 'sku': 'W10531315', 'brand': 'Brastemp',
                'category': 'Sensor', 'store': 'Loja Teste', 'seller': 'Loja Teste', 'source': 'Dufrio',
                'url': 'https://www.dufrio.com.br/sensor-w10531315', 'price': price,
                'availability': availability, 'image_url': None}

    def test_only_confirmed_unavailability_accepts_missing_price(self):
        identity = ProductIdentity(sku='W10531315')
        self.assertTrue(_valid_offer(identity, self.offer()))
        self.assertFalse(_valid_offer(identity, self.offer(availability=None)))
        self.assertFalse(_valid_offer(identity, self.offer(price=0)))

    def test_structured_out_of_stock_without_price_is_collected(self):
        item = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Sensor W10531315',
                'offers': {'@type': 'Offer', 'availability': 'https://schema.org/OutOfStock', 'seller': {'name': 'Loja Teste'}}}
        response = HtmlResponse(url=self.offer()['url'], body=('<html><body><script type="application/ld+json">' + json.dumps(item) + '</script></body></html>').encode(), encoding='utf-8')
        source = SimpleNamespace(store='dufrio', seller='Loja Teste', brand=None, external_product_id=None, asin=None, item_id=None)
        result = parse_product_response(response, source, ProductIdentity(sku='W10531315'), DEFAULT_PROFILE)
        self.assertEqual(result['status'], 'OK')
        self.assertIsNone(result['item']['price'])
        self.assertEqual(result['item']['availability'], 'out_of_stock')

    def test_unavailable_seller_is_listed_but_does_not_change_best_price(self):
        available = {**self.offer(25, 'in_stock'), 'id': 1, 'current_price': 25}
        missing = {**self.offer(), 'id': 2, 'store': 'Unavailable seller', 'current_price': None}
        rows = _build_comparison_rows([available, missing], 'W10531315')
        self.assertEqual(rows[0]['cheapest_price'], 25)
        self.assertEqual(rows[0]['pma'], 25)
        self.assertEqual(len(rows[0]['offers']), 2)

    def test_excel_exports_a_sku_with_only_unavailable_unpriced_offers(self):
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            _save_result(self.offer(), db)
            db.commit()
            summary = analytics_sheet_summary(skus='W10531315', search='', source=None, seller=None, page=1, page_size=50, db=db)
            self.assertEqual(summary['items'][0]['availability'], 'out_of_stock')
            self.assertIsNone(summary['items'][0]['cheapest_price'])
            response = export_analytics_sheet(skus='W10531315', search='', source=None, seller=None, db=db)
            async def read():
                return b''.join([chunk async for chunk in response.body_iterator])
            workbook = load_workbook(BytesIO(asyncio.run(read())))
            values = list(workbook['Ofertas'].values)
            self.assertEqual(len(values), 2)
            self.assertIn('Sem estoque', values[1])
            self.assertNotIn(0.0, values[1][:12])
        engine.dispose()

    def test_existing_database_migrates_and_preserves_previous_prices_and_indexes(self):
        engine = create_engine('sqlite://')
        old = MetaData()
        for table in Base.metadata.sorted_tables:
            table.to_metadata(old)
        old.tables['price_history'].c.price.nullable = False
        old.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            _save_result(self.offer(25, 'in_stock'), db)
            db.commit()
        ensure_nullable_price(engine)
        ensure_nullable_price(engine)
        with sessionmaker(bind=engine)() as db:
            _save_result(self.offer(), db)
            db.commit()
            self.assertEqual(db.query(PriceHistory).count(), 2)
            self.assertEqual(db.query(PriceHistory).order_by(PriceHistory.id).first().price, 25)
            latest = _latest_products_query(db).all()
            self.assertEqual(len(latest), 1)
            self.assertIsNone(latest[0][1].price)
            rows = list(_cached_sku_observations(db, 'W10531315'))
            payload = _serialize_results(rows, db, resolve_redirects=False)
            self.assertIsNone(payload[0]['current_price'])
            summary = build_summary_from_cached_results(payload, 'W10531315')
            self.assertEqual(len(summary['results']), 1)
            self.assertEqual(summary['comparison'], [])
        with engine.connect() as connection:
            self.assertTrue(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_price_history_store'").first())
        engine.dispose()
