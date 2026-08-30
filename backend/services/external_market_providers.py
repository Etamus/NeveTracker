from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urlencode

import requests

from services.http_client import create_resilient_session
from services.integration_settings import get_runtime_provider_settings
from services.provider_quota import reserve_provider_quota


BRIGHT_DATA_ENDPOINT = "https://api.brightdata.com/request"
APIFY_ENDPOINT = "https://api.apify.com/v2/acts"
SERPER_ENDPOINT = "https://google.serper.dev/shopping"
DATAFORSEO_ENDPOINT = "https://api.dataforseo.com/v3/merchant/google/products"
DATAFORSEO_SELLERS_ENDPOINT = "https://api.dataforseo.com/v3/merchant/google/sellers"
SEARCHAPI_ENDPOINT = "https://www.searchapi.io/api/v1/search"
SESSION = create_resilient_session(pool_size=4)


def _first_value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, dict):
                for nested_key in (
                    "value",
                    "amount",
                    "current",
                    "regular",
                    "displayed_price",
                    "delivery_message",
                    "name",
                    "text",
                    "url",
                ):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        return nested_value
            return value
    return None


def _price_value(value):
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None

    cleaned = re.sub(r"[^0-9,.-]", "", value.strip())
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _days_from_text(value):
    text = str(value or "").lower()
    range_match = re.search(
        r"\b(\d{1,3})\s*(?:a|ate|até|-)\s*(\d{1,3})\s*(?:dias?|days?)",
        text,
        re.I,
    )
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))
    single_match = re.search(r"\b(\d{1,3})\s*(?:dias?|days?)", text, re.I)
    if single_match:
        days = int(single_match.group(1))
        return days, days
    return None, None


def _shipping_from_text(value):
    text = str(value or "").strip()
    lowered = text.lower()
    if any(
        term in lowered
        for term in (
            "frete gratis",
            "frete grátis",
            "entrega gratis",
            "entrega grátis",
            "free shipping",
            "free delivery",
        )
    ):
        return 0.0
    if any(term in lowered for term in ("frete", "shipping", "delivery")):
        price_match = re.search(
            r"(?:R\$|\$)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:\.\d{2})?)",
            text,
            re.I,
        )
        return _price_value(price_match.group(1)) if price_match else None
    return None


def _installment_details(value, cash_price):
    text = str(value or "")
    match = re.search(
        r"\b(\d{1,2})\s*x\s*(?:de\s*)?(?:R\$\s*)?([\d.,]+)",
        text,
        re.I,
    )
    if not match:
        return {}
    count = int(match.group(1))
    installment_value = _price_value(match.group(2))
    total = round(count * installment_value, 2) if installment_value else None
    if not total or total < cash_price:
        return {}
    return {
        "installment_count": count,
        "installment_value": installment_value,
        "installment_price": total,
        "payment_difference": round(total - cash_price, 2),
    }


