from sqlalchemy import Boolean, Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(String, nullable=True)
    brand = Column(String, index=True)
    image_url = Column(String, nullable=True)
    category = Column(String, index=True)
    sku = Column(String, index=True, nullable=True)
    
    # Relacionamento: Um produto tem muitos registros de preço
    prices = relationship("PriceHistory", back_populates="product")

class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    price = Column(Float, nullable=False)
    store = Column(String, index=True)
    source = Column(String, index=True, nullable=True)
    discovery_source = Column(String, index=True, nullable=True)
    url = Column(String)
    cash_price = Column(Float, nullable=True)
    installment_price = Column(Float, nullable=True)
    installment_count = Column(Integer, nullable=True)
    installment_value = Column(Float, nullable=True)
    payment_difference = Column(Float, nullable=True)
    shipping_cost = Column(Float, nullable=True)
    shipping_method = Column(String, nullable=True)
    delivery_min_days = Column(Integer, nullable=True)
    delivery_max_days = Column(Integer, nullable=True)
    handling_min_days = Column(Integer, nullable=True)
    handling_max_days = Column(Integer, nullable=True)
    destination_zip = Column(String, nullable=True)
    availability = Column(String, nullable=True)
    logistics_source = Column(String, nullable=True)
    logistics_captured_at = Column(DateTime, nullable=True)
    is_cached = Column(Boolean, default=False, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="prices")


class KnownSku(Base):
    __tablename__ = "known_skus"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExtractionSchedule(Base):
    __tablename__ = "extraction_schedule"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, default=False, nullable=False)
    policy = Column(String, default="custom", nullable=False)
    postal_code = Column(String, nullable=True)
    start_time = Column(String, default="09:00", nullable=False)
    end_time = Column(String, default="18:00", nullable=False)
    days = Column(String, default="1", nullable=False)
    repeat_every_hours = Column(Integer, default=0, nullable=False)
    force_refresh = Column(Boolean, default=True, nullable=False)
    last_run_key = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    last_site_run = Column(DateTime, nullable=True)
    last_retail_run = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
