"""Persistent page cursors for HTML discovery in precise extraction."""

import json
import re
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from sqlalchemy.orm import object_session

from models.product import ProductSource, ScrapeCursor
from crawlers.paged_discovery import (
    STORE_HOSTS,
    amazon_offer_url,
    fetch_amazon_offer_page,
    fetch_search_page,
    fetch_seller_offer_page,
    search_url,
)
from services.product_resolver import resolve_candidate
from services.browser_health import browser_available, record_browser_result
from crawlers.indexed_discovery import SEARCH_ENGINES, fetch_indexed_page, indexed_search_url


OPERATIONAL_KINDS = frozenset({"search", "indexed_search", "seller_offers"})

REFRESH_AFTER = timedelta(hours=24)
FAILED_RETRY_AFTER = timedelta(minutes=30)
AMAZON_OFFER_PAGES_PER_RUN = 5
SELLER_OFFER_PAGES_PER_RUN = 4
BROWSER_ATTEMPTS_PER_RUN = 3
MULTI_SELLER_STORES = {"mercado_livre", "leroy_merlin", "magalu"}


def retire_redundant_seller_pages(db, sku):
    db.query(ScrapeCursor).filter(ScrapeCursor.sku == sku,
        ScrapeCursor.kind.notin_(OPERATIONAL_KINDS), ScrapeCursor.completed.is_(False)).update(
        {ScrapeCursor.completed: True, ScrapeCursor.last_status: 'AUDIT_ONLY',
         ScrapeCursor.failure_reason: 'Registro de auditoria; não é uma página da fila.'},
        synchronize_session=False)
    # A Shopee PDP belongs to one shop/item. Its sellers are discovered through
    # search pagination; the PDP is already collected as a ProductSource.
    # There is no second multi-seller page to crawl for that same listing.
    db.query(ScrapeCursor).filter_by(sku=sku, store='shopee', kind='seller_offers').update(
        {ScrapeCursor.completed: True, ScrapeCursor.last_status: 'NOT_APPLICABLE',
         ScrapeCursor.failure_reason: 'Anúncio individual já tratado pela coleta de produto.'},
        synchronize_session=False)


def _cursor(db, sku, store, kind, key, initial_url):
    cursor = db.query(ScrapeCursor).filter_by(
        sku=sku, store=store, kind=kind, key=key,
    ).first()
    if cursor is None:
        cursor = ScrapeCursor(
            sku=sku, store=store, kind=kind, key=key,
            initial_url=initial_url, next_url=initial_url,
            completed=False, updated_at=datetime.min, visited_urls="[]",
        )
        db.add(cursor)
        db.flush()
    elif not cursor.initial_url:
        cursor.initial_url = initial_url
    elif kind == 'search' and cursor.initial_url != initial_url:
        # Search routes evolve. A previous blocked route must not suppress a new
        # transport/parser for a full day or resume pagination on the old query.
        cursor.initial_url = initial_url
        cursor.next_url = initial_url
        cursor.completed = False
        cursor.visited_urls = '[]'
        cursor.last_status = None
        cursor.last_attempted_at = None
        cursor.last_checked_at = None
        cursor.browser_attempted_at = None
        cursor.browser_attempted_url = None
        cursor.failure_reason = None
    return cursor


def pagination_key(url):
    """Ignore tracking changes, while preserving real page/offset/seller tokens."""
    parsed=urlparse(str(url or ''))
    ignored={'fpig','form','ghsh','ghacc','ghpl','cvid'} if (parsed.hostname or '').endswith('bing.com') else set()
    query=sorted((key,value) for key,value in parse_qsl(parsed.query,keep_blank_values=True)
                 if key.lower() not in ignored and not key.lower().startswith('utm_'))
    return urlunparse(parsed._replace(path=parsed.path.rstrip('/') or '/',query=urlencode(query),fragment=''))


