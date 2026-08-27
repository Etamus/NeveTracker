from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from cachetools import TTLCache, cached
from cachetools.keys import hashkey

from services.http_client import create_resilient_session
from services.store_registry import find_target_store


SEARCH_URL = "https://www.bing.com/shop"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
RESULT_CACHE = TTLCache(maxsize=300, ttl=1800)
CACHE_LOCK = RLock()


def _headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Referer": "https://www.bing.com/",
    }


def _as_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    cleaned = re.sub(r"[^\d,.]", "", str(value))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        price = float(cleaned)
    except ValueError:
        return None
    return price if price > 0 else None


def _price_from_card(card):
    selectors = (
        ".br-focusPrice",
        ".pd-price.br-standardPrice",
        ".br-standardPrice",
        ".pd-price",
        "[class*='currentPrice']",
        "[class*='salePrice']",
    )
    for selector in selectors:
        node = card.select_one(selector)
        price = _as_float(node.get_text(" ", strip=True) if node else None)
        if price:
            return price

    text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    prices = []
    for match in re.finditer(r"R\$\s*[\d.]+(?:\s*,\s*\d{2})?", text, re.I):
        before = text[max(0, match.start() - 16):match.start()]
        after = text[match.end():match.end() + 24]
        if re.search(r"\d+\s*x\s*(?:de)?\s*$", before, re.I):
            continue
        if re.search(r"(?:por m[eê]s|parcela|sem juros)", after, re.I):
            continue
        price = _as_float(match.group())
        if price:
            prices.append(price)
    return min(prices) if prices else None


def _card_for_name(name_node):
    candidate = name_node
    fallback = name_node.parent
    for _ in range(7):
        candidate = candidate.parent
        if candidate is None:
            break
        if candidate.name in {"li", "article"} or candidate.get("data-entityid"):
            return candidate
        classes = " ".join(candidate.get("class") or [])
        if re.search(r"(?:product|item|slide|result)", classes, re.I):
            fallback = candidate
    return fallback


def _product_title(card, name_node):
    candidates = []
    for image in card.select("img[alt]"):
        value = re.sub(r"\s+", " ", image.get("alt") or "").strip()
        if len(value) > 8 and value.lower() not in {"imagem", "image"}:
            candidates.append(value)
    visible_name = re.sub(r"\s+", " ", name_node.get_text(" ", strip=True)).strip()
    if visible_name:
        candidates.append(visible_name)
    return max(candidates, key=len, default="")


def _seller_from_card(card):
    selectors = (
        ".br-sellerName",
        ".br-pdMerchantName",
        ".br-pdSeller",
        "[class*='sellerName']",
        "[class*='merchantName']",
        "[class*='seller']",
        "[class*='merchant']",
    )
    for selector in selectors:
        node = card.select_one(selector)
        if not node:
            continue
        seller = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        seller = re.sub(r"^(?:vendido\s+)?(?:por|de)\s+", "", seller, flags=re.I)
        if 1 < len(seller) <= 100:
            return seller

    card_text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
    store = find_target_store(card_text)
    return store["label"] if store else None


def _product_url(card, name_node):
    link = name_node.find_parent("a", href=True)
    if not link:
        links = card.select("a[href]")
        link = next(
            (
                candidate
                for candidate in links
                if re.search(r"/shop/(?:product|productpage)|product", candidate.get("href") or "", re.I)
            ),
            links[0] if links else None,
        )
    href = link.get("href") if link else None
    return urljoin("https://www.bing.com", href) if href else ""


def _image_url(card):
    for image in card.select("img"):
        for attribute in ("data-src", "data-original", "src"):
            value = image.get(attribute)
            if value and not value.startswith("data:"):
                return urljoin("https://www.bing.com", value)
    return None


