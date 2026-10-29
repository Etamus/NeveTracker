import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import BoundedSemaphore
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from models.product import KnownSku, PriceHistory, Product, ProductSource
from scrapers.comparison import (
    scrape_comparison_offers,
    scrape_target_catalog,
)
from services.store_registry import STORE_BY_LABEL, TARGET_STORES, find_target_store
from services.offer_enrichment import enrich_offers, normalize_postal_code
from services.bing_shopping import search_bing_shopping
from services.external_market_providers import run_external_market_provider
from services.integration_settings import (
    get_runtime_provider_settings,
    get_search_provider_names,
    should_run_provider,
)
from services.product_resolver import ProductIdentity, canonical_brand, resolve_candidate
from services.structured_products import number, offer_available, seller_scope_key

STORE_ITEM_LIMIT = 100
CATALOG_ROW_LIMIT = 30
DETAIL_ENRICHMENT_LIMIT = 160

try:
    MAX_PARALLEL_SKUS = int(os.getenv("NEVETRACKER_MAX_PARALLEL_SKUS", "2"))
except (TypeError, ValueError):
    MAX_PARALLEL_SKUS = 2
MAX_PARALLEL_SKUS = min(3, max(1, MAX_PARALLEL_SKUS))
SCRAPE_SLOTS = BoundedSemaphore(MAX_PARALLEL_SKUS)

KNOWN_SKUS = {
    "W10866791",
    "W10624070",
    "W10510889",
    "W10866790",
    "W11298039",
    "W11559186",
    "W11393065",
    "W10347201",
    "W11387305",
    "W10515645",
    "W10581058",
    "W11100357",
    "W10866789",
    "W10907728",
    "W10894262",
    "W11360276",
    "W10287527",
    "W10287547",
    "W10585027",
    "326075868",
    "W11132264",
    "W11393157",
    "W10833267",
    "W11619403",
    "W11661613",
    "W10320833",
    "326043145",
    "W10351342",
    "W10200101",
    "W11300695",
    "W10420716",
    "W10638541",
    "W11100356",
    "W10758992",
    "W11375406",
    "W11619317",
    "W10455522",
    "W10169457",
    "W10915770",
    "W10476183",
    "326075876",
    "W10657521",
    "W10638550",
    "326073242",
    "W11245249",
    "W10544738",
    "W10686098",
    "W11109516",
    "W10577330",
    "W10705755",
    "W10544750",
    "W10644162",
    "W10911678",
    "W11228626",
    "326065620",
    "W10544749",
    "W11108178",
    "W11693441",
    "326047477",
    "W10911706",
    "W10390580",
    "W10516861",
    "W11195523",
    "W10238102",
    "W11185123",
    "W10212150",
    "W10336914",
    "W11109236",
}

ELECTRONICS_TERMS = {
    "iphone",
    "celular",
    "smartphone",
    "notebook",
    "macbook",
    "ipad",
    "tablet",
    "playstation",
    "ps5",
    "xbox",
    "nintendo",
    "tv",
    "televisao",
    "monitor",
    "camera",
}

APPLIANCE_TERMS = {
    "ar",
    "condicionado",
    "cooktop",
    "coifa",
    "fogao",
    "freezer",
    "geladeira",
    "lava",
    "lavadora",
    "maquina",
    "microondas",
    "refrigerador",
}

ACCESSORY_TERMS = {
    "adaptador",
    "adesivo",
    "borracha",
    "bolsa",
    "cabo",
    "capa",
    "carregador",
    "case",
    "capacitor",
    "componentes",
    "controle",
    "estojo",
    "filtro",
    "fonte",
    "gaveta",
    "grade",
    "kit",
    "lampada",
    "mangueira",
    "motor",
    "painel",
    "pelicula",
    "peca",
    "pecas",
    "placa",
    "prateleira",
    "refil",
    "resistencia",
    "sensor",
    "suporte",
    "tampa",
    "termostato",
    "ventilador",
    "ventoinha",
    "vidro",
}

PRODUCT_STOP_WORDS = {
    "a",
    "apple",
    "as",
    "barata",
    "barato",
    "br",
    "celular",
    "com",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "frete",
    "em",
    "gratis",
    "na",
    "no",
    "o",
    "os",
    "para",
    "por",
    "pre",
    "preco",
    "precos",
    "preos",
    "excelente",
    "excelentes",
    "promocao",
}

STORE_WORDS = {
    "amazon",
    "click",
    "comclick",
    "dufrio",
    "friolar",
    "gold",
    "livre",
    "loja",
    "magalu",
    "magazine",
    "mercado",
    "merlin",
    "mg",
    "mota",
    "parts",
    "refrigeracao",
    "leroy",
    "service",
    "shopee",
}

KNOWN_BRANDS = {
    "apple",
    "brastemp",
    "consul",
    "continental",
    "electrolux",
    "elgin",
    "fast",
    "fischer",
    "hisense",
    "hq",
    "lg",
    "midea",
    "panasonic",
    "philco",
    "samsung",
    "shop",
}

VARIANT_SCRAPERS = {
    "scrape_amazon",
    "scrape_leroy",
    "scrape_mercadoLivre",
    "scrape_shopee",
    "search_mercado_livre",
}

OFFER_DETAIL_FIELDS = (
    "discovery_source",
    "cash_price",
    "installment_price",
    "installment_count",
    "installment_value",
    "payment_difference",
    "shipping_cost",
    "shipping_method",
    "delivery_min_days",
    "delivery_max_days",
    "delivery_days_type",
    "handling_min_days",
    "handling_max_days",
    "destination_zip",
    "availability",
    "fulfillment_type", "fulfilled_by", "fulfillment_evidence",
    "logistics_source",
    "logistics_captured_at",
    "is_cached",
)


