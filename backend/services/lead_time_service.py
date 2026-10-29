"""Persistent, destination-specific delivery quotes, separate from price history."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import math
import threading
from urllib.parse import urlparse

from database import SessionLocal
from models.product import LeadTimeQuote
from services.lead_time_destinations import CAPITALS, BY_UF
from services.structured_products import LOGISTICS_FIELDS, offer_available
from services.store_registry import TARGET_STORES, find_target_store

_wake = threading.Event()
_start_lock = threading.Lock()
_worker = None
_quote_fields = ('shipping_cost', 'delivery_min_days', 'delivery_max_days', 'delivery_days_type',
                 'shipping_method', 'logistics_source')


def sku_offers(db, sku):
    from services.scraper_service import _cached_sku_observations, _cached_item, _offer_url_identity
    from services.precise_extraction import _valid_store_url
    found = {}
    for product, price in _cached_sku_observations(db, sku):
        store = find_target_store(price.source, price.store, price.url)
        if not store or not price.store or urlparse(price.url or '').scheme != 'https' or not _valid_store_url(price.url, store['key']):
            continue
        key = (store['key'], price.store, _offer_url_identity(price.url))
        item = _cached_item(product, price, store['label'])
        item['seller'] = price.store
        item['_price_id'] = price.id
        item['_logistics_date'] = (price.logistics_captured_at or price.timestamp).isoformat() if (price.logistics_captured_at or price.timestamp) else None
        found[key] = item
    return list(found.values())


def quote_key(sku, item, cep):
    from services.scraper_service import _offer_url_identity
    return hashlib.sha256(json.dumps([sku, item['source'], item['seller'], _offer_url_identity(item['url']), cep]).encode()).hexdigest()


def _clean_item(item):
    return {key: value for key, value in item.items() if key not in LOGISTICS_FIELDS and not key.startswith('_') and key != 'is_cached'}


def _selected_sources(source=None, sources=None):
    selected = sources if sources is not None else ([source] if source else [])
    return {store['label'] if (store := find_target_store(value)) else value for value in selected}


def enqueue_quotes(db, sku, ufs, force_refresh=False, source=None, sources=None):
    selected = _selected_sources(source, sources)
    offers = [item for item in sku_offers(db, sku) if not selected or item['source'] in selected]
    if not offers:
        return {'queued': 0, 'message': 'Nenhuma oferta salva para este SKU. Faça uma extração primeiro.'}
    now = datetime.utcnow()
    queued = 0
    for item in offers:
        if not offer_available(item):
            continue
        for uf in ufs:
            dest = BY_UF[uf]
            key = quote_key(sku, item, dest['postal_code'])
            row = db.query(LeadTimeQuote).filter_by(quote_key=key).first()
            if row and (row.status in {'queued', 'running'} or (
                not force_refresh and row.status == 'quoted' and row.captured_at and now-row.captured_at < timedelta(hours=24))):
                continue
            if not row:
                row = LeadTimeQuote(quote_key=key, sku=sku, source=item['source'], seller=item['seller'],
                                    url=item['url'], uf=uf, postal_code=dest['postal_code'])
                db.add(row)
            row.item_json = json.dumps(_clean_item(item), ensure_ascii=False)
            row.status = 'queued'
            row.attempts = 0
            row.error = None
            row.updated_at = now
            queued += 1
    db.commit()
    _wake.set()
    return {'queued': queued, 'message': f'{queued} cotações adicionadas à fila.' if queued else 'Cotações já atualizadas ou em andamento.'}


def _cell(row):
    return {**{key: getattr(row, key) for key in _quote_fields}, 'status': row.status,
            'seller': row.seller, 'url': row.url, 'captured_at': row.captured_at,
            'error': row.error, 'postal_code': row.postal_code,
            'stale': bool(row.captured_at and datetime.utcnow()-row.captured_at > timedelta(hours=24))}


def _best_cell(cells):
    # Choose one complete observed service; never mix freight and transit time
    # from different sellers. Unknown day types cannot beat a known quote.
    ready = [cell for cell in cells if cell.get('delivery_max_days') is not None and cell['status'] == 'quoted' and not cell.get('stale')]
    if not ready:
        ready = [cell for cell in cells if cell.get('delivery_max_days') is not None and cell['status'] == 'quoted']
    if ready:
        if len({cell.get('delivery_days_type') for cell in ready}) > 1:
            return {'status': 'mixed', 'alternatives': cells, 'quoted_sellers': len({cell['seller'] for cell in ready}),
                    'pending': sum(cell['status'] in {'queued', 'running'} for cell in cells)}
        # Conservative comparison: prefer business-day quotes when a store has
        # mixed day types. The original day type remains visible to the user.
        selected = min(ready, key=lambda cell: (cell.get('delivery_days_type') != 'business', cell['delivery_max_days'],
                                               cell.get('shipping_cost') is None, cell.get('shipping_cost') or 0))
    else:
        selected = next((cell for cell in cells if cell['status'] in {'running', 'queued'}), cells[0] if cells else {'status': 'not_queried'})
    return {**selected, 'quoted_sellers': len({cell['seller'] for cell in cells if cell['status'] == 'quoted'}),
            'pending': sum(cell['status'] in {'queued', 'running'} for cell in cells),
            'alternatives': cells}


def get_matrix(db, sku):
    offers = sku_offers(db, sku)
    active = {(item['source'], item['seller'], item['url']): item for item in offers}
    records = db.query(LeadTimeQuote).filter_by(sku=sku).all()
    cells = {}
    relevant = []
    for row in records:
        if (row.source, row.seller, row.url) not in active or not offer_available(active[(row.source, row.seller, row.url)]):
            continue
        relevant.append(row)
        cells.setdefault((row.source, row.seller, row.uf), []).append(_cell(row))
    # Exact CEP only: a saved SP quote must never fill another state's column.
    for item in offers:
        if not offer_available(item):
            continue
        for dest in CAPITALS:
            key = (item['source'], item['seller'], dest['uf'])
            if item.get('destination_zip') != dest['postal_code'] or item.get('delivery_max_days') is None:
                continue
            if not any(cell.get('url') == item['url'] for cell in cells.get(key, [])):
                captured = datetime.fromisoformat(item['_logistics_date']) if item.get('_logistics_date') else None
                cells.setdefault(key, []).append({**{field: item.get(field) for field in _quote_fields},
                    'status': 'quoted', 'seller': item['seller'], 'url': item['url'], 'captured_at': captured,
                    'postal_code': dest['postal_code'], 'stale': not captured or datetime.utcnow()-captured > timedelta(hours=24)})
    stores = []
    for store in TARGET_STORES:
        store_offers = [item for item in offers if item['source'] == store['label']]
        sellers = []
        for name in sorted({item['seller'] for item in store_offers}):
            seller_items = [item for item in store_offers if item['seller'] == name]
            unavailable = all(not offer_available(item) for item in seller_items)
            seller_cells = {dest['uf']: _best_cell(cells.get((store['label'], name, dest['uf']), [])) for dest in CAPITALS}
            if unavailable:
                seller_cells = {dest['uf']: {'status': 'out_of_stock'} for dest in CAPITALS}
            sellers.append({'name': name, 'cells': seller_cells})
        aggregate = {dest['uf']: _best_cell([cell for seller in sellers for cell in seller['cells'][dest['uf']].get('alternatives', [])]) for dest in CAPITALS}
        if store_offers and all(not offer_available(item) for item in store_offers):
            aggregate = {dest['uf']: {'status': 'out_of_stock'} for dest in CAPITALS}
        if not store_offers:
            aggregate = {dest['uf']: {'status': 'no_offers'} for dest in CAPITALS}
        stores.append({'key': store['key'], 'name': store['label'], 'offer_count': len(store_offers), 'sellers': sellers, 'cells': aggregate})
    pending = sum(row.status in {'queued', 'running'} for row in relevant)
    return {'sku': sku, 'destinations': CAPITALS, 'stores': stores,
            'progress': {'total': len(relevant), 'completed': len(relevant)-pending, 'pending': pending},
            'has_offers': bool(offers)}


def get_shipping_map(db, sku, source=None, sources=None):
    """Freight observations, including valid amounts without a delivery date."""
    selected = _selected_sources(source, sources)
    offers = [item for item in sku_offers(db, sku) if offer_available(item) and (not selected or item['source'] in selected)]
    active = {(item['source'], item['seller'], item['url']) for item in offers}
    options = {dest['uf']: [] for dest in CAPITALS}
    now = datetime.utcnow()

    def add(uf, item):
        amount = item.get('shipping_cost')
        if amount is None or not math.isfinite(amount) or amount < 0:
            return
        captured = item.get('captured_at')
        if not captured:
            return
        options[uf].append({**item, 'stale': now - captured > timedelta(hours=24)})

    records = db.query(LeadTimeQuote).filter_by(sku=sku).all()
    for row in records:
        if (row.source, row.seller, row.url) not in active or row.uf not in BY_UF or row.postal_code != BY_UF[row.uf]['postal_code']:
            continue
        if row.status in {'quoted', 'not_quoted'}:
            add(row.uf, {'source': row.source, 'seller': row.seller, 'url': row.url,
                         'shipping_cost': row.shipping_cost, 'captured_at': row.captured_at})
    for item in offers:
        for dest in CAPITALS:
            if item.get('destination_zip') != dest['postal_code']:
                continue
            add(dest['uf'], {'source': item['source'], 'seller': item['seller'], 'url': item['url'],
                            'shipping_cost': item.get('shipping_cost'),
                            'captured_at': datetime.fromisoformat(item['_logistics_date']) if item.get('_logistics_date') else None})
    states = []
    for dest in CAPITALS:
        latest = {}
        for value in options[dest['uf']]:
            key = (value['source'], value['seller'], value['url'])
            if key not in latest or value['captured_at'] > latest[key]['captured_at']:
                latest[key] = value
        values = list(latest.values())
        fresh = [value for value in values if not value['stale']]
        eligible = fresh or values
        # Match the existing service selection: lowest paid freight when one
        # exists, retaining a confirmed zero when it is the only option.
        paid = [value for value in eligible if value['shipping_cost'] > 0]
        best = min(paid or eligible, key=lambda value: value['shipping_cost'], default=None)
        states.append({**dest, 'quote': best, 'quote_count': len(values)})
    return {'sku': sku, 'states': states, 'sources': sorted({item['source'] for item in offers}),
            'has_offers': bool(offers), 'pending': sum(row.status in {'queued', 'running'} for row in records
                if (row.source, row.seller, row.url) in active)}


def _page_context(item):
    from services.http_client import create_resilient_session, recover_public_html
    from crawlers.base import parse_product_response
    from crawlers.profiles import STORE_PROFILES, DEFAULT_PROFILE
    from scrapy.http import HtmlResponse
    from services.product_resolver import ProductIdentity, normalize_text
    from types import SimpleNamespace
    store = find_target_store(item['source'], item['url'])
    if not store:
        return None
    with create_resilient_session(pool_size=2) as session:
        response = session.get(item['url'], timeout=(5, 12))
        response = recover_public_html(response, item['url'], 12)
    if response.status_code != 200:
        return None
    source = SimpleNamespace(store=store['key'], seller=item['seller'], brand=item.get('brand'), external_product_id=None, asin=None, item_id=None)
    identity = ProductIdentity(sku=item['sku'], model=item.get('model') or item['sku'])
    result = parse_product_response(HtmlResponse(url=response.url, body=response.content, encoding=response.encoding or 'utf-8'),
                                    source, identity, STORE_PROFILES.get(store['key'], DEFAULT_PROFILE))
    matched = [offer for offer in result.get('items', []) if normalize_text(offer.get('seller')) == normalize_text(item['seller']) and offer.get('url') == item['url']]
    context = matched[0].get('_page_context') if len(matched) == 1 else None
    if not context and len(matched) == 1 and store['key'] in {'electrolux', 'midea', 'consul', 'brastemp'}:
        # These providers quote using selected-product metadata, not DOM forms.
        # Retain original JSON scripts only; a large page with two voltage
        # variants can then be reused for the exact SKU URL without 27 GETs.
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        scripts = soup.select('script[type="application/ld+json"], script#__NEXT_DATA__')
        compact = '<html><body>' + ''.join(str(script) for script in scripts) + '</body></html>'
        if len(compact) <= 4_000_000:
            context = {'offer_url': item['url'], 'page_url': response.url, 'html': compact}
    return context if context and context.get('offer_url') == item['url'] else None


def _process_group(ids):
    from services.offer_enrichment import _fetch_details_uncached
    from services.playwright_fallback import collect_shipping_with_playwright
    from services.browser_health import shipping_form_available, browser_available
    from services.crawler_control import get_control
    with SessionLocal() as db:
        first = db.query(LeadTimeQuote).filter_by(id=ids[0]).one()
        item = json.loads(first.item_json)
        allow_browser = get_control(db).playwright_shipping_fallback
    try:
        context = _page_context(item)
    except Exception:
        context = None
    if context:
        item['_page_context'] = context
    for record_id in ids:
        with SessionLocal() as db:
            row = db.query(LeadTimeQuote).filter_by(id=record_id, status='running').first()
            if not row:
                continue
            cep = row.postal_code
        try:
            details = _fetch_details_uncached(item['url'], item.get('price'), cep, item=item)
            if details.get('destination_zip') and details['destination_zip'] != cep:
                raise ValueError('Cotação retornada para um CEP diferente do solicitado.')
            if details.get('delivery_max_days') is None and allow_browser and shipping_form_available(item['url']) and browser_available(item['url']):
                browser = collect_shipping_with_playwright(item['url'], cep, 12)
                if browser.get('destination_zip') == cep:
                    # Keep one service together; don't combine HTTP freight with
                    # a different browser-selected delivery service.
                    details = browser
            status = 'quoted' if details.get('delivery_max_days') is not None else 'not_quoted'
            if not offer_available(details):
                details, status = {}, 'out_of_stock'
            error = None if status == 'quoted' else 'A loja não forneceu prazo confirmado para este CEP.'
        except Exception as exc:
            details, status, error = {}, 'failed', str(exc)[:400]
        with SessionLocal() as db:
            row = db.query(LeadTimeQuote).filter_by(id=record_id, status='running').first()
            if not row:
                continue
            for field in _quote_fields:
                setattr(row, field, details.get(field))
            row.status, row.error = status, error
            row.captured_at = row.updated_at = datetime.utcnow()
            db.commit()


def process_quote_batch():
    with SessionLocal() as db:
        rows = db.query(LeadTimeQuote).filter_by(status='queued').order_by(LeadTimeQuote.updated_at, LeadTimeQuote.id).all()
        groups, hosts = {}, {}
        for row in rows:
            key = (row.sku, row.source, row.seller, row.url)
            host = urlparse(row.url).hostname
            if key not in groups:
                if len(groups) >= 4 or hosts.get(host, 0) >= 2:
                    continue
                groups[key] = []
                hosts[host] = hosts.get(host, 0) + 1
            groups[key].append(row.id)
            row.status = 'running'
            row.attempts = (row.attempts or 0) + 1
        db.commit()
    if not groups:
        return False
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_process_group, ids) for ids in groups.values()]
        for future in futures:
            future.result()
    return True


def start_lead_time_worker():
    global _worker
    with _start_lock:
        if _worker and _worker.is_alive():
            return
        with SessionLocal() as db:
            db.query(LeadTimeQuote).filter_by(status='running').update({'status': 'queued'})
            db.commit()
        def run():
            while True:
                try:
                    if process_quote_batch():
                        continue
                except Exception as exc:
                    print(f'[LEAD_TIME] {exc}')
                    # Failed task groups remain resumable, rather than leaving
                    # an indefinite spinner after an unexpected worker error.
                    with SessionLocal() as db:
                        for row in db.query(LeadTimeQuote).filter_by(status='running'):
                            row.status = 'failed' if row.attempts >= 2 else 'queued'
                            row.error = str(exc)[:400]
                        db.commit()
                _wake.wait(3)
                _wake.clear()
        _worker = threading.Thread(target=run, name='lead-time-quotes', daemon=True)
        _worker.start()
