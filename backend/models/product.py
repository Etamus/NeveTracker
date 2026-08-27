from sqlalchemy import Boolean, Column, Integer, String, Float, DateTime, ForeignKey, Text
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
    subfamily = Column(String, index=True, nullable=True)
    category = Column(String, index=True, nullable=True)
    cluster = Column(String, index=True, nullable=True)
    market_type = Column(String, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id = Column(String(36), primary_key=True)
    kind = Column(String(20), default="manual", nullable=False, index=True)
    status = Column(String(30), default="queued", nullable=False, index=True)
    priority = Column(Integer, default=20, nullable=False, index=True)
    request_key = Column(String(64), nullable=False, index=True)
    scope = Column(String(20), default="all", nullable=False)
    force_refresh = Column(Boolean, default=True, nullable=False)
    refresh_if_stale = Column(Boolean, default=False, nullable=False)
    postal_code = Column(String, nullable=True)
    schedule_run_key = Column(String, nullable=True, index=True)
    batch_size = Column(Integer, default=30, nullable=False)
    total_skus = Column(Integer, default=0, nullable=False)
    processed_skus = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    cached_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    current_sku = Column(String, nullable=True)
    message = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship(
        "ExtractionJobItem",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ExtractionJobItem.position",
    )


class ExtractionJobItem(Base):
    __tablename__ = "extraction_job_items"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(36), ForeignKey("extraction_jobs.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    position = Column(Integer, nullable=False)
    status = Column(String(20), default="queued", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    message = Column(Text, nullable=True)
    results_count = Column(Integer, default=0, nullable=False)
    stores_count = Column(Integer, default=0, nullable=False)
    comparison_count = Column(Integer, default=0, nullable=False)
    in_database = Column(Boolean, default=False, nullable=False)
    offer_count = Column(Integer, default=0, nullable=False)
    store_count = Column(Integer, default=0, nullable=False)
    last_update = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    job = relationship("ExtractionJob", back_populates="items")