def _advance(cursor, initial_url, fetch, progress):
    now = datetime.utcnow()
    if getattr(cursor, 'kind', None) == 'search' and cursor.next_url:
        from crawlers.paged_discovery import preserve_search_scope
        if pagination_key(preserve_search_scope(initial_url, cursor.next_url)) != pagination_key(cursor.next_url):
            # Old checkpoints may already have escaped the search. Restart this
            # query so pages skipped by that historical bug are recovered too.
            cursor.next_url = initial_url
            cursor.completed = False
            cursor.visited_urls = '[]'
            cursor.last_status = None
            cursor.last_attempted_at = None
            cursor.browser_attempted_at = None
            cursor.browser_attempted_url = None
    cursor.last_checked_at = now
    if cursor.completed:
        if cursor.updated_at and now - cursor.updated_at < REFRESH_AFTER:
            return []
        cursor.next_url = initial_url
        cursor.completed = False
        cursor.visited_urls = "[]"
    from services.marketplace_intervention import suspension as pending,defer_access
    access=pending(getattr(cursor,'store',None))
    if access:
        cursor.last_status='ACCESS_REQUIRED';cursor.failure_reason=access['reason']
        return []
    current_url = cursor.next_url or initial_url
    cursor.initial_url = getattr(cursor, "initial_url", None) or initial_url
    cursor.next_url = current_url
    collection_after = progress.get("collection_after")
    failed_this_job = bool(
        collection_after and cursor.last_attempted_at and cursor.last_attempted_at >= collection_after
        and cursor.last_status not in {None, "OK", "OK_BROWSER"}
    )
    if failed_this_job and not browser_retry_due(cursor, now):
        return []
    if (
        cursor.last_status not in {None, "OK", "OK_BROWSER"}
        and cursor.browser_attempted_url == current_url
        and cursor.last_attempted_at
        and now - cursor.last_attempted_at < FAILED_RETRY_AFTER
        and not browser_retry_due(cursor, now)
    ):
        return []
    diagnostics = {"status": cursor.last_status, "reason": cursor.failure_reason} if failed_this_job else {}
    # Persist the checkpoint before network I/O. A newly flushed cursor used to
    # retain SQLite's write lock during navigation, blocking other collectors.
    session = object_session(cursor) if hasattr(cursor, "_sa_instance_state") else None
    cursor.last_attempted_at = now
    if session is not None:
        session.commit()
    items, next_url, success = [], None, False
    if not failed_this_job:
        try:
            items, next_url, success = fetch(current_url, diagnostics=diagnostics)
        except Exception as exc:
            diagnostics.update(status="PARSER_ERROR", reason=str(exc)[:300])
    cursor.last_attempted_at = now
    used_browser = False
    browser_checked=False
    if not success and (
        browser_available(current_url)
        and diagnostics.get("status") != "SEARCH_MISMATCH"
        and progress.get("browser_attempts", 0) < BROWSER_ATTEMPTS_PER_RUN
        and cursor.store not in progress.get("browser_stores", set())
        and (
            cursor.browser_attempted_url != current_url
            or not cursor.browser_attempted_at
            or now - cursor.browser_attempted_at >= REFRESH_AFTER
            or browser_retry_due(cursor, now)
        )
    ):
        progress["browser_attempts"] = progress.get("browser_attempts", 0) + 1
        progress.setdefault("browser_stores", set()).add(cursor.store)
        cursor.browser_attempted_at = now
        cursor.browser_attempted_url = current_url
        browser_diagnostics = {}
        browser_checked=True
        try:
            browser_items, browser_next, browser_success = fetch(
                current_url, diagnostics=browser_diagnostics, use_browser=True,
            )
        except Exception as exc:
            browser_items, browser_next, browser_success = [], None, False
            browser_diagnostics.update(status="PARSER_ERROR", reason=str(exc)[:300])
        if cursor.store != "mercado_livre":
            record_browser_result(current_url, "OK" if browser_success else browser_diagnostics.get("status"))
        if browser_success:
            items, next_url, success = browser_items, browser_next, True
            diagnostics = browser_diagnostics
            used_browser = True
        else:
            if cursor.store in {'mercado_livre','shopee'} and browser_diagnostics.get('status'):
                diagnostics['status']=browser_diagnostics['status']
            diagnostics["reason"] = (
                f"{diagnostics.get('reason') or 'HTML indisponivel'}; "
                f"navegador: {browser_diagnostics.get('reason') or 'sem resultado'}"
            )
    if not success:
        cursor.last_status = diagnostics.get("status") or "PARSER_ERROR"
        cursor.failure_reason = str(diagnostics.get("reason") or "Pagina indisponivel")[:500]
        if cursor.store in {'mercado_livre','shopee'} and cursor.last_status in {'BLOCKED','LOGIN_REQUIRED','ACCESS_REQUIRED'} and browser_checked:
            from services.marketplace_intervention import require_access
            require_access(cursor.store,cursor.sku,cursor.failure_reason,current_url)
        elif cursor.last_status in {'BROWSER_BUSY','BROWSER_ERROR','TIMEOUT','RETRY_LATER','LOGIN_PENDING'}:
            defer_access(cursor.store,cursor.sku,cursor.failure_reason)
        return []
    if used_browser:
        from services.marketplace_intervention import record_transport_recovery
        record_transport_recovery(cursor.store)
    cursor.last_status = "OK_BROWSER" if used_browser else "OK"
    if next_url and getattr(cursor, 'kind', None) == 'search':
        from crawlers.paged_discovery import preserve_search_scope
        next_url = preserve_search_scope(initial_url, next_url)
    cursor.failure_reason = None
    try:
        visited = set(json.loads(cursor.visited_urls or "[]"))
    except (TypeError, ValueError):
        visited = set()
    visited.add(current_url)
    if next_url and pagination_key(next_url) in {pagination_key(value) for value in visited}:
        next_url = None
    if items:
        identities=sorted({(pagination_key(item.get('url')),str(item.get('seller') or item.get('store') or ''))
                           for item in items if item.get('url')})
        if identities:
            fingerprint='page-sha256:'+hashlib.sha256(json.dumps(identities,ensure_ascii=False).encode()).hexdigest()
            if fingerprint in visited:next_url=None
            visited.add(fingerprint)
    cursor.visited_urls = json.dumps(sorted(visited))
    cursor.next_url = next_url or initial_url
    cursor.completed = not bool(next_url)
    cursor.updated_at = now
    cursor.pages_completed = (cursor.pages_completed or 0) + 1
    cursor.candidates_found = (cursor.candidates_found or 0) + len(items)
    progress["pages_advanced"] = progress.get("pages_advanced", 0) + 1
    if used_browser:
        for item in items:
            item["discovery_method"] = "browser_pages"
    return items


