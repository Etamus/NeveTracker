from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import math
import re
from types import SimpleNamespace
from sqlalchemy.orm import sessionmaker
from threading import Condition, RLock
from cachetools import TTLCache, cached
from urllib.parse import urlparse

from crawlers.paged_discovery import STORE_HOSTS
from crawlers.registry import collect_product_source
from crawlers.scrapy_engine import collect_sources_with_scrapy
from services.offer_enrichment import enrich_offers, normalize_postal_code
from models.product import KnownSku, PriceHistory, Product, ProductSource, ScrapeCursor
from services.bing_shopping import search_bing_shopping
from scrapers.comparison import scrape_comparison_offers
from services.crawler_control import get_control
from services.integration_settings import (
    get_runtime_provider_settings,
)
from services.product_resolver import identity_from_models, resolve_candidate
from services.scrape_pagination import (
    browser_retry_due, coverage_by_store, discover_html_pages, discover_indexed_pages, enqueue_seller_offer_page,
)
from services.product_sources import (
    active_sources,
    mark_source_result,
    upsert_product_source,
)
from services.playwright_fallback import collect_product_with_playwright, collect_shipping_with_playwright
from services.store_registry import TARGET_STORES, find_target_store
from services.structured_products import merge_offer_details, offer_available
from services.browser_health import browser_available, record_browser_result


MAX_COLLECTION_PER_RUN = 40
MAX_COLLECTION_PER_STORE = 12
MAX_DISCOVERY_STORES_PER_RUN = 3
FAILED_SOURCE_RETRY_AFTER = timedelta(minutes=30)
_external_cache = TTLCache(maxsize=1000, ttl=900)
_external_condition = Condition(RLock())


@cached(_external_cache, key=lambda search, term: (search, term.strip().lower()), condition=_external_condition)
def _shared_external_search(search, term):
    # Parallel storefronts share one in-flight comparator request for this SKU.
    return search(term) or []


def _source_due(source, collection_after, now):
    if collection_after and source.last_attempted_at and source.last_attempted_at >= collection_after:
        return False
    if getattr(source, 'store', None) == 'mercado_livre':
        from services.mercado_livre_session import validated_after
        if validated_after(source.last_attempted_at):
            return True
    return not (
        source.validation_status in {"blocked", "timeout", "parser_error"}
        and source.last_attempted_at
        and now - source.last_attempted_at < FAILED_SOURCE_RETRY_AFTER
    )


def _source_batch(sources, limit=MAX_COLLECTION_PER_RUN, per_store=MAX_COLLECTION_PER_STORE):
    grouped = defaultdict(list)
    for source in sources:
        grouped[source.store].append(source)
    selected = []
    for index in range(per_store):
        for store_sources in grouped.values():
            if index < len(store_sources):
                selected.append(store_sources[index])
                if len(selected) >= limit:
                    return selected
    return selected


def _identity(db, sku):
    known = db.query(KnownSku).filter(KnownSku.sku == sku).first()
    if not known:
        known = KnownSku(sku=sku)
        db.add(known)
        db.flush()
    products = db.query(Product).filter(Product.sku == sku).all()
    return identity_from_models(known, products), products


def _bootstrap_existing_sources(db, identity, products):
    created = 0
    for product in products:
        latest_by_url = {}
        for price in product.prices:
            current = latest_by_url.get(price.url)
            if not current or (price.timestamp or datetime.min) > (current.timestamp or datetime.min):
                latest_by_url[price.url] = price
        for price in latest_by_url.values():
            store = find_target_store(price.source, price.store, price.url)
            if not store:
                continue
            existing_source = (
                db.query(ProductSource.id)
                .filter(
                    ProductSource.sku == identity.sku,
                    ProductSource.store == store["key"],
                    ProductSource.url == price.url,
                )
                .first()
            )
            if existing_source:
                continue
            candidate = {
                "name": product.name,
                "url": price.url,
                "seller": price.store,
                "brand": product.brand,
            }
            match = resolve_candidate(identity, candidate)
            if match.accepted and upsert_product_source(db, identity, store["key"], candidate, match):
                created += 1
    if created:
        db.commit()
    return created


