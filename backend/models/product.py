from sqlalchemy import Boolean, Column, Integer, String, Float, DateTime, ForeignKey, Text, UniqueConstraint, Index, select, false
from sqlalchemy.orm import relationship, deferred, aliased
from database import Base
from datetime import datetime
from storage_codec import PackedJSONText

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
    __table_args__ = (
        Index('ix_price_latest_offer', 'product_id', 'destination_zip', 'timestamp', 'id'),
        Index('ix_price_product_latest', 'product_id', 'timestamp', 'id'),
        Index('ix_price_source_seller', 'source', 'store'),
        Index('ix_price_timestamp', 'timestamp'),
        Index('ix_price_url_latest', 'url', 'timestamp', 'id'),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    price = Column(Float, nullable=True)
    store = Column(String, index=True)
    source = Column(String, nullable=True)
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
    delivery_days_type = Column(String, nullable=True)
    handling_min_days = Column(Integer, nullable=True)
    handling_max_days = Column(Integer, nullable=True)
    destination_zip = Column(String, nullable=True)
    availability = Column(String, nullable=True)
    fulfillment_type = Column(String, nullable=True)
    fulfilled_by = Column(String, nullable=True)
    fulfillment_evidence = Column(String, nullable=True)
    logistics_source = Column(String, nullable=True)
    logistics_captured_at = Column(DateTime, nullable=True)
    is_cached = Column(Boolean, default=False, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="prices")


def latest_price_at_destination(candidate):
    newer = aliased(PriceHistory)
    return (select(newer.id).where(newer.product_id == candidate.product_id,
        newer.destination_zip.is_not_distinct_from(candidate.destination_zip))
        .order_by(newer.timestamp.desc(), newer.id.desc()).limit(1)
        .correlate(candidate).scalar_subquery())


class KnownSku(Base):
    __tablename__ = "known_skus"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, index=True, nullable=False)
    description = Column(String, nullable=True)
    subfamily = Column(String, index=True, nullable=True)
    category = Column(String, index=True, nullable=True)
    cluster = Column(String, index=True, nullable=True)
    portfolio = Column(String, index=True, nullable=True)
    market_type = Column(String, index=True, nullable=True)
    ean = Column(String, index=True, nullable=True)
    mpn = Column(String, index=True, nullable=True)
    model = Column(String, index=True, nullable=True)
    brand = Column(String, index=True, nullable=True)
    internal_code = Column(String, nullable=True)
    external_id = Column(String, nullable=True)
    reference_price = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LeadTimeQuote(Base):
    __tablename__ = 'lead_time_quotes'
    __table_args__ = (Index('ix_quote_sku_captured', 'sku', 'captured_at', 'status'),)
    id = Column(Integer, primary_key=True)
    quote_key = Column(String, unique=True, nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False)
    seller = Column(String, nullable=False)
    url = Column(String, nullable=False)
    uf = Column(String(2), nullable=False)
    postal_code = Column(String(8), nullable=False)
    item_json = Column(PackedJSONText(), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default='queued', index=True)
    shipping_cost = Column(Float, nullable=True)
    delivery_min_days = Column(Integer, nullable=True)
    delivery_max_days = Column(Integer, nullable=True)
    delivery_days_type = Column(String, nullable=True)
    shipping_method = Column(String, nullable=True)
    logistics_source = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    captured_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LeadTimeDestination(Base):
    __tablename__ = 'lead_time_destinations'
    postal_code = Column(String(8), primary_key=True)
    uf = Column(String(2), nullable=False)
    city = Column(String, nullable=False)
    position = Column(Integer, nullable=False, default=0)


class OfferObservation(Base):
    """Append-only observations, independent from mutable latest-price rows."""
    __tablename__ = 'offer_observations'
    __table_args__ = (Index('ix_observation_offer_latest', 'offer_key', 'observed_at', 'id'),)
    id = Column(Integer, primary_key=True)
    legacy_price_id = Column(Integer, unique=True, nullable=True)
    offer_key = Column(String(64), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    source = Column(String, nullable=False, index=True)
    seller = Column(String, nullable=False, index=True)
    brand = Column(String, nullable=True, index=True)
    name = Column(String, nullable=True)
    url = Column(Text, nullable=False)
    price = Column(Float, nullable=True)
    availability = Column(String, nullable=True)
    postal_code = Column(String(8), nullable=True)
    observed_at = Column(DateTime, nullable=False, index=True)
    changed = Column(Boolean, nullable=False, default=False)
    origin = Column(String, nullable=False, default='collection')
    snapshot_json = Column(PackedJSONText(), nullable=False)


class ExtractionSchedule(Base):
    __tablename__ = "extraction_schedule"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, default=False, nullable=False)
    owner_user_id = Column(Integer, nullable=True)
    policy = Column(String, default="custom", nullable=False)
    postal_code = Column(String, nullable=True)
    start_time = Column(String, default="09:00", nullable=False)
    end_time = Column(String, default="18:00", nullable=False)
    days = Column(String, default="1", nullable=False)
    repeat_every_hours = Column(Integer, default=0, nullable=False)
    force_refresh = Column(Boolean, default=True, nullable=False)
    only_stale = Column(Boolean, default=False, nullable=False)
    last_run_key = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    last_site_run = Column(DateTime, nullable=True)
    last_retail_run = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id = Column(String(36), primary_key=True)
    owner_user_id = Column(Integer, nullable=True, index=True)
    api_with_scraping = Column(Boolean, nullable=False, default=True)
    kind = Column(String(20), default="manual", nullable=False, index=True)
    status = Column(String(30), default="queued", nullable=False, index=True)
    priority = Column(Integer, default=20, nullable=False, index=True)
    request_key = Column(String(64), nullable=False, index=True)
    scope = Column(String(20), default="all", nullable=False)
    mode = Column(String(20), default="quick", nullable=False, index=True)
    force_refresh = Column(Boolean, default=True, nullable=False)
    refresh_if_stale = Column(Boolean, default=False, nullable=False)
    postal_code = Column(String, nullable=True)
    schedule_run_key = Column(String, nullable=True, index=True)
    postal_codes_json = Column(Text, nullable=True)
    stores_json = Column(Text, nullable=True)
    sellers_json = Column(Text, nullable=True)
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


