import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models.product  # noqa: F401
import models.auth  # noqa: F401
from database import Base, SessionLocal, engine
from models.product import KnownSku
from routes.auth import auth_router
from routes.integrations import integration_router
from routes.lead_time import lead_time_router
from routes.analysis import analysis_router
from routes.products import process_extraction_queue_item, product_router
from services.auth_service import ensure_owner_user
from services.extraction_queue import (
    configure_extraction_processor,
    start_extraction_queue,
)
from services.extraction_scheduler import start_extraction_scheduler
from services.scraper_service import KNOWN_SKUS
from services.sku_metadata_seed import RETIRED_AUTO_SEED_SKUS, seed_sku_business_metadata

Base.metadata.create_all(bind=engine)


def _ensure_sqlite_columns():
    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as connection:
        control_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(crawler_control)")}
        if "playwright_product_fallback" not in control_columns:
            connection.exec_driver_sql(
                "ALTER TABLE crawler_control ADD COLUMN playwright_product_fallback BOOLEAN NOT NULL DEFAULT 1"
            )
        product_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(products)")
        }
        if "sku" not in product_columns:
            connection.exec_driver_sql("ALTER TABLE products ADD COLUMN sku VARCHAR")
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_products_sku ON products (sku)"
            )

        price_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(price_history)")
        }
        if "source" not in price_columns:
            connection.exec_driver_sql("ALTER TABLE price_history ADD COLUMN source VARCHAR")
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_price_history_source ON price_history (source)"
            )

        additive_columns = {
            "discovery_source": "VARCHAR",
            "cash_price": "FLOAT",
            "installment_price": "FLOAT",
            "installment_count": "INTEGER",
            "installment_value": "FLOAT",
            "payment_difference": "FLOAT",
            "shipping_cost": "FLOAT",
            "shipping_method": "VARCHAR",
            "delivery_min_days": "INTEGER",
            "delivery_max_days": "INTEGER",
            "delivery_days_type": "VARCHAR",
            "handling_min_days": "INTEGER",
            "handling_max_days": "INTEGER",
            "destination_zip": "VARCHAR",
            "availability": "VARCHAR",
            "fulfillment_type": "VARCHAR",
            "fulfilled_by": "VARCHAR",
            "fulfillment_evidence": "VARCHAR",
            "logistics_source": "VARCHAR",
            "logistics_captured_at": "DATETIME",
            "is_cached": "BOOLEAN NOT NULL DEFAULT 0",
        }
        for column_name, column_type in additive_columns.items():
            if column_name not in price_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE price_history ADD COLUMN {column_name} {column_type}"
                )

        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_price_history_discovery_source "
            "ON price_history (discovery_source)"
        )

        known_sku_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(known_skus)")
        }
        known_sku_additive_columns = {
            "internal_code": "VARCHAR",
            "external_id": "VARCHAR",
            "reference_price": "FLOAT",
            "subfamily": "VARCHAR",
            "category": "VARCHAR",
            "cluster": "VARCHAR",
            "portfolio": "VARCHAR",
            "market_type": "VARCHAR",
            "updated_at": "DATETIME",
            "ean": "VARCHAR",
            "mpn": "VARCHAR",
            "model": "VARCHAR",
            "brand": "VARCHAR",
        }
        for column_name, column_type in known_sku_additive_columns.items():
            if column_name not in known_sku_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE known_skus ADD COLUMN {column_name} {column_type}"
                )
        for column_name in (
            "subfamily", "category", "cluster", "portfolio", "market_type", "ean", "mpn", "model", "brand"
        ):
            connection.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS ix_known_skus_{column_name} "
                f"ON known_skus ({column_name})"
            )

        schedule_columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(extraction_schedule)"
            )
        }
        schedule_additive_columns = {
            "start_time": "VARCHAR NOT NULL DEFAULT '09:00'",
            "end_time": "VARCHAR NOT NULL DEFAULT '18:00'",
            "days": "VARCHAR NOT NULL DEFAULT '1'",
            "repeat_every_hours": "INTEGER NOT NULL DEFAULT 0",
            "force_refresh": "BOOLEAN NOT NULL DEFAULT 1",
            "only_stale": "BOOLEAN NOT NULL DEFAULT 0",
            "last_run_key": "VARCHAR",
            "last_run": "DATETIME",
        }
        for column_name, column_type in schedule_additive_columns.items():
            if column_name not in schedule_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE extraction_schedule ADD COLUMN {column_name} {column_type}"
                )
        connection.exec_driver_sql(
            "UPDATE extraction_schedule SET policy = 'custom' WHERE policy != 'custom'"
        )

        job_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(extraction_jobs)")
        }
        if "mode" not in job_columns:
            connection.exec_driver_sql(
                "ALTER TABLE extraction_jobs ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'quick'"
            )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_extraction_jobs_mode ON extraction_jobs (mode)"
        )

        source_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(product_sources)")
        }
        if "last_attempted_at" not in source_columns:
            connection.exec_driver_sql(
                "ALTER TABLE product_sources ADD COLUMN last_attempted_at DATETIME"
            )
        for column_name, column_type in {
            "listing_price": "FLOAT",
            "listing_name": "TEXT",
            "listing_method": "VARCHAR",
            "listing_seen_at": "DATETIME",
        }.items():
            if column_name not in source_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE product_sources ADD COLUMN {column_name} {column_type}"
                )

        cursor_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(scrape_cursors)")
        }
        for column_name, column_type in {
            "initial_url": "TEXT",
            "last_attempted_at": "DATETIME",
            "last_checked_at": "DATETIME",
            "last_status": "VARCHAR",
            "failure_reason": "VARCHAR",
            "browser_attempted_at": "DATETIME",
            "browser_attempted_url": "TEXT",
            "visited_urls": "TEXT",
            "pages_completed": "INTEGER NOT NULL DEFAULT 0",
            "candidates_found": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column_name not in cursor_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE scrape_cursors ADD COLUMN {column_name} {column_type}"
                )

        auth_user_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(auth_users)")
        }
        if "role" not in auth_user_columns:
            connection.exec_driver_sql(
                "ALTER TABLE auth_users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'viewer'"
            )
        if "avatar_data_url" not in auth_user_columns:
            connection.exec_driver_sql(
                "ALTER TABLE auth_users ADD COLUMN avatar_data_url TEXT"
            )
        connection.exec_driver_sql(
            "UPDATE auth_users SET role = CASE "
            "WHEN username = 'DEUSM2' OR is_owner = 1 THEN 'developer' "
            "WHEN is_admin = 1 THEN 'governor' "
            "ELSE 'viewer' END "
            "WHERE role IS NULL OR role = '' OR "
            "(role = 'viewer' AND (is_admin = 1 OR is_owner = 1))"
        )