def _direct_candidates(store, identity, timeout):
    if store["key"] in {"amazon", "magalu"}:
        return []

    candidates = []
    scrapers = tuple(
        scraper for scraper in store.get("direct_scrapers", ())
        if not (store["key"] == "mercado_livre" and scraper.__name__ == "scrape_mercadoLivre")
    )
    mercado_livre_mode = "always"
    if store["key"] == "mercado_livre":
        settings = get_runtime_provider_settings("mercado_livre")
        if not settings["enabled"] or not settings["configured"]:
            return []
        mercado_livre_mode = settings["usage_mode"]
        if mercado_livre_mode == "fallback":
            scrapers = tuple(sorted(
                scrapers,
                key=lambda scraper: scraper.__name__ == "search_mercado_livre",
            ))
    for scraper in scrapers:
        if (
            scraper.__name__ == "search_mercado_livre"
            and mercado_livre_mode == "fallback"
            and candidates
        ):
            continue
        for term in tuple(dict.fromkeys((identity.sku, *identity.search_terms)))[:3]:
            try:
                result = scraper(term) or []
            except Exception as exc:
                print(f"[DISCOVERY][{store['key'].upper()}] PARSER_ERROR: {exc}")
                result = []
            candidates.extend(result)
    return candidates


def _belongs_to_store(item, store_key):
    matched = find_target_store(item.get("source"), item.get("store"), item.get("url"))
    return bool(matched and matched["key"] == store_key)


def _valid_store_url(url, store_key):
    if store_key == "magalu":
        from services.magalu_public import is_public_storefront
        if is_public_storefront(url):
            return urlparse(url).scheme == "https"
    hostname = (urlparse(str(url or "")).hostname or "").lower()
    expected = STORE_HOSTS.get(store_key, "")
    return bool(expected and (hostname == expected or hostname.endswith(f".{expected}")))


def _valid_offer(identity, item):
    store = find_target_store(item.get("source"))
    price = item.get("price")
    seller = str(item.get("seller") or item.get("store") or "").strip()
    title_skus = {value.upper() for value in re.findall(r"\bW\d{8}\b", str(item.get("name") or ""), re.I)}
    if (
        not store or not _valid_store_url(item.get("url"), store["key"])
        or not seller or isinstance(price, bool)
        or (price is None and offer_available(item))
        or (price is not None and (not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0))
        or (re.fullmatch(r'W\d{8}', identity.sku.upper()) and title_skus and identity.sku.upper() not in title_skus)
        or not resolve_candidate(identity, item).accepted
    ):
        return False
    shipping = item.get("shipping_cost")
    if shipping is not None and (
        isinstance(shipping, bool) or not isinstance(shipping, (int, float))
        or not math.isfinite(shipping) or shipping < 0
    ):
        return False
    for field in ("delivery_min_days", "delivery_max_days"):
        value = item.get(field)
        if value is None:
            continue
        try:
            numeric_days = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            isinstance(value, bool) or not math.isfinite(numeric_days)
            or numeric_days < 0 or not numeric_days.is_integer()
        ):
            return False
        item[field] = int(numeric_days)
    minimum = item.get("delivery_min_days")
    maximum = item.get("delivery_max_days")
    return not (minimum is not None and maximum is not None and maximum < minimum)


def _external_candidates(identity, store_key):
    result = []
    seen = set()
    for term in tuple(dict.fromkeys((identity.sku, *identity.search_terms)))[:2]:
        for search in (search_bing_shopping, scrape_comparison_offers):
            try:
                candidates = _shared_external_search(search, term)
            except Exception as exc:
                print(f"[DISCOVERY][{store_key.upper()}] SEARCH_ERROR: {exc}")
                continue
            for item in candidates:
                key = (item.get("url"), item.get("seller") or item.get("store"))
                if _belongs_to_store(item, store_key) and key not in seen:
                    seen.add(key)
                    result.append(item)
    return result


def _html_and_parallel_api(db, identity, store, control, progress, include_other_sources):
    """Overlap an explicitly enabled always-on API with public HTML discovery.

    Only the calling thread accesses the SQLAlchemy session. Fallback-only
    providers retain their conditional use and do not consume extra quota.
    """
    settings = get_runtime_provider_settings('mercado_livre') if store['key'] == 'mercado_livre' else {}
    parallel = include_other_sources and getattr(control, 'api_with_scraping', True) and settings.get('enabled') and settings.get('configured') and settings.get('usage_mode') == 'always'
    def html():
        try:
            return discover_html_pages(db, identity, store, control.request_timeout_seconds, progress=progress)
        except Exception as exc:
            print(f"[DISCOVERY][{store['key'].upper()}] HTML_ERROR: {exc}")
            return []
    if not parallel:
        return html(), None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_direct_candidates, store, identity, control.request_timeout_seconds)
        candidates = html()
        try:
            direct = future.result()
        except Exception as exc:
            print(f"[DISCOVERY][{store['key'].upper()}] API_ERROR: {exc}")
            direct = []
        return candidates, direct


