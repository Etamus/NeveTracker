"""Speed-first collection: concurrent first pages, no PDP/browser retry cascade."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from threading import BoundedSemaphore
from time import monotonic

from services.extraction_resources import (
    ExtractionCancelled, check_cancelled, remaining_request_time, request_deadline,
    request_slot, settings, submit_in_context,
)
from services.store_registry import TARGET_STORES


_workers = ThreadPoolExecutor(max_workers=32, thread_name_prefix='quick-store')
_capacity = BoundedSemaphore(64)


def first_page(store, sku):
    timeout = min(settings()["quick_timeout_seconds"], remaining_request_time() or float('inf'))
    from services.marketplace_intervention import suspension
    access = suspension(store['key'])
    if access:
        raise ValueError(access.get('reason') or 'A loja está aguardando verificação de acesso.')
    from services.vtex_public import HOSTS, catalog, catalog_offers
    if store['key'] in HOSTS:
        return catalog_offers(store['key'], sku, catalog(store['key'], sku, timeout, first_page_only=True))
    from crawlers.paged_discovery import fetch_search_page, search_url
    diagnostics = {}
    values, _, _ = fetch_search_page(store['key'], search_url(store['key'], sku),
                                     timeout=timeout, use_browser=False, diagnostics=diagnostics)
    if not values and diagnostics.get('status') not in (None, 'OK'):
        raise ValueError(diagnostics.get('reason') or diagnostics['status'])
    return values


def _completed_stores(futures, deadline):
    pending = set(futures)
    try:
        while pending:
            check_cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            completed, pending = wait(pending, timeout=min(.1, remaining), return_when=FIRST_COMPLETED)
            for future in completed:
                yield futures[future], future
        for future in pending:
            yield futures[future], None
    finally:
        for future in pending:
            future.cancel()


def _scope_shipping(item, requested_zip):
    from services.offer_enrichment import normalize_postal_code
    from services.structured_products import LOGISTICS_FIELDS
    quoted_zip = item.get('destination_zip')
    if requested_zip and quoted_zip and normalize_postal_code(str(quoted_zip)) == requested_zip:
        return item if quoted_zip == requested_zip else {**item, 'destination_zip': requested_zip}
    return {key: value for key, value in item.items() if key not in LOGISTICS_FIELDS}


def scope_cached_results(items, postal_code):
    """For each seller listing, prefer the cached quote for the requested CEP."""
    from services.offer_enrichment import normalize_postal_code
    requested = normalize_postal_code(postal_code) if postal_code else None
    ranked = {}
    for item in items:
        scoped = _scope_shipping(item, requested)
        key = (item.get('id'), item.get('url'), item.get('source'), item.get('store'))
        local_price = not requested or bool(scoped.get('destination_zip')) or not item.get('destination_zip')
        rank = (local_price, item.get('last_update') or datetime.min)
        if key not in ranked or rank > ranked[key][0]:
            ranked[key] = (rank, scoped)
    return [value for _, value in ranked.values()]


def run_quick_extraction(sku, db, postal_code=None, scope='all', stores=None, catalog_snapshots=None, sellers=None, listing_snapshots=None):
    from services.seller_selection import seller_matches
    from services.scraper_service import (
        _normalize_result, _save_result, _cached_sku_observations, _offer_identity, build_summary_from_cached_results,
    )
    from services.offer_enrichment import normalize_postal_code
    from routes.products import _serialize_results
    from services.vtex_public import catalog_offers
    selected = [store for store in TARGET_STORES
                if (scope == 'all' or store.get('scope') == scope) and (not stores or store['key'] in stores)]
    keys = {store['key'] for store in selected}
    started = datetime.utcnow()
    fresh = processed = 0
    requested_zip = normalize_postal_code(postal_code) if postal_code else None
    errors = []
    deadline = monotonic() + settings()["quick_timeout_seconds"]

    def collect(store):
        with request_deadline(deadline):
            remaining_request_time()
            if listing_snapshots is not None and store['key'] in listing_snapshots:
                candidates = listing_snapshots[store['key']]
                if candidates is None:
                    raise ValueError('A consulta ao preço publicado falhou nesta loja.')
                return candidates, monotonic()
            if catalog_snapshots and store['key'] in catalog_snapshots:
                return catalog_offers(store['key'], sku, catalog_snapshots[store['key']]), monotonic()
            with request_slot(store['key']):
                return first_page(store, sku), monotonic()

    # Only network work runs in the shared pool. Slow stores cannot force
    # this SKU to wait past its budget or retain a database session.
    futures = {}
    try:
        for store in selected:
            check_cancelled()
            remaining = deadline - monotonic()
            while remaining > 0 and not _capacity.acquire(timeout=min(.1, remaining)):
                check_cancelled()
                remaining = deadline - monotonic()
            if remaining <= 0:
                errors.append({'store': store['key'], 'reason': 'Capacidade da coleta rápida ocupada.'})
                continue
            try:
                future = submit_in_context(_workers, collect, store)
            except BaseException:
                _capacity.release()
                raise
            future.add_done_callback(lambda _: _capacity.release())
            futures[future] = store
    except ExtractionCancelled:
        for future in futures:
            future.cancel()
        raise
    seen = set()
    for store, future in _completed_stores(futures, deadline):
        check_cancelled()
        if future is None:
            errors.append({'store': store['key'], 'reason': 'Tempo da consulta rápida esgotado.'})
            continue
        try:
            candidates, finished = future.result()
            if finished > deadline:
                raise TimeoutError('Tempo da consulta rápida esgotado.')
            for candidate in candidates:
                check_cancelled()
                try:
                    if candidate.get('_seller_unknown'):
                        candidate = {**candidate, 'seller': 'Não informado'}
                    item = _normalize_result(candidate, sku, store['label'])
                    if not item or not seller_matches(sellers,item,store['key']):
                        continue
                    identity = _offer_identity(item)
                    if identity in seen:
                        continue
                    item['sku'] = sku
                    # Never relabel generic search-page logistics as a CEP quotation.
                    item = _scope_shipping(item, requested_zip)
                    with db.begin_nested():
                        processed += _save_result(item, db)
                    seen.add(identity)
                    fresh += 1
                except Exception as exc:
                    errors.append({'store': store['key'], 'reason': str(exc)[:300]})
        except Exception as exc:
            errors.append({'store': store['key'], 'reason': str(exc)[:300]})
        if db.in_transaction():
            db.commit()
    from services.store_registry import find_target_store
    rows = list(_cached_sku_observations(db, sku, preferred_zip=requested_zip))
    saved = _serialize_results(rows, db, resolve_redirects=False, fresh_after=started)
    saved = [_scope_shipping(item, requested_zip) for item in saved
             if (store := find_target_store(item.get('source'))) and store['key'] in keys and seller_matches(sellers,item)]
    summary = build_summary_from_cached_results(saved, sku)
    summary.update(fresh_count=fresh, processed_count=processed, mode='quick', limitations=errors)
    return summary
