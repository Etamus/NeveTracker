"""Persistent, destination-specific delivery quotes, separate from price history."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import math
import threading
from urllib.parse import urlparse
from sqlalchemy import func, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import defer

from database import SessionLocal
from models.product import LeadTimeQuote, PriceHistory, latest_price_at_destination
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
        previous = found.get(key)
        product_ids = previous['_product_ids'] if previous else set()
        product_ids.add(product.id)
        if previous and (price.timestamp or datetime.min, price.id) < previous['_last_record']:
            continue
        item = _cached_item(product, price, store['label'])
        item['seller'] = price.store
        item['_price_id'] = price.id
        item['_logistics_date'] = (price.logistics_captured_at or price.timestamp).isoformat() if (price.logistics_captured_at or price.timestamp) else None
        item['_product_ids'] = product_ids
        item['_last_record'] = (price.timestamp or datetime.min, price.id)
        found[key] = item
    return list(found.values())


def _saved_postal_quotes(db, offers, postals, covered=None):
    # Older extractions wrote freight and transit times only to price_history.
    # Scope the lookup to validated active listings and rank BEFORE testing the
    # values, so a later empty check cannot resurrect an older shipping price.
    covered = {scope: set(postal_codes) for scope, postal_codes in (covered or {}).items()}
    by_product = {}
    for item in offers:
        if not offer_available(item):
            continue
        scope = (item['source'], item['seller'], item['url'])
        known = covered.setdefault(scope, set())
        postal = item.get('destination_zip')
        if postal in postals and postal not in known:
            known.add(postal)
            yield {'source': item['source'], 'seller': item['seller'], 'url': item['url'], 'postal_code': postal,
                   'captured_at': datetime.fromisoformat(item['_logistics_date']) if item.get('_logistics_date') else None,
                   'price': item.get('price', item.get('current_price')),
                   **{field: item.get(field) for field in _quote_fields}}
        if len(known) < len(postals):
            by_product.update((product_id, item) for product_id in item.get('_product_ids', ()))
    product_ids = list(by_product)
    saved = {}
    if postals:
        for offset in range(0, len(product_ids), 400):
            columns = [getattr(PriceHistory, field) for field in _quote_fields]
            query = db.query(PriceHistory.id, PriceHistory.product_id, PriceHistory.store, PriceHistory.source,
                             PriceHistory.url, PriceHistory.destination_zip, PriceHistory.availability, PriceHistory.price,
                             PriceHistory.timestamp, PriceHistory.logistics_captured_at, *columns).filter(
                PriceHistory.product_id.in_(product_ids[offset:offset + 400]), PriceHistory.destination_zip.in_(postals),
                PriceHistory.id == latest_price_at_destination(PriceHistory))
            for row in query:
                item = by_product[row.product_id]
                if row.store != item['seller'] or row.url != item['url']:
                    continue
                if row.source != item['source']:
                    store = find_target_store(row.source, row.store, row.url)
                    if not store or store['label'] != item['source']:
                        continue
                listing = (item['source'], item['seller'], item['url'])
                if row.destination_zip in covered.get(listing, ()):
                    continue
                key = (*listing, row.destination_zip)
                stamp = (row.timestamp or datetime.min, row.id)
                if key not in saved or stamp > saved[key][0]:
                    saved[key] = (stamp, row)
    for (source, seller, url, postal_code), (_, row) in saved.items():
        if offer_available({'availability': row.availability}):
            yield {'source': source, 'seller': seller, 'url': url, 'postal_code': postal_code,
                   'captured_at': row.logistics_captured_at or row.timestamp,
                   'price': row.price,
                   **{field: getattr(row, field) for field in _quote_fields}}


def quote_key(sku, item, cep):
    from services.scraper_service import _offer_url_identity
    return hashlib.sha256(json.dumps([sku, item['source'], item['seller'], _offer_url_identity(item['url']), cep]).encode()).hexdigest()


def _clean_item(item):
    return {key: value for key, value in item.items() if key not in LOGISTICS_FIELDS and not key.startswith('_') and key != 'is_cached'}


def _selected_sources(source=None, sources=None):
    selected = sources if sources is not None else ([source] if source else [])
    return {store['label'] if (store := find_target_store(value)) else value for value in selected}


def enqueue_quotes(db, sku, ufs, force_refresh=False, source=None, sources=None, postal_codes=None):
    selected = _selected_sources(source, sources)
    offers = [item for item in sku_offers(db, sku) if not selected or item['source'] in selected]
    if not offers:
        return {'queued': 0, 'message': 'Nenhuma oferta salva para este SKU. Faça uma extração primeiro.'}
    if postal_codes is None:
        requested = [BY_UF[uf] for uf in ufs]
    else:
        from services.delivery_destinations import destinations
        available = {dest['postal_code']: dest for dest in [*CAPITALS, *destinations(db)]}
        unknown = set(postal_codes) - set(available)
        if unknown:
            raise ValueError('Salve os CEPs na seleção de destinos antes de consultar: ' + ', '.join(sorted(unknown)))
        requested = [available[cep] for cep in dict.fromkeys(postal_codes)]
    now = datetime.utcnow()
    queued = 0
    table = LeadTimeQuote.__table__
    statement = None
    if db.get_bind().dialect.name == "sqlite":
        insert = sqlite_insert(table)
        eligible = (table.c.status != "queued") & (table.c.status != "running")
        if not force_refresh:
            eligible &= or_(table.c.status != "quoted", table.c.delivery_max_days.is_(None),
                            table.c.captured_at.is_(None), table.c.captured_at <= now - timedelta(hours=24))
        statement = insert.on_conflict_do_update(
            index_elements=[table.c.quote_key],
            set_={key: insert.excluded[key] for key in ("item_json", "status", "attempts", "error", "updated_at")},
            where=eligible,
        )
    for item in offers:
        if not offer_available(item):
            continue
        snapshot = json.dumps(_clean_item(item), ensure_ascii=False)
        if statement is not None:
            batch = [dict(quote_key=quote_key(sku, item, dest['postal_code']), sku=sku, source=item['source'],
                          seller=item['seller'], url=item['url'], uf=dest['uf'], postal_code=dest['postal_code'],
                          item_json=snapshot, status='queued', attempts=0, error=None, updated_at=now)
                     for dest in requested]
            if batch:
                queued += db.connection().execute(statement, batch).rowcount
            continue
        for dest in requested:
            uf = dest['uf']
            key = quote_key(sku, item, dest['postal_code'])
            row = db.query(LeadTimeQuote).filter_by(quote_key=key).first()
            if row and (row.status in {'queued', 'running'} or (
                not force_refresh and row.status == 'quoted' and row.delivery_max_days is not None and row.captured_at and now-row.captured_at < timedelta(hours=24))):
                continue
            if not row:
                row = LeadTimeQuote(quote_key=key, sku=sku, source=item['source'], seller=item['seller'],
                                    url=item['url'], uf=uf, postal_code=dest['postal_code'])
                db.add(row)
            row.item_json = snapshot
            row.status = 'queued'
            row.attempts = 0
            row.error = None
            row.updated_at = now
            queued += 1
    db.commit()
    _wake.set()
    return {'queued': queued, 'message': f'{queued} cotações adicionadas à fila.' if queued else 'Cotações já atualizadas ou em andamento.'}


def _cell(row):
    # Older workers recorded minimum-only estimates as not_quoted.
    status = 'quoted' if row.status == 'not_quoted' and row.delivery_min_days is not None else row.status
    return {**{key: getattr(row, key) for key in _quote_fields}, 'status': status,
            'seller': row.seller, 'url': row.url, 'captured_at': row.captured_at,
            'error': row.error, 'postal_code': row.postal_code,
            'stale': bool(row.captured_at and datetime.utcnow()-row.captured_at > timedelta(hours=24))}


def _best_cell(cells):
    # Choose one complete observed service; never mix freight and transit time
    # from different sellers. Unknown day types cannot beat a known quote.
    quoted = [cell for cell in cells if cell['status'] == 'quoted' and
              (cell.get('delivery_max_days') is not None or cell.get('delivery_min_days') is not None)]
    ready = [cell for cell in quoted if not cell.get('stale')] or quoted
    if ready:
        if len({cell.get('delivery_days_type') for cell in ready}) > 1:
            return {'status': 'mixed', 'alternatives': cells, 'quoted_sellers': len({cell['seller'] for cell in ready}),
                    'pending': sum(cell['status'] in {'queued', 'running'} for cell in cells)}
        # A lower bound alone cannot beat a confirmed upper bound.
        selected = min(ready, key=lambda cell: (cell.get('delivery_max_days') is None,
                                               cell.get('delivery_max_days') if cell.get('delivery_max_days') is not None else cell['delivery_min_days'],
                                               cell.get('shipping_cost') is None, cell.get('shipping_cost') or 0))
    else:
        selected = next((cell for cell in cells if cell['status'] in {'running', 'queued'}), cells[0] if cells else {'status': 'not_queried'})
    return {**selected, 'quoted_sellers': len({cell['seller'] for cell in cells if cell['status'] == 'quoted'}),
            'pending': sum(cell['status'] in {'queued', 'running'} for cell in cells),
            'alternatives': cells}


def get_matrix(db, sku):
    from services.delivery_destinations import destinations
    chosen = destinations(db)
    by_cep = {dest["postal_code"]: dest for dest in chosen}
    offers = sku_offers(db, sku)
    active = {(item['source'], item['seller'], item['url']): item for item in offers}
    records = (db.query(LeadTimeQuote).filter_by(sku=sku)
               .filter(LeadTimeQuote.postal_code.in_(by_cep))
               .options(defer(LeadTimeQuote.item_json)).all())
    cells = {}
    covered = {}
    relevant = []
    for row in records:
        if (row.source, row.seller, row.url) not in active or not offer_available(active[(row.source, row.seller, row.url)]):
            continue
        dest = by_cep.get(row.postal_code)
        if not dest or dest['uf'] != row.uf:
            continue
        relevant.append(row)
        covered.setdefault((row.source, row.seller, row.url), set()).add(row.postal_code)
        cells.setdefault((row.source, row.seller, dest['key']), []).append(_cell(row))
    # A quote from price history can fill only its original listing and CEP.
    now = datetime.utcnow()
    for quote in _saved_postal_quotes(db, offers, by_cep, covered):
        if quote['delivery_max_days'] is None and quote['delivery_min_days'] is None:
            continue
        dest = by_cep[quote['postal_code']]
        key = (quote['source'], quote['seller'], dest['key'])
        captured = quote['captured_at']
        cells.setdefault(key, []).append({**{field: quote[field] for field in _quote_fields},
            'status': 'quoted', 'seller': quote['seller'], 'url': quote['url'], 'captured_at': captured,
            'postal_code': dest['postal_code'], 'stale': not captured or now-captured > timedelta(hours=24)})
    stores = []
    for store in TARGET_STORES:
        store_offers = [item for item in offers if item['source'] == store['label']]
        sellers = []
        for name in sorted({item['seller'] for item in store_offers}):
            seller_items = [item for item in store_offers if item['seller'] == name]
            unavailable = all(not offer_available(item) for item in seller_items)
            seller_cells = {dest['key']: _best_cell(cells.get((store['label'], name, dest['key']), [])) for dest in chosen}
            if unavailable:
                seller_cells = {dest['key']: {'status': 'out_of_stock'} for dest in chosen}
            sellers.append({'name': name, 'cells': seller_cells})
        aggregate = {dest['key']: _best_cell([cell for seller in sellers for cell in seller['cells'][dest['key']].get('alternatives', [])]) for dest in chosen}
        if store_offers and all(not offer_available(item) for item in store_offers):
            aggregate = {dest['key']: {'status': 'out_of_stock'} for dest in chosen}
        if not store_offers:
            aggregate = {dest['key']: {'status': 'no_offers'} for dest in chosen}
        stores.append({'key': store['key'], 'name': store['label'], 'offer_count': len(store_offers), 'sellers': sellers, 'cells': aggregate})
    pending = sum(row.status in {'queued', 'running'} for row in relevant)
    return {'sku': sku, 'destinations': chosen, 'stores': stores,
            'progress': {'total': len(relevant), 'completed': len(relevant)-pending, 'pending': pending},
            'has_offers': bool(offers)}


def get_shipping_map(db, sku, source=None, sources=None):
    """Freight observations, including valid amounts without a delivery date."""
    selected = _selected_sources(source, sources)
    offers = [item for item in sku_offers(db, sku) if offer_available(item) and (not selected or item['source'] in selected)]
    active = {(item['source'], item['seller'], item['url']) for item in offers}
    options = {dest['uf']: [] for dest in CAPITALS}
    now = datetime.utcnow()
    from services.delivery_destinations import destinations
    chosen=destinations(db)
    allowed_postals={dest['postal_code']:dest['uf'] for dest in chosen}
    by_state={dest['uf']:dest for dest in chosen}
    by_postal = {dest['postal_code']: dest for dest in chosen}
    comparisons = {postal: {} for postal in allowed_postals}

    def add(uf, item):
        amount = item.get('shipping_cost')
        if amount is None or not math.isfinite(amount) or amount < 0:
            return
        captured = item.get('captured_at')
        if not captured:
            return
        item = {**item, 'stale': now - captured > timedelta(hours=24)}
        options[uf].append(item)
        price = item.get('price')
        if isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price) or price <= 0:
            return
        postal = item['postal_code']
        key = (item['source'], item['seller'], item['url'])
        previous = comparisons[postal].get(key)
        if not previous or captured > previous['captured_at']:
            comparisons[postal][key] = {**item, 'total': round(price + amount, 2)}

    pending = 0
    records = db.query(LeadTimeQuote.source, LeadTimeQuote.seller, LeadTimeQuote.url, LeadTimeQuote.uf,
                       LeadTimeQuote.postal_code, LeadTimeQuote.status, LeadTimeQuote.shipping_cost,
                       LeadTimeQuote.captured_at, LeadTimeQuote.delivery_min_days,
                       LeadTimeQuote.delivery_max_days, LeadTimeQuote.delivery_days_type,
                       LeadTimeQuote.item_json).filter(
        LeadTimeQuote.sku == sku, LeadTimeQuote.postal_code.in_(allowed_postals))
    if selected:
        records = records.filter(LeadTimeQuote.source.in_(selected))
    covered = {}
    for row in records:
        if (row.source, row.seller, row.url) not in active or row.uf not in BY_UF or allowed_postals.get(row.postal_code) != row.uf:
            continue
        if row.status in {'queued', 'running'}:
            pending += 1
        else:
            covered.setdefault((row.source, row.seller, row.url), set()).add(row.postal_code)
        if row.status in {'quoted', 'not_quoted'} and row.shipping_cost is not None:
            snapshot = json.loads(row.item_json or '{}')
            price = snapshot.get('price') if offer_available(snapshot) else None
            add(row.uf, {'source': row.source, 'seller': row.seller, 'url': row.url,
                         'shipping_cost': row.shipping_cost, 'captured_at': row.captured_at, 'postal_code': row.postal_code,
                         'price': price, 'delivery_min_days': row.delivery_min_days,
                         'delivery_max_days': row.delivery_max_days, 'delivery_days_type': row.delivery_days_type})
    # Preserve the map's legacy fallback. A failed or incomplete newer check
    # must not make an older price eligible for the delivered-offer ranking.
    for quote in _saved_postal_quotes(db, offers, allowed_postals):
        uf = allowed_postals[quote['postal_code']]
        scope = (quote['source'], quote['seller'], quote['url'])
        add(uf, {**quote, 'price': None} if quote['postal_code'] in covered.get(scope, ()) else quote)
    states = []
    for dest in CAPITALS:
        latest = {}
        for value in options[dest['uf']]:
            key = (value['source'], value['seller'], value['url'], value['postal_code'])
            if key not in latest or value['captured_at'] > latest[key]['captured_at']:
                latest[key] = value
        values = list(latest.values())
        fresh = [value for value in values if not value['stale']]
        eligible = fresh or values
        # Match the existing service selection: lowest paid freight when one
        # exists, retaining a confirmed zero when it is the only option.
        paid = [value for value in eligible if value['shipping_cost'] > 0]
        best = min(paid or eligible, key=lambda value: value['shipping_cost'], default=None)
        destination = by_postal[best['postal_code']] if best else by_state.get(dest['uf'], {})
        states.append({**dest, **destination, 'quote': best, 'quote_count': len(values)})
    delivered = []
    for postal, records in comparisons.items():
        values = list(records.values())
        eligible = [value for value in values if not value['stale']] or values
        fastest = {}
        for unit in ('business', 'calendar', None):
            candidates = [value for value in eligible if value['delivery_max_days'] is not None
                          and (value['delivery_days_type'] if value['delivery_days_type'] in ('business', 'calendar') else None) == unit]
            if candidates:
                fastest[unit or 'unknown'] = min(candidates, key=lambda value: (value['delivery_max_days'], value['total']))
        delivered.append({**by_postal[postal], 'offer_count': len(eligible),
                          'cheapest': min(eligible, key=lambda value: value['total'], default=None),
                          'fastest': fastest})
    return {'sku': sku, 'states': states, 'delivered': delivered, 'sources': sorted({item['source'] for item in offers}),
            'has_offers': bool(offers), 'pending': pending}


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


def _process_group(ids, claimed_at):
    from services.offer_enrichment import _fetch_details_uncached
    from services.playwright_fallback import collect_shipping_with_playwright
    from services.browser_health import shipping_form_available, browser_available
    from services.crawler_control import get_control
    with SessionLocal() as db:
        snapshot = (db.query(LeadTimeQuote.item_json).filter(LeadTimeQuote.id.in_(ids))
                    .filter_by(status="running", updated_at=claimed_at).first())
        if not snapshot:
            return
        item = json.loads(snapshot[0])
        allow_browser = get_control(db).playwright_shipping_fallback
    try:
        context = _page_context(item)
    except Exception:
        context = None
    if context:
        item['_page_context'] = context
    for record_id in ids:
        with SessionLocal() as db:
            cep = db.query(LeadTimeQuote.postal_code).filter_by(id=record_id, status='running', updated_at=claimed_at).scalar()
            if cep is None:
                continue
        try:
            details = _fetch_details_uncached(item['url'], item.get('price'), cep, item=item)
            if details.get('destination_zip') and details['destination_zip'] != cep:
                raise ValueError('Cotação retornada para um CEP diferente do solicitado.')
            if details.get('delivery_max_days') is None and allow_browser and shipping_form_available(item['url']) and browser_available(item['url']):
                try:
                    browser = collect_shipping_with_playwright(item['url'], cep, 12)
                except Exception:
                    if not any(details.get(k) is not None for k in ('delivery_min_days', 'shipping_cost')):
                        raise
                    browser = {}
                if (browser.get('destination_zip') == cep and
                        (browser.get('delivery_max_days') is not None or
                         (details.get('delivery_min_days') is None and (browser.get('delivery_min_days') is not None or
                          (details.get('shipping_cost') is None and browser.get('shipping_cost') is not None))) or
                         not offer_available(browser))):
                    # Keep one service together; don't combine HTTP freight with
                    # a different browser-selected delivery service.
                    details = browser
            status = 'quoted' if any(details.get(k) is not None for k in ('delivery_min_days', 'delivery_max_days')) else 'not_quoted'
            if not offer_available(details):
                details, status = {}, 'out_of_stock'
            error = None if status == 'quoted' else 'A loja não forneceu prazo confirmado para este CEP.'
        except Exception as exc:
            details, status, error = {}, 'failed', str(exc)[:400]
        with SessionLocal() as db:
            now = datetime.utcnow()
            values = {field: details.get(field) for field in _quote_fields}
            db.query(LeadTimeQuote).filter_by(id=record_id, status='running', updated_at=claimed_at).update(
                {**values, "status": status, "error": error, "updated_at": now, "captured_at": now},
                synchronize_session=False)
            db.commit()


def process_quote_batch():
    with SessionLocal() as db:
        columns = (LeadTimeQuote.sku, LeadTimeQuote.source, LeadTimeQuote.seller, LeadTimeQuote.url)
        candidates = (db.query(*columns).filter_by(status='queued').group_by(*columns)
                      .order_by(func.min(LeadTimeQuote.updated_at), func.min(LeadTimeQuote.id)))
        groups, hosts = {}, {}
        for key in candidates.yield_per(32):
            host = urlparse(key.url).hostname
            if hosts.get(host, 0) >= 2:
                continue
            groups[tuple(key)] = []
            hosts[host] = hosts.get(host, 0) + 1
            if len(groups) == 4:
                break
        claimed_at = datetime.utcnow()
        for key in groups:
            filters = [col == val for col, val in zip(columns, key)] + [LeadTimeQuote.status == "queued"]
            claim = update(LeadTimeQuote).where(*filters).values(
                status="running", updated_at=claimed_at, attempts=func.coalesce(LeadTimeQuote.attempts, 0) + 1)
            if db.get_bind().dialect.update_returning:
                groups[key] = list(db.scalars(claim.returning(LeadTimeQuote.id)))
            else:
                ids = db.query(LeadTimeQuote.id).filter(*filters).with_for_update().all()
                groups[key] = [row.id for row in ids]
                if ids:
                    db.execute(claim.where(LeadTimeQuote.id.in_(groups[key])))
        db.commit()
    if not groups:
        return False
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_process_group, ids, claimed_at) for ids in groups.values() if ids]
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