def _discover_store(
    db, identity, store, control, known_source_ids=None, retry_failed=False,
    progress=None, include_other_sources=True,
):
    known_source_ids = set(known_source_ids or ())
    saved = []
    seen = set()
    seen_with_price = set()
    saved_ids = set()

    def save_candidates(candidates, method):
        for candidate in candidates:
            candidate_method = candidate.get("discovery_method") or method
            if not _valid_store_url(candidate.get("url"), store["key"]):
                continue
            title_skus = {value.upper() for value in re.findall(
                r"\bW\d{8}\b", str(candidate.get("name") or candidate.get("title") or ""), re.I,
            )}
            if re.fullmatch(r'W\d{8}', identity.sku.upper()) and title_skus and identity.sku.upper() not in title_skus:
                continue
            identity_key = (candidate.get("url"), candidate.get("seller") or candidate.get("store"))
            has_price = isinstance(candidate.get("price"), (int, float)) and candidate["price"] > 0
            if identity_key in seen and (not has_price or identity_key in seen_with_price):
                continue
            seen.add(identity_key)
            if has_price:
                seen_with_price.add(identity_key)
            match = resolve_candidate(identity, candidate)
            if not match.accepted and candidate.get('_requires_pdp_verification'):
                db.commit()
                provisional = ProductSource(sku=identity.sku, store=store['key'], url=candidate['url'],
                                            seller=candidate.get('seller'))
                verified = collect_product_source(provisional, identity, None, control.request_timeout_seconds)
                confirmed = next((item for item in verified.get('items', []) if _valid_offer(identity, item)), None)
                if confirmed:
                    candidate = confirmed
                    candidate_method = 'search_pdp_verified'
                    match = resolve_candidate(identity, candidate)
            if not match.accepted:
                print(f"[MATCH][{store['key'].upper()}] INVALID_MATCH para {identity.sku}")
                continue
            source = upsert_product_source(db, identity, store["key"], candidate, match)
            if source:
                enqueue_seller_offer_page(db, identity, store["key"], candidate)
            if source and has_price:
                source.listing_method = candidate_method
            if source and source.id not in saved_ids and (not retry_failed or source.id not in known_source_ids):
                saved.append(source)
                saved_ids.add(source.id)
                print(
                    f"[DISCOVERY][{store['key'].upper()}] {identity.sku} via {match.method} "
                    f"({match.confidence:.2f}, {candidate_method})"
                )

    html_candidates, parallel_direct = _html_and_parallel_api(db, identity, store, control, progress, include_other_sources)
    save_candidates(html_candidates, "html_pages")
    db.commit()
    if control.external_discovery_fallback:
        indexed_candidates = discover_indexed_pages(
            db, identity, store, control.request_timeout_seconds, progress=progress,
        )
        save_candidates(indexed_candidates, "indexed_search")
        db.commit()
    skip_fallback_api = (
        store["key"] == "mercado_livre"
        and (not getattr(control, 'api_with_scraping', True) or (
            get_runtime_provider_settings("mercado_livre")["usage_mode"] == "fallback"
            and (html_candidates or (known_source_ids and not retry_failed))))
    )
    direct_candidates = parallel_direct if parallel_direct is not None else (
        [] if skip_fallback_api or not include_other_sources
        else _direct_candidates(store, identity, control.request_timeout_seconds)
    )
    save_candidates(direct_candidates, "direct")
    db.commit()
    if (
        include_other_sources and not saved and not direct_candidates
        and not skip_fallback_api and store.get("direct_scrapers")
    ):
        from services.scraper_service import _run_store_scrapers

        try:
            quick_candidates = _run_store_scrapers(store, identity.sku)
        except Exception as exc:
            print(f"[FALLBACK][{store['key'].upper()}] QUICK_ERROR: {exc}")
            quick_candidates = []
        if quick_candidates:
            print(f"[FALLBACK][{store['key'].upper()}] Busca rapida acionada")
            save_candidates(quick_candidates, "quick_fallback")
    if (
        include_other_sources
        and control.external_discovery_fallback
    ):
        save_candidates(_external_candidates(identity, store["key"]), "external_fallback")
    db.commit()
    if not saved:
        print(f"[DISCOVERY][{store['key'].upper()}] NOT_FOUND para {identity.sku}")
    return saved


