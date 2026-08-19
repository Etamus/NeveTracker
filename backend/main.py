import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models.product  # noqa: F401
from database import Base, engine
from models.product import KnownSku
from routes.products import product_router
from services.extraction_scheduler import start_extraction_scheduler
from services.scraper_service import KNOWN_SKUS

Base.metadata.create_all(bind=engine)


def _ensure_sqlite_columns():
    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as connection:
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
            "handling_min_days": "INTEGER",
            "handling_max_days": "INTEGER",
            "destination_zip": "VARCHAR",
            "availability": "VARCHAR",
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


_ensure_sqlite_columns()


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

        for sku in sorted(discovered - existing):
            connection.execute(KnownSku.__table__.insert().values(sku=sku))


_seed_known_skus()
app = FastAPI(title="Comparador de Precos")

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


@app.on_event("startup")
def start_background_services():
    start_extraction_scheduler()


@app.get("/")
async def root():
    return {"status": "API Online"}
