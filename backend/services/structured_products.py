"""Read public product metadata without mixing products, sellers or destinations."""
from __future__ import annotations

import math
import re
from datetime import date
from urllib.parse import urljoin, urlparse

import extruct
from services.shipping_selection import shipping_sort_key
from bs4 import BeautifulSoup

from services.product_resolver import normalize_text, resolve_candidate

LOGISTICS_FIELDS = (
    "shipping_cost", "shipping_method", "delivery_min_days", "delivery_max_days",
    "delivery_days_type",
    "handling_min_days", "handling_max_days", "destination_zip", "shipping_zip_code",
    "logistics_source", "logistics_captured_at",
)
UNAVAILABLE_STATES = {"outofstock", "out_of_stock", "out of stock", "discontinued", "soldout", "sold_out", "sem estoque", "esgotado", "indisponivel", "indisponível"}


def offer_available(offer):
    return str(offer.get("availability") or "").rsplit("/", 1)[-1].lower() not in UNAVAILABLE_STATES


def seller_scope_key(offer):
    from services.store_registry import find_target_store
    source = offer.get("source") or offer.get("store")
    storefront = find_target_store(source)
    seller = str(offer.get("store") or "").strip()
    return (storefront["key"] if storefront else normalize_text(source), normalize_text(seller) or seller.casefold())


def merge_offer_details(item, details):
    result = dict(item)
    if details.get("logistics_source") and any(details.get(field) is not None for field in (
        "shipping_cost", "delivery_min_days", "delivery_max_days",
    )):
        for field in LOGISTICS_FIELDS:
            result.pop(field, None)
    result.update(details)
    return result


def as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def number(value, allow_zero=False):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if re.search(r"-\s*\d", value):
            return None
        if len(re.findall(r"\d[\d.,]*", value)) != 1:
            return None
        value = re.sub(r"[^\d,.]", "", value)
        if "," in value:
            value = value.replace(".", "").replace(",", ".")
        elif value.count(".") > 1:
            value = value.replace(".", "")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (result > 0 or allow_zero and result == 0) else None


def types(node):
    return {str(value).rsplit("/", 1)[-1].rsplit("#", 1)[-1] for value in as_list(node.get("@type"))}


def party_name(value):
    if isinstance(value, list):
        return next((name for part in value if (name := party_name(part))), None)
    if isinstance(value, dict):
        return party_name(value.get("name"))
    return str(value).strip() if value is not None and str(value).strip() else None


def _compact(value):
    if isinstance(value, list):
        values = [_compact(item) for item in value]
        return values[0] if len(values) == 1 else values
    if not isinstance(value, dict):
        return value
    if "@value" in value:
        return value["@value"]
    return {
        re.sub(r"^https?://schema\.org[/#]", "", key): _compact(item)
        for key, item in value.items()
    }


def product_nodes(html, base_url=""):
    """Extruct handles JSON-LD, microdata and RDFa; references stay local to this page."""
    try:
        extracted = extruct.extract(
            html, base_url=base_url or None,
            syntaxes=["json-ld", "microdata", "rdfa"], uniform=True, errors="ignore",
        )
    except (ValueError, TypeError, UnicodeError):
        return []
    nodes = []
    by_id = {}

    def visit(value, depth=0):
        if depth > 25 or len(nodes) >= 3000:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            nodes.append(value)
            if isinstance(value.get("@id"), str):
                key = urljoin(base_url, value["@id"])
                by_id[key] = {**by_id.get(key, {}), **value}
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item, depth + 1)

    for syntax in ("json-ld", "microdata", "rdfa"):
        visit(_compact(extracted.get(syntax, [])))

    def expand(value, seen=frozenset(), depth=0):
        if depth > 20:
            return value
        if isinstance(value, list):
            return [expand(item, seen, depth + 1) for item in value]
        if not isinstance(value, dict):
            return value
        identifier = urljoin(base_url, value["@id"]) if isinstance(value.get("@id"), str) else None
        if identifier in seen:
            return {key: item for key, item in value.items() if key in {"@id", "@type", "name"}}
        if identifier:
            value = {**by_id.get(identifier, {}), **value}
            seen = seen | {identifier}
        return {key: expand(item, seen, depth + 1) for key, item in value.items()}

    return [expand(node) for node in nodes if types(node) & {"Product", "ProductGroup", "Offer", "AggregateOffer"}]