def _collect_sources(db, identity, sources, postal_code, control, progress=None):
    if not sources:
        return [], set()
    grouped = defaultdict(list)
    for source in sources:
        grouped[source.store].append(source)
    collected = []
    logistics_indices = []
    successful_stores = set()
    http_sources = [source for source in sources if source.store not in {"mercado_livre", "shopee"}]
    try:
        http_results = collect_sources_with_scrapy(
            http_sources, identity,
            control.concurrent_requests_per_domain,
            control.request_timeout_seconds,
        ) if http_sources else {}
    except Exception as exc:
        http_results = {
            source.id: {"status": "PARSER_ERROR", "reason": str(exc)}
            for source in http_sources
        }
    for store_key, store_sources in grouped.items():
        selected = store_sources
        if store_key in {"mercado_livre", "shopee"}:
            workers = min(control.concurrent_requests_per_domain, len(selected))
            collector = collect_product_source
            if store_key == 'mercado_livre' and not getattr(control, 'api_with_scraping', True):
                from crawlers.mercado_livre_public import collect_public_product
                collector = collect_public_product
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                futures = {
                    executor.submit(
                        collector,
                        source,
                        identity,
                        postal_code,
                        control.request_timeout_seconds,
                    ): source
                    for source in selected
                }
                results = {}
                for future in as_completed(futures):
                    source = futures[future]
                    try:
                        results[source.id] = future.result()
                    except Exception as exc:
                        results[source.id] = {"status": "PARSER_ERROR", "reason": str(exc)}
        else:
            results = http_results

        browser_sources = [source for source in selected if (
            getattr(control, "playwright_product_fallback", False)
            and browser_available(source.url)
            and (results.get(source.id) or {}).get("status") in {"BLOCKED", "TIMEOUT", "PARSER_ERROR"}
            and not (results.get(source.id) or {}).get("_retry_after")
        )]
        if browser_sources:
            if progress:
                progress(f"{identity.sku}: abrindo {store_key} no navegador para recuperar paginas")
            first = browser_sources[0]
            http_status = (results.get(first.id) or {}).get("status")
            browser_result = collect_product_with_playwright(
                first, identity, postal_code, control.request_timeout_seconds,
                include_shipping=bool(getattr(control, "playwright_shipping_fallback", False)),
            )
            if browser_result.get("status") != "BROWSER_ERROR":
                results[first.id] = browser_result
            browser_status = browser_result.get("status")
            failed_block_recovery = http_status == "BLOCKED" and browser_status not in {"OK", "OUT_OF_STOCK"}
            if store_key != 'mercado_livre':
                record_browser_result(first.url, 'OK' if browser_status == 'OK' else 'BLOCKED' if failed_block_recovery else browser_status)
            if (
                browser_status not in {"BLOCKED", "BROWSER_ERROR", "LOGIN_REQUIRED"} and not failed_block_recovery
                or store_key == 'mercado_livre' and browser_status == 'BLOCKED' and browser_available(first.url)
            ):
                workers = min(2, control.concurrent_requests_per_domain)
                with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                    futures = {executor.submit(
                        collect_product_with_playwright, source, identity,
                        postal_code, control.request_timeout_seconds,
                        include_shipping=bool(getattr(control, "playwright_shipping_fallback", False)),
                    ): source for source in browser_sources[1:]}
                    for future in as_completed(futures):
                        source = futures[future]
                        try:
                            recovered = future.result()
                            if recovered.get("status") != "BROWSER_ERROR":
                                results[source.id] = recovered
                        except Exception as exc:
                            print(f"[FALLBACK][{store_key.upper()}] BROWSER_ERROR: {exc}")

        for source in selected:
            result = results.get(source.id) or {
                "status": "PARSER_ERROR", "reason": "Coletor nao retornou esta fonte",
            }
            status = result.get("status") or "PARSER_ERROR"
            mark_source_result(source, status, result.get("reason"))
            if status == "OK":
                items = result.get("items") or [result["item"]]
                successful_stores.add(store_key)
                offer_urls = [offer.get("url") for offer in items]
                for item in items:
                    if not _valid_offer(identity, item):
                        print(f"[COLLECTION][{store_key.upper()}] Oferta de vendedor invalida")
                        continue
                    seller_match = resolve_candidate(identity, item)
                    seller_source = upsert_product_source(db, identity, store_key, item, seller_match)
                    if seller_source and seller_source.id != source.id:
                        mark_source_result(seller_source, 'OK')
                        seller_source.listing_method = "pdp_offers"
                    if (
                        postal_code and store_key != "mercado_livre" and offer_urls.count(item.get("url")) == 1
                        and offer_available(item) and item.get("_logistics_attempted_zip") != postal_code
                        and not (item.get("destination_zip") == postal_code and item.get("shipping_cost") is not None and item.get("delivery_max_days") is not None)
                    ):
                        logistics_indices.append(len(collected))
                    collected.append(item)
                source.match_confidence = result["match"].confidence
                source.match_method = result["match"].method
                if result.get('seller_coverage_limited'):
                    source.validation_status = 'partial'
                    source.failure_reason = (
                        f"Pagina anuncia {result.get('declared_seller_offers')} ofertas; "
                        f"{len(items)} ofertas individuais publicas foram lidas. "
                        "A abertura de mais opcoes pode exigir login no Mercado Livre."
                    )
            else:
                candidate = result.get('candidate')
                if store_key == 'shopee' and candidate and resolve_candidate(identity, candidate).accepted:
                    # Public metadata can confirm identity while prices remain
                    # redacted. Preserve discovery, not a fabricated price.
                    source.listing_name = candidate.get('name') or source.listing_name
                    source.shop_id = candidate.get('shop_id') or source.shop_id
                    source.item_id = candidate.get('item_id') or source.item_id
                if status == "OUT_OF_STOCK":
                    db.query(PriceHistory).filter(
                        PriceHistory.url == source.url, PriceHistory.store == source.seller,
                    ).update({PriceHistory.availability: "out_of_stock"}, synchronize_session=False)
                print(
                    f"[COLLECTION][{store_key.upper()}] {status}: "
                    f"{result.get('reason') or identity.sku}"
                )
                listing_price = getattr(source, "listing_price", None)
                listing_seen_at = getattr(source, "listing_seen_at", None)
                if (
                    status in {"BLOCKED", "TIMEOUT", "PARSER_ERROR"}
                    and listing_price is not None
                    and listing_seen_at is not None
                    and datetime.utcnow() - listing_seen_at <= timedelta(hours=24)
                ):
                    collected.append({
                        "name": getattr(source, "listing_name", None) or identity.name or identity.sku,
                        "sku": identity.sku,
                        "brand": source.brand or identity.brand or store_key,
                        "category": identity.name or identity.sku,
                        "source": next(
                            (store["label"] for store in TARGET_STORES if store["key"] == store_key),
                            store_key,
                        ),
                        "store": source.seller,
                        "seller": source.seller,
                        "url": source.url,
                        "price": listing_price,
                        "image_url": None,
                        "match_confidence": source.match_confidence,
                        "match_method": source.match_method,
                        "discovery_source": f"{getattr(source, 'listing_method', None) or 'search'}_listing",
                        "captured_at": listing_seen_at.isoformat(),
                        "is_cached": True,
                    })
                    print(f"[COLLECTION][{store_key.upper()}] Preco preservado da listagem para {source.seller}")
        db.commit()
    if logistics_indices:
        if progress:
            progress(f"{identity.sku}: cotando frete e prazo de {len(logistics_indices)} ofertas para {postal_code}")
        http_indices = [index for index in logistics_indices if collected[index].get('_http_logistics_attempted_zip') != postal_code]
        enriched = enrich_offers([collected[index] for index in http_indices], postal_code, force_refresh=True,
                                 per_domain=control.concurrent_requests_per_domain) if http_indices else []
        for index, item in zip(http_indices, enriched):
            if item.get("destination_zip") == postal_code:
                item["shipping_zip_code"] = postal_code
            collected[index] = item
        if control.playwright_shipping_fallback:
            missing = [
                index for index in logistics_indices
                if collected[index].get("shipping_cost") is None
                or collected[index].get("delivery_max_days") is None
            ]
            by_host = defaultdict(list)
            for index in missing:
                by_host[urlparse(collected[index]["url"]).netloc].append(index)
            remaining = []
            def probe_host(pair):
                host, indices = pair
                return host, indices, collect_shipping_with_playwright(
                    collected[indices[0]]["url"], postal_code, control.request_timeout_seconds)
            with ThreadPoolExecutor(max_workers=min(2, len(by_host)) or 1) as executor:
                probes = list(executor.map(probe_host, by_host.items()))
            for host, indices, shipping in probes:
                first = indices[0]
                if shipping.get("_blocked_status") in {403, 429}:
                    print(f"[FALLBACK][{host}] Navegador bloqueado; demais paginas ficam para outra extracao")
                    continue
                if shipping.get('_unsupported_shipping'):
                    print(f"[FALLBACK][{host}] Nenhum formulario ou integracao de frete acessivel; demais URLs do host nao repetem essa tentativa neste lote")
                    continue
                collected[first] = merge_offer_details(collected[first], shipping)
                remaining.extend(indices[1:])
            def browser_shipping(index):
                item = collected[index]
                return index, collect_shipping_with_playwright(
                    item["url"], postal_code, control.request_timeout_seconds,
                )
            if remaining:
                with ThreadPoolExecutor(max_workers=min(2, len(remaining))) as executor:
                    for index, shipping in executor.map(browser_shipping, remaining):
                        if not shipping.get("_blocked_status"):
                            collected[index] = merge_offer_details(collected[index], shipping)
    return collected, successful_stores