def _normalize_text(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text.lower()


def _tokens(value):
    return {
        "brastemp" if token == "brastem" else token
        for token in re.findall(r"[a-z0-9]+", _normalize_text(value))
    }


def _extract_skus(value):
    normalized = _normalize_text(value).upper()
    skus = re.findall(r"\bW\d{6,}\b|\bMD-RT\d+[A-Z]+\d*\b|\b(?:BWF|BRO|CRD|CRM|BRM|BWJ|LEE|IM|IF|CIX)\d+[A-Z]*\b|\b\d{8,}\b", normalized)
    return list(dict.fromkeys(skus))


def _primary_sku(value):
    skus = _extract_skus(value)
    return skus[0] if skus else None


def _item_sku(item, fallback_query=None):
    for value in (
        item.get("sku"),
        item.get("name"),
        item.get("url"),
        fallback_query,
    ):
        sku = _primary_sku(value or "")
        if sku:
            return sku
    return None


def _matches_query_sku(item, sku):
    if not sku:
        return True
    return sku == _item_sku(item)


def _query_variants(product_query):
    normalized = _normalize_text(product_query)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    token_list = re.findall(r"[a-z0-9]+", normalized)

    variants = []
    seen_variants = set()
    for variant in (
        product_query.strip(),
        normalized,
        " ".join(token_list),
        " ".join(reversed(token_list)) if 1 < len(token_list) <= 4 else "",
    ):
        variant = variant.strip()
        variant_key = _normalize_text(variant)
        if variant and variant_key not in seen_variants:
            variants.append(variant)
            seen_variants.add(variant_key)

    return variants


def _is_accessory_mismatch(item, product_query):
    query_tokens = _tokens(product_query)
    if not query_tokens.intersection(ELECTRONICS_TERMS | APPLIANCE_TERMS):
        return False

    name_tokens = _tokens(item["name"])
    return any(
        term in name_tokens and term not in query_tokens for term in ACCESSORY_TERMS
    )


def _minimum_expected_price(product_query):
    query_tokens = _tokens(product_query)

    if query_tokens.intersection(ACCESSORY_TERMS):
        return 1

    if query_tokens.intersection(
        {
            "condicionado",
            "cooktop",
            "fogao",
            "freezer",
            "geladeira",
            "lavadora",
            "maquina",
            "microondas",
            "refrigerador",
        }
    ):
        return 500

    if query_tokens.intersection({"notebook", "macbook"}):
        return 700

    if query_tokens.intersection({"celular", "iphone", "smartphone", "tablet", "tv"}):
        return 250

    return 1


def _is_generic_listing_name(name):
    normalized = _normalize_text(name)
    generic_phrases = (
        "barata",
        "barato",
        "com precos",
        "compare",
        "comprar",
        "em oferta",
        "melhor preco",
        "melhores precos",
        "ofertas",
        "precos excelentes",
        "promocao",
    )
    return any(phrase in normalized for phrase in generic_phrases)


def _sort_key(item, product_query):
    query_tokens = _tokens(product_query)
    name_tokens = _tokens(item["name"])
    missing_terms = len(query_tokens - name_tokens)
    phrase_miss = int(_normalize_text(product_query) not in _normalize_text(item["name"]))
    return (not offer_available(item), missing_terms, phrase_miss, _sortable_price(item))


def _sortable_price(item):
    value = item.get('current_price', item.get('price'))
    return float(value) if value is not None and offer_available(item) else float('inf')


def _normalize_result(item, product_query, store_label=None):
    name = (item.get("name") or "").strip()
    url = (item.get("url") or "").strip()
    store = store_label or (item.get("store") or "").strip() or "Loja"
    seller = (item.get("seller") or item.get("store") or store).strip()
    source = (item.get("source") or store_label or store).strip()
    query_sku = _primary_sku(product_query)
    sku = _item_sku(item)

    price = number(item.get("price"))

    if not name or not url or (price is None and offer_available(item)):
        return None

    if re.search(r'\b(?:garantia estendida|seguro garantia|servico de instalacao)\b', _normalize_text(name)):
        return None

    if query_sku and sku != query_sku:
        return None
    if query_sku and not resolve_candidate(ProductIdentity(sku=query_sku, model=query_sku), item).accepted:
        return None

    if not query_sku:
        query_tokens = _product_tokens(product_query)
        name_tokens = _product_tokens(name)
        shared_tokens = query_tokens.intersection(name_tokens)
        required_numeric = _numeric_tokens(query_tokens)
        required_brands = _brand_tokens(query_tokens)

        if query_tokens and not shared_tokens:
            return None
        if required_numeric and not required_numeric.issubset(name_tokens):
            return None
        if required_brands and not required_brands.issubset(name_tokens):
            return None
        if len(query_tokens) >= 3 and len(shared_tokens) / len(query_tokens) < 0.5:
            return None

    if price is not None and price < _minimum_expected_price(product_query):
        return None

    if _is_generic_listing_name(name):
        return None

    normalized = {
        "name": name[:255],
        "price": price,
        "store": seller[:100],
        "source": source[:100],
        "url": url[:1000],
        "brand": str(canonical_brand(item.get("brand") or store or "Oferta"))[:100],
        "category": (item.get("category") or product_query)[:100],
        "image_url": item.get("image_url"),
        "sku": sku,
    }
    normalized.update(
        {
            field: item.get(field)
            for field in OFFER_DETAIL_FIELDS
            if item.get(field) is not None
        }
    )
    return normalized


def _offer_url_identity(url):
    parsed_url = urlsplit(url)
    identity_params = [
        (key, value)
        for key, value in parse_qsl(parsed_url.query, keep_blank_values=False)
        if key.lower()
        in {
            "item_id", "wid", "pdp_filters", "offer_id", "oid", "seller", "seller_id", "shop_id", "m", "smid", "variant", "skuid", "idsku"
        }
    ]
    return urlunsplit(
        (
            parsed_url.scheme.lower(),
            parsed_url.netloc.lower(),
            parsed_url.path.rstrip("/"),
            urlencode(sorted(identity_params)),
            "",
        )
    )


def _offer_identity(item):
    return (
        _offer_url_identity(item["url"]),
        _normalize_text(item.get("source") or item["store"]),
        _normalize_text(item["store"]),
    )


def _deduplicate(items):
    unique = []
    seen_urls = set()
    seen_listings = set()

    for item in items:
        url_key = _offer_identity(item)
        listing_key = (
            _normalize_text(item.get("source") or item["store"]),
            _normalize_text(item["store"]),
            " ".join(sorted(_product_tokens(item["name"]))),
            round(item["price"], 2) if item.get('price') is not None else None,
        )

        if url_key in seen_urls or listing_key in seen_listings:
            continue
        seen_urls.add(url_key)
        seen_listings.add(listing_key)
        unique.append(item)

    return unique


def _product_response(item, fallback_id):
    response = {
        "id": item.get("id") or fallback_id,
        "name": item["name"],
        "brand": item["brand"],
        "category": item["category"],
        "image_url": item.get("image_url"),
        "current_price": item["price"],
        "store": item["store"],
        "source": item.get("source") or item["store"],
        "url": item["url"],
        "sku": item.get("sku"),
        "last_update": item.get("last_update") or datetime.utcnow().isoformat(),
    }
    response.update(
        {
            field: item.get(field)
            for field in OFFER_DETAIL_FIELDS
            if item.get(field) is not None
        }
    )
    response.setdefault("cash_price", response["current_price"])
    response.setdefault("is_cached", False)
    return response


def _product_tokens(name):
    normalized = _normalize_text(name)
    normalized = normalized.replace("frostfree", "frost free")
    normalized = re.sub(r"(\d+)\s*(btus?|btu)\b", r"\1btu", normalized)
    normalized = re.sub(r"(\d+)\s*(litros?|lts?|lt|l)\b", r"\1l", normalized)
    normalized = re.sub(r"(\d+)\s*(volts?|volt|v)\b", r"\1v", normalized)
    normalized = re.sub(r"(\d+)\s*(polegadas?|pol)\b", r"\1pol", normalized)
    normalized = re.sub(r"(\d+)\s*(quilos?|kg)\b", r"\1kg", normalized)
    normalized = re.sub(r"(\d+)\s*(gb|tb|ml|w)\b", r"\1\2", normalized)

    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if token not in PRODUCT_STOP_WORDS
        and token not in STORE_WORDS
        and len(token) > 1
    }
    if "geladeira" in tokens:
        tokens.add("refrigerador")
    if "refrigerador" in tokens:
        tokens.add("geladeira")
    if "geladeiras" in tokens:
        tokens.update({"geladeira", "refrigerador"})
    if "refrigeradores" in tokens:
        tokens.update({"geladeira", "refrigerador"})
    return tokens


