"""Mercado Livre HTML and embedded application-state extraction."""

from datetime import datetime
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
import chompjs

from services.mercado_livre_browser import allowed_url, fetch_public_html
from services.product_resolver import resolve_candidate
from services.product_resolver import _pack_quantity, MatchResult
from services.structured_products import number, offer_available, party_name


def embedded_states(soup):
    for script in soup.select('script'):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 12_000_000:
            continue
        if script.get('id') in {'__PRELOADED_STATE__', '__NEXT_DATA__'} or script.get('type') == 'application/json':
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                pass
        for match in re.finditer(r'(?:window\.)?__(?:PRELOADED_STATE|INITIAL_STATE)__\s*=\s*', raw):
            try:
                yield chompjs.parse_js_object(raw[match.end():])
            except ValueError:
                continue


def _walk(value):
    if isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key.lower() not in {'recommendations', 'recommended_products', 'related_products', 'tracking', 'analytics'}:
                yield from _walk(child)


def _product_url(value, base):
    url = urljoin(base, str(value or ''))
    if not allowed_url(url):
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    selected = (query.get('wid') or fragment.get('wid') or [''])[0]
    filters = (query.get('pdp_filters') or fragment.get('pdp_filters') or [''])[0]
    physical = re.search(r'item_id:(MLB\d+)', filters, re.I)
    selected = selected or (physical.group(1) if physical else '')
    if physical and parsed.hostname.startswith('click'):
        return 'https://produto.mercadolivre.com.br/MLB-' + physical.group(1)[3:]
    if not re.search(r'/(?:p/MLB\d+|up/MLBU\d+|MLB-?\d+)', parsed.path, re.I):
        return None
    if re.fullmatch(r'MLB\d+', selected, re.I):
        query['wid'] = [selected.upper()]
        query['pdp_filters'] = ['item_id:' + selected.upper()]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=''))


def _item_id(url):
    parsed = urlparse(str(url or ''))
    query = parse_qs(parsed.query)
    selected = (query.get('wid') or query.get('item_id') or [''])[0]
    if not selected:
        filtered = re.search(r'item_id:(MLB\d+)', (query.get('pdp_filters') or [''])[0], re.I)
        selected = filtered.group(1) if filtered else ''
    if re.fullmatch(r'MLB\d+', selected, re.I):
        return selected.upper()
    if '/p/' not in parsed.path and '/up/' not in parsed.path:
        match = re.search(r'MLB-?(\d+)', parsed.path, re.I)
        return 'MLB' + match.group(1) if match else None
    return None


def _money(node):
    if not node:
        return None
    fraction = node.select_one('.andes-money-amount__fraction')
    cents = node.select_one('.andes-money-amount__cents')
    if fraction:
        return number(fraction.get_text(strip=True) + ',' + (cents.get_text(strip=True) if cents else '00'))
    return number(node.get_text(' ', strip=True))


def _seller(value):
    if isinstance(value, dict):
        return value.get('nickname') or value.get('name') or value.get('text') or (
            f"Mercado Livre Loja {value['id']}" if value.get('id') else None)
    text = str(value or '').strip()
    return re.sub(r'^(?:por|vendido por|vendido e entregue por)\s+', '', text, flags=re.I) or None


def _price(value):
    if isinstance(value, dict):
        value = value.get('current_price') or value
        if not isinstance(value, dict):
            return number(value)
        if value.get('currency') not in {None, 'BRL'} or value.get('currency_id') not in {None, 'BRL'}:
            return None
        value = value.get('value') if 'value' in value else value.get('amount')
    return number(value)


def _state_card(node, base):
    card = node.get('polycard') or node
    if not isinstance(card, dict):
        return None
    metadata = card.get('metadata') or {}
    components = card.get('components') or []
    by_id = {part.get('id') or part.get('type'): part for part in components if isinstance(part, dict)}
    title = node.get('title') or node.get('name')
    title_part = by_id.get('title') or {}
    embedded_title = title_part.get('title') or {}
    title = title or (embedded_title.get('text') if isinstance(embedded_title, dict) else embedded_title) or title_part.get('text')
    url = _product_url(metadata.get('url') or node.get('permalink') or node.get('url'), base)
    if not isinstance(title, str) or not url:
        return None
    item_id = node.get('item_id') or metadata.get('item_id') or _item_id(url)
    metadata_id = metadata.get('id')
    if not item_id and re.fullmatch(r'MLB\d+', str(metadata_id or ''), re.I) and metadata_id.lower() not in urlparse(url).path.lower():
        item_id = metadata_id
    if item_id and '/p/' in urlparse(url).path:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query['wid'] = [item_id]
        query['pdp_filters'] = ['item_id:' + item_id]
        url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
    price_part = by_id.get('price') or {}
    seller_part = by_id.get('seller') or {}
    seller = _seller(node.get('seller') or seller_part.get('seller') or seller_part.get('text'))
    price = _price(price_part.get('price') or node.get('price'))
    return {'name': title, 'url': url, 'seller': seller, 'store': seller, 'price': price,
            'source': 'Mercado Livre', 'item_id': item_id, '_seller_unknown': not bool(seller),
            'discovery_source': 'mercado_livre_embedded_state'}