def product_candidate(product):
    return {
        "name": party_name(product.get("name")),
        "brand": party_name(product.get("brand")),
        "url": product.get("url") or "",
        "ean": product.get("gtin13") or product.get("gtin14") or product.get("gtin"),
        "mpn": product.get("mpn"),
        "model": party_name(product.get("model")),
        "structured_sku": product.get("sku"),
    }


def select_product(nodes, identity):
    candidates = []
    for node in nodes:
        if "Product" not in types(node):
            continue
        match = resolve_candidate(identity, product_candidate(node))
        if match.accepted:
            candidates.append((match.confidence, bool(node.get("offers")), node, match))
    selected = max(candidates, key=lambda item: item[:2], default=None)
    return (selected[2], selected[3]) if selected else ({}, None)


def product_offers(product, nodes=()):
    def expand(value):
        for offer in as_list(value):
            if not isinstance(offer, dict):
                continue
            if "AggregateOffer" in types(offer):
                yield from expand(offer.get("offers"))
            else:
                yield offer
    offers = list(expand(product.get("offers")))
    identifier = product.get("@id")
    for node in nodes:
        offered = node.get("itemOffered")
        if "Offer" in types(node) and isinstance(offered, dict) and identifier and offered.get("@id") == identifier:
            offers.append(node)
    unique = []
    for offer in offers:
        if offer not in unique:
            unique.append(offer)
    return unique


def offer_price(offer):
    if offer.get("validForMemberTier"):
        return None
    quantity = offer.get("eligibleQuantity") or {}
    if isinstance(quantity, dict):
        minimum = number(quantity.get("minValue", quantity.get("value")), allow_zero=True)
        maximum = number(quantity.get("maxValue", quantity.get("value")), allow_zero=True)
        if minimum is not None and minimum > 1 or maximum is not None and maximum < 1:
            return None
    if str(offer.get("priceCurrency") or "BRL").upper() != "BRL":
        return None
    if offer.get("priceValidUntil"):
        try:
            if date.fromisoformat(str(offer["priceValidUntil"])[:10]) < date.today():
                return None
        except ValueError:
            pass
    price = number(offer.get("price"))
    if price is not None:
        return price
    prices = []
    for specification in as_list(offer.get("priceSpecification")):
        if not isinstance(specification, dict):
            continue
        price_type = str(specification.get("priceType") or "").rsplit("/", 1)[-1].lower()
        if (
            str(specification.get("priceCurrency") or "BRL").upper() != "BRL"
            or price_type in {"strikethroughprice", "listprice", "msrp"}
            or specification.get("validForMemberTier")
        ):
            continue
        value = number(specification.get("price"))
        if value is not None:
            prices.append(value)
    return min(prices, default=None)


def select_offer(offers, seller=None, url=None):
    """An ambiguous marketplace offer must never borrow another seller's details."""
    if seller:
        matches = [offer for offer in offers if normalize_text(party_name(offer.get("seller"))) == normalize_text(seller)]
        if matches:
            offers = matches
        elif any(party_name(offer.get("seller")) for offer in offers):
            return None
    if len(offers) == 1:
        return offers[0]
    if url:
        matches = [offer for offer in offers if offer.get("url") and urljoin(url, offer["url"]) == url]
        if len(matches) == 1:
            return matches[0]
    return None


def payment_text(soup):
    selectors = (
        ".product-info-main", ".product__info-container", ".produto-principal",
        "#centerCol", '[data-testid="product-detail"]', "main .product-info", ".product-form",
    )
    scope = next((node for selector in selectors if (node := soup.select_one(selector))), soup.body or soup)
    clone = BeautifulSoup(str(scope), "html.parser")
    for node in clone.select(
        "script, style, nav, footer, .related, .upsells, .cross-sells, .recommendations, "
        "[id*='recommend'], [class*='recommend'], [data-testid*='recommend']"
    ):
        node.decompose()
    return clone.get_text(" ", strip=True)


def _days(value):
    if not isinstance(value, dict):
        return None, None
    unit = str(value.get("unitCode") or value.get("unitText") or "DAY").lower()
    factors = {"day": 1, "days": 1, "d": 1, "dia": 1, "dias": 1,
               "h": 1 / 24, "hur": 1 / 24, "hour": 1 / 24, "hours": 1 / 24,
               "min": 1 / 1440, "minute": 1 / 1440, "w": 7, "wee": 7, "week": 7}
    if unit not in factors:
        return None, None
    minimum = number(value.get("minValue", value.get("value")), allow_zero=True)
    maximum = number(value.get("maxValue", value.get("value")), allow_zero=True)
    if minimum is not None and maximum is not None and maximum < minimum:
        return None, None
    return tuple(item * factors[unit] if item is not None else None for item in (minimum, maximum))


