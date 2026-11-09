from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunsplit

from models.product import PriceHistory, ProductSource
from services.product_resolver import canonical_brand
from services.structured_products import number


def source_identifiers(item):
    url = str(item.get("url") or "")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", parsed.path, re.I)
    filtered_id = re.search(r"item_id:(MLB\d+)", (query.get("pdp_filters") or [""])[0], re.I)
    ml_value = (query.get("wid") or query.get("item_id") or [filtered_id.group(1) if filtered_id else parsed.path if "/p/" not in parsed.path else ""])[0]
    ml_match = re.search(r"MLB-?(\d+)", ml_value, re.I)
    from services.shopee_public import item_ids
    shopee_shop, shopee_item = item_ids(url)
    return {
        "external_product_id": item.get("external_product_id") or item.get("product_id"),
        "asin": item.get("asin") or (asin_match.group(1).upper() if asin_match else None),
        "item_id": item.get("item_id") or (
            f"MLB{ml_match.group(1)}" if ml_match else shopee_item
        ),
        "shop_id": item.get("shop_id") or (
            shopee_shop or (query.get("shop_id") or [None])[0]
        ),
    }


def canonical_source_url(value):
    parsed = urlparse(str(value or "").strip())
    identity_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() in {
            "item_id", "wid", "pdp_filters", "offer_id", "oid", "seller", "seller_id", "shop_id", "m", "smid", "variant"
        }
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            urlencode(sorted(identity_query)),
            "",
        )
    )


def upsert_product_source(db, identity, store_key, item, match):
    seller = None if item.get("_seller_unknown") else str(item.get("seller") or item.get("store") or store_key)[:120]
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
            validation_status="discovered",
        )
        db.add(source)
    for field, value in identifiers.items():
        if value:
            setattr(source, field, value)
    for field in ("ean", "mpn", "model", "brand"):
        value = item.get(field)
        if value:
            setattr(source, field, canonical_brand(value) if field == "brand" else value)
    if not source.model and str(item.get("structured_sku") or "").upper() == identity.sku.upper():
        source.model = item["structured_sku"]
    source.match_confidence = match.confidence
    source.match_method = match.method
    listing_price = number(item.get("price"))
    if listing_price is not None:
        source.listing_price = float(listing_price)
        source.listing_name = item.get("name")
        source.listing_seen_at = datetime.utcnow()
    source.active = True
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
        ProductSource.last_attempted_at.asc(),
        ProductSource.id.asc(),
    ).all()


def mark_source_result(source, status, reason=None):
    now = datetime.utcnow()
    source.validation_status = status.lower()
    source.failure_reason = str(reason or "")[:500] or None
    source.last_attempted_at = now
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
        "listing_price": source.listing_price,
        "listing_seen_at": source.listing_seen_at,
        "active": source.active,
        "validation_status": source.validation_status,
        "failure_reason": source.failure_reason,
        "first_seen_at": source.first_seen_at,
        "last_attempted_at": source.last_attempted_at,
        "last_validated_at": source.last_validated_at,
        "last_collected_at": source.last_collected_at,
    }
