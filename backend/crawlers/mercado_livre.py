from __future__ import annotations

import re
from datetime import datetime

from services.http_client import create_resilient_session
from services.offer_enrichment import enrich_offer
from services.product_resolver import resolve_candidate
from services.integration_settings import get_runtime_provider_settings
from crawlers.mercado_livre_public import collect_public_product, _item_id as public_item_id


API_ROOT = "https://api.mercadolibre.com"


def _item_id(source):
    selected = public_item_id(source.url)
    if selected:
        return selected
    if "/p/" in source.url:
        return None
    value = source.item_id or source.external_product_id or source.url
    match = re.search(r"MLB-?(\d+)", str(value or ""), re.I)
    return f"MLB{match.group(1)}" if match else None


def collect_mercado_livre(source, identity, postal_code=None, timeout=15):
    result = collect_public_product(source, identity, postal_code, timeout)
    if result["status"] in {"OK", "INVALID_MATCH", "OUT_OF_STOCK"}:
        return result
    settings = get_runtime_provider_settings("mercado_livre")
    if settings["enabled"] and settings["configured"] and _item_id(source):
        official = _collect_official_item(source, identity, postal_code, timeout)
        if official["status"] == "OK":
            return official
    return result


def _collect_official_item(source, identity, postal_code=None, timeout=15):
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
    price = float(payload.get("price") or 0) or None
    unavailable = payload.get('available_quantity') == 0 or payload.get('status') in {'paused', 'closed'}
    if (price is None or price <= 0) and not unavailable:
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
        "availability": "out_of_stock" if unavailable else "in_stock" if payload.get('available_quantity') is not None and payload['available_quantity'] > 0 else None,
        "image_url": next((picture.get("secure_url") for picture in payload.get("pictures") or [] if picture.get("secure_url")), None),
        "match_confidence": match.confidence,
        "match_method": match.method,
        "captured_at": datetime.utcnow().isoformat(),
        "discovery_source": "mercado_livre_api",
    }
    logistics = (payload.get('shipping') or {}).get('logistic_type')
    if logistics in {'fulfillment', 'self_service'}:
        item.update(fulfillment_type='2P' if logistics == 'fulfillment' else '3P',
                    fulfilled_by='Mercado Livre' if logistics == 'fulfillment' else seller,
                    fulfillment_evidence=f'Mercado Livre shipping.logistic_type={logistics}')
    if postal_code:
        item = enrich_offer(item, postal_code)
        if item.get("destination_zip") == postal_code:
            item["shipping_zip_code"] = postal_code
    print(f"[COLLECTION][MERCADO_LIVRE] {item_id} coletado")
    return {"status": "OK", "item": item, "match": match}