def parse_search_html(html, base):
    soup = BeautifulSoup(html, 'html.parser')
    items, seen = [], set()
    def add(item):
        if not item:
            return
        key = (item['url'], item.get('seller'))
        if key in seen:
            return
        seen.add(key)
        if not item.get('seller'):
            item['_seller_unknown'] = True
            # A catalog's price without a seller cannot become a seller's quote.
            item['price'] = None
        items.append(item)
    for card in soup.select('li.ui-search-layout__item, div.poly-card'):
        link = card.select_one('a.poly-component__title[href], h2 a[href], a.ui-search-link[href]')
        url = _product_url(link.get('href'), base) if link else None
        if not url:
            continue
        seller_node = card.select_one('.poly-component__seller, .ui-search-official-store-label')
        seller = _seller(seller_node.get_text(' ', strip=True)) if seller_node else None
        price = _money(card.select_one('.poly-price__current .andes-money-amount, .ui-search-price__second-line .andes-money-amount'))
        add({'name': link.get_text(' ', strip=True), 'url': url, 'seller': seller,
             'store': seller, 'source': 'Mercado Livre', 'price': price,
             'item_id': _item_id(url), 'discovery_source': 'mercado_livre_html'})
    for state in embedded_states(soup):
        for node in _walk(state):
            add(_state_card(node, base))
    next_url = None
    next_link = soup.select_one('.andes-pagination__button--next a[href], a[rel="next"][href]')
    if next_link:
        absolute = urljoin(base, next_link.get('href'))
        if allowed_url(absolute) and urlparse(absolute).hostname == urlparse(base).hostname and absolute != base:
            next_url = absolute
    if not next_url:
        control = soup.select_one('a[data-andes-pagination-control="next"]:not([data-andes-state="disabled"])')
        selected = soup.select_one('[data-andes-pagination-item][data-andes-state="selected"]')
        if control and selected:
            try:
                page = int(selected.get_text(strip=True))
                parsed = urlparse(base)
                path = re.sub(r'_Desde_\d+(?:_NoIndex_True)?', '', parsed.path)
                cards = soup.select('div.poly-card') or soup.select('li.ui-search-layout__item')
                organic = sum(bool((link := card.select_one('a.poly-component__title[href], h2 a[href]')))
                              and not (urlparse(link.get('href')).hostname or '').startswith('click') for card in cards)
                previous = re.search(r'_Desde_(\d+)', parsed.path)
                start = int(previous.group(1)) if previous else 1
                next_url = urlunparse(parsed._replace(path=path.rstrip('/') + f'_Desde_{start + (organic or 48)}_NoIndex_True', fragment=''))
            except ValueError:
                pass
    return items, next_url


def _page_seller(soup):
    node = soup.select_one('.ui-pdp-buy-box-offers__seller a, .ui-pdp-seller-summary__link, .ui-pdp-seller-summary__link-trigger, .ui-pdp-seller__link, .ui-pdp-seller__link-trigger, .ui-pdp-seller__header__info-container a, .ui-pdp-seller__label-text-with-icon a')
    if node:
        return _seller(node.get_text(' ', strip=True))
    if 'ui-pdp-buy-box-offers__offer-list-item' in (soup.get('class') or []):
        text = soup.get_text(' ', strip=True)
        match = re.search(r'Vendido por\s+(?:Loja oficial\s+)?(.+)$', text, re.I)
        return match.group(1).strip() if match else None
    return None


