from models.product import CrawlerControl


def get_control(db):
    control = db.query(CrawlerControl).filter(CrawlerControl.id == 1).first()
    if not control:
        control = CrawlerControl(id=1)
        db.add(control)
        db.commit()
        db.refresh(control)
    return control


def serialize_control(control):
    return {
        "default_mode": control.default_mode,
        "external_discovery_fallback": control.external_discovery_fallback,
        "playwright_shipping_fallback": control.playwright_shipping_fallback,
        "concurrent_requests_per_domain": control.concurrent_requests_per_domain,
        "request_timeout_seconds": control.request_timeout_seconds,
        "updated_at": control.updated_at,
    }


def update_control(db, payload):
    control = get_control(db)
    for field, value in payload.model_dump().items():
        setattr(control, field, value)
    db.commit()
    db.refresh(control)
    return control