class ProductSource(Base):
    __tablename__ = "product_sources"
    __table_args__ = (
        UniqueConstraint(
            "sku", "store", "seller", "url", name="uq_product_source_identity"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    sku = Column(String, nullable=False, index=True)
    store = Column(String, nullable=False, index=True)
    seller = Column(String, nullable=True, index=True)
    url = Column(Text, nullable=False)
    external_product_id = Column(String, nullable=True, index=True)
    asin = Column(String, nullable=True, index=True)
    item_id = Column(String, nullable=True, index=True)
    shop_id = Column(String, nullable=True, index=True)
    ean = Column(String, nullable=True, index=True)
    mpn = Column(String, nullable=True, index=True)
    model = Column(String, nullable=True, index=True)
    brand = Column(String, nullable=True, index=True)
    match_confidence = Column(Float, nullable=False, default=0)
    match_method = Column(String, nullable=False, default="unverified")
    listing_price = Column(Float, nullable=True)
    listing_name = Column(Text, nullable=True)
    listing_method = Column(String, nullable=True)
    listing_seen_at = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=True, index=True)
    validation_status = Column(String, nullable=False, default="valid", index=True)
    failure_reason = Column(String, nullable=True)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_attempted_at = Column(DateTime, nullable=True)
    last_validated_at = Column(DateTime, nullable=True)
    last_collected_at = Column(DateTime, nullable=True)
    invalidated_at = Column(DateTime, nullable=True)


class ScrapeCursor(Base):
    __tablename__ = "scrape_cursors"
    __table_args__ = (
        UniqueConstraint("sku", "store", "kind", "key", name="uq_scrape_cursor"),
    )

    id = Column(Integer, primary_key=True)
    sku = Column(String, nullable=False, index=True)
    store = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    key = Column(String, nullable=False)
    initial_url = Column(Text, nullable=True)
    next_url = Column(Text, nullable=False)
    completed = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_attempted_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    last_status = Column(String, nullable=True)
    failure_reason = Column(String, nullable=True)
    browser_attempted_at = Column(DateTime, nullable=True)
    browser_attempted_url = Column(Text, nullable=True)
    visited_urls = Column(Text, nullable=True)
    pages_completed = Column(Integer, nullable=False, default=0)
    candidates_found = Column(Integer, nullable=False, default=0)


class CrawlerControl(Base):
    __tablename__ = "crawler_control"

    id = Column(Integer, primary_key=True, default=1)
    default_mode = Column(String(20), nullable=False, default="quick")
    external_discovery_fallback = Column(Boolean, nullable=False, default=True)
    playwright_product_fallback = Column(Boolean, nullable=False, default=True)
    playwright_shipping_fallback = Column(Boolean, nullable=False, default=True)
    api_with_scraping = Column(Boolean, nullable=False, default=True)
    concurrent_requests_per_domain = Column(Integer, nullable=False, default=2)
    request_timeout_seconds = Column(Integer, nullable=False, default=15)
    precise_sku_workers = Column(Integer, nullable=False, default=8)
    quick_sku_workers = Column(Integer, nullable=False, default=4)
    quote_workers = Column(Integer, nullable=False, default=12)
    quick_timeout_seconds = Column(Integer, nullable=False, default=5)
    discovery_stores_per_round = Column(Integer, nullable=False, default=3)
    adaptive_parallelism = Column(Boolean, nullable=False, default=True)
    reuse_public_browser = Column(Boolean, nullable=False, default=True)
    browser_failure_cooldown_seconds = Column(Integer, nullable=False, default=30)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    hardware_acceleration = Column(Boolean, nullable=False, default=False, server_default=false())


class ExtractionJobItem(Base):
    __tablename__ = "extraction_job_items"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(36), ForeignKey("extraction_jobs.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    position = Column(Integer, nullable=False)
    status = Column(String(20), default="queued", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    store_phase = Column(String(20), nullable=False, default="legacy")
    waiting_stores_json = Column(Text, nullable=True)
    resume_stores_json = Column(Text, nullable=True)
    message = Column(Text, nullable=True)
    results_count = Column(Integer, default=0, nullable=False)
    stores_count = Column(Integer, default=0, nullable=False)
    comparison_count = Column(Integer, default=0, nullable=False)
    in_database = Column(Boolean, default=False, nullable=False)
    offer_count = Column(Integer, default=0, nullable=False)
    store_count = Column(Integer, default=0, nullable=False)
    last_update = Column(DateTime, nullable=True)
    results_json = deferred(Column(PackedJSONText(), nullable=True))
    progress_json = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    job = relationship("ExtractionJob", back_populates="items")