def _spec_tokens(tokens, query_tokens):
    return {
        token
        for token in tokens
        if token not in query_tokens and any(character.isdigit() for character in token)
    }


def _brand_tokens(tokens):
    return tokens.intersection(KNOWN_BRANDS)


def _model_tokens(tokens):
    return {
        token
        for token in tokens
        if re.search(r"[a-z]+\d|\d+[a-z]+", token)
    }


def _numeric_tokens(tokens):
    return {token for token in tokens if any(character.isdigit() for character in token)}


def _has_accessory_token(tokens):
    return bool(tokens.intersection(ACCESSORY_TERMS))


def _same_product(cluster_tokens, item_tokens, query_tokens):
    if not cluster_tokens or not item_tokens:
        return False

    if _has_accessory_token(cluster_tokens) != _has_accessory_token(item_tokens):
        return False

    cluster_brands = _brand_tokens(cluster_tokens)
    item_brands = _brand_tokens(item_tokens)
    if cluster_brands and item_brands and not cluster_brands.intersection(item_brands):
        return False

    cluster_models = _model_tokens(cluster_tokens)
    item_models = _model_tokens(item_tokens)
    if cluster_models and item_models and not cluster_models.intersection(item_models):
        return False

    cluster_numbers = _numeric_tokens(cluster_tokens)
    item_numbers = _numeric_tokens(item_tokens)
    if cluster_numbers and item_numbers and not cluster_numbers.intersection(item_numbers):
        return False

    shared = cluster_tokens.intersection(item_tokens)
    union_score = len(shared) / max(1, max(len(cluster_tokens), len(item_tokens)))
    containment_score = len(shared) / max(1, min(len(cluster_tokens), len(item_tokens)))
    shared_numbers = {
        token for token in shared if any(character.isdigit() for character in token)
    }

    return len(shared) >= 3 and (
        union_score >= 0.52
        or containment_score >= 0.66
        or (bool(shared_numbers) and containment_score >= 0.55)
    )


