import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache, cached
from cachetools.keys import hashkey

SOURCES = (
    ("Buscape", "https://www.buscape.com.br"),
    ("Zoom", "https://www.zoom.com.br"),
)

TARGET_STORE_ALIASES = {
    "Amazon Brasil": ("amazon", "amazon.com.br"),
    "ComClick": ("comclick", "com click"),
    "Dufrio": ("dufrio",),
    "Friolar": ("friolar", "friolar pecas", "friolar peças"),
    "Gold Service": ("gold service", "goldservice"),
    "Magazine Luiza": ("magazine luiza", "magalu"),
    "Mercado Livre": ("mercado livre", "mercadolivre"),
    "MG Parts": ("mg parts", "mgparts"),
    "Refrigeração Mota": ("refrigeracao mota", "refrigeração mota"),
    "Shopee": ("shopee",),
    "Leroy Merlin": ("leroy", "leroy merlin"),
}

cache = TTLCache(maxsize=100, ttl=900)
cache_lock = RLock()
COMPARISON_STORE_LIMIT = 60
LEAD_TIMEOUT = 8


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
    }


def _as_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return float(str(value).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _seller_from_url(value, fallback=None):
    parsed = urlparse(value or "")
    query = parse_qs(parsed.query)
    seller_id = (query.get("seller_id") or [""])[0].strip()

    if seller_id:
        return seller_id

    if "produto.mercadolivre" in parsed.netloc:
        item_match = re.search(r"/(MLB-\d+)", parsed.path, re.I)
        if item_match:
            return f"{item_match.group(1).upper()}-ML"

    return fallback


def _pretty_seller(value, source_label):
    seller = (value or "").strip()
    if not seller:
        return source_label

    if seller.lower() == source_label.lower():
        return seller

    suffixes = {
        "Magazine Luiza": "Magalu",
        "Mercado Livre": "ML",
        "Shopee": "Shopee",
    }
    suffix = suffixes.get(source_label)
    if suffix and not seller.lower().endswith(f"-{suffix.lower()}"):
        return f"{seller}-{suffix}"

    return seller


@cached(cache, key=lambda lead_url: hashkey("lead", lead_url), lock=cache_lock)
def _lead_redirect_info(lead_url: str):
    try:
        response = requests.get(lead_url, headers=_headers(), timeout=LEAD_TIMEOUT)
        if response.status_code != 200:
            return {}

        soup = BeautifulSoup(response.text, "html.parser")
        next_data = soup.find("script", id="__NEXT_DATA__")
        if not next_data or not next_data.string:
            return {}

        data = json.loads(next_data.string)
        page_props = data.get("props", {}).get("pageProps", {})
        redirect_url = page_props.get("urlToRedirect")
        if not redirect_url:
            return {}

        redirect_response = requests.get(
            redirect_url,
            headers=_headers(),
            timeout=LEAD_TIMEOUT,
            allow_redirects=False,
        )
        location = redirect_response.headers.get("location") or redirect_url
        return {
            "url": location,
            "seller": _seller_from_url(location, page_props.get("rawName")),
        }
    except Exception as e:
        print(f"Erro ao resolver lojista no comparador: {e}")
        return {}


def _enrich_offer_url(product_url, source_label, seller_name):
    if "/lead?" not in product_url:
        seller = _seller_from_url(product_url, seller_name)
        return product_url, _pretty_seller(seller, source_label)

    info = _lead_redirect_info(product_url)
    final_url = info.get("url") or product_url
    seller = info.get("seller") or seller_name
    return final_url, _pretty_seller(seller, source_label)


def _normalize_store(value):
    return "".join(ch for ch in (value or "").lower() if ch.isalnum() or ch.isspace())


def _target_store_label(merchant_name):
    normalized = _normalize_store(merchant_name)
    for label, aliases in TARGET_STORE_ALIASES.items():
        if any(_normalize_store(alias) in normalized for alias in aliases):
            return label
    return None


def _extract_hits(html):
    soup = BeautifulSoup(html, "html.parser")
    next_data = soup.find("script", id="__NEXT_DATA__")
    if not next_data or not next_data.string:
        return []

    data = json.loads(next_data.string)
    return (
        data.get("props", {})
        .get("initialReduxState", {})
        .get("hits", {})
        .get("hits", [])
    )


def _extract_offer_list(html):
    soup = BeautifulSoup(html, "html.parser")
    next_data = soup.find("script", id="__NEXT_DATA__")
    if not next_data or not next_data.string:
        return []

    data = json.loads(next_data.string)
    return (
        data.get("props", {})
        .get("initialReduxState", {})
        .get("offers", {})
        .get("offerList", [])
    )


@cached(cache, key=lambda product_url: hashkey("offers", product_url), lock=cache_lock)
def _product_offer_list(product_url: str):
    try:
        response = requests.get(product_url, headers=_headers(), timeout=8)
        if response.status_code != 200:
            return []
        return _extract_offer_list(response.text)
    except Exception as e:
        print(f"Erro ao buscar ofertas agrupadas em comparador: {e}")
        return []


@cached(cache, key=lambda product_query: hashkey("search", product_query), lock=cache_lock)
def _search_hits(product_query: str):
    hits = []

    for source_name, base_url in SOURCES:
        url = f"{base_url}/search?q={quote_plus(product_query)}"

        try:
            response = requests.get(url, headers=_headers(), timeout=8)
            if response.status_code != 200:
                print(f"{source_name} retornou HTTP {response.status_code}")
                continue

            hits.extend(
                {
                    **item,
                    "_source_name": source_name,
                    "_base_url": base_url,
                }
                for item in _extract_hits(response.text)[:50]
            )
        except Exception as e:
            print(f"Erro ao buscar em {source_name}: {e}")

    return hits


@cached(
    cache,
    key=lambda product_query, limit=10: hashkey("catalog", product_query, limit),
    lock=cache_lock,
)
def scrape_comparison_catalog(product_query: str, limit: int = 10):
    products = []
    seen = set()

    for item in _search_hits(product_query):
        if item.get("type") != "product":
            continue

        name = item.get("name") or item.get("shortName") or item.get("title")
        price = _as_float(item.get("price"))
        relative_url = item.get("url")
        source_id = item.get("sourceId") or item.get("objectId") or name
        store_count = int(item.get("storeCount") or 0)
        best_offer = item.get("bestOffer") or {}

        if not name or not price or not relative_url or store_count < 2:
            continue

        key = str(source_id).strip().lower()
        if key in seen:
            continue
        seen.add(key)

        products.append(
            {
                "name": name.strip(),
                "price": price,
                "store_count": store_count,
                "best_store": best_offer.get("merchantName") or "Comparador",
                "url": urljoin(item["_base_url"], relative_url),
                "brand": "Comparador",
                "category": item.get("categoryName") or product_query,
                "image_url": item.get("image"),
            }
        )

        if len(products) >= limit:
            break

    return sorted(
        products,
        key=lambda item: (-item["store_count"], item["price"], item["name"]),
    )


@cached(
    cache,
    key=lambda product_query, limit=30: hashkey("target_catalog", product_query, limit),
    lock=cache_lock,
)
def scrape_target_catalog(product_query: str, limit: int = 30):
    products = []

    for item in scrape_comparison_catalog(product_query, limit=limit):
        best_by_store = {}

        for offer in _product_offer_list(item["url"]):
            seller_name = offer.get("sellerName") or ""
            store_label = _target_store_label(seller_name)
            if not store_label:
                continue

            price = _as_float(offer.get("price"))
            name = (offer.get("name") or item["name"] or "").strip()
            if not name or not price:
                continue

            final_url, seller = _enrich_offer_url(
                item["url"],
                store_label,
                seller_name.strip() or store_label,
            )

            current = best_by_store.get(seller)
            if current and current["price"] <= price:
                continue

            best_by_store[seller] = {
                "name": name,
                "price": price,
                "store": seller,
                "source": store_label,
                "seller": seller,
                "url": final_url,
                "brand": store_label,
                "category": item.get("category") or product_query,
                "image_url": offer.get("imageUrl") or item.get("image_url"),
            }

        if not best_by_store:
            continue

        offers = sorted(best_by_store.values(), key=lambda offer: offer["price"])
        products.append(
            {
                "name": item["name"],
                "price": offers[0]["price"],
                "store_count": len(offers),
                "best_store": offers[0]["store"],
                "url": offers[0]["url"],
                "brand": "Comparador",
                "category": item.get("category") or product_query,
                "image_url": item.get("image_url"),
                "target_offers": offers,
            }
        )

    return sorted(
        products,
        key=lambda item: (-item["store_count"], item["price"], item["name"]),
    )


def _comparison_candidate(item, product_query):
    best_offer = item.get("bestOffer") or {}
    merchant_name = best_offer.get("merchantName") or item.get("merchantName") or ""
    store_label = _target_store_label(merchant_name)
    name = item.get("name") or item.get("shortName") or item.get("title")
    price = _as_float(item.get("price"))
    relative_url = item.get("url")

    if not name or not price or not relative_url:
        return None

    source_label = store_label or merchant_name.strip() or item.get("_source_name") or "Comparador"

    return {
        "name": name.strip(),
        "price": price,
        "store": merchant_name.strip() or source_label,
        "source": source_label,
        "seller": merchant_name.strip() or source_label,
        "discovery_source": item.get("_source_name") or "Comparador",
        "url": urljoin(item["_base_url"], relative_url),
        "brand": store_label,
        "category": item.get("categoryName") or product_query,
        "image_url": item.get("image"),
    }


def _resolve_comparison_candidate(candidate):
    final_url, seller = _enrich_offer_url(
        candidate["url"],
        candidate["source"],
        candidate["seller"],
    )
    return {**candidate, "url": final_url, "store": seller, "seller": seller}


@cached(
    cache,
    key=lambda product_query: hashkey("comparison", product_query),
    lock=cache_lock,
)
def scrape_comparison_offers(product_query: str):
    """Collect comparator offers once and distribute them by source later."""
    candidates = []
    counts_by_source = {}
    seen = set()

    for item in _search_hits(product_query):
        candidate = _comparison_candidate(item, product_query)
        if not candidate:
            continue

        source = candidate["source"]
        if counts_by_source.get(source, 0) >= COMPARISON_STORE_LIMIT:
            continue

        key = (
            source,
            candidate["name"].lower(),
            round(candidate["price"], 2),
            candidate["url"],
        )
        if key in seen:
            continue

        seen.add(key)
        counts_by_source[source] = counts_by_source.get(source, 0) + 1
        candidates.append(candidate)

    if not candidates:
        return []

    worker_count = min(8, len(candidates))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        resolved = list(executor.map(_resolve_comparison_candidate, candidates))

    return sorted(resolved, key=lambda item: (item["source"], item["price"]))