def _stores_requiring_discovery(stores, successful_stores, known_stores=None, attempted_stores=None):
    known_stores = set(successful_stores if known_stores is None else known_stores)
    attempted_stores = set(
        (store["key"] for store in stores) if attempted_stores is None else attempted_stores
    )
    return [
        store for store in stores
        if store.get("scope") == "retail"
        or store["key"] not in known_stores
        or (store["key"] in attempted_stores and store["key"] not in successful_stores)
    ]


def _discovery_batch(db, sku, stores):
    """Rotate stores between batches so one blocked marketplace cannot monopolize a job."""
    last_attempted = {}
    for store, attempted_at in db.query(
        ScrapeCursor.store, ScrapeCursor.last_attempted_at,
    ).filter(ScrapeCursor.sku == sku).all():
        if attempted_at and (store not in last_attempted or attempted_at > last_attempted[store]):
            last_attempted[store] = attempted_at
    return sorted(
        stores,
        key=lambda store: (last_attempted.get(store["key"]) is not None,
                           last_attempted.get(store["key"]) or datetime.min),
    )[:MAX_DISCOVERY_STORES_PER_RUN]


def _discovery_attempted_since(cursors, started_at):
    return {
        cursor.store for cursor in cursors
        if cursor.last_checked_at and (started_at is None or cursor.last_checked_at >= started_at)
    }