def _asin(url):
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(url or ""), re.I)
    return match.group(1).upper() if match else None


def enqueue_seller_offer_page(db, identity, store_key, candidate):
    if store_key not in MULTI_SELLER_STORES or not resolve_candidate(identity, candidate).accepted:
        return None
    parsed = urlparse(str(candidate.get("url") or ""))
    host = (parsed.hostname or "").lower()
    expected = STORE_HOSTS[store_key]
    from services.magalu_public import is_public_storefront
    if not (host == expected or host.endswith(f".{expected}") or store_key == 'magalu' and is_public_storefront(candidate.get('url', ''))):
        return None
    if not parsed.path or parsed.path == "/":
        return None
    base_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    key = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:32]
    return _cursor(db, identity.sku, store_key, "seller_offers", key, base_url)


def discover_html_pages(db, identity, store, timeout, progress=None):
    """Advance one search page per term and a few Amazon seller pages per run."""
    store_key = store["key"]
    if store_key not in STORE_HOSTS:
        return []

    progress = progress if progress is not None else {}
    candidates = []
    for term in tuple(dict.fromkeys((identity.sku, *identity.search_terms)))[:3]:
        initial_url = search_url(store_key, term)
        cursor = _cursor(db, identity.sku, store_key, "search", term, initial_url)
        page_items = _advance(
            cursor, initial_url,
            lambda url, **kwargs: fetch_search_page(store_key, url, timeout, **kwargs),
            progress,
        )
        candidates.extend(page_items)
        if store_key == "amazon":
            for item in page_items:
                if _asin(item.get("url")) and resolve_candidate(identity, item).accepted:
                    asin = _asin(item["url"])
                    _cursor(db, identity.sku, "amazon", "seller_offers", asin, amazon_offer_url(asin))
        elif store_key in MULTI_SELLER_STORES:
            for item in page_items:
                enqueue_seller_offer_page(db, identity, store_key, item)

    if store_key == "amazon":
        cutoff = datetime.utcnow() - REFRESH_AFTER
        due = (
            db.query(ScrapeCursor)
            .filter(
                ScrapeCursor.sku == identity.sku,
                ScrapeCursor.store == "amazon",
                ScrapeCursor.kind == "seller_offers",
                (ScrapeCursor.completed.is_(False)) | (ScrapeCursor.updated_at < cutoff),
            )
            .order_by(ScrapeCursor.last_attempted_at.asc(), ScrapeCursor.id.asc())
            .all()
        )
        for cursor in _seller_page_batch(due, AMAZON_OFFER_PAGES_PER_RUN):
            candidates.extend(_advance(
                cursor, amazon_offer_url(cursor.key),
                lambda url, asin=cursor.key, **kwargs: fetch_amazon_offer_page(
                    asin, url, identity, timeout, **kwargs,
                ),
                progress,
            ))
    elif store_key in MULTI_SELLER_STORES:
        cutoff = datetime.utcnow() - REFRESH_AFTER
        due = (
            db.query(ScrapeCursor)
            .filter(
                ScrapeCursor.sku == identity.sku,
                ScrapeCursor.store == store_key,
                ScrapeCursor.kind == "seller_offers",
                (ScrapeCursor.completed.is_(False)) | (ScrapeCursor.updated_at < cutoff),
            )
            .order_by(ScrapeCursor.last_attempted_at.asc(), ScrapeCursor.id.asc())
            .all()
        )
        for cursor in _seller_page_batch(due, SELLER_OFFER_PAGES_PER_RUN):
            candidates.extend(_advance(
                cursor, cursor.initial_url or cursor.next_url,
                lambda url, **kwargs: fetch_seller_offer_page(
                    store_key, url, identity, timeout, **kwargs,
                ),
                progress,
            ))

    return candidates


