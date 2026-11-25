"""Conservative same-day catalog checks. Unknown/incomplete evidence never skips work."""
from datetime import datetime, time, timezone
from time import monotonic
import json

from sqlalchemy import or_, update
from database import begin_report_snapshot
from models.product import LeadTimeQuote, OfferObservation, PriceHistory, Product, ProductSource, ScrapeCursor, latest_price_at_destination
from services.price_observations import LOCAL, record_observation
from services.structured_products import offer_available


COMMERCIAL_FIELDS = ('price', 'cash_price', 'installment_price', 'installment_count',
                     'installment_value', 'availability', 'brand', 'name')
SCHEDULED_PRICE_CHECK = 'scheduled_price_check'


def day_start_utc(now=None):
    now = now or datetime.utcnow()
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    local = aware.astimezone(LOCAL)
    return datetime.combine(local.date(), time.min, LOCAL).astimezone(timezone.utc).replace(tzinfo=None)


def unchanged_catalog_stores(db, sku, stores, postal_codes, now=None):
    """One complete public catalog request replaces repeated discovery/quotes.

    Only supported authoritative catalogs qualify, and only when every current
    seller/variant has complete same-day quotes at every requested destination.
    Returned quotes retain their original capture time and are explicitly cached.
    Any change, missing data, new seller, timeout or parser error falls through.
    """
    from services.vtex_public import HOSTS, LABELS, catalog, catalog_offers
    from services.extraction_resources import fair_map
    from services.lead_time_service import quote_key
    from services.product_sources import canonical_source_url
    from services.scrape_pagination import OPERATIONAL_KINDS
    if not postal_codes:
        return {}, {}
    midnight = day_start_utc(now)
    sources = db.query(ProductSource).filter(ProductSource.sku == sku,
        ProductSource.store.in_([key for key in stores if key in HOSTS]),
        ProductSource.active.is_(True)).all()
    grouped = {}
    for source in sources:
        grouped.setdefault(source.store, []).append(source)
    if grouped:
        unfinished = db.query(ScrapeCursor.store).filter(
            ScrapeCursor.sku == sku, ScrapeCursor.store.in_(grouped),
            ScrapeCursor.kind.in_(OPERATIONAL_KINDS), ScrapeCursor.completed.is_(False),
        ).distinct()
        for (store,) in unfinished:
            grouped.pop(store, None)
    quotes = db.query(LeadTimeQuote).filter(LeadTimeQuote.sku == sku,
        LeadTimeQuote.source.in_([LABELS[key] for key in grouped]),
        LeadTimeQuote.postal_code.in_(postal_codes), LeadTimeQuote.captured_at >= midnight).all() if grouped else []
    by_key = {row.quote_key: row for row in quotes}
    unchanged, snapshots = {}, {}
    eligible = {}
    for store, known in grouped.items():
        if any(source.validation_status != 'ok' for source in known):
            continue
        # Missing destinations cannot qualify as unchanged. Leave those stores
        # to the precise collector instead of fetching their catalog twice.
        if not all(
            (row := by_key.get(quote_key(sku, {'source': LABELS[store], 'seller': source.seller, 'url': source.url}, cep)))
            and row.status in {'quoted', 'out_of_stock'}
            for source in known for cep in postal_codes
        ):
            continue
        eligible[store] = known

    def read_catalog(store):
        try:
            return catalog(store, sku)
        except Exception:
            return None

    for store, products in fair_map(read_catalog, eligible, key=lambda store: store):
        if products is None:
            continue
        snapshots[store] = products
        try:
            current = catalog_offers(store, sku, products)
            known_keys = {(canonical_source_url(source.url), source.seller) for source in eligible[store]}
            live_keys = {(canonical_source_url(item['url']), item.get('seller') or item.get('store')) for item in current}
            if not current or known_keys != live_keys:
                continue
            reused = []
            complete = True
            for item in current:
                for cep in postal_codes:
                    row = by_key.get(quote_key(sku, item, cep))
                    if row is None or row.status not in {'quoted', 'out_of_stock'}:
                        complete = False
                        break
                    old = json.loads(row.item_json)
                    if any(item.get(field) != old.get(field) for field in COMMERCIAL_FIELDS):
                        complete = False
                        break
                    if offer_available(item) and (row.shipping_cost is None or row.delivery_max_days is None):
                        complete = False
                        break
                    if not offer_available(item) and row.status != 'out_of_stock':
                        complete = False
                        break
                    reused.append({**old, 'is_cached': True,
                        'logistics_captured_at': row.captured_at.isoformat(),
                        '_same_day_price_checked': True})
                if not complete:
                    break
            if complete:
                unchanged[store] = reused
        except Exception:
            # A failed check is not evidence of an unchanged price.
            continue
    return unchanged, snapshots


