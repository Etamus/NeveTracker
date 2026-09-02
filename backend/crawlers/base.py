from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlparse

from scrapy.http import HtmlResponse

from services.http_client import create_resilient_session
from services.offer_enrichment import enrich_offer
from services.product_resolver import resolve_candidate
from services.store_registry import TARGET_STORES


def _number(value):
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    cleaned = re.sub(r"[^\d,.]", "", str(value or ""))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        result = float(cleaned)
    except ValueError:
        return None
    return result if result > 0 else None


def _jsonld_nodes(response):
    nodes = []

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            nodes.append(value)
            collect(value.get("@graph"))

    for raw in response.css('script[type="application/ld+json"]::text').getall():
        try:
            collect(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return nodes


def _typed_node(nodes, expected):
    for node in nodes:
        value = node.get("@type")
        values = value if isinstance(value, list) else [value]
        if expected in values:
            return node
    return {}


def _first(response, selectors):
    for selector in selectors:
        value = response.css(selector).get()
        if value:
            return re.sub(r"\s+", " ", value).strip()
    return None


def _offer(product):
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = next((item for item in offers if isinstance(item, dict)), {})
    return offers if isinstance(offers, dict) else {}


def _party_name(value):
    if isinstance(value, dict):
        return value.get("name")
    return str(value or "").strip() or None


def _availability(value):
    normalized = str(value or "").rsplit("/", 1)[-1].lower()
    if normalized in {"instock", "limitedavailability", "preorder"}:
        return "in_stock"
    if normalized in {"outofstock", "discontinued", "soldout"}:
        return "out_of_stock"
    return normalized or None


def _shipping(offer):
    details = offer.get("shippingDetails") or []
    if isinstance(details, dict):
        details = [details]
    selected = next((item for item in details if isinstance(item, dict)), {})
    rate = selected.get("shippingRate") or {}
    delivery = selected.get("deliveryTime") or {}
    handling = delivery.get("handlingTime") or {}
    transit = delivery.get("transitTime") or {}
    return {
        "shipping_cost": _number(rate.get("value")),
        "delivery_min_days": transit.get("minValue"),
        "delivery_max_days": transit.get("maxValue"),
        "handling_min_days": handling.get("minValue"),
        "handling_max_days": handling.get("maxValue"),
    }


def _installments(text, cash_price):
    match = re.search(r"(\d{1,2})\s*x\s*(?:de\s*)?R\$\s*([\d.,]+)", text or "", re.I)
    if not match:
        return {}
    count = int(match.group(1))
    value = _number(match.group(2))
    total = round(count * value, 2) if value else None
    if not total or (cash_price and total < cash_price):
        return {}
    return {
        "installment_count": count,
        "installment_value": value,
        "installment_price": total,
        "payment_difference": round(max(0, total - cash_price), 2) if cash_price else None,
    }


def parse_product_response(response, source, identity, profile):
    store_label = next(
        (store["label"] for store in TARGET_STORES if store["key"] == source.store),
        source.store,
    )
    nodes = _jsonld_nodes(response)
    product = _typed_node(nodes, "Product")
    offer = _offer(product)
    title = product.get("name") or _first(response, profile.get("title", ()))
    price = _number(
        offer.get("price")
        or offer.get("lowPrice")
        or _first(response, profile.get("price", ()))
    )
    seller = _party_name(offer.get("seller")) or _first(
        response, profile.get("seller", ())
    ) or source.seller or store_label
    brand = _party_name(product.get("brand")) or source.brand or identity.brand
    candidate = {
        "name": title,
        "url": response.url,
        "brand": brand,
        "ean": product.get("gtin13") or product.get("gtin"),
        "mpn": product.get("mpn"),
        "model": product.get("model"),
        "external_product_id": source.external_product_id,
        "asin": source.asin,
        "item_id": source.item_id,
    }
    match = resolve_candidate(identity, candidate)
    if not match.accepted:
        return {"status": "INVALID_MATCH", "reason": match.reason, "match": match}
    if not title or not price:
        return {"status": "PARSER_ERROR", "reason": "PDP sem titulo ou preco", "match": match}

    body_text = " ".join(response.css("body ::text").getall())
    image = product.get("image")
    if isinstance(image, list):
        image = next(iter(image), None)
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    result = {
        "name": str(title).strip(),
        "sku": identity.sku,
        "ean": candidate["ean"],
        "mpn": candidate["mpn"],
        "model": candidate["model"],
        "brand": brand or store_label,
        "category": identity.name or identity.sku,
        "store": seller,
        "seller": seller,
        "source": store_label,
        "url": response.url,
        "image_url": image,
        "price": price,
        "cash_price": price,
        "availability": _availability(offer.get("availability")),
        "match_confidence": match.confidence,
        "match_method": match.method,
        "captured_at": datetime.utcnow().isoformat(),
        "discovery_source": "product_sources",
        **{key: value for key, value in _installments(body_text, price).items() if value is not None},
        **{key: value for key, value in _shipping(offer).items() if value is not None},
    }
    return {"status": "OK", "item": result, "match": match}


def collect_http(source, identity, postal_code, profile, timeout=15):
    domain = urlparse(source.url).netloc
    session = create_resilient_session(pool_size=2)
    try:
        response = session.get(
            source.url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=(5, timeout),
        )
    except Exception as exc:
        return {"status": "TIMEOUT", "reason": str(exc)}
    finally:
        session.close()
    if response.status_code in {404, 410}:
        return {"status": "NOT_FOUND", "reason": f"HTTP {response.status_code}"}
    if response.status_code in {401, 403, 429}:
        return {"status": "BLOCKED", "reason": f"HTTP {response.status_code}"}
    if response.status_code != 200 or not response.text:
        return {"status": "PARSER_ERROR", "reason": f"HTTP {response.status_code}"}

    scrapy_response = HtmlResponse(
        url=response.url,
        status=response.status_code,
        body=response.content,
        encoding=response.encoding or "utf-8",
    )
    parsed = parse_product_response(scrapy_response, source, identity, profile)
    if parsed.get("status") != "OK":
        return parsed
    item = parsed["item"]
    if postal_code:
        item = enrich_offer(item, postal_code)
        item["shipping_zip_code"] = postal_code
    print(f"[COLLECTION][{source.store.upper()}] {domain} coletado para {identity.sku}")
    return {**parsed, "item": item}