def discover_indexed_pages(db, identity, store, timeout, progress=None):
    """Advance each independent domain search; cursors resume beyond the first page."""
    progress = progress if progress is not None else {}
    items = []
    # The manufacturer's code anchors this search. Marketplace cards are validated
    # separately, so kits and unrelated compatibility mentions cannot add offers.
    for engine in SEARCH_ENGINES:
        initial = indexed_search_url(engine, store["key"], identity.sku)
        cursor = _cursor(db, identity.sku, store["key"], "indexed_search", engine, initial)
        items.extend(_advance(
            cursor, initial,
            lambda url, engine=engine, **kwargs: fetch_indexed_page(
                engine, store["key"], url, timeout, **kwargs,
            ), progress,
        ))
    return items


def _seller_page_batch(cursors, limit):
    now = datetime.utcnow()
    eligible = [cursor for cursor in cursors if (
        cursor.completed or cursor.last_attempted_at is None
        or cursor.last_status in {None, "OK", "OK_BROWSER"}
        or browser_retry_due(cursor, now)
    )]
    from services.mercado_livre_browser import public_http_available
    deferred = [cursor for cursor in eligible if getattr(cursor, 'store', None) == 'mercado_livre'
                and not public_http_available(cursor.next_url or cursor.initial_url)
                and not browser_available(cursor.next_url or cursor.initial_url)]
    # No network work can happen during the two confirmed login pauses. Record
    # the explicit limitation for every pending URL now, rather than spending
    # dozens of queue rounds cycling through groups of four inaccessible pages.
    deferred_ids = {id(cursor) for cursor in deferred}
    return [cursor for cursor in eligible if id(cursor) not in deferred_ids][:limit] + deferred