def _parse_product_html(html, url, source, identity, postal_code=None):
    from scrapy.http import HtmlResponse
    from crawlers.base import parse_product_response
    from crawlers.profiles import STORE_PROFILES

    response = HtmlResponse(url=url, body=html.encode('utf-8'), encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    title_node = soup.select_one('h1.ui-pdp-title, h1')
    page_title = title_node.get_text(' ', strip=True) if title_node else ''
    attributes = {row.select_one('th').get_text(' ', strip=True).lower(): row.select_one('td').get_text(' ', strip=True)
                  for row in soup.select('.ui-pdp-specs__table__row, .ui-vpp-striped-specs__row') if row.select_one('th') and row.select_one('td')}
    page_candidate = {'name': page_title, 'url': url, 'ean': attributes.get('código universal de produto'),
                      'mpn': attributes.get('mpn')}
    # A W-code supplied as the default model is a part number, not the product's
    # marketing model (e.g. CIX06). Explicit catalog models still get checked.
    if identity.model and identity.model != identity.sku:
        page_candidate['model'] = attributes.get('modelo')
    page_match = resolve_candidate(identity, page_candidate)
    variations = ' '.join(node.get_text(' ', strip=True) for node in soup.select('.ui-pdp-outside_variations__title'))
    package = re.search(r'Quantidade de refis\s*:\s*(\d+)', variations, re.I)
    if package and int(package.group(1)) != _pack_quantity(identity.name):
        page_match = MatchResult(False, 0.0, 'invalid_match', 'Quantidade do kit diferente da unidade pesquisada')
    if not page_match.accepted and page_match.reason in {
        'Quantidade do kit diferente da unidade pesquisada', 'EAN divergente', 'MPN divergente', 'MODEL divergente',
    }:
        return {'status': 'INVALID_MATCH', 'reason': page_match.reason, 'match': page_match}
    stock_notice = soup.select_one('.ui-pdp-stock-information, .ui-pdp-unavailable, .ui-pdp-message')
    unavailable = bool(re.search(r'sem estoque|esgotado|indispon[ií]vel|an[uú]ncio pausado', stock_notice.get_text(' ', strip=True) if stock_notice else '', re.I))
    if page_match.accepted and unavailable and (_page_seller(soup) or source.seller):
        seller = _page_seller(soup) or source.seller
        item = {**page_candidate, 'sku': identity.sku, 'seller': seller, 'store': seller, 'source': 'Mercado Livre',
                'price': None, 'cash_price': None, 'availability': 'out_of_stock', 'brand': attributes.get('marca') or identity.brand or 'Não informada',
                'category': identity.name or identity.sku, 'image_url': None, 'captured_at': datetime.utcnow().isoformat(),
                'match_confidence': page_match.confidence, 'match_method': page_match.method, 'discovery_source': 'mercado_livre_html'}
        return {'status': 'OK', 'item': item, 'items': [item], 'match': page_match}
    if page_match.accepted:
        main = soup.select_one('.ui-pdp-buybox:not(.ui-pdp-other-sellers-item__buybox)')
        roots = ([main] if main else []) + list(soup.select('.ui-pdp-buy-box-offers__offer-list-item, .ui-pdp-other-sellers-item'))
        dom_items, seen = [], set()
        for root in roots:
            seller = _page_seller(root)
            price_root = root if root is not main else soup.select_one('.ui-pdp-container__top-wrapper, .ui-pdp-container__row--price, .ui-pdp-price')
            # The main price is outside its buybox. Remove other sellers and
            # recommendations before finding its primary price block.
            if root is main:
                primary = BeautifulSoup(html, 'html.parser')
                for extra in primary.select('.ui-pdp-other-sellers, .ui-pdp-other-sellers-item, .ui-pdp-recommendations'):
                    extra.decompose()
                price_root = primary
            price = _money(price_root.select_one('.ui-pdp-price__second-line .andes-money-amount')) if price_root else None
            item_input = root.select_one('input[name="item_id"], input[name="itemId"], input.andes-radio__input[value^="MLB"]')
            item_id = item_input.get('value') if item_input else None
            item_id = item_id or (re.search(r'MLB\d+', root.get('id') or '').group() if re.search(r'MLB\d+', root.get('id') or '') else None)
            if not seller or price is None or price <= 0 or not item_id or (seller, item_id) in seen:
                continue
            seen.add((seller, item_id))
            selected_url = _product_url(url, url)
            selected = urlparse(selected_url)
            query = parse_qs(selected.query)
            query.update(wid=[item_id], pdp_filters=['item_id:' + item_id])
            offer_url = urlunparse(selected._replace(query=urlencode(query, doseq=True)))
            available = bool(root.select_one('button[type="submit"]:not([disabled])'))
            dom_items.append({**page_candidate, 'sku': identity.sku, 'seller': seller, 'store': seller,
                             'source': 'Mercado Livre', 'url': offer_url, 'item_id': item_id,
                             'brand': attributes.get('marca') or identity.brand or 'Mercado Livre',
                             'category': identity.name or identity.sku, 'image_url': None,
                             'price': price, 'cash_price': price, 'availability': 'in_stock' if available else None,
                             'captured_at': datetime.utcnow().isoformat(),
                             'match_confidence': page_match.confidence, 'match_method': page_match.method,
                             'discovery_source': 'mercado_livre_seller_cards'})
        if dom_items:
            count_text = soup.get_text(' ', strip=True)
            declared = re.search(r'(\d+)\s+produtos? novos? a partir', count_text, re.I)
            return {'status': 'OK', 'item': dom_items[0], 'items': dom_items, 'match': page_match,
                    'declared_seller_offers': int(declared.group(1)) if declared else None,
                    'seller_coverage_limited': bool(declared and len(dom_items) < int(declared.group(1)))}
    primary = BeautifulSoup(html, 'html.parser')
    for extra in primary.select('.ui-pdp-other-sellers, .ui-pdp-other-sellers-item, .ui-pdp-recommendations'):
        extra.decompose()
    response = HtmlResponse(url=url, body=str(primary).encode('utf-8'), encoding='utf-8')
    parsed = parse_product_response(response, source, identity, STORE_PROFILES['mercado_livre'], postal_code)
    if parsed['status'] == 'OK':
        for item in parsed['items']:
            item['item_id'] = _item_id(item['url'])
            item['discovery_source'] = 'mercado_livre_html'
        return parsed
    for state in embedded_states(soup):
        for node in _walk(state):
            candidate = _state_card(node, url)
            if not candidate or urlparse(candidate['url']).path.rstrip('/') != urlparse(url).path.rstrip('/'):
                continue
            match = resolve_candidate(identity, candidate)
            if match.accepted and candidate.get('seller') and candidate.get('price') is not None:
                item = {**candidate, 'sku': identity.sku, 'brand': getattr(source, 'brand', None) or identity.brand or 'Mercado Livre',
                        'category': identity.name or identity.sku, 'image_url': None,
                        'captured_at': datetime.utcnow().isoformat(), 'cash_price': candidate['price'],
                        'match_confidence': match.confidence, 'match_method': match.method}
                return {'status': 'OK', 'item': item, 'items': [item], 'match': match}
    title_node = soup.select_one('h1.ui-pdp-title, h1')
    title = title_node.get_text(' ', strip=True) if title_node else ''
    attributes = {}
    for row in soup.select('.ui-pdp-specs__table__row, .ui-vpp-striped-specs__row'):
        key, value = row.select_one('th'), row.select_one('td')
        if key and value:
            attributes[key.get_text(' ', strip=True).lower()] = value.get_text(' ', strip=True)
    candidate = {'name': title, 'url': url, 'model': attributes.get('modelo'),
                 'brand': attributes.get('marca'), 'mpn': attributes.get('mpn'),
                 'ean': attributes.get('código universal de produto')}
    match = resolve_candidate(identity, candidate)
    if not match.accepted:
        return {'status': 'INVALID_MATCH', 'reason': match.reason, 'match': match}
    seller = _page_seller(soup)
    price_node = soup.select_one('.ui-pdp-price__second-line .andes-money-amount, .ui-pdp-price--size-large .andes-money-amount')
    price = _money(price_node)
    if not seller or price is None:
        return {'status': 'PARSER_ERROR', 'reason': 'Pagina sem preco e vendedor individual publicados', 'match': match}
    item = {**candidate, 'sku': identity.sku, 'seller': seller, 'store': seller, 'source': 'Mercado Livre',
            'item_id': _item_id(url), 'price': price, 'cash_price': price, 'category': identity.name or identity.sku,
            'image_url': None, 'captured_at': datetime.utcnow().isoformat(),
            'match_confidence': match.confidence, 'match_method': match.method,
            'discovery_source': 'mercado_livre_html'}
    return {'status': 'OK', 'item': item, 'items': [item], 'match': match}


def parse_product_html(html, url, source, identity, postal_code=None):
    result = _parse_product_html(html, url, source, identity, postal_code)
    if len(result.get('items', [])) == 1:
        from services.fulfillment import fulfillment_details
        item = result['items'][0]
        item.update(fulfillment_details(html, 'Mercado Livre', item.get('seller') or item.get('store')))
    return result


def collect_public_product(source, identity, postal_code=None, timeout=15, use_browser=False):
    def parse_page(page):
        return parse_product_html(page.content(), page.url, source, identity, postal_code)
    fetched = fetch_public_html(source.url, timeout, use_browser, page_handler=parse_page)
    if fetched['status'] != 'OK' or 'items' in fetched:
        if fetched['status'] in {'LOGIN_REQUIRED', 'JS_REQUIRED', 'HTTP_ERROR'}:
            fetched = {**fetched, '_access_status': fetched['status'], 'status': 'BLOCKED'}
        return fetched
    return parse_product_html(fetched['html'], fetched['url'], source, identity, postal_code)