def run_precise_extraction(
    sku, db, postal_code=None, scope="all", rediscover=False,
    collection_after=None, resume=False, progress=None, api_with_scraping=None,
):
    from services.scraper_service import _cached_sku_observations, _save_result, build_summary_from_cached_results
    from routes.products import _serialize_results

    postal_code = normalize_postal_code(postal_code)
    execution_started = datetime.utcnow()
    identity, products = _identity(db, sku)
    from types import SimpleNamespace
    stored_control = get_control(db)
    control = SimpleNamespace(**{field: getattr(stored_control, field, default) for field, default in {
        'request_timeout_seconds': 15, 'concurrent_requests_per_domain': 2,
        'external_discovery_fallback': True, 'playwright_product_fallback': True,
        'playwright_shipping_fallback': True, 'api_with_scraping': True,
    }.items()})
    if api_with_scraping is not None:
        control.api_with_scraping = api_with_scraping
    selected_stores = [
        store for store in TARGET_STORES
        if scope == "all" or store.get("scope") == scope
    ]
    selected_keys = {store["key"] for store in selected_stores}
    _bootstrap_existing_sources(db, identity, products)
    if rediscover:
        db.query(ProductSource).filter(
            ProductSource.sku == sku,
            ProductSource.store.in_(selected_keys),
        ).update({ProductSource.active: False}, synchronize_session=False)
        db.query(ScrapeCursor).filter(
            ScrapeCursor.sku == sku,
            ScrapeCursor.store.in_(selected_keys),
        ).update(
            {
                ScrapeCursor.completed: True,
                ScrapeCursor.updated_at: datetime.min,
                ScrapeCursor.last_status: None,
                ScrapeCursor.failure_reason: None,
                ScrapeCursor.last_attempted_at: None,
                ScrapeCursor.last_checked_at: None,
                ScrapeCursor.browser_attempted_at: None,
                ScrapeCursor.browser_attempted_url: None,
                ScrapeCursor.visited_urls: "[]",
            },
            synchronize_session=False,
        )
        db.commit()

    if not resume:
        for cursor in db.query(ScrapeCursor).filter(
            ScrapeCursor.sku == sku, ScrapeCursor.store.in_(selected_keys),
            ScrapeCursor.completed.is_(True),
        ).all():
            cursor.completed = False
            cursor.next_url = cursor.initial_url or cursor.next_url
            cursor.visited_urls = "[]"
        db.commit()

    current_terms = tuple(dict.fromkeys((identity.sku, *identity.search_terms)))[:3]
    db.query(ScrapeCursor).filter(
        ScrapeCursor.sku == sku, ScrapeCursor.store.in_(selected_keys),
        ScrapeCursor.kind == "search", ScrapeCursor.key.notin_(current_terms),
    ).update({ScrapeCursor.completed: True}, synchronize_session=False)
    db.commit()

    known_sources = active_sources(db, sku, selected_keys)
    product_names = {product.id: product.name for product in products}
    for source in known_sources:
        title = getattr(source, "listing_name", None) or product_names.get(getattr(source, "product_id", None))
        if title:
            match = resolve_candidate(identity, {"name": title, "url": source.url})
            if not match.accepted and match.reason in {
                "Quantidade do kit diferente da unidade pesquisada",
                "Componente anunciado diferente da peca pesquisada",
            }:
                mark_source_result(source, "INVALID_MATCH", match.reason)
    db.commit()
    known_sources = [source for source in known_sources if getattr(source, "active", True)]
    now = datetime.utcnow()
    eligible_sources = [
        source for source in known_sources
        if _source_due(source, collection_after, now)
    ]
    known_batch = _source_batch(eligible_sources)
    if progress:
        progress(f"{sku}: coletando {len(known_batch)} paginas de produto conhecidas")
    collected, successful_stores = _collect_sources(db, identity, known_batch, postal_code, control, progress)
    cursors = db.query(ScrapeCursor).filter(
        ScrapeCursor.sku == sku,
        ScrapeCursor.store.in_(selected_keys),
    ).all()
    cursor_work_stores = {
        cursor.store for cursor in cursors
        if not cursor.completed and (
            cursor.last_attempted_at is None
            or cursor.last_status in {"OK", "OK_BROWSER"}
            or browser_retry_due(cursor)
        )
    }
    stores_to_discover = _stores_requiring_discovery(
        selected_stores, successful_stores,
        known_stores={source.store for source in known_sources},
        attempted_stores={source.store for source in known_batch},
    )
    stores_to_discover = [
        store for store in selected_stores
        if store in stores_to_discover or store["key"] in cursor_work_stores
    ]

    new_sources = []
    discovery_progress = {"collection_after": collection_after}
    known_source_ids = {source.id for source in known_sources}
    attempted_stores = {source.store for source in known_batch}
    attempted_discovery_stores = _discovery_attempted_since(cursors, collection_after)
    if collection_after:
        attempted_this_job = {
            cursor.store for cursor in cursors
            if cursor.last_checked_at and cursor.last_checked_at >= collection_after
            and (cursor.kind != "search" or cursor.key in current_terms)
        }
        stores_to_discover = [
            store for store in stores_to_discover
            if store["key"] not in attempted_this_job or store["key"] in cursor_work_stores
        ]
    discovery_stores = _discovery_batch(db, sku, stores_to_discover)
    pending_stores = len(stores_to_discover) - len(discovery_stores)
    known_ids_by_store = {
        store["key"]: {source.id for source in known_sources if source.store == store["key"]}
        for store in discovery_stores
    }
    def discover_one(store, session, store_progress):
        store_known_ids = known_ids_by_store[store["key"]]
        return _discover_store(
            session, identity, store, discovery_control,
            known_source_ids=store_known_ids,
            retry_failed=store["key"] in attempted_stores and store["key"] not in successful_stores,
            progress=store_progress,
            include_other_sources=not resume or store["key"] not in attempted_discovery_stores,
        )

    discovery_control = SimpleNamespace(
        api_with_scraping=control.api_with_scraping,
        request_timeout_seconds=control.request_timeout_seconds,
        external_discovery_fallback=getattr(control, "external_discovery_fallback", True),
    )
    bind = db.get_bind()
    # In-memory SQLite fixtures share one connection; production uses isolated
    # sessions and short transactions, never one ORM session across threads.
    parallel_discovery = len(discovery_stores) > 1 and str(bind.url) not in {"sqlite://", "sqlite:///:memory:"}
    if parallel_discovery:
        db.commit()
        if progress:
            progress(f"{sku}: buscando lojistas em " + ", ".join(store['label'] for store in discovery_stores))
        worker_sessions = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
        def discover_worker(store):
            with worker_sessions() as worker_db:
                return [source.id for source in discover_one(store, worker_db, {"collection_after": collection_after})]
        found_ids = set()
        with ThreadPoolExecutor(max_workers=min(3, len(discovery_stores))) as executor:
            futures = {executor.submit(discover_worker, store): store for store in discovery_stores}
            for future in as_completed(futures):
                try:
                    found_ids.update(future.result())
                except Exception as exc:
                    print(f"[DISCOVERY][{futures[future]['key'].upper()}] PARSER_ERROR: {exc}")
        db.expire_all()
        if found_ids:
            new_sources.extend(db.query(ProductSource).filter(ProductSource.id.in_(found_ids)).all())
    for store in ([] if parallel_discovery else discovery_stores):
        if progress:
            progress(f"{sku}: buscando lojistas em {store['label']}")
        try:
            new_sources.extend(discover_one(store, db, discovery_progress))
        except Exception as exc:
            db.rollback()
            print(f"[DISCOVERY][{store['key'].upper()}] PARSER_ERROR: {exc}")
    new_sources = [source for source in new_sources if source.id not in known_source_ids]
    remaining = MAX_COLLECTION_PER_RUN - len(known_batch)
    if new_sources and remaining:
        if progress:
            progress(f"{sku}: conferindo {min(len(new_sources), remaining)} novas ofertas")
        new_items, _ = _collect_sources(
            db, identity, _source_batch(new_sources, limit=remaining), postal_code, control, progress
        )
        collected.extend(new_items)

    processed_count = 0
    fresh_count = 0
    reused_count = 0
    for item in collected:
        if not _valid_offer(identity, item):
            print(f"[COLLECTION][VALIDATION] Oferta rejeitada para {sku}")
            continue
        item["destination_zip"] = item.pop("shipping_zip_code", None) or item.get("destination_zip")
        try:
            with db.begin_nested():
                processed_count += _save_result(item, db)
                if item.get("is_cached"):
                    reused_count += 1
                else:
                    fresh_count += 1
        except Exception as exc:
            print(f"[COLLECTION][PERSISTENCE] PARSER_ERROR: {exc}")
    db.commit()
    if progress:
        progress(f"{sku}: consolidando {fresh_count} ofertas verificadas")
    cached = _serialize_results(list(_cached_sku_observations(db, sku)), db,
                                fresh_after=collection_after or execution_started)
    summary = build_summary_from_cached_results(cached, sku)
    summary["processed_count"] = processed_count
    summary["fresh_count"] = fresh_count
    summary["reused_listing_count"] = reused_count
    summary["mode"] = "precise"
    summary["coverage_by_store"] = coverage_by_store(db, sku, selected_stores)
    summary["failed_sources"] = sum(row["failed_sources"] for row in summary["coverage_by_store"])
    summary['limited_sources'] = sum(row.get('limited_sources', 0) for row in summary['coverage_by_store'])
    summary["failed_sources_without_price"] = sum(
        row["failed_sources_without_price"] for row in summary["coverage_by_store"]
    )
    active_after = active_sources(db, sku, selected_keys)
    summary["source_count"] = len(active_after)
    if collection_after:
        pending_sources = sum(
            _source_due(source, collection_after, datetime.utcnow())
            for source in active_after
        )
        pending_pages = db.query(ScrapeCursor).filter(
            ScrapeCursor.sku == sku,
            ScrapeCursor.store.in_(selected_keys),
            ScrapeCursor.completed.is_(False),
        ).count()
        summary["pending_sources"] = pending_sources
        summary["pending_stores"] = pending_stores
        summary["pending_pages"] = pending_pages
        pending_browser = sum(store["browser_pending"] for store in summary["coverage_by_store"])
        pending_unattempted = sum(store["unattempted_pages"] for store in summary["coverage_by_store"])
        summary["pending_browser"] = pending_browser
        summary["pending_unattempted_pages"] = pending_unattempted
        actionable_pages = sum(
            not cursor.completed and (
                cursor.last_attempted_at is None or cursor.last_status in {"OK", "OK_BROWSER"}
                or browser_retry_due(cursor)
            )
            for cursor in db.query(ScrapeCursor).filter(
                ScrapeCursor.sku == sku, ScrapeCursor.store.in_(selected_keys),
            ).all()
        )
        summary["pending_actionable_pages"] = actionable_pages
        summary["more_work"] = bool(
            pending_sources or pending_stores or actionable_pages
        )
    return summary