def scheduled_price_check(db, sku, stores, postal_code, mode, *, now=None, job_created_at=None):
    """Verify today's known prices using public listings before doing a full refresh."""
    from crawlers.paged_discovery import fetch_search_page, search_url
    from services.extraction_resources import fair_map, remaining_request_time, request_deadline, settings
    from services.product_resolver import ProductIdentity, resolve_candidate
    from services.quick_extraction import first_page
    from services.scrape_pagination import OPERATIONAL_KINDS
    from services.scraper_service import _cached_item, _normalize_result, _offer_identity
    from services.store_registry import TARGET_STORES, find_target_store
    from services.vtex_public import HOSTS, catalog, catalog_offers

    checked_at = now or datetime.utcnow()
    midnight = day_start_utc(checked_at)
    configs = {store['key']: store for store in TARGET_STORES if store['key'] in stores
               and (mode == 'quick' or store['key'] in HOSTS or store['scope'] == 'sites')}
    if not configs:
        return {}, {}, {}
    destinations = (postal_code, f'{postal_code[:5]}-{postal_code[5:]}')
    labels = {label for store in configs.values() for label in (store['label'], *store.get('aliases', ()))}
    baseline = {}
    # Select the most recent observation per offer and destination. A recent
    # success for one seller cannot hide an older or failed observation for another.
    rows = db.query(Product, PriceHistory).join(PriceHistory, PriceHistory.product_id == Product.id).filter(
        Product.sku == sku,
        PriceHistory.source.in_(labels),
        or_(PriceHistory.destination_zip.in_(destinations), PriceHistory.destination_zip.is_(None),
            PriceHistory.destination_zip == ''),
        PriceHistory.id == latest_price_at_destination(PriceHistory),
    )
    for product, price in rows:
        store = find_target_store(price.source)
        if not store or store['key'] not in configs or not price.url or not price.timestamp:
            continue
        item = _cached_item(product, price, store['label'])
        identity = _offer_identity(item)
        scoped = price.destination_zip in destinations
        rank = (scoped and price.timestamp >= midnight, price.timestamp, price.id)
        offers = baseline.setdefault(store['key'], {})
        if identity not in offers or rank > offers[identity][0]:
            offers[identity] = (rank, item, price)

    for store, offers in list(baseline.items()):
        if any(price.is_cached or not midnight <= price.timestamp <= checked_at or
               (offer_available(item) and price.price is None) or
               (mode == 'precise' and offer_available(item) and
                (price.destination_zip not in destinations or price.shipping_cost is None or price.delivery_max_days is None))
               for _, item, price in offers.values()):
            baseline.pop(store)
    sources = {}
    for source in db.query(ProductSource).filter(ProductSource.sku == sku, ProductSource.store.in_(configs)):
        if source.active:
            sources.setdefault(source.store, []).append(source)
    if mode == 'precise':
        cursors = db.query(ScrapeCursor).filter(ScrapeCursor.sku == sku, ScrapeCursor.store.in_(configs),
                                              ScrapeCursor.kind.in_(OPERATIONAL_KINDS)).all()
        # A completed, same-day search that published no candidates can be
        # verified again cheaply. Empty pages after a block are never reused.
        for cursor in cursors:
            if (cursor.store not in sources and cursor.kind == 'search' and cursor.key == sku and
                cursor.completed and cursor.last_status in {'OK', 'OK_BROWSER'} and not cursor.candidates_found and
                cursor.last_checked_at and midnight <= cursor.last_checked_at <= checked_at):
                baseline.setdefault(cursor.store, {})
        for cursor in cursors:
            if not cursor.completed:
                baseline.pop(cursor.store, None)
    for store, offers in list(baseline.items()):
        known = sources.get(store, [])
        source_keys = {_offer_identity({'url': source.url, 'store': source.seller or configs[store]['label'],
                                       'source': configs[store]['label']}) for source in known}
        if any(source.validation_status != 'ok' for source in known) or (
            mode == 'precise' and source_keys != offers.keys()
        ):
            baseline.pop(store)
    if not baseline:
        return {}, {}, {}

    timeout = settings()['quick_timeout_seconds']
    deadline = monotonic() + timeout

    def read(store):
        try:
            with request_deadline(deadline if mode == 'quick' else None):
                remaining_request_time()
                if store in HOSTS:
                    products = catalog(store, sku, timeout, first_page_only=mode == 'quick')
                    remaining_request_time()
                    return catalog_offers(store, sku, products), products, mode == 'precise' or len(products) < 50
                if mode == 'quick':
                    items = first_page(configs[store], sku)
                    remaining_request_time()
                    return items, None, True
                diagnostics = {}
                rows, next_url, parsed = fetch_search_page(store, search_url(store, sku), timeout=timeout,
                                                          diagnostics=diagnostics, use_browser=False)
                return rows, None, parsed and not next_url and diagnostics.get('status') == 'OK'
        except Exception:
            return None, None, False

    unchanged, catalogs, listings = {}, {}, {}
    sku_identity = ProductIdentity(sku=sku, model=sku)
    # The quick collector uses first pages as well. Read its other stores in
    # the same wave and hand their results back to avoid a second pass.
    probes = list(fair_map(read, configs if mode == 'quick' else baseline, key=lambda key: key))
    for store, (items, products, complete) in probes:
        if products is not None:
            catalogs[store] = products
        if mode == 'quick':
            listings[store] = items
        if store not in baseline or not complete or items is None:
            continue
        if not items:
            if not baseline[store]:
                unchanged[store] = []
            continue
        current = {}
        for item in items:
            if not isinstance(item, dict) or item.get('_seller_unknown') or item.get('_requires_pdp_verification'):
                current = {}
                break
            try:
                normalized = _normalize_result(item, sku, configs[store]['label'])
            except (AttributeError, KeyError, TypeError, ValueError):
                current = {}
                break
            if normalized is None and resolve_candidate(sku_identity, item).accepted:
                current = {}
                break
            if normalized:
                identity = _offer_identity(normalized)
                if identity in current and current[identity] != normalized:
                    current = {}
                    break
                current[identity] = normalized
        old = baseline[store]
        if current.keys() != old.keys() or any(
            item['price'] != old[identity][1]['price']
            or offer_available(item) != offer_available(old[identity][1])
            or any(item.get(field) is not None and item[field] != old[identity][1].get(field)
                   for field in COMMERCIAL_FIELDS[:5])
            for identity, item in current.items()
        ):
            continue
        verified = _renew_confirmed_prices(db, old.values(), checked_at, job_created_at)
        if verified is not None:
            unchanged[store] = verified
    if unchanged:
        db.commit()
    return unchanged, catalogs, listings