def _build_comparison_rows(products, product_query):
    unavailable = [product for product in products if not offer_available(product)]
    products = [product for product in products if offer_available(product) and number(product.get("current_price")) is not None]
    clusters = []
    query_tokens = _product_tokens(product_query)
    query_sku = _primary_sku(product_query)
    sku_products = [
        product
        for product in products
        if _item_sku(product, product_query)
        and (not query_sku or _item_sku(product, product_query) == query_sku)
    ]
    products_to_cluster = sku_products if sku_products else (
        products if not query_sku else []
    )

    for product in products_to_cluster:
        product_sku = _item_sku(product, product_query)
        item_tokens = _product_tokens(product["name"])

        if product_sku:
            item_tokens.discard(product_sku.lower())
            target_cluster = None

            for cluster in clusters:
                if cluster.get("sku") != product_sku:
                    continue
                target_cluster = cluster
                break

            if not target_cluster:
                target_cluster = {
                    "sku": product_sku,
                    "tokens": set(item_tokens),
                    "offers": [],
                }
                clusters.append(target_cluster)

            target_cluster["tokens"].update(item_tokens)
            target_cluster["offers"].append(product)
            continue

        target_cluster = None

        for cluster in clusters:
            if _same_product(cluster["tokens"], item_tokens, query_tokens):
                target_cluster = cluster
                break

        if not target_cluster:
            target_cluster = {"tokens": set(item_tokens), "offers": []}
            clusters.append(target_cluster)

        target_cluster["tokens"].update(item_tokens)
        target_cluster["offers"].append(product)

    rows = []
    for index, cluster in enumerate(clusters, start=1):
        offers = sorted(cluster["offers"], key=lambda item: item["current_price"])
        best_by_store = {}
        for offer in offers:
            current = best_by_store.get(seller_scope_key(offer))
            if not current or offer["current_price"] < current["current_price"]:
                best_by_store[seller_scope_key(offer)] = offer

        store_offers = sorted(
            best_by_store.values(),
            key=lambda item: item["current_price"],
        )
        cheapest = store_offers[0]
        prices = [offer["current_price"] for offer in store_offers]
        pma = sum(prices) / len(prices)
        difference_value = cheapest["current_price"] - pma
        difference_percent = (
            abs(difference_value) / pma * 100
            if pma > 0
            else 0
        )

        rows.append(
            {
                "id": index,
                "sku": cluster.get("sku"),
                "name": (
                    f"{cluster['sku']} - {cheapest['name']}"
                    if cluster.get("sku") and cluster["sku"] not in cheapest["name"].upper()
                    else cheapest["name"]
                ),
                "store_count": len(store_offers),
                "offer_count": len(offers),
                "pma": round(pma, 2),
                "cheapest_price": cheapest["current_price"],
                "cheapest_store": cheapest["store"],
                "cheapest_url": cheapest["url"],
                "difference_value": round(difference_value, 2),
                "difference_percent": round(difference_percent, 2),
                "offers": offers,
            }
        )

    for row in rows:
        if row.get('sku'):
            row['offers'].extend(offer for offer in unavailable if _primary_sku(offer.get('sku') or offer.get('name') or '') == row['sku'])
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            -row["store_count"],
            -row["offer_count"],
            row["cheapest_price"],
            row["name"],
        ),
    )
    return sorted_rows


def _catalog_product_response(item, fallback_id):
    return {
        "id": fallback_id,
        "name": item["name"],
        "brand": item.get("brand") or "Comparador",
        "category": item.get("category") or "Produto",
        "image_url": item.get("image_url"),
        "current_price": item["price"],
        "store": item.get("best_store") or "Comparador",
        "source": item.get("best_store") or "Comparador",
        "url": item["url"],
        "sku": item.get("sku") or _primary_sku(item.get("name") or ""),
        "last_update": datetime.utcnow().isoformat(),
    }


def _build_catalog_rows(product_query):
    if _primary_sku(product_query):
        return []

    rows = []

    for index, item in enumerate(scrape_target_catalog(product_query, limit=CATALOG_ROW_LIMIT), start=1):
        offers = [
            _product_response(offer, (900000 + index) * 10 + offer_index)
            for offer_index, offer in enumerate(item.get("target_offers") or [], start=1)
            if offer_available(offer)
        ]
        if not offers:
            continue

        store_offers = {}
        for offer in sorted(offers, key=_sortable_price):
            store_offers.setdefault(seller_scope_key(offer), offer)

        best_offers = sorted(
            store_offers.values(),
            key=lambda offer: offer["current_price"],
        )
        cheapest = best_offers[0]
        prices = [offer["current_price"] for offer in best_offers]
        pma = sum(prices) / len(prices)
        difference_value = cheapest["current_price"] - pma
        rows.append(
            {
                "id": 900000 + index,
                "name": item["name"],
                "store_count": len(best_offers),
                "offer_count": len(offers),
                "pma": round(pma, 2),
                "cheapest_price": cheapest["current_price"],
                "cheapest_store": cheapest["store"],
                "cheapest_url": cheapest["url"],
                "difference_value": round(difference_value, 2),
                "difference_percent": round(abs(difference_value) / pma * 100, 2)
                if pma > 0
                else 0,
                "offers": best_offers,
            }
        )

    return rows


def _same_row_product(left, right, product_query):
    left_sku = left.get("sku") or _primary_sku(left.get("name") or "")
    right_sku = right.get("sku") or _primary_sku(right.get("name") or "")
    query_sku = _primary_sku(product_query)

    if query_sku:
        return left_sku == query_sku and right_sku == query_sku

    if left_sku and right_sku:
        return left_sku == right_sku

    left_tokens = _product_tokens(left["name"])
    right_tokens = _product_tokens(right["name"])
    query_tokens = _product_tokens(product_query)

    return _same_product(left_tokens, right_tokens, query_tokens)