def delivery_range(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        count = number(value, allow_zero=True)
        return (math.ceil(count), math.ceil(count)) if count is not None else (None, None)
    text = str(value or "").lower()
    text = text.replace("até", "ate")
    scalar = re.fullmatch(r"\s*(\d{1,3})(?:\s*(?:a|ate|-)\s*(\d{1,3}))?\s*", text)
    match = scalar or re.search(r"\b(\d{1,3})(?:\s*(?:a|ate|-)\s*(\d{1,3}))?\s*(?:dias?|horas?)\b", text)
    if not match:
        return None, None
    minimum, maximum = int(match.group(1)), int(match.group(2) or match.group(1))
    if maximum < minimum:
        return None, None
    factor = 1 / 24 if re.search(r"\bhoras?\b", text) else 1
    return math.ceil(minimum * factor), math.ceil(maximum * factor)


def _destination_allowed(destination, postal_code):
    if not isinstance(destination, dict):
        return not destination
    countries = [str(country).upper() for country in as_list(destination.get("addressCountry"))]
    if countries and not any(country in {"BR", "BRA", "BRASIL", "BRAZIL"} for country in countries):
        return False
    codes = as_list(destination.get("postalCode"))
    ranges = as_list(destination.get("postalCodeRange"))
    if not codes and not ranges:
        return not (destination.get("addressRegion") or destination.get("addressLocality"))
    if not postal_code:
        return False
    for code in codes:
        compact = re.sub(r"[^0-9*]", "", str(code))
        if compact and re.fullmatch(re.escape(compact).replace(r"\*", r"\d*"), postal_code):
            return True
    for interval in ranges:
        if not isinstance(interval, dict):
            continue
        begin = re.sub(r"\D", "", str(interval.get("postalCodeBegin") or ""))
        end = re.sub(r"\D", "", str(interval.get("postalCodeEnd") or ""))
        if len(begin) == len(end) == 8 and begin <= postal_code <= end:
            return True
    return False


def shipping_details(offer, postal_code=None):
    options = []
    for details in as_list(offer.get("shippingDetails")):
        if not isinstance(details, dict) or details.get("doesNotShip") is True or details.get("validForMemberTier"):
            continue
        destinations = as_list(details.get("shippingDestination"))
        if destinations and not any(_destination_allowed(destination, postal_code) for destination in destinations):
            continue
        rate = details.get("shippingRate")
        if isinstance(rate, dict):
            if str(rate.get("currency") or "BRL").upper() != "BRL":
                continue
            quantity = rate.get("eligibleQuantity") or {}
            if isinstance(quantity, dict) and (number(quantity.get("minValue"), allow_zero=True) or 0) > 1:
                continue
            threshold = rate.get("eligibleTransactionVolume") or details.get("eligibleTransactionVolume") or {}
            if isinstance(threshold, dict):
                minimum_price = number(threshold.get("minPrice", threshold.get("minValue")), allow_zero=True)
                current_price = offer_price(offer)
                if minimum_price is not None and (current_price is None or current_price < minimum_price):
                    continue
            rate = rate.get("value", rate.get("maxValue"))
        delivery = details.get("deliveryTime") or {}
        handling_min, handling_max = _days(delivery.get("handlingTime"))
        transit_min, transit_max = _days(delivery.get("transitTime"))
        def total_days(handling, transit):
            if transit is None or (delivery.get("handlingTime") and handling is None):
                return None
            return math.ceil((handling or 0) + transit)
        option = {
            "shipping_cost": number(rate, allow_zero=True),
            "delivery_min_days": total_days(handling_min, transit_min),
            "delivery_max_days": total_days(handling_max, transit_max),
            "handling_min_days": math.ceil(handling_min) if handling_min is not None else None,
            "handling_max_days": math.ceil(handling_max) if handling_max is not None else None,
            "delivery_days_type": "business" if delivery.get("businessDays") else "calendar",
            "logistics_source": "structured_product",
        }
        if any(option[field] is not None for field in ("shipping_cost", "delivery_min_days", "delivery_max_days")):
            options.append(option)
    selected = min(options, key=lambda item: shipping_sort_key(item['shipping_cost'], item['delivery_max_days']), default={})
    return {key: value for key, value in selected.items() if value is not None}
