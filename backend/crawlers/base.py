from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from scrapy.http import HtmlResponse
from bs4 import BeautifulSoup

from services.http_client import create_resilient_session, recover_public_html
from services.offer_enrichment import enrich_offer
from services.product_resolver import normalize_text, resolve_candidate
from services.store_registry import TARGET_STORES
from services.structured_products import (
    number, offer_price, party_name, payment_text, product_candidate,
    product_nodes, product_offers, select_product, shipping_details, types,
)


def _number(value, allow_zero=False):
    return number(value, allow_zero=allow_zero)


def _jsonld_nodes(response):
    return product_nodes(response.text, response.url)


def _typed_node(nodes, expected):
    for node in nodes:
        if expected in types(node):
            return node
    return {}


def _first(response, selectors):
    for selector in selectors:
        if selector.endswith("::text"):
            nodes = response.css(selector[:-6])
            value = " ".join(nodes[0].xpath(".//text()").getall()) if nodes else None
        else:
            value = response.css(selector).get()
        if value and (normalized := re.sub(r"\s+", " ", value).strip()):
            return normalized
    return None


def _offers(product):
    return product_offers(product)


def _offer_price(offer):
    return offer_price(offer)


def _party_name(value):
    return party_name(value)


def _availability(value):
    normalized = str(value or "").rsplit("/", 1)[-1].lower()
    words = normalize_text(normalized)
    if words in {"sem estoque", "esgotado", "produto esgotado", "indisponivel", "nao disponivel", "fora de estoque", "out of stock"}:
        return "out_of_stock"
    if words in {"em estoque", "disponivel"}:
        return "in_stock"
    if normalized in {"instock", "limitedavailability"}:
        return "in_stock"
    if normalized == "preorder":
        return "preorder"
    if normalized in {"outofstock", "discontinued", "soldout"}:
        return "out_of_stock"
    return normalized or None


def _shipping(offer, postal_code=None):
    return shipping_details(offer, postal_code)


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


