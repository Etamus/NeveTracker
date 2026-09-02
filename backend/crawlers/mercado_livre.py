from __future__ import annotations

import re
from datetime import datetime

from services.http_client import create_resilient_session
from services.offer_enrichment import enrich_offer
from services.product_resolver import resolve_candidate


API_ROOT = "https://api.mercadolibre.com"


def _item_id(source):
    value = source.item_id or source.external_product_id or source.url
    match = re.search(r"MLB-?(\d+)", str(value or ""), re.I)
    return f"MLB{match.group(1)}" if match else None


def collect_mercado_livre(source, identity, postal_code=None, timeout=15):
    item_id = _item_id(source)
    if not item_id:
        return {"status": "PARSER_ERROR", "reason": "item_id ausente"}
    session = create_resilient_session(pool_size=2)
    try:
        response = session.get(f"{API_ROOT}/items/{item_id}", timeout=(5, timeout))
        if response.status_code == 404:
            return {"status": "NOT_FOUND", "reason": "Item removido"}
        if response.status_code != 200:
            return {"status": "BLOCKED", "reason": f"HTTP {response.status_code}"}
        payload = response.json()
        seller_id = payload.get("seller_id")
        seller = source.seller
        if seller_id:
            seller_response = session.get(
                f"{API_ROOT}/users/{seller_id}", timeout=(5, timeout)
            )
            if seller_response.status_code == 200:
                seller = seller_response.json().get("nickname") or seller
    except Exception as exc:
        return {"status": "TIMEOUT", "reason": str(exc)}
    finally:
        session.close()

    attributes = {
        item.get("id"): item.get("value_name")
        for item in payload.get("attributes") or []
    }
    candidate = {
        "name": payload.get("title"),
        "url": payload.get("permalink") or source.url,
        "brand": attributes.get("BRAND") or source.brand,
        "ean": attributes.get("GTIN") or attributes.get("EAN"),
        "mpn": attributes.get("MPN"),
        "model": attributes.get("MODEL"),
        "item_id": item_id,
    }
    match = resolve_candidate(identity, candidate)
    if not match.accepted:
        return {"status": "INVALID_MATCH", "reason": match.reason, "match": match}
    price = float(payload.get("price") or 0)
    if price <= 0:
        return {"status": "PARSER_ERROR", "reason": "Preco ausente", "match": match}
    item = {
        **candidate,
        "sku": identity.sku,
        "store": seller or "Mercado Livre",
        "seller": seller or "Mercado Livre",
        "source": "Mercado Livre",
        "category": payload.get("category_id") or identity.sku,
        "price": price,
        "cash_price": price,
        "availability": "in_stock" if (payload.get("available_quantity") or 0) > 0 else "out_of_stock",
        "image_url": next((picture.get("secure_url") for picture in payload.get("pictures") or [] if picture.get("secure_url")), None),
        "match_confidence": match.confidence,
        "match_method": match.method,
        "captured_at": datetime.utcnow().isoformat(),
        "discovery_source": "mercado_livre_api",
    }
    if postal_code:
        item = enrich_offer(item, postal_code)
        item["shipping_zip_code"] = postal_code
    print(f"[COLLECTION][MERCADO_LIVRE] {item_id} coletado")
    return {"status": "OK", "item": item, "match": match}