def _comparison_row_from_offers(row_id, name, offers, sku=None):
    valid_offers = sorted(
        [
            offer
            for offer in offers
            if number(offer.get("current_price")) is not None and not isinstance(offer.get("current_price"), str) and offer_available(offer)
        ],
        key=lambda offer: offer["current_price"],
    )
    best_by_store = {}
    if not valid_offers:
        return None
    for offer in valid_offers:
        current = best_by_store.get(seller_scope_key(offer))
        if not current or offer["current_price"] < current["current_price"]:
            best_by_store[seller_scope_key(offer)] = offer

    store_offers = sorted(
        best_by_store.values(),
        key=lambda offer: offer["current_price"],
    )
    cheapest = store_offers[0]
    prices = [offer["current_price"] for offer in store_offers]
    pma = sum(prices) / len(prices)
    difference_value = cheapest["current_price"] - pma

    return {
        "id": row_id,
        "sku": sku or _primary_sku(name),
        "name": name,
        "store_count": len(store_offers),
        "offer_count": len(valid_offers),
        "pma": round(pma, 2),
        "cheapest_price": cheapest["current_price"],
        "cheapest_store": cheapest["store"],
        "cheapest_url": cheapest["url"],
        "difference_value": round(difference_value, 2),
        "difference_percent": round(abs(difference_value) / pma * 100, 2)
        if pma > 0
        else 0,
        "offers": [*store_offers, *[offer for offer in offers if not offer_available(offer)]],
    }


def _merge_catalog_rows(actual_rows, catalog_rows, product_query):
    merged_rows = []
    used_catalog_indexes = set()

    for actual_row in actual_rows:
        matching_index = None
        matching_catalog = None

        for index, catalog_row in enumerate(catalog_rows):
            if index in used_catalog_indexes:
                continue
            if _same_row_product(actual_row, catalog_row, product_query):
                matching_index = index
                matching_catalog = catalog_row
                break

        if matching_catalog:
            used_catalog_indexes.add(matching_index)
            merged_rows.append(
                _comparison_row_from_offers(
                    actual_row["id"],
                    matching_catalog["name"],
                    [*actual_row["offers"], *matching_catalog["offers"]],
                    actual_row.get("sku") or matching_catalog.get("sku"),
                )
            )
        else:
            merged_rows.append(actual_row)

    merged_rows.extend(
        catalog_row
        for index, catalog_row in enumerate(catalog_rows)
        if index not in used_catalog_indexes
    )

    return sorted(
        [row for row in merged_rows if row],
        key=lambda row: (
            -row["store_count"],
            -row["offer_count"],
            row["cheapest_price"],
            row["name"],
        ),
    )


def _build_payloads(raw_by_store, product_query):
    store_payloads = []
    available_results = []
    response_id = 1
    query_sku = _primary_sku(product_query)

    for store_config in TARGET_STORES:
        store_items = sorted(
            _deduplicate(
                [
                    item
                    for item in raw_by_store.get(store_config["key"], [])
                    if _matches_query_sku(item, query_sku)
                ]
            ),
            key=lambda item: _sort_key(item, product_query),
        )
        store_products = []

        for store_item in store_items[:STORE_ITEM_LIMIT]:
            store_products.append(_product_response(store_item, response_id))
            response_id += 1

        available_results.extend(store_products)
        best_product = store_products[0] if store_products else None
        cache_only = bool(store_products) and all(
            product.get("is_cached") for product in store_products
        )

        if best_product:
            store_payloads.append(
                {
                    "key": store_config["key"],
                    "label": store_config["label"],
                    "available": True,
                    "message": "Cache local" if cache_only else None,
                    "product": best_product,
                    "products": store_products,
                    "cadence": store_config.get("cadence"),
                }
            )
        else:
            store_payloads.append(
                {
                    "key": store_config["key"],
                    "label": store_config["label"],
                    "available": False,
                    "message": "Sem retorno atual",
                    "product": None,
                    "products": [],
                    "cadence": store_config.get("cadence"),
                }
            )

    return available_results, store_payloads


def _append_discovered_store_payloads(store_payloads, available_results):
    registered_names = {
        _normalize_text(name)
        for store in TARGET_STORES
        for name in (store["label"], *store.get("aliases", ()))
    }
    discovered = {}
    for product in available_results:
        source = product.get("source") or product.get("store") or "Outras lojas"
        if _normalize_text(source) in registered_names:
            continue
        discovered.setdefault(source, []).append(product)

    for source, products in sorted(discovered.items()):
        ordered = sorted(products, key=_sortable_price)
        store_payloads.append(
            {
                "key": f"discovered_{_normalize_text(source).replace(' ', '_')}",
                "label": source,
                "available": True,
                "message": "Descoberta via comparador",
                "product": ordered[0],
                "products": ordered,
                "cadence": "3x_daily",
            }
        )
    return store_payloads


def _source_health(store_payloads):
    return [
        {
            "key": store.get("key"),
            "label": store.get("label"),
            "status": (
                "cached"
                if store.get("available") and store.get("message") == "Cache local"
                else "available"
                if store.get("available")
                else "empty"
            ),
            "offer_count": len(store.get("products") or []),
            "cadence": store.get("cadence"),
            "message": store.get("message"),
        }
        for store in store_payloads
    ]


def build_summary_from_cached_results(available_results, product_query):
    store_payloads = []

    for store_config in TARGET_STORES:
        source_names = {
            _normalize_text(store_config["label"]),
            *[
                _normalize_text(alias)
                for alias in store_config.get("aliases", [])
            ],
        }
        store_products = sorted(
            [
                product
                for product in available_results
                if _normalize_text(product.get("source") or product.get("store"))
                in source_names
            ],
            key=_sortable_price,
        )
        best_product = store_products[0] if store_products else None
        cache_only = bool(store_products) and all(
            product.get("is_cached") for product in store_products
        )

        store_payloads.append(
            {
                "key": store_config["key"],
                "label": store_config["label"],
                "available": bool(best_product),
                "message": (
                    "Cache local"
                    if cache_only
                    else None
                    if best_product
                    else "Sem retorno atual"
                ),
                "product": best_product,
                "products": store_products,
                "cadence": store_config.get("cadence"),
            }
        )

    _append_discovered_store_payloads(store_payloads, available_results)

    return {
        "processed_count": 0,
        "results": available_results,
        "stores": store_payloads,
        "comparison": _build_comparison_rows(available_results, product_query),
        "source_health": _source_health(store_payloads),
    }