_ensure_sqlite_columns()
from services.availability_migration import ensure_nullable_price
ensure_nullable_price(engine)


def _seed_known_skus():
    with engine.begin() as connection:
        existing = {
            row[0]
            for row in connection.exec_driver_sql("SELECT sku FROM known_skus")
        }
        discovered = set(KNOWN_SKUS)
        product_rows = connection.exec_driver_sql(
            "SELECT sku, name FROM products"
        ).fetchall()
        for stored_sku, product_name in product_rows:
            for value in (stored_sku, product_name):
                discovered.update(
                    re.findall(r"\bW\d{6,}\b|\b\d{8,}\b", (value or "").upper())
                )

        for sku in sorted(discovered - existing - RETIRED_AUTO_SEED_SKUS):
            connection.execute(KnownSku.__table__.insert().values(sku=sku))


_seed_known_skus()

from services.price_observations import backfill_observations
with SessionLocal() as history_db:
    backfill_observations(history_db)

with SessionLocal() as metadata_db:
    seed_sku_business_metadata(metadata_db)

with SessionLocal() as auth_db:
    ensure_owner_user(auth_db)

app = FastAPI(title="NeveTracker")

origins = [
    "https://hudok139.github.io",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(product_router, prefix="/products", tags=["Products"])
app.include_router(lead_time_router, prefix='/products/lead-time', tags=['Lead Time'])
app.include_router(analysis_router, prefix='/analysis', tags=['Analysis'])
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(
    integration_router,
    prefix="/integrations",
    tags=["Integrations"],
)


@app.on_event("startup")
def start_background_services():
    configure_extraction_processor(process_extraction_queue_item)
    start_extraction_queue()
    start_extraction_scheduler()
    from services.lead_time_service import start_lead_time_worker
    start_lead_time_worker()


@app.get("/")
async def root():
    return {"status": "API Online"}