def _logistics_details(card_text):
    normalized = re.sub(r"\s+", " ", card_text or "")
    shipping_cost = None
    if re.search(r"(?:frete|entrega)\s+gr[aá]tis", normalized, re.I):
        shipping_cost = 0.0
    else:
        shipping_match = re.search(
            r"(?:frete|entrega)[^\dR$]{0,20}R\$\s*([\d.,]+)",
            normalized,
            re.I,
        )
        if shipping_match:
            shipping_cost = _as_float(shipping_match.group(1))

    days_match = re.search(
        r"(?:em|entre|prazo de)\s+(\d{1,3})(?:\s*(?:a|at[eé]|-)\s*(\d{1,3}))?\s+dias?",
        normalized,
        re.I,
    )
    minimum_days = int(days_match.group(1)) if days_match else None
    maximum_days = int(days_match.group(2) or days_match.group(1)) if days_match else None
    return {
        "shipping_cost": shipping_cost,
        "shipping_method": "Frete gratis" if shipping_cost == 0 else None,
        "delivery_min_days": minimum_days,
        "delivery_max_days": maximum_days,
        "logistics_source": "bing_shopping" if shipping_cost is not None or days_match else None,
    }


def _payment_details(card_text, cash_price):
    match = re.search(
        r"(\d{1,2})\s*x\s*(?:de\s*)?R\$\s*([\d.,]+)",
        card_text or "",
        re.I,
    )
    if not match:
        return {"cash_price": cash_price}
    count = int(match.group(1))
    installment_value = _as_float(match.group(2))
    total = round(count * installment_value, 2) if installment_value else None
    if not total or total < cash_price:
        return {"cash_price": cash_price}
    return {
        "cash_price": cash_price,
        "installment_price": total,
        "installment_count": count,
        "installment_value": installment_value,
        "payment_difference": round(total - cash_price, 2),
    }


