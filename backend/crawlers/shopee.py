from __future__ import annotations

from datetime import datetime

from services.http_client import create_resilient_session
from services.product_resolver import resolve_candidate


API_ROOT = "https://shopee.com.br/api/v4"


def _price(value):
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return None
    return result / 100000 if result > 100000 else result or None


def collect_shopee(source, identity, postal_code=None, timeout=15):
    if not source.item_id or not source.shop_id:
        return {"status": "PARSER_ERROR", "reason": "item_id/shop_id ausente"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": source.url,
        "x-api-source": "pc",
    }
    session = create_resilient_session(pool_size=2)
    try:
        response = session.get(
            f"{API_ROOT}/item/get",
            params={"itemid": source.item_id, "shopid": source.shop_id},
            headers=headers,
            timeout=(5, timeout),
        )
        if response.status_code == 404:
            return {"status": "NOT_FOUND", "reason": "Item removido"}
        if response.status_code != 200:
            return {"status": "BLOCKED", "reason": f"HTTP {response.status_code}"}
        payload = response.json().get("data") or {}
        shop_response = session.get(
            f"{API_ROOT}/shop/get_shop_base",
            params={"shopid": source.shop_id},
            headers=headers,
            timeout=(5, timeout),
        )
        shop = shop_response.json().get("data") or {} if shop_response.status_code == 200 else {}
    except Exception as exc:
        return {"status": "TIMEOUT", "reason": str(exc)}
    finally:
        session.close()

    seller = shop.get("name") or shop.get("account", {}).get("username") or source.seller
    candidate = {
        "name": payload.get("name"),
        "url": source.url,
        "brand": (payload.get("brand") or {}).get("name") if isinstance(payload.get("brand"), dict) else payload.get("brand"),
        "model": next((attribute.get("value") for attribute in payload.get("attributes") or [] if str(attribute.get("name") or "").lower() in {"modelo", "model"}), None),
        "item_id": str(source.item_id),
    }
    match = resolve_candidate(identity, candidate)
    if not match.accepted:
        return {"status": "INVALID_MATCH", "reason": match.reason, "match": match}
    price = _price(payload.get("price") or payload.get("price_min"))
    if not price:
        return {"status": "PARSER_ERROR", "reason": "Preco ausente", "match": match}
    installment = payload.get("installment_plans") or []
    plan = next((value for value in installment if isinstance(value, dict)), {})
    count = plan.get("installment_count") or plan.get("tenure")
    installment_value = _price(plan.get("installment_amount"))
    installment_total = round(count * installment_value, 2) if count and installment_value else None
    item = {
        **candidate,
        "sku": identity.sku,
        "store": seller or "Shopee",
        "seller": seller or "Shopee",
        "source": "Shopee",
        "category": identity.sku,
        "price": price,
        "cash_price": price,
        "installment_count": count,
        "installment_value": installment_value,
        "installment_price": installment_total,
        "payment_difference": round(max(0, installment_total - price), 2) if installment_total else None,
        "availability": "in_stock" if (payload.get("stock") or 0) > 0 else "out_of_stock",
        "image_url": f"https://down-br.img.susercontent.com/file/{payload.get('image')}" if payload.get("image") else None,
        "match_confidence": match.confidence,
        "match_method": match.method,
        "captured_at": datetime.utcnow().isoformat(),
        "discovery_source": "shopee_internal_api",
        "shipping_zip_code": postal_code,
    }
    print(f"[COLLECTION][SHOPEE] {source.shop_id}/{source.item_id} coletado; seller={seller}")
    return {"status": "OK", "item": item, "match": match}