def browser_retry_due(cursor, now=None):
    now = now or datetime.utcnow()
    if getattr(cursor, "kind", None) is not None and cursor.kind not in OPERATIONAL_KINDS:
        return False
    if getattr(cursor, 'last_status', None) == 'SEARCH_MISMATCH':
        # _advance deliberately excludes these pages from browser recovery.
        # Classifying them as actionable creates a permanent no-op queue loop.
        return False
    if getattr(cursor, 'kind', None) == 'indexed_search':
        # A store-session validation cannot unlock Bing/DuckDuckGo. Their
        # failed HTTP cursors have no browser fallback and used to stay due
        # forever simply because browser_attempted_at was empty.
        return False
    from services.marketplace_intervention import suspension as pending,released_after
    if pending(getattr(cursor,'store',None)):return False
    if getattr(cursor,'store',None) in {'mercado_livre','shopee'} and not cursor.completed and cursor.last_status not in {None,'OK','OK_BROWSER'}:
        if released_after(cursor.store,cursor.last_attempted_at):return True
    if getattr(cursor,'store',None)=='shopee' and not cursor.completed:
        from services.marketplace_session import validated_after
        if validated_after('shopee',cursor.browser_attempted_at or cursor.last_attempted_at):return True
    if getattr(cursor, 'store', None) == 'mercado_livre' and not cursor.completed and cursor.last_status not in {None, 'OK', 'OK_BROWSER'}:
        from services.mercado_livre_session import validated_after
        if validated_after(cursor.browser_attempted_at or cursor.last_attempted_at):
            return True
    if not browser_available(cursor.next_url or getattr(cursor, "initial_url", None)):
        return False
    return bool(
        not cursor.completed
        and cursor.last_status not in {None, "OK", "OK_BROWSER"}
        and (
            cursor.browser_attempted_url != (cursor.next_url or getattr(cursor, "initial_url", None))
            or not cursor.browser_attempted_at
            or now - cursor.browser_attempted_at >= REFRESH_AFTER
        )
    )


def coverage_by_store(db, sku, stores, sellers=None):
    cursors = db.query(ScrapeCursor).filter(ScrapeCursor.sku == sku, ScrapeCursor.kind.in_(OPERATIONAL_KINDS)).all()
    sources = db.query(ProductSource).filter(
        ProductSource.sku == sku, ProductSource.active.is_(True),
    ).all()
    if sellers:
        from services.seller_selection import seller_matches
        sources=[source for source in sources if seller_matches(sellers,source,source.store,allow_unknown=True)]
    result = []
    for store in stores:
        key = store["key"]
        pages = [cursor for cursor in cursors if cursor.store == key]
        found = [source for source in sources if source.store == key]
        failed = [cursor for cursor in pages if not cursor.completed and cursor.last_status not in {None, "OK", "OK_BROWSER"}]
        failed_sources = [
            source for source in found
            if source.validation_status in {"blocked", "timeout", "parser_error"}
        ]
        limited_sources = [source for source in found if source.validation_status == 'partial']
        result.append({
            "store": key,
            "label": store["label"],
            "searches": sum(cursor.kind == "search" for cursor in pages),
            "indexed_searches": sum(cursor.kind == "indexed_search" for cursor in pages),
            "seller_pages": sum(cursor.kind == "seller_offers" for cursor in pages),
            "pages_completed": sum(cursor.pages_completed or 0 for cursor in pages),
            "candidates_found": sum(cursor.candidates_found or 0 for cursor in pages),
            "open_pages": sum(not cursor.completed for cursor in pages),
            "unattempted_pages": sum(not cursor.completed and cursor.last_attempted_at is None for cursor in pages),
            "failed_pages": len(failed),
            "browser_pending": sum(browser_retry_due(cursor) for cursor in pages),
            "browser_recovered": sum(cursor.last_status == "OK_BROWSER" for cursor in pages),
            "sources": len(found),
            "sellers": len({source.seller for source in found if source.seller}),
            "sources_without_identified_seller": sum(not source.seller for source in found),
            "failed_sources": len(failed_sources),
            "limited_sources": len(limited_sources),
            "failed_sources_without_price": sum(source.listing_price is None for source in failed_sources),
            "coverage_status": (
                "pending" if any(not cursor.completed and (cursor.last_attempted_at is None or cursor.last_status in {"OK", "OK_BROWSER"}) for cursor in pages)
                else "limited" if failed or failed_sources or limited_sources
                else "searched" if pages
                else "not_searched"
            ),
            "failures": [
                {"kind": cursor.kind, "status": cursor.last_status, "reason": cursor.failure_reason}
                for cursor in failed[:5]
            ],
            "source_failures": [
                {
                    "seller": source.seller,
                    "status": source.validation_status,
                    "reason": source.failure_reason,
                    "listing_price_available": source.listing_price is not None,
                }
                for source in failed_sources[:5]
            ],
            "limitations": [{"seller": source.seller, "reason": source.failure_reason} for source in limited_sources[:5]],
        })
    return result
