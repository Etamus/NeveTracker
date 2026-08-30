import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache, cached

from services.http_client import create_resilient_session


DETAIL_CACHE = TTLCache(maxsize=600, ttl=3600)
DETAIL_CACHE_LOCK = RLock()
DETAIL_WORKERS = 8


def normalize_postal_code(value):
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 8 else None


def _session():
    return create_resilient_session(pool_size=10)


def _headers(url, accept="text/html"):
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": url,
    }


def _blocked_page_response(response):
    if response.status_code in {401, 403, 408, 425, 429, 500, 502, 503, 504}:
        return True
    text = response.text or ""
    if response.status_code != 200 or len(text) < 500:
        return True
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1) if title_match else "")
    return bool(
        re.search(
            r"access denied|captcha|cloudflare|robot check|temporarily blocked",
            title,
            re.I,
        )
    )


def _as_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    cleaned = re.sub(r"[^\d,.]", "", str(value))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _valid_price(value, reference):
    return bool(
        value
        and value > 0
        and (not reference or reference * 0.45 <= value <= reference * 3)
    )


def _parse_payment_details(text, current_price):
    normalized = re.sub(r"\s+", " ", text or "")
    money = r"([\d.]+(?:\s*,\s*\d{2})?)"
    cash_candidates = []

    patterns = (
        rf"R\$\s*{money}\s*(?:\d+(?:,\d+)?%\s*)?(?:a|à)\s+vista",
        rf"R\$\s*{money}\s*(?:via|no|com)\s+pix",
        rf"(?:pix|boleto)[^R$]{{0,45}}R\$\s*{money}",
        rf"R\$\s*{money}[^R$]{{0,20}}(?:pix|boleto)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, re.I):
            candidate = _as_float(match.group(1))
            if _valid_price(candidate, current_price):
                cash_candidates.append(candidate)

    cash_price = min(cash_candidates) if cash_candidates else current_price
    installment_candidates = []
    for match in re.finditer(
        rf"(?:R\$\s*{money}\s*)?(?:ou\s*)?(?:em\s*)?(\d{{1,2}})\s*x\s*(?:de\s*)?R\$\s*{money}",
        normalized,
        re.I,
    ):
        stated_total = _as_float(match.group(1)) if match.group(1) else None
        count = int(match.group(2))
        installment_value = _as_float(match.group(3))
        total = stated_total or (count * installment_value if installment_value else None)
        if count > 0 and _valid_price(total, cash_price or current_price):
            installment_candidates.append((total, count, installment_value))

    pix_pair = re.search(
        rf"R\$\s*{money}\s*(?:ou|por)\s*R\$\s*{money}\s*(?:via|no|com)\s+pix",
        normalized,
        re.I,
    )
    if pix_pair:
        regular = _as_float(pix_pair.group(1))
        pix = _as_float(pix_pair.group(2))
        if _valid_price(pix, current_price):
            cash_price = min(cash_price or pix, pix)
        if _valid_price(regular, cash_price):
            installment_candidates.append((regular, 1, regular))

    installment_price = None
    installment_count = None
    installment_value = None
    if installment_candidates:
        installment_price, installment_count, installment_value = max(
            installment_candidates,
            key=lambda candidate: (candidate[1], candidate[0]),
        )

    if installment_price is None and current_price and cash_price and current_price > cash_price:
        installment_price = current_price
        installment_count = 1
        installment_value = current_price

    difference = None
    if cash_price and installment_price:
        difference = round(max(0, installment_price - cash_price), 2)

    return {
        "cash_price": round(cash_price, 2) if cash_price else None,
        "installment_price": round(installment_price, 2) if installment_price else None,
        "installment_count": installment_count,
        "installment_value": round(installment_value, 2) if installment_value else None,
        "payment_difference": difference,
    }


def _jsonld_items(soup):
    items = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        values = data if isinstance(data, list) else [data]
        for value in values:
            if not isinstance(value, dict):
                continue
            graph = value.get("@graph")
            items.extend(graph if isinstance(graph, list) else [value])
    return items


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _quantity_days(value):
    if not isinstance(value, dict):
        return (None, None)
    minimum = _as_float(value.get("minValue") or value.get("value"))
    maximum = _as_float(value.get("maxValue") or value.get("value"))
    unit = str(value.get("unitCode") or value.get("unitText") or "day").lower()
    factor = 1 / 24 if unit in {"h", "hour", "hours", "hora", "horas"} else 1
    return (
        math.ceil(minimum * factor) if minimum is not None else None,
        math.ceil(maximum * factor) if maximum is not None else None,
    )


def _jsonld_offer_details(soup):
    offers = []
    for item in _jsonld_items(soup):
        item_type = item.get("@type") if isinstance(item, dict) else None
        if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
            offers.extend(_as_list(item.get("offers")))
        elif item_type in {"Offer", "AggregateOffer"}:
            offers.append(item)

    availability = None
    shipping_options = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        availability_value = offer.get("availability")
        if availability_value:
            availability = str(availability_value).rsplit("/", 1)[-1]
        for details in _as_list(offer.get("shippingDetails")):
            if not isinstance(details, dict) or details.get("doesNotShip") is True:
                continue
            rate = details.get("shippingRate")
            if isinstance(rate, dict):
                rate = rate.get("value") or rate.get("maxValue")
            rate = _as_float(rate)
            delivery = details.get("deliveryTime") or {}
            handling_min, handling_max = _quantity_days(delivery.get("handlingTime"))
            transit_min, transit_max = _quantity_days(delivery.get("transitTime"))
            delivery_min = sum(value or 0 for value in (handling_min, transit_min)) or None
            delivery_max = sum(value or 0 for value in (handling_max, transit_max)) or None
            shipping_options.append(
                {
                    "shipping_cost": rate,
                    "delivery_min_days": delivery_min,
                    "delivery_max_days": delivery_max,
                    "handling_min_days": handling_min,
                    "handling_max_days": handling_max,
                    "logistics_source": "jsonld",
                }
            )

    selected = min(
        shipping_options,
        key=lambda option: (
            option.get("shipping_cost") is None,
            option.get("shipping_cost") or 0,
            option.get("delivery_max_days") or 9999,
        ),
        default={},
    )
    if availability:
        selected["availability"] = availability
    return selected


def _payment_scope_text(soup):
    selectors = (
        ".product-form",
        ".product-info-main",
        ".product__info-container",
        ".produto-principal",
        "main .product-info",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if "R$" in text:
                return text
    return soup.get_text(" ", strip=True)


def _loja_integrada_shipping(session, soup, html, url, postal_code):
    product_match = re.search(r"var\s+PRODUTO_ID\s*=\s*['\"](\d+)", html)
    endpoint_match = re.search(
        r"URL_PRODUTO_FRETE_CALCULAR\s*=\s*['\"]([^'\"]+)", html
    )
    if not product_match or not endpoint_match:
        return {}

    response = session.get(
        endpoint_match.group(1),
        params={
            "cep": postal_code,
            "produto_id": product_match.group(1),
            "quantidade": 1,
        },
        headers={**_headers(url, "application/json"), "X-Requested-With": "XMLHttpRequest"},
        timeout=12,
    )
    if response.status_code != 200:
        return {}
    try:
        rates = response.json()
    except ValueError:
        return {}
    if not isinstance(rates, list):
        return {}

    options = []
    for rate in rates:
        if not isinstance(rate, dict) or rate.get("msgErro"):
            continue
        method_name = str(rate.get("name") or rate.get("originalName") or "")
        if re.search(r"retir|pickup|coleta", method_name, re.I):
            continue
        cost = _as_float(rate.get("price"))
        days = _as_float(rate.get("deliveryTime"))
        options.append((cost if cost is not None else math.inf, days or math.inf, rate))
    if not options:
        return {}
    _, _, selected = min(options, key=lambda option: (option[0], option[1]))
    days = _as_float(selected.get("deliveryTime"))
    return {
        "shipping_cost": _as_float(selected.get("price")),
        "shipping_method": selected.get("name") or selected.get("originalName"),
        "delivery_min_days": math.ceil(days) if days is not None else None,
        "delivery_max_days": math.ceil(days) if days is not None else None,
        "logistics_source": "loja_integrada",
    }


def _dufrio_shipping(session, soup, url, postal_code):
    if "shippingQuote" not in str(soup):
        return {}
    params = {}
    for selector in ("#product_addtocart_form", "#product-shipping-form"):
        form = soup.select_one(selector)
        if not form:
            continue
        for field in form.select("[name]"):
            params[field.get("name")] = field.get("value") or ""
    if not params.get("product") and not params.get("product_id"):
        return {}
    params["postcode"] = postal_code
    response = session.get(
        f"{urlparse(url).scheme}://{urlparse(url).netloc}/catalog/product/shippingQuote",
        params=params,
        headers=_headers(url, "application/json"),
        timeout=12,
    )
    if response.status_code != 200:
        return {}
    try:
        groups = response.json()
    except ValueError:
        return {}
    rates = [rate for values in groups.values() for rate in values] if isinstance(groups, dict) else []
    if not rates:
        return {}
    selected = min(
        rates,
        key=lambda rate: (_as_float(rate.get("price")) is None, _as_float(rate.get("price")) or 0),
    )
    title = " ".join(
        str(value or "")
        for value in (selected.get("method_title"), selected.get("method_description"))
    )
    day_match = re.search(r"(\d+)\s*dias?", title, re.I)
    days = int(day_match.group(1)) if day_match else None
    return {
        "shipping_cost": _as_float(selected.get("price")),
        "shipping_method": selected.get("method_title") or selected.get("carrier_title"),
        "delivery_min_days": days,
        "delivery_max_days": days,
        "logistics_source": "dufrio",
    }


def _postal_state(session, postal_code):
    try:
        response = session.get(
            f"https://viacep.com.br/ws/{postal_code}/json/",
            headers={"Accept": "application/json", "User-Agent": _headers("")["User-Agent"]},
            timeout=8,
        )
        if response.status_code == 200:
            state = str(response.json().get("uf") or "").upper()
            if re.fullmatch(r"[A-Z]{2}", state):
                return state
    except (requests.RequestException, ValueError):
        pass
    return None


def _shopify_shipping(session, url, postal_code):
    product_url = url.split("?", 1)[0].rstrip("/")
    product_response = session.get(
        f"{product_url}.js",
        headers=_headers(url, "application/json"),
        timeout=12,
    )
    if product_response.status_code != 200:
        return {}
    try:
        product = product_response.json()
    except ValueError:
        return {}
    variants = product.get("variants") or []
    variant = next((item for item in variants if item.get("available")), None)
    if not variant:
        return {"availability": "OutOfStock", "logistics_source": "shopify"}

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    add_response = session.post(
        f"{base_url}/cart/add.js",
        data={"id": variant.get("id"), "quantity": 1},
        headers=_headers(url, "application/json"),
        timeout=12,
    )
    if add_response.status_code != 200:
        return {}
    state = _postal_state(session, postal_code)
    params = {
        "shipping_address[zip]": postal_code,
        "shipping_address[country]": "BR",
    }
    if state:
        params["shipping_address[province]"] = state
    response = session.get(
        f"{base_url}/cart/shipping_rates.json",
        params=params,
        headers=_headers(url, "application/json"),
        timeout=15,
    )
    if response.status_code != 200:
        return {}
    try:
        rates = response.json().get("shipping_rates") or []
    except (AttributeError, ValueError):
        return {}
    rates = [
        rate
        for rate in rates
        if not re.search(r"retir|pickup|coleta", str(rate.get("name") or ""), re.I)
    ]
    if not rates:
        return {}
    selected = min(
        rates,
        key=lambda rate: (_as_float(rate.get("price")) is None, _as_float(rate.get("price")) or 0),
    )
    delivery_days = selected.get("delivery_days") or []
    minimum = _as_float(delivery_days[0]) if delivery_days else None
    maximum = _as_float(delivery_days[-1]) if delivery_days else minimum
    return {
        "shipping_cost": _as_float(selected.get("price")),
        "shipping_method": selected.get("name"),
        "delivery_min_days": math.ceil(minimum) if minimum is not None else None,
        "delivery_max_days": math.ceil(maximum) if maximum is not None else None,
        "availability": "InStock",
        "logistics_source": "shopify",
    }


def _tray_shipping(session, soup, url, postal_code):
    trigger = soup.select_one('[data-app="product.shipping-calculate"][data-url]')
    template = trigger.get("data-url") if trigger else None
    if not template or template.count("%s") < 4:
        return {}
    try:
        endpoint = template % (postal_code[:5], postal_code[5:], "1", "")
    except (TypeError, ValueError):
        return {}
    response = session.get(
        urljoin(url, endpoint),
        headers=_headers(url),
        timeout=15,
    )
    if response.status_code != 200:
        return {}
    rate_soup = BeautifulSoup(response.text, "html.parser")
    rates = []
    for row in rate_soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue
        text = row.get_text(" ", strip=True)
        if re.search(r"retir|pickup|coleta", text, re.I):
            continue
        cost_match = re.search(r"R\$\s*[\d.]+(?:,\d{2})?", text)
        day_match = re.search(r"(\d+)\s*dia", text, re.I)
        cost = _as_float(cost_match.group()) if cost_match else None
        if cost is None:
            continue
        method = cells[1].get_text(" ", strip=True) if len(cells) > 1 else None
        days = int(day_match.group(1)) if day_match else None
        rates.append((cost, days or 9999, method))
    if not rates:
        return {}
    cost, days, method = min(rates, key=lambda rate: (rate[0], rate[1]))
    return {
        "shipping_cost": cost,
        "shipping_method": method,
        "delivery_min_days": days if days != 9999 else None,
        "delivery_max_days": days if days != 9999 else None,
        "logistics_source": "tray",
    }


def _mercado_livre_shipping(session, url, postal_code):
    access_token = os.getenv("MERCADO_LIVRE_ACCESS_TOKEN", "").strip()
    if not access_token:
        return {}
    item_match = re.search(r"\b(MLB-?\d+)\b", url or "", re.I)
    if not item_match:
        return {}
    item_id = item_match.group(1).replace("-", "").upper()
    endpoint = f"https://api.mercadolibre.com/items/{item_id}/shipping_options"
    response = session.get(
        endpoint,
        params={"zip_code": postal_code},
        headers={"Authorization": f"Bearer {access_token}", **_headers(url, "application/json")},
        timeout=12,
    )
    if response.status_code != 200:
        return {}
    try:
        options = response.json().get("options", [])
    except (AttributeError, ValueError):
        return {}
    if not options:
        return {}

    def option_days(option):
        estimate = option.get("estimated_delivery_time") or {}
        shipping = _as_float(estimate.get("shipping"))
        unit = str(estimate.get("unit") or "hour").lower()
        if shipping is not None:
            return math.ceil(shipping / 24) if "hour" in unit else math.ceil(shipping)
        date_value = estimate.get("date")
        if date_value:
            try:
                target = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
                return max(0, math.ceil((target - datetime.now(timezone.utc)).total_seconds() / 86400))
            except ValueError:
                return None
        return None

    selected = min(
        options,
        key=lambda option: (
            _as_float(option.get("cost")) is None,
            _as_float(option.get("cost")) or 0,
            option_days(option) or 9999,
        ),
    )
    days = option_days(selected)
    return {
        "shipping_cost": _as_float(selected.get("cost")),
        "shipping_method": selected.get("name"),
        "delivery_min_days": days,
        "delivery_max_days": days,
        "logistics_source": "mercado_livre_api",
    }


@cached(
    DETAIL_CACHE,
    key=lambda url, current_price, postal_code=None: (url, round(current_price or 0, 2), postal_code),
    lock=DETAIL_CACHE_LOCK,
)
def _fetch_details(url, current_price, postal_code=None):
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {}

    session = _session()
    try:
        response = session.get(url, headers=_headers(url), timeout=12)
        if _blocked_page_response(response):
            if postal_code and "mercadolivre.com" in parsed.netloc:
                return _mercado_livre_shipping(session, url, postal_code)
            return {}
        if response.status_code != 200 or not response.text:
            if postal_code and "mercadolivre.com" in parsed.netloc:
                return _mercado_livre_shipping(session, url, postal_code)
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        text = _payment_scope_text(soup)
        details = {
            **_parse_payment_details(text, current_price),
            **_jsonld_offer_details(soup),
        }

        if postal_code:
            shipping = {}
            if re.search(r"friolar|goldservice", parsed.netloc, re.I):
                shipping = _loja_integrada_shipping(
                    session, soup, response.text, url, postal_code
                )
            elif "dufrio.com.br" in parsed.netloc:
                shipping = _dufrio_shipping(session, soup, url, postal_code)
            elif "mgparts.com.br" in parsed.netloc:
                shipping = _shopify_shipping(session, url, postal_code)
            elif "comclick.com.br" in parsed.netloc:
                shipping = _tray_shipping(session, soup, url, postal_code)
            elif "mercadolivre.com" in parsed.netloc:
                shipping = _mercado_livre_shipping(session, url, postal_code)
            details.update({key: value for key, value in shipping.items() if value is not None})
            details["destination_zip"] = postal_code
            if shipping or details.get("delivery_max_days") is not None:
                details["logistics_captured_at"] = datetime.utcnow().isoformat()

        return {key: value for key, value in details.items() if value is not None}
    except requests.RequestException as exc:
        print(f" WARNING: Nao foi possivel enriquecer {parsed.netloc}: {exc}")
        return {}
    finally:
        session.close()


def enrich_offer(item, postal_code=None):
    postal_code = normalize_postal_code(postal_code)
    details = _fetch_details(item.get("url"), item.get("price"), postal_code)
    enriched = {**item, **details}
    if enriched.get("cash_price"):
        enriched["price"] = enriched["cash_price"]
    if enriched.get("installment_price") and enriched.get("cash_price"):
        enriched["payment_difference"] = round(
            max(0, enriched["installment_price"] - enriched["cash_price"]), 2
        )
    return enriched


def enrich_offers(items, postal_code=None):
    if not items:
        return []
    worker_count = min(DETAIL_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(lambda item: enrich_offer(item, postal_code), items))