def _coerce_datetime(value):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


def _price_detail_values(item, cached=False):
    values = {
        field: item.get(field)
        for field in OFFER_DETAIL_FIELDS
        if field not in {"logistics_captured_at", "is_cached"}
    }
    values["cash_price"] = values.get("cash_price") or item.get("price")
    captured_at = _coerce_datetime(item.get("logistics_captured_at"))
    values["logistics_captured_at"] = captured_at
    values["is_cached"] = cached or bool(item.get("is_cached"))
    return values


def _save_result(item, db: Session):
    item = {**item, "brand": canonical_brand(item.get("brand"))}
    item = {**item, "url": _offer_url_identity(item["url"])}
    candidates = (
        db.query(PriceHistory)
        .filter(
            PriceHistory.url == item["url"],
        )
        .order_by(PriceHistory.timestamp.desc(), PriceHistory.id.desc())
        .all()
    )
    existing_price = next((price for price in candidates if seller_scope_key(
        {"source": price.source, "store": price.store}
    ) == seller_scope_key(item)), None)
    observed_at = datetime.utcnow()
    if item.get("is_cached"):
        observed_at = _coerce_datetime(item.get("captured_at") or item.get("last_update"))
        if not observed_at or (existing_price and existing_price.timestamp and existing_price.timestamp >= observed_at):
            return 0

    if not existing_price:
        new_product = Product(
            name=item["name"],
            brand=item["brand"],
            category=item["category"],
            image_url=item["image_url"],
            sku=item.get("sku"),
        )
        db.add(new_product)
        db.flush()

        new_price = PriceHistory(
            product_id=new_product.id,
            price=item["price"],
            store=item["store"],
            source=item.get("source") or item["store"],
            url=item["url"],
            timestamp=observed_at,
            **_price_detail_values(item),
        )
        db.add(new_price)
        return 1

    product = db.query(Product).filter(Product.id == existing_price.product_id).first()
    if product:
        product.name = item["name"]
        product.brand = item["brand"]
        product.category = item["category"]
        product.image_url = item["image_url"] or product.image_url
        product.sku = item.get("sku") or product.sku

    source = item.get("source") or item["store"]
    if (
        existing_price.price != item["price"]
        or existing_price.store != item["store"]
        or existing_price.source != source
        or offer_available({'availability': existing_price.availability}) != offer_available(item)
    ):
        new_price = PriceHistory(
            product_id=existing_price.product_id,
            price=item["price"],
            store=item["store"],
            source=source,
            url=item["url"],
            timestamp=observed_at,
            **_price_detail_values(item),
        )
        db.add(new_price)
        return 1

    for field, value in _price_detail_values(item).items():
        setattr(existing_price, field, value)
    existing_price.timestamp = observed_at
    return 0


def _price_details_from_model(price, cached=True):
    details = {
        field: getattr(price, field, None)
        for field in OFFER_DETAIL_FIELDS
        if field not in {"logistics_captured_at", "is_cached"}
    }
    captured_at = getattr(price, "logistics_captured_at", None)
    if captured_at:
        details["logistics_captured_at"] = captured_at.isoformat()
    details["is_cached"] = cached
    return details


def _cached_item(product, latest_price, source):
    return {
        "name": product.name,
        "price": latest_price.price,
        "store": latest_price.store,
        "source": source,
        "url": latest_price.url,
        "brand": product.brand,
        "category": product.category,
        "image_url": product.image_url,
        "sku": product.sku,
        "last_update": (
            latest_price.timestamp.isoformat() if latest_price.timestamp else None
        ),
        **_price_details_from_model(latest_price),
    }


def _cached_sku_items_by_store(db: Session, sku: str):
    cached = {store["key"]: [] for store in TARGET_STORES}
    store_by_name = {}
    for store in TARGET_STORES:
        for name in (store["label"], *store.get("aliases", ())):
            store_by_name[_normalize_text(name)] = store

    for product, latest_price in _cached_sku_observations(db, sku):
        source = latest_price.source or latest_price.store
        store = store_by_name.get(_normalize_text(source))
        if not store:
            continue

        cached[store["key"]].append(_cached_item(product, latest_price, source))

    return cached


def _cached_sku_observations(db, sku):
    known = db.query(KnownSku).filter(KnownSku.sku == sku).first()
    identity = ProductIdentity(sku=sku, model=getattr(known, "model", None) or sku,
                               name=getattr(known, "description", None), brand=getattr(known, "brand", None))
    sources = {(source.url, source.seller): source for source in db.query(ProductSource).filter(ProductSource.sku == sku).all()}
    for product in db.query(Product).filter(Product.sku == sku).all():
        price = max(product.prices, key=lambda value: (value.timestamp or datetime.min, value.id or 0), default=None)
        if not price or (number(price.price) is None and offer_available({'availability': price.availability})):
            continue
        source = sources.get((_offer_url_identity(price.url), price.store))
        if source and source.validation_status in {"invalid_match", "not_found"}:
            continue
        candidate = {"name": product.name, "url": price.url, "brand": product.brand,
                     "model": source.model if source else None, "mpn": source.mpn if source else None,
                     "ean": source.ean if source else None}
        if resolve_candidate(identity, candidate).accepted:
            yield product, price


