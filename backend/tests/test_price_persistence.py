import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models.product import PriceHistory, Product
from routes.products import _sku_status_map
from services.scraper_service import _deduplicate, _save_result


class PricePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _offer(self, seller, price, **details):
        return {
            "name": "Peça Whirlpool W10531315",
            "price": price,
            "store": seller,
            "source": "Amazon Brasil",
            "url": "https://amazon.example/dp/B000000001",
            "brand": "Whirlpool",
            "category": "Peças",
            "image_url": None,
            "sku": "W10531315",
            **details,
        }

    def test_same_page_keeps_each_seller_separate(self):
        offers = _deduplicate([
            self._offer("Seller A", 120),
            self._offer("Seller B", 135),
        ])
        self.assertEqual(len(offers), 2)
        for offer in offers:
            _save_result(offer, self.db)
        self.db.commit()

        offers = self.db.query(PriceHistory).order_by(PriceHistory.store).all()
        self.assertEqual([(offer.store, offer.price) for offer in offers], [
            ("Seller A", 120), ("Seller B", 135),
        ])
        self.assertEqual(self.db.query(Product).count(), 2)
        self.assertEqual(_sku_status_map(self.db, ["W10531315"])["W10531315"]["offer_count"], 2)
        self.assertEqual(_sku_status_map(self.db)["W10531315"]["offer_count"], 2)

    def test_new_observation_does_not_reuse_old_shipping_or_zip(self):
        _save_result(self._offer(
            "Seller A", 120, shipping_cost=15, delivery_max_days=3,
            destination_zip="01001000",
        ), self.db)
        self.db.commit()

        _save_result(self._offer("Seller A", 125, destination_zip="22041001"), self.db)
        self.db.commit()

        latest = self.db.query(PriceHistory).order_by(PriceHistory.id.desc()).first()
        self.assertEqual(latest.price, 125)
        self.assertEqual(latest.destination_zip, "22041001")
        self.assertIsNone(latest.shipping_cost)
        self.assertIsNone(latest.delivery_max_days)
