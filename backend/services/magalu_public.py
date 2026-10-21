"""Equivalent public Magalu storefront URLs; offers retain their actual channel URL."""
import re
import json
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

MIRROR_HOST = "www.magazinevoce.com.br"


def public_storefront_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.magazineluiza.com.br", "m.magazineluiza.com.br", "magazineluiza.com.br"}:
        return None
    if not (re.search(r"/p/[a-z0-9]+/", parsed.path, re.I) or parsed.path.startswith("/busca/")):
        return None
    return urlunparse(("https", MIRROR_HOST, "/magazineoficialesbr" + parsed.path, "", parsed.query, ""))


def is_public_storefront(url):
    return urlparse(url).hostname in {MIRROR_HOST, "magazinevoce.com.br"}


def embedded_seller_offers(response, identity):
    """Read only the selected PDP's seller offers, never recommendations."""
    from services.product_resolver import resolve_candidate
    from services.structured_products import number
    try:
        data = json.loads(response.css('script#__NEXT_DATA__::text').get() or '{}')
        product = data['props']['pageProps']['data']['product']
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(product, dict) or not resolve_candidate(identity, {"name": product.get("title")}).accepted:
        return []
    rows = product.get('offers') or [product]
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        seller = row.get('seller') or {}
        prices = row.get('price') or {}
        if not isinstance(seller, dict) or not isinstance(prices, dict):
            continue
        seller_id = str(seller.get('id') or '')
        cash = number(prices.get('bestPrice')) or number(prices.get('price'))
        regular = number(prices.get('price'))
        if not seller_id or cash is None or seller_id in seen or prices.get('currency', 'BRL') != 'BRL':
            continue
        seen.add(seller_id)
        parsed = urlparse(response.url)
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k != 'seller_id']
        query.append(('seller_id', seller_id))
        url = urlunparse((parsed.scheme, parsed.netloc, re.sub('/+', '/', parsed.path), '', urlencode(query), ''))
        availability = row.get('available')
        if availability is None and seller_id == (product.get('seller') or {}).get('id'):
            availability = product.get('available')
        result.append({'@type': 'Offer', 'seller': {'name': seller_id + '-Magalu'},
                       'name': product['title'],
                       'price': cash, 'priceCurrency': 'BRL', 'url': url,
                       'cash_price': cash, 'installment_price': regular,
                       'payment_difference': regular - cash if regular is not None else None,
                       'availability': 'OutOfStock' if availability is False else 'InStock' if availability is True else None})
    return result