def _cached_discovered_sku_items(db: Session, sku: str):
    registered_names = {
        _normalize_text(name)
        for store in TARGET_STORES
        for name in (store["label"], *store.get("aliases", ()))
    }
    items = []
    for product, latest_price in _cached_sku_observations(db, sku):
        source = latest_price.source or latest_price.store
        if _normalize_text(source) not in registered_names:
            items.append(_cached_item(product, latest_price, source))
    return items


def _run_store_scrapers(store_config, product_query):
    store_results = []
    from services.manufacturer_pages import MANUFACTURER_STORES
    if store_config['key'] in MANUFACTURER_STORES:
        from crawlers.paged_discovery import fetch_search_page, search_url
        from crawlers.registry import collect_product_source
        from types import SimpleNamespace
        from services.product_resolver import ProductIdentity, resolve_candidate
        identity = ProductIdentity(sku=product_query, model=product_query)
        candidates, _, _ = fetch_search_page(store_config['key'], search_url(store_config['key'], product_query), timeout=12)
        for candidate in candidates:
            if not resolve_candidate(identity, candidate).accepted:
                continue
            source = SimpleNamespace(store=store_config['key'], url=candidate['url'], seller=candidate.get('seller'), brand=None,
                                     external_product_id=None, asin=None, item_id=None)
            result = collect_product_source(source, identity, timeout=12)
            for item in result.get('items', []):
                normalized = _normalize_result(item, product_query, store_config['label'])
                if normalized:
                    store_results.append(normalized)
        return store_results
    query_variants = _query_variants(product_query)
    scraper_functions = store_config.get("direct_scrapers", ())
    mercado_livre_mode = "always"
    if store_config.get("key") == "mercado_livre":
        mercado_livre_mode = get_runtime_provider_settings("mercado_livre")["usage_mode"]
        if mercado_livre_mode == "fallback":
            scraper_functions = tuple(sorted(
                scraper_functions,
                key=lambda scraper: scraper.__name__ == "search_mercado_livre",
            ))

    for scraper_func in scraper_functions:
        if (
            scraper_func.__name__ == "search_mercado_livre"
            and mercado_livre_mode == "fallback"
            and store_results
        ):
            continue
        normalized_results = []
        variants = (
            query_variants
            if scraper_func.__name__ in VARIANT_SCRAPERS
            else query_variants[:1]
        )

        for variant in variants:
            try:
                results = scraper_func(variant)
            except Exception as e:
                print(f" WARNING: Scraper {scraper_func.__name__} falhou: {e}")
                results = []

            normalized_results.extend(
                normalized
                for item in results
                if (
                    normalized := _normalize_result(
                        item,
                        product_query,
                        store_config["label"],
                    )
                )
                and not _is_accessory_mismatch(normalized, product_query)
            )

            if len(normalized_results) >= STORE_ITEM_LIMIT:
                break

        if normalized_results:
            print(
                " LOG: "
                f"{store_config['label']} via {scraper_func.__name__} "
                f"retornou {len(normalized_results)} itens."
            )
            store_results.extend(normalized_results)
        else:
            print(
                " WARNING: "
                f"{store_config['label']} via {scraper_func.__name__} "
                "retornou lista vazia."
            )

    return sorted(
        _deduplicate(store_results),
        key=lambda item: _sort_key(item, product_query),
    )


def _interleave_store_items(raw_by_store, discovered_items):
    buckets = [
        [
            {**item, "_bucket": store_key}
            for item in store_items
        ]
        for store_key, store_items in raw_by_store.items()
        if store_items
    ]
    if discovered_items:
        buckets.append(
            [{**item, "_bucket": "_discovered"} for item in discovered_items]
        )

    interleaved = []
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket):
                interleaved.append(bucket[index])
    return interleaved


