from types import SimpleNamespace

from models.product import CrawlerControl


FIXED_SETTINGS = {
    "external_discovery_fallback": True,
    "playwright_product_fallback": True,
    "playwright_shipping_fallback": True,
    "reuse_public_browser": True,
    "adaptive_parallelism": True,
    "discovery_stores_per_round": 3,
}


def _get_stored_control(db):
    control = db.query(CrawlerControl).filter(CrawlerControl.id == 1).first()
    if not control:
        control = CrawlerControl(id=1)
        db.add(control)
        db.commit()
        db.refresh(control)
    return control


def get_control(db):
    from services.extraction_resources import configure

    # Older databases and imported backups may still contain custom values.
    control = SimpleNamespace(**{**serialize_control(_get_stored_control(db)), **FIXED_SETTINGS})
    configure(control)
    return control


def serialize_control(control):
    return {
        "default_mode": control.default_mode,
        "external_discovery_fallback": control.external_discovery_fallback,
        "playwright_product_fallback": control.playwright_product_fallback,
        "playwright_shipping_fallback": control.playwright_shipping_fallback,
        "api_with_scraping": control.api_with_scraping,
        "concurrent_requests_per_domain": control.concurrent_requests_per_domain,
        "request_timeout_seconds": control.request_timeout_seconds,
        "precise_sku_workers": control.precise_sku_workers,
        "quick_sku_workers": control.quick_sku_workers,
        "quote_workers": control.quote_workers,
        "quick_timeout_seconds": control.quick_timeout_seconds,
        "discovery_stores_per_round": control.discovery_stores_per_round,
        "adaptive_parallelism": control.adaptive_parallelism,
        "hardware_acceleration": control.hardware_acceleration,
        "reuse_public_browser": control.reuse_public_browser,
        "browser_failure_cooldown_seconds": control.browser_failure_cooldown_seconds,
        "updated_at": control.updated_at,
    }


def update_control(db, payload):
    control = _get_stored_control(db)
    for field, value in {**payload.model_dump(), **FIXED_SETTINGS}.items():
        setattr(control, field, value)
    db.commit()
    db.refresh(control)
    from services.extraction_resources import configure
    configure(control)
    return control
