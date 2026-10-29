"""Read only the selected product from public manufacturer page hydration."""
import json
from threading import Condition, RLock
from cachetools import TTLCache, cached
from bs4 import BeautifulSoup
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from services.product_resolver import resolve_candidate, normalize_identifier

MANUFACTURER_STORES = {'electrolux', 'midea', 'consul', 'brastemp'}
_sitemap_cache = TTLCache(maxsize=8, ttl=3600)
_sitemap_condition = Condition(RLock())


@cached(_sitemap_cache, condition=_sitemap_condition)
def manufacturer_sitemap(store, timeout=12):
    from curl_cffi.requests import Session
    host = ('loja.' if store == 'electrolux' else 'www.') + store + '.com.br'
    root = 'https://' + host + '/sitemap.xml'
    pending, visited, products = [root], set(), set()
    with Session(impersonate='chrome') as session:
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)
            response = session.get(url, timeout=(5, timeout))
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'xml')
            if soup.find('sitemapindex'):
                for node in soup.find_all('loc'):
                    child = node.get_text(strip=True)
                    if urlsplit(child).hostname == host and ('product' in urlsplit(child).path or 'sitemap' in urlsplit(child).path and '/sitemap/' not in urlsplit(child).path):
                        pending.append(child)
            elif soup.find('urlset'):
                for node in soup.find_all('loc'):
                    product_url = node.get_text(strip=True)
                    if urlsplit(product_url).hostname == host and urlsplit(product_url).path.rstrip('/').endswith('/p'):
                        products.add(product_url)
            else:
                raise ValueError('Sitemap sem estrutura XML de produtos')
    return tuple(sorted(products))


def sitemap_candidates(store, term, timeout=12):
    from services.product_resolver import ProductIdentity
    identity = ProductIdentity(sku=term, model=term)
    candidates = []
    for url in manufacturer_sitemap(store, timeout):
        name = urlsplit(url).path.strip('/').removesuffix('/p').replace('-', ' ')
        item = {'name': name, 'url': url, 'discovery_method': 'manufacturer_sitemap'}
        if resolve_candidate(identity, item).accepted:
            candidates.append(item)
    return candidates


def selected_product(response, source, identity, store_label):
    if source.store not in MANUFACTURER_STORES:
        return None
    raw = response.css('script#__NEXT_DATA__::text').get()
    if not raw:
        return None
    try:
        product = json.loads(raw)['props']['pageProps']['data']['product']
    except (ValueError, KeyError, TypeError):
        return None
    group = product.get('isVariantOf') or {}
    name = ' '.join(filter(None, (group.get('name'), product.get('name'))))
    candidate = {'name': name, 'url': response.url, 'ean': product.get('gtin')}
    # Midea publishes voltage-specific suffixes 1/2 for this declared family.
    # Never use arbitrary prefix matching (e.g. IF44 must not match IF44S).
    if source.store == 'midea' and identity.sku.upper() == 'MD-RT411FGF01':
        if normalize_identifier(product.get('name', '').split(' - ')[0]) in {'mdrt411fgf011', 'mdrt411fgf012'}:
            candidate['model'] = identity.sku
    if not resolve_candidate(identity, candidate).accepted:
        return None
    offers = []
    for offer in (product.get('offers') or {}).get('offers', []):
        seller = offer.get('seller') or {}
        seller_id = str(seller.get('identifier') or '')
        seller_name = seller.get('name') or (store_label if seller_id == '1' else None)
        if not seller_name:
            # Unnamed partner identity is retained rather than merged into the store.
            seller_name = f'{seller_id}-{store_label}' if seller_id else None
        if not seller_name:
            continue
        parts = urlsplit(response.url)
        params = dict(parse_qsl(parts.query))
        params.update({'skuId': str(product['sku']), 'seller_id': seller_id})
        offers.append({**offer, '@type': 'Offer', 'seller': {'name': seller_name},
                       'url': urlunsplit((*parts[:3], urlencode(params), ''))})
    if not offers and product.get('availableQuantity') == 0:
        offers.append({'@type': 'Offer', 'availability': 'https://schema.org/OutOfStock',
                       'seller': {'name': store_label}, 'url': response.url})
    return {'@type': 'Product', **candidate, 'brand': product.get('brand'),
            'sku': product.get('sku'), 'image': product.get('image'),
            'offers': offers}