def _run_all_scrapers(product_query: str, db: Session, postal_code=None, scope="all"):
    postal_code = normalize_postal_code(postal_code)
    scope = scope if scope in {"all", "sites", "retail"} else "all"
    print(f" LOG: Iniciando buscas gratuitas para '{product_query}' ({scope})")
    raw_by_store = {store["key"]: [] for store in TARGET_STORES}
    selected_stores = [
        store
        for store in TARGET_STORES
        if scope == "all" or store.get("scope") == scope
    ]
    worker_count = min(8, max(1, len(selected_stores) + 2))
    comparison_items = []
    bing_shopping_items = []
    external_provider_runs = []

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        store_futures = {
            executor.submit(_run_store_scrapers, store, product_query): store
            for store in selected_stores
            if store.get("direct_scrapers")
        }
        comparison_future = (
            executor.submit(scrape_comparison_offers, product_query)
            if scope in {"all", "retail"}
            else None
        )
        bing_shopping_future = (
            executor.submit(search_bing_shopping, product_query)
            if scope in {"all", "retail"}
            else None
        )

        for future in as_completed(store_futures):
            store = store_futures[future]
            try:
                raw_by_store[store["key"]] = future.result()
            except Exception as exc:
                print(f" WARNING: Extracao de {store['label']} falhou: {exc}")

        if comparison_future:
            try:
                comparison_items = comparison_future.result()
            except Exception as exc:
                print(f" WARNING: Comparadores falharam: {exc}")

        if bing_shopping_future:
            try:
                bing_shopping_items = bing_shopping_future.result()
                print(
                    " LOG: Bing Shopping retornou "
                    f"{len(bing_shopping_items)} itens."
                )
            except Exception as exc:
                print(f" WARNING: Bing Shopping falhou: {exc}")

    if scope in {"all", "retail"}:
        current_offer_count = (
            sum(len(items) for items in raw_by_store.values())
            + len(comparison_items)
            + len(bing_shopping_items)
        )
        found_store_keys = {
            store_key
            for store_key, items in raw_by_store.items()
            if items
        }
        for discovered_item in [*comparison_items, *bing_shopping_items]:
            discovered_store = find_target_store(
                discovered_item.get("source"),
                discovered_item.get("store"),
                discovered_item.get("url"),
            )
            if discovered_store:
                found_store_keys.add(discovered_store["key"])
        missing_store_count = len(
            {store["key"] for store in selected_stores} - found_store_keys
        )
        external_providers = [
            provider
            for provider in get_search_provider_names()
            if should_run_provider(
                provider,
                current_offer_count,
                missing_store_count,
            )
        ]
        if external_providers:
            with ThreadPoolExecutor(max_workers=len(external_providers)) as executor:
                provider_futures = {
                    executor.submit(
                        run_external_market_provider,
                        provider,
                        product_query,
                    ): provider
                    for provider in external_providers
                }
                for future in as_completed(provider_futures):
                    external_provider_runs.append(future.result())

    discovered_items = []
    selected_store_keys = {store["key"] for store in selected_stores}
    external_items = [
        item
        for provider_run in external_provider_runs
        for item in provider_run["items"]
    ]
    for item in [*comparison_items, *bing_shopping_items, *external_items]:
        store = STORE_BY_LABEL.get(item.get("source")) or find_target_store(
            item.get("source"),
            item.get("store"),
            item.get("url"),
        )
        normalized = _normalize_result(
            item,
            product_query,
            store["label"] if store else None,
        )
        if normalized and not _is_accessory_mismatch(normalized, product_query):
            if store and store["key"] in selected_store_keys:
                raw_by_store[store["key"]].append(normalized)
            elif not store:
                discovered_items.append(normalized)

    query_sku = _primary_sku(product_query)
    cached_by_store = {store["key"]: [] for store in TARGET_STORES}
    cached_discovered = []
    if query_sku:
        cached_by_store = _cached_sku_items_by_store(db, query_sku)
        cached_discovered = _cached_discovered_sku_items(db, query_sku)
        tagged_items = _interleave_store_items(raw_by_store, discovered_items)
        enriched = enrich_offers(
            tagged_items[:DETAIL_ENRICHMENT_LIMIT],
            postal_code,
            force_refresh=True,
        )
        enriched.extend(tagged_items[DETAIL_ENRICHMENT_LIMIT:])
        raw_by_store = {store["key"]: [] for store in TARGET_STORES}
        discovered_items = []
        for enriched_item in enriched:
            item = enriched_item
            bucket = item.pop("_bucket", "_discovered")
            if bucket == "_discovered":
                discovered_items.append(item)
            else:
                raw_by_store[bucket].append(item)

    fresh_results, _fresh_store_payloads = _build_payloads(raw_by_store, product_query)
    fresh_discovered = [
        _product_response(item, 800000 + index)
        for index, item in enumerate(
            sorted(_deduplicate(discovered_items), key=_sortable_price),
            start=1,
        )
    ]
    fresh_results.extend(fresh_discovered)
    if query_sku:
        for store in TARGET_STORES:
            if not raw_by_store[store["key"]]:
                raw_by_store[store["key"]] = cached_by_store[store["key"]]

        fresh_identities = {_offer_identity(item) for item in fresh_discovered}
        for cached_item in cached_discovered:
            if _offer_identity(cached_item) not in fresh_identities:
                discovered_items.append(cached_item)

    available_results, store_payloads = _build_payloads(raw_by_store, product_query)
    available_results.extend(
        _product_response(item, 850000 + index)
        for index, item in enumerate(
            sorted(_deduplicate(discovered_items), key=_sortable_price),
            start=1,
        )
    )
    store_payloads = _append_discovered_store_payloads(
        store_payloads, available_results
    )

    processed_count = 0
    storage_limit = STORE_ITEM_LIMIT * (len(TARGET_STORES) + 4)
    for index, item in enumerate(fresh_results[:storage_limit]):
        storage_item = {
            "name": item["name"],
            "price": item["current_price"],
            "store": item["store"],
            "source": item.get("source") or item["store"],
            "url": item["url"],
            "brand": item["brand"],
            "category": item["category"],
            "image_url": item.get("image_url"),
            "sku": item.get("sku"),
            **{
                field: item.get(field)
                for field in OFFER_DETAIL_FIELDS
                if item.get(field) is not None
            },
        }
        try:
            processed_count += _save_result(storage_item, db)
        except Exception as e:
            print(f" ERROR: Falha ao processar item {index + 1}: {e}")
            db.rollback()

    db.commit()
    actual_comparison_rows = _build_comparison_rows(available_results, product_query)
    catalog_comparison_rows = _build_catalog_rows(product_query)
    comparison_rows = _merge_catalog_rows(
        actual_comparison_rows,
        catalog_comparison_rows,
        product_query,
    )
    print(
        " LOG: Finalizado. "
        f"{len(available_results)} ofertas encontradas, "
        f"{len(comparison_rows)} linhas comparativas, "
        f"{processed_count} precos novos/atualizados."
    )
    provider_health = [
        {
            "key": provider_run["provider"],
            "label": provider_run["label"],
            "status": provider_run["status"],
            "offer_count": len(provider_run["items"]),
            "message": provider_run["message"],
        }
        for provider_run in external_provider_runs
    ]
    return {
        "processed_count": processed_count,
        "fresh_count": len(fresh_results),
        "results": available_results,
        "stores": store_payloads,
        "comparison": comparison_rows,
        "source_health": [*_source_health(store_payloads), *provider_health],
        "scope": scope,
    }


def run_all_scrapers(product_query: str, db: Session, postal_code=None, scope="all"):
    with SCRAPE_SLOTS:
        return _run_all_scrapers(
            product_query,
            db,
            postal_code=postal_code,
            scope=scope,
        )
