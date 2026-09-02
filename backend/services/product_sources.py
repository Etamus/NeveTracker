from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunsplit

from models.product import PriceHistory, ProductSource


def source_identifiers(item):
    url = str(item.get("url") or "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", parsed.path, re.I)
    ml_match = re.search(r"MLB-?(\d+)", url, re.I)
    shopee_match = re.search(r"/product/(\d+)/(\d+)", parsed.path)
    return {
        "external_product_id": item.get("external_product_id") or item.get("product_id"),
        "asin": item.get("asin") or (asin_match.group(1).upper() if asin_match else None),
        "item_id": item.get("item_id") or (
            f"MLB{ml_match.group(1)}" if ml_match else shopee_match.group(2) if shopee_match else None
        ),
        "shop_id": item.get("shop_id") or (
            shopee_match.group(1) if shopee_match else (query.get("shop_id") or [None])[0]
        ),
    }


def canonical_source_url(value):
    parsed = urlparse(str(value or "").strip())
    identity_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() in {
            "item_id", "offer_id", "oid", "seller", "seller_id", "shop_id", "m", "smid"
        }
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            urlencode(identity_query),
            "",
        )
    )


def upsert_product_source(db, identity, store_key, item, match):
    seller = str(item.get("seller") or item.get("store") or store_key)[:120]
    url = canonical_source_url(item.get("url"))[:2000]
    if not url:
        return None
    source = (
        db.query(ProductSource)
        .filter(
            ProductSource.sku == identity.sku,
            ProductSource.store == store_key,
            ProductSource.seller == seller,
            ProductSource.url == url,
        )
        .first()
    )
    identifiers = source_identifiers(item)
    if not source:
        latest_price = (
            db.query(PriceHistory)
            .filter(
                PriceHistory.url == url,
                PriceHistory.store == seller,
            )
            .order_by(PriceHistory.timestamp.desc())
            .first()
        )
        source = ProductSource(
            product_id=latest_price.product_id if latest_price else None,
            sku=identity.sku,
            store=store_key,
            seller=seller,
            url=url,
            first_seen_at=datetime.utcnow(),
        )
        db.add(source)
    source.external_product_id = identifiers["external_product_id"]
    source.asin = identifiers["asin"]
    source.item_id = identifiers["item_id"]
    source.shop_id = identifiers["shop_id"]
    source.ean = item.get("ean")
    source.mpn = item.get("mpn")
    source.model = item.get("model")
    source.brand = item.get("brand")
    source.match_confidence = match.confidence
    source.match_method = match.method
    source.active = True
    source.validation_status = "valid"
    source.failure_reason = None
    source.last_validated_at = datetime.utcnow()
    source.invalidated_at = None
    db.flush()
    return source


def active_sources(db, sku, store_keys=None):
    query = db.query(ProductSource).filter(
        ProductSource.sku == sku,
        ProductSource.active.is_(True),
    )
    if store_keys:
        query = query.filter(ProductSource.store.in_(store_keys))
    return query.order_by(
        ProductSource.store.asc(),
        ProductSource.match_confidence.desc(),
        ProductSource.last_validated_at.desc(),
    ).all()


def mark_source_result(source, status, reason=None):
    now = datetime.utcnow()
    source.validation_status = status.lower()
    source.failure_reason = str(reason or "")[:500] or None
    if status == "OK":
        source.active = True
        source.last_validated_at = now
        source.last_collected_at = now
    elif status in {"NOT_FOUND", "INVALID_MATCH"}:
        source.active = False
        source.invalidated_at = now
        source.last_validated_at = now


def serialize_source(source):
    return {
        "id": source.id,
        "sku": source.sku,
        "store": source.store,
        "seller": source.seller,
        "url": source.url,
        "external_product_id": source.external_product_id,
        "asin": source.asin,
        "item_id": source.item_id,
        "shop_id": source.shop_id,
        "ean": source.ean,
        "mpn": source.mpn,
        "model": source.model,
        "brand": source.brand,
        "match_confidence": source.match_confidence,
        "match_method": source.match_method,
        "active": source.active,
        "validation_status": source.validation_status,
        "failure_reason": source.failure_reason,
        "first_seen_at": source.first_seen_at,
        "last_validated_at": source.last_validated_at,
        "last_collected_at": source.last_collected_at,
    }