def _iter_candidate_dicts(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_candidate_dicts(item)
        return
    if not isinstance(value, dict):
        return

    has_name = any(value.get(key) for key in ("title", "name", "productTitle", "product_name"))
    has_price = any(
        value.get(key) not in (None, "")
        for key in (
            "price",
            "priceValue",
            "price_value",
            "extracted_price",
            "currentPrice",
            "base_price",
            "total_price",
        )
    )
    if has_name and has_price:
        yield value

    for nested in value.values():
        if isinstance(nested, (dict, list)):
            yield from _iter_candidate_dicts(nested)


def _normalize_provider_item(item, query, provider):
    title = str(
        _first_value(item, "title", "name", "productTitle", "product_name") or ""
    ).strip()
    price = _price_value(
        _first_value(
            item,
            "priceValue",
            "price_value",
            "extracted_price",
            "currentPrice",
            "base_price",
            "total_price",
            "price",
        )
    )
    url = str(
        _first_value(
            item,
            "productUrl",
            "product_url",
            "productLink",
            "product_link",
            "sellerLink",
            "seller_link",
            "link",
            "url",
        )
        or ""
    ).strip()
    seller = str(
        _first_value(
            item,
            "merchantName",
            "merchant_name",
            "merchant",
            "sellerName",
            "seller_name",
            "seller",
            "source",
            "store",
        )
        or "Loja descoberta"
    ).strip()
    image_url = _first_value(
        item,
        "imageUrl",
        "image_url",
        "thumbnail",
        "thumbnailUrl",
        "image",
    )

    if not title or not price or not url.startswith(("http://", "https://")):
        return None

    delivery_data = _first_value(
        item,
        "delivery",
        "deliveryInfo",
        "delivery_info",
        "shipping",
        "shippingInfo",
        "shipping_info",
        "details",
    )
    delivery_text = delivery_data
    if isinstance(delivery_text, dict):
        delivery_text = _first_value(
            delivery_text,
            "delivery_message",
            "message",
            "text",
            "displayed_price",
        )
    delivery_min, delivery_max = _days_from_text(delivery_text)
    shipping_cost = _price_value(
        _first_value(
            item,
            "shippingCost",
            "shipping_cost",
            "deliveryCost",
            "shipping_price",
            "extracted_delivery_price",
        )
    )
    if shipping_cost is None and isinstance(delivery_data, dict):
        shipping_cost = _price_value(
            _first_value(delivery_data, "delivery_price", "price", "cost")
        )
    if shipping_cost is None:
        shipping_cost = _shipping_from_text(delivery_text)

    installment_data = _first_value(
        item,
        "installments",
        "installment",
        "payment_terms",
        "paymentTerms",
        "displayed_payment_breakdown",
        "payment_methods",
    )
    result = {
        "name": title,
        "price": price,
        "store": seller,
        "seller": seller,
        "source": seller,
        "url": url,
        "brand": _first_value(item, "brand") or "Oferta",
        "category": _first_value(item, "category") or query,
        "image_url": image_url,
        "sku": query,
        "cash_price": price,
        "discovery_source": provider,
        "shipping_method": str(delivery_text).strip() if delivery_text else None,
        "logistics_source": provider,
        "availability": _first_value(
            item,
            "product_availability",
            "availability",
            "stockStatus",
            "stock_status",
        ),
        **_installment_details(installment_data, price),
    }
    if shipping_cost is not None:
        result["shipping_cost"] = shipping_cost
    if delivery_min is not None:
        result["delivery_min_days"] = delivery_min
        result["delivery_max_days"] = delivery_max
    return {key: value for key, value in result.items() if value is not None}


def _normalize_payload(payload, query, provider, limit):
    results = []
    seen = set()
    for candidate in _iter_candidate_dicts(payload):
        item = _normalize_provider_item(candidate, query, provider)
        if not item:
            continue
        identity = (item["url"], item["store"].lower(), round(item["price"], 2))
        if identity in seen:
            continue
        seen.add(identity)
        results.append(item)
        if len(results) >= limit:
            break
    return results


def search_bright_data(query):
    settings = get_runtime_provider_settings("bright_data")
    limit = settings["max_results"]
    google_url = "https://www.google.com/search?" + urlencode(
        {
            "tbm": "shop",
            "q": query,
            "hl": "pt-BR",
            "gl": "br",
            "num": limit,
            "brd_json": 1,
        }
    )
    reserve_provider_quota("bright_data", 1)
    response = SESSION.post(
        BRIGHT_DATA_ENDPOINT,
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "zone": settings["zone"],
            "url": google_url,
            "format": "raw",
        },
        timeout=(10, 75),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Bright Data respondeu HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError:
        payload = json.loads(response.text)
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _normalize_payload(payload, query, "bright_data", limit)


def search_apify(query):
    settings = get_runtime_provider_settings("apify")
    limit = settings["max_results"]
    actor_id = quote(settings["actor_id"].replace("/", "~"), safe="~")
    allowed_counts = (10, 20, 30, 40, 50, 100)
    result_count = min(allowed_counts, key=lambda count: abs(count - limit))
    reserve_provider_quota("apify", result_count)
    response = SESSION.post(
        f"{APIFY_ENDPOINT}/{actor_id}/run-sync-get-dataset-items",
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
        },
        params={"timeout": 120, "clean": "true"},
        json={
            "query": query,
            "queries": [query],
            "country": "br",
            "language": "pt-br",
            "num": str(result_count),
            "max_pages": 1,
            "date_range": "anytime",
        },
        timeout=(10, 135),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Apify respondeu HTTP {response.status_code}.")
    return _normalize_payload(response.json(), query, "apify", limit)


def search_serper(query):
    settings = get_runtime_provider_settings("serper")
    limit = settings["max_results"]
    reserve_provider_quota("serper", 1)
    response = SESSION.post(
        SERPER_ENDPOINT,
        headers={
            "X-API-KEY": settings["api_key"],
            "Content-Type": "application/json",
        },
        json={
            "q": query,
            "gl": "br",
            "hl": "pt-br",
            "num": limit,
        },
        timeout=(10, 45),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Serper respondeu HTTP {response.status_code}.")
    return _normalize_payload(response.json(), query, "serper", limit)


def _dataforseo_task_result(payload):
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not tasks or not isinstance(tasks[0], dict):
        return None, []
    task = tasks[0]
    status_code = task.get("status_code")
    status_message = str(task.get("status_message") or "").lower()
    if status_code not in (None, 20000, 20100) and not any(
        state in status_message
        for state in ("queue", "progress", "processing", "not ready")
    ):
        raise RuntimeError(
            f"DataForSEO respondeu codigo {status_code}."
        )
    return task.get("id"), task.get("result") or []


def _dataforseo_seller_results(product_ids, authentication):
    if not product_ids:
        return []
    selected_ids = product_ids[:3]
    seller_depth = 10
    reserve_provider_quota("dataforseo", len(selected_ids) * seller_depth)
    response = SESSION.post(
        f"{DATAFORSEO_SELLERS_ENDPOINT}/task_post",
        auth=authentication,
        headers={"Content-Type": "application/json"},
        json=[
            {
                "product_id": product_id,
                "location_code": 2076,
                "language_code": "pt",
                "depth": seller_depth,
            }
            for product_id in selected_ids
        ],
        timeout=(10, 45),
    )
    if response.status_code >= 400:
        return []
    tasks = response.json().get("tasks") or []
    pending_ids = {
        task.get("id")
        for task in tasks
        if isinstance(task, dict)
        and task.get("id")
        and task.get("status_code") in {20000, 20100}
    }
    collected = []
    for _attempt in range(20):
        if not pending_ids:
            break
        time.sleep(2)
        for task_id in tuple(pending_ids):
            result_response = SESSION.get(
                f"{DATAFORSEO_SELLERS_ENDPOINT}/task_get/advanced/{task_id}",
                auth=authentication,
                timeout=(10, 30),
            )
            if result_response.status_code >= 400:
                pending_ids.remove(task_id)
                continue
            _returned_id, result = _dataforseo_task_result(result_response.json())
            if result:
                collected.extend(result)
                pending_ids.remove(task_id)
    return collected


def search_dataforseo(query):
    settings = get_runtime_provider_settings("dataforseo")
    limit = settings["max_results"]
    authentication = (settings["login"], settings["api_key"])
    reserve_provider_quota("dataforseo", limit)
    response = SESSION.post(
        f"{DATAFORSEO_ENDPOINT}/task_post",
        auth=authentication,
        headers={"Content-Type": "application/json"},
        json=[
            {
                "keyword": query,
                "location_code": 2076,
                "language_code": "pt",
                "depth": limit,
            }
        ],
        timeout=(10, 45),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"DataForSEO respondeu HTTP {response.status_code}.")
    task_id, result = _dataforseo_task_result(response.json())
    if result:
        product_ids = list(
            dict.fromkeys(
                str(_first_value(item, "product_id", "productId") or "")
                for item in _iter_candidate_dicts(result)
                if _first_value(item, "product_id", "productId")
            )
        )
        seller_results = _dataforseo_seller_results(product_ids, authentication)
        return _normalize_payload(
            [seller_results, result],
            query,
            "dataforseo",
            limit,
        )
    if not task_id:
        return []

    for _attempt in range(20):
        time.sleep(2)
        result_response = SESSION.get(
            f"{DATAFORSEO_ENDPOINT}/task_get/advanced/{task_id}",
            auth=authentication,
            timeout=(10, 30),
        )
        if result_response.status_code >= 400:
            raise RuntimeError(
                f"DataForSEO respondeu HTTP {result_response.status_code}."
            )
        _result_task_id, result = _dataforseo_task_result(result_response.json())
        if result:
            product_ids = list(
                dict.fromkeys(
                    str(_first_value(item, "product_id", "productId") or "")
                    for item in _iter_candidate_dicts(result)
                    if _first_value(item, "product_id", "productId")
                )
            )
            seller_results = _dataforseo_seller_results(
                product_ids,
                authentication,
            )
            return _normalize_payload(
                [seller_results, result],
                query,
                "dataforseo",
                limit,
            )
    raise RuntimeError("DataForSEO excedeu o tempo de processamento da consulta.")


def _searchapi_get(settings, **params):
    reserve_provider_quota("searchapi", 1)
    try:
        response = SESSION.get(
            SEARCHAPI_ENDPOINT,
            headers={"Authorization": f"Bearer {settings['api_key']}"},
            params=params,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"SearchAPI indisponivel: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"SearchAPI respondeu HTTP {response.status_code}.")
    return response.json()


def _searchapi_amazon_items(payload):
    results = []
    for source_item in payload.get("organic_results") or []:
        if not isinstance(source_item, dict):
            continue
        item = dict(source_item)
        item["seller_name"] = "Amazon Brasil"
        item["source"] = "Amazon Brasil"
        fulfillment = item.get("fulfillment") or {}
        standard_delivery = fulfillment.get("standard_delivery") or {}
        if isinstance(standard_delivery, dict) and standard_delivery.get("text"):
            item["delivery"] = standard_delivery["text"]
        results.append(item)
    return results


def search_searchapi(query):
    settings = get_runtime_provider_settings("searchapi")
    limit = settings["max_results"]
    payloads = []
    errors = []

    try:
        shopping_payload = _searchapi_get(
            settings,
            engine="google_shopping",
            q=query,
            gl="br",
            hl="pt-br",
        )
        offer_payloads = []
        product_tokens = list(
            dict.fromkeys(
                str(item.get("product_token") or "")
                for item in shopping_payload.get("shopping_results") or []
                if isinstance(item, dict) and item.get("product_token")
            )
        )
        for product_token in product_tokens[:2]:
            try:
                offer_payloads.append(
                    _searchapi_get(
                        settings,
                        engine="google_product_offers",
                        product_token=product_token,
                        gl="br",
                        hl="pt-br",
                    )
                )
            except RuntimeError as exc:
                errors.append(str(exc))
        payloads.extend(offer_payloads)
        payloads.append(shopping_payload)
    except RuntimeError as exc:
        errors.append(str(exc))

    try:
        amazon_payload = _searchapi_get(
            settings,
            engine="amazon_search",
            q=query,
            amazon_domain="amazon.com.br",
            language="pt_BR",
            delivery_country="br",
        )
        payloads.insert(0, _searchapi_amazon_items(amazon_payload))
    except RuntimeError as exc:
        errors.append(str(exc))

    if not payloads:
        raise RuntimeError(errors[0] if errors else "SearchAPI nao retornou dados.")
    return _normalize_payload(payloads, query, "searchapi", limit)


def run_external_market_provider(provider, query):
    providers = {
        "bright_data": ("Bright Data", search_bright_data),
        "apify": ("Apify", search_apify),
        "serper": ("Serper", search_serper),
        "dataforseo": ("DataForSEO", search_dataforseo),
        "searchapi": ("SearchAPI", search_searchapi),
    }
    label, search = providers[provider]
    try:
        items = search(query)
        return {
            "provider": provider,
            "label": label,
            "status": "available" if items else "empty",
            "message": None if items else "A fonte respondeu sem ofertas compativeis.",
            "items": items,
        }
    except Exception as exc:
        print(f" WARNING: {label} indisponivel nesta consulta: {exc}")
        return {
            "provider": provider,
            "label": label,
            "status": "error",
            "message": str(exc),
            "items": [],
        }