def _renew_confirmed_prices(db, previous, checked_at, job_created_at):
    """Update known rows in one transaction without rediscovering their product IDs."""
    changes, verified = [], []
    for _, item, price in previous:
        captured = price.logistics_captured_at
        if captured is None and (item.get('shipping_cost') is not None or item.get('delivery_max_days') is not None):
            captured = price.timestamp
        reused = {**item, 'is_cached': False, 'id': price.product_id, 'current_price': item['price'],
                  'logistics_captured_at': captured, '_same_day_price_checked': True}
        if not job_created_at or price.timestamp < job_created_at:
            changes.append((price.id, price.timestamp, reused))
            reused['last_update'] = checked_at
        verified.append(reused)
    if not changes:
        return verified
    begin_report_snapshot(db)
    with db.begin_nested() as transaction:
        for price_id, timestamp, item in changes:
            affected = db.execute(update(PriceHistory).where(PriceHistory.id == price_id, PriceHistory.timestamp == timestamp)
                                  .values(timestamp=checked_at, logistics_captured_at=item['logistics_captured_at']))
            if affected.rowcount != 1:
                transaction.rollback()
                return None
        linked = {price_id for (price_id,) in db.query(OfferObservation.legacy_price_id).filter(
            OfferObservation.legacy_price_id.in_([price_id for price_id, _, _ in changes]))}
        for price_id, _, item in changes:
            record_observation(db, item, checked_at, origin=SCHEDULED_PRICE_CHECK,
                               legacy_price_id=None if price_id in linked else price_id)
    return verified