def parse_product_response(response, source, identity, profile, postal_code=None):
    page_title = _first(response, ("title::text",)) or ""
    if re.search(r"captcha|robot check|access denied|acesso negado|attention required", page_title, re.I):
        return {"status": "BLOCKED", "reason": "PDP retornou verificacao em vez do produto"}
    store_label = next(
        (store["label"] for store in TARGET_STORES if store["key"] == source.store),
        source.store,
    )
    nodes = _jsonld_nodes(response)
    product, structured_match = select_product(nodes, identity)
    from services.manufacturer_pages import selected_product
    hydrated = selected_product(response, source, identity, store_label)
    if hydrated:
        product = hydrated
        structured_match = resolve_candidate(identity, hydrated)
    offers = product_offers(product, nodes)
    if source.store == 'magalu':
        from services.magalu_public import embedded_seller_offers
        offers = embedded_seller_offers(response, identity) or offers
    aggregate = product.get('offers') or {}
    aggregate_unavailable = isinstance(aggregate, dict) and _availability(aggregate.get('availability')) == 'out_of_stock'
    if product.get("offers") and not offers and not aggregate_unavailable:
        return {"status": "PARSER_ERROR", "reason": "PDP sem oferta individual"}
    title = party_name(product.get("name")) or _first(response, profile.get("title", ()))
    if not product and any("Product" in types(node) for node in nodes):
        title_match = resolve_candidate(identity, {"name": title})
        if not title_match.accepted:
            return {"status": "INVALID_MATCH", "reason": "Metadados e titulo nao confirmam o produto pesquisado", "match": title_match}
    page_price = _first(response, profile.get("price", ())) if not offers else None
    page_seller = _first(response, profile.get("seller", ()))
    page_availability = _availability(_first(response, profile.get("availability", ()))) if len(offers) <= 1 else None
    if aggregate_unavailable:
        page_availability = 'out_of_stock'
    brand = _party_name(product.get("brand")) or source.brand or identity.brand
    candidate = {
        **product_candidate(product),
        "name": title,
        "url": product.get("url") or (response.url if not product else ""),
        "brand": brand,
        "ean": product.get("gtin13") or product.get("gtin"),
        "mpn": product.get("mpn"),
        "model": product.get("model"),
        "external_product_id": source.external_product_id,
        "asin": source.asin,
        "item_id": source.item_id,
    }
    match = structured_match or resolve_candidate(identity, candidate)
    if not match.accepted:
        return {"status": "INVALID_MATCH", "reason": match.reason, "match": match}
    if not title:
        return {"status": "PARSER_ERROR", "reason": "PDP sem titulo ou preco", "match": match}

    body_text = payment_text(BeautifulSoup(response.text, "html.parser"))
    image = product.get("image")
    if isinstance(image, list):
        image = next(iter(image), None)
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    base = {
        "name": str(title).strip(),
        "sku": identity.sku,
        "ean": candidate["ean"],
        "mpn": candidate["mpn"],
        "model": candidate["model"],
        "structured_sku": candidate.get("structured_sku"),
        "brand": brand or store_label,
        "category": identity.name or identity.sku,
        "source": store_label,
        "image_url": urljoin(response.url, str(image)) if image else None,
        "match_confidence": match.confidence,
        "match_method": match.method,
        "captured_at": datetime.utcnow().isoformat(),
        "discovery_source": "product_sources",
    }
    items = []
    seen = set()
    for offer in offers or [{}]:
        if not offers:
            currency = response.css('meta[property="product:price:currency"]::attr(content)').get()
            if currency and currency.upper() != "BRL":
                continue
        price = _offer_price(offer) if offers else _number(page_price)
        availability = _availability(offer.get('availability')) or page_availability
        if price is None and availability != 'out_of_stock':
            continue
        explicit_seller = _party_name(offer.get("seller"))
        if len(offers) > 1 and not explicit_seller:
            continue
        seller = explicit_seller or page_seller or source.seller or store_label
        offer_url = urljoin(response.url, str(offer.get("url") or response.url))
        if source.store == 'electrolux' and offer.get('sku'):
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(offer_url)
            params = dict(parse_qsl(parts.query))
            params['skuId'] = str(offer['sku'])
            offer_url = urlunsplit((*parts[:3], urlencode(params), ''))
        if urlparse(offer_url).hostname != urlparse(response.url).hostname:
            continue
        key = (offer_url, seller)
        if key in seen:
            continue
        seen.add(key)
        item = {
            **base,
            "store": seller,
            "seller": seller,
            "url": offer_url,
            "price": price,
            "cash_price": price,
            "availability": availability,
            **_shipping(offer, postal_code),
        }
        if len(offers) <= 1:
            item.update({
                key: value for key, value in _installments(body_text, price).items()
                if value is not None
            })
        item.update({key: offer[key] for key in ('cash_price', 'installment_price', 'payment_difference')
                     if offer.get(key) is not None})
        if len(offers) <= 1:
            from services.fulfillment import fulfillment_details
            item.update(fulfillment_details(response.text, store_label, seller))
        items.append(item)
    if not items:
        if page_availability == "out_of_stock" or offers and all(_availability(offer.get("availability")) == "out_of_stock" for offer in offers):
            return {"status": "OUT_OF_STOCK", "reason": "Produto explicitamente sem estoque e sem preco atual", "match": match}
        return {"status": "PARSER_ERROR", "reason": "PDP sem preco de oferta individual", "match": match}
    if len(items) == 1 and len(response.text) <= 4_000_000:
        items[0]['_page_context'] = {'offer_url': items[0]['url'], 'page_url': response.url, 'html': response.text}
    return {"status": "OK", "item": items[0], "items": items, "match": match}


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
    response = recover_public_html(response, source.url, timeout)
    if response.status_code in {401, 403, 429}:
        return {"status": "BLOCKED", "reason": f"HTTP {response.status_code}", "_retry_after": response.headers.get("Retry-After")}
    if response.status_code != 200 or not response.text:
        return {"status": "PARSER_ERROR", "reason": f"HTTP {response.status_code}"}

    scrapy_response = HtmlResponse(
        url=response.url,
        status=response.status_code,
        body=response.content,
        encoding=response.encoding or "utf-8",
    )
    parsed = parse_product_response(scrapy_response, source, identity, profile)
    from services.magalu_public import is_public_storefront
    if source.store == "magalu" and is_public_storefront(response.url):
        for item in parsed.get("items", []):
            item["discovery_source"] = "magalu_public_storefront"
    if parsed.get("status") != "OK":
        return parsed
    items = parsed["items"]
    if postal_code:
        items = [enrich_offer(item, postal_code) for item in items]
        for item in items:
            if item.get("destination_zip") == postal_code:
                item["shipping_zip_code"] = postal_code
    print(f"[COLLECTION][{source.store.upper()}] {domain} coletado para {identity.sku}")
    return {**parsed, "item": items[0], "items": items}