def _matches_query(title, url, product_query):
    query_skus = re.findall(r"\bW\d{6,}\b|\b\d{8,}\b", product_query.upper())
    searchable = f"{title} {url}".upper()
    if query_skus:
        return all(sku in searchable for sku in query_skus)

    query_tokens = set(re.findall(r"[a-z0-9]+", product_query.lower()))
    result_tokens = set(re.findall(r"[a-z0-9]+", f"{title} {url}".lower()))
    if not query_tokens:
        return False
    shared = query_tokens.intersection(result_tokens)
    return len(shared) >= max(1, (len(query_tokens) + 1) // 2)


def _parse_results(html, product_query):
    soup = BeautifulSoup(html or "", "html.parser")
    products = []
    seen = set()
    for name_node in soup.select(".br-pdItemName"):
        card = _card_for_name(name_node)
        if not card:
            continue
        title = _product_title(card, name_node)
        price = _price_from_card(card)
        seller = _seller_from_card(card)
        url = _product_url(card, name_node)
        if (
            not title
            or not price
            or not seller
            or not url
            or not _matches_query(title, url, product_query)
        ):
            continue

        store = find_target_store(seller, url, card.get_text(" ", strip=True))
        source = store["label"] if store else seller
        identity = (title.lower(), round(price, 2), seller.lower(), url)
        if identity in seen:
            continue
        seen.add(identity)
        card_text = card.get_text(" ", strip=True)
        products.append(
            {
                "name": title,
                "price": price,
                "store": seller,
                "seller": seller,
                "source": source,
                "url": url,
                "brand": source,
                "category": product_query,
                "image_url": _image_url(card),
                "discovery_source": "bing_shopping",
                **{
                    key: value
                    for key, value in _payment_details(card_text, price).items()
                    if value is not None
                },
                **{
                    key: value
                    for key, value in _logistics_details(card_text).items()
                    if value is not None
                },
            }
        )
    return products


def _duckduckgo_destination(href):
    absolute_url = urljoin("https://duckduckgo.com", href or "")
    parsed = urlparse(absolute_url)
    if parsed.netloc.endswith("duckduckgo.com"):
        destination = (parse_qs(parsed.query).get("uddg") or [""])[0]
        if destination:
            absolute_url = unquote(destination)
    parsed = urlparse(absolute_url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return absolute_url


def _jsonld_products(soup):
    products = []

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        if not isinstance(value, dict):
            return
        item_type = value.get("@type")
        item_types = item_type if isinstance(item_type, list) else [item_type]
        if "Product" in item_types:
            products.append(value)
        collect(value.get("@graph"))

    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            collect(json.loads(raw))
        except (TypeError, ValueError):
            continue
    return products


def _offer_values(product):
    offers = product.get("offers") if isinstance(product, dict) else None
    offers = offers if isinstance(offers, list) else [offers]
    return [offer for offer in offers if isinstance(offer, dict)]


def _product_page_offer(candidate):
    candidate_url, search_title, product_query = candidate
    configured_store = find_target_store(candidate_url, search_title)
    if not configured_store:
        return None

    try:
        session = create_resilient_session(pool_size=4)
        response = session.get(
            candidate_url,
            headers=_headers(),
            timeout=(5, 15),
        )
        if response.status_code != 200:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    product_records = _jsonld_products(soup)
    product = next(
        (
            item
            for item in product_records
            if product_query.lower() in str(item.get("name") or "").lower()
        ),
        product_records[0] if product_records else {},
    )
    offers = _offer_values(product)
    offer = min(
        offers,
        key=lambda item: _as_float(
            item.get("price") or item.get("lowPrice") or item.get("highPrice")
        ) or float("inf"),
        default={},
    )
    price = _as_float(offer.get("price") or offer.get("lowPrice") or offer.get("highPrice"))
    if not price:
        price_node = (
            soup.select_one('meta[property="product:price:amount"][content]')
            or soup.select_one('[itemprop="price"][content]')
        )
        price = _as_float(price_node.get("content") if price_node else None)
    if not price:
        return None

    title = re.sub(
        r"\s+",
        " ",
        str(
            product.get("name")
            or (soup.select_one('meta[property="og:title"][content]') or {}).get("content")
            or search_title
        ),
    ).strip()
    if not title:
        return None

    seller_data = offer.get("seller") or product.get("brand") or {}
    if isinstance(seller_data, dict):
        seller = seller_data.get("name")
    else:
        seller = str(seller_data or "").strip()
    seller = seller or configured_store["label"]
    image = product.get("image")
    if isinstance(image, list):
        image = next((value for value in image if value), None)
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    if not image:
        image_node = soup.select_one('meta[property="og:image"][content]')
        image = image_node.get("content") if image_node else None

    availability_value = offer.get("availability")
    availability = str(availability_value).rsplit("/", 1)[-1] if availability_value else None
    return {
        "name": title,
        "price": price,
        "cash_price": price,
        "store": seller,
        "seller": seller,
        "source": configured_store["label"],
        "url": response.url,
        "brand": configured_store["label"],
        "category": product_query,
        "image_url": image,
        "availability": availability,
        "discovery_source": "duckduckgo_product_page",
    }


def _discover_product_pages(product_query):
    try:
        session = create_resilient_session(pool_size=4)
        response = session.get(
            DUCKDUCKGO_URL,
            params={"q": f'"{product_query}" comprar'},
            headers=_headers(),
            timeout=(5, 15),
        )
        if response.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    seen_urls = set()
    for result in soup.select(".result"):
        link = result.select_one(".result__a[href]")
        if not link:
            continue
        destination = _duckduckgo_destination(link.get("href"))
        title = link.get_text(" ", strip=True)
        if (
            not destination
            or destination in seen_urls
            or not find_target_store(destination, title)
        ):
            continue
        seen_urls.add(destination)
        candidates.append((destination, title, product_query))
        if len(candidates) >= 12:
            break

    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as executor:
        products = list(executor.map(_product_page_offer, candidates))
    return [product for product in products if product]


def _fetch_results(product_query):
    variants = (
        {"q": product_query, "FORM": "SHOPTB"},
        {"q": product_query, "FORM": "SHOPTB", "mkt": "pt-BR"},
    )
    for params in variants:
        try:
            session = create_resilient_session(pool_size=4)
            response = session.get(
                SEARCH_URL,
                params=params,
                headers=_headers(),
                timeout=(5, 15),
            )
            if response.status_code != 200:
                continue
            products = _parse_results(response.text, product_query)
            if products:
                return products
        except Exception as exc:
            print(f"Bing Shopping falhou: {exc}")
    return _discover_product_pages(product_query)


@cached(
    RESULT_CACHE,
    key=lambda product_query: hashkey(product_query.strip().lower()),
    lock=CACHE_LOCK,
)
def search_bing_shopping(product_query: str):
    """Discover supplemental shopping offers without credentials or paid APIs."""
    if not product_query.strip():
        return []
    products = _fetch_results(product_query.strip())
    if products:
        print(f"Bing Shopping retornou {len(products)} ofertas complementares.")
    else:
        print("Bing Shopping sem retorno; mantendo fontes diretas e comparadores.")
    return sorted(products, key=lambda product: (product["source"], product["price"]))
