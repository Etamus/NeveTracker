from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from crawlers.amazon import discover_sellers as discover_amazon_sellers
from crawlers.magalu import discover as discover_magalu
from crawlers.registry import collect_product_source
from crawlers.scrapy_engine import collect_sources_with_scrapy
from services.offer_enrichment import enrich_offer, normalize_postal_code
from models.product import KnownSku, Product, ProductSource
from services.bing_shopping import search_bing_shopping
from scrapers.comparison import scrape_comparison_offers
from services.crawler_control import get_control
from services.external_market_providers import run_external_market_provider
from services.integration_settings import (
    get_runtime_provider_settings,
    get_search_provider_names,
    should_run_provider,
)
from services.product_resolver import identity_from_models, resolve_candidate
from services.product_sources import (
    active_sources,
    mark_source_result,
    upsert_product_source,
)
from services.playwright_fallback import collect_shipping_with_playwright
from services.store_registry import TARGET_STORES, find_target_store


MAX_SOURCES_PER_STORE = 30


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
    if store["key"] == "magalu":
        candidates = []
        for term in identity.search_terms[:3]:
            candidates.extend(discover_magalu(term, timeout))
            if candidates:
                break
        return candidates

    candidates = []
    scrapers = store.get("direct_scrapers", ())
    mercado_livre_mode = "always"
    if store["key"] == "mercado_livre":
        mercado_livre_mode = get_runtime_provider_settings("mercado_livre")["usage_mode"]
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
        for term in identity.search_terms[:3]:
            try:
                result = scraper(term) or []
            except Exception as exc:
                print(f"[DISCOVERY][{store['key'].upper()}] PARSER_ERROR: {exc}")
                result = []
            candidates.extend(result)
            if result:
                break
    if store["key"] == "amazon" and candidates:
        seller_candidates = []
        for candidate in candidates[:5]:
            seller_candidates.extend(discover_amazon_sellers(candidate, timeout))
        candidates.extend(seller_candidates)
    return candidates


def _belongs_to_store(item, store_key):
    matched = find_target_store(item.get("source"), item.get("store"), item.get("url"))
    return bool(matched and matched["key"] == store_key)


def _external_candidates(identity, store_key):
    for term in identity.search_terms[:2]:
        candidates = [
            item for item in (search_bing_shopping(term) or [])
            if _belongs_to_store(item, store_key)
        ]
        if candidates:
            return candidates
        candidates = [
            item for item in (scrape_comparison_offers(term) or [])
            if _belongs_to_store(item, store_key)
        ]
        if candidates:
            return candidates

    for provider in get_search_provider_names():
        if not should_run_provider(provider, 0, 1):
            continue
        provider_run = run_external_market_provider(provider, identity.search_terms[0])
        candidates = [
            item for item in provider_run.get("items") or []
            if _belongs_to_store(item, store_key)
        ]
        if candidates:
            print(f"[FALLBACK][{store_key.upper()}] {provider} acionado")
            return candidates
    return []


def _discover_store(db, identity, store, control):
    candidates = _direct_candidates(store, identity, control.request_timeout_seconds)
    method = "direct"
    if not candidates and control.external_discovery_fallback:
        candidates = _external_candidates(identity, store["key"])
        method = "external_fallback"

    saved = []
    seen = set()
    for candidate in candidates:
        identity_key = (candidate.get("url"), candidate.get("seller") or candidate.get("store"))
        if identity_key in seen:
            continue
        seen.add(identity_key)
        match = resolve_candidate(identity, candidate)
        if not match.accepted:
            print(f"[MATCH][{store['key'].upper()}] INVALID_MATCH para {identity.sku}")
            continue
        source = upsert_product_source(db, identity, store["key"], candidate, match)
        if source:
            saved.append(source)
            print(
                f"[DISCOVERY][{store['key'].upper()}] {identity.sku} via {match.method} "
                f"({match.confidence:.2f}, {method})"
            )
        if len(saved) >= MAX_SOURCES_PER_STORE:
            break
    db.commit()
    if not saved:
        print(f"[DISCOVERY][{store['key'].upper()}] NOT_FOUND para {identity.sku}")
    return saved


def _collect_sources(db, identity, sources, postal_code, control):
    if not sources:
        return []
    grouped = defaultdict(list)
    for source in sources:
        grouped[source.store].append(source)
    collected = []
    for store_key, store_sources in grouped.items():
        selected = store_sources[:MAX_SOURCES_PER_STORE]
        if store_key in {"mercado_livre", "shopee"}:
            workers = min(control.concurrent_requests_per_domain, len(selected))
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                futures = {
                    executor.submit(
                        collect_product_source,
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
            results = collect_sources_with_scrapy(
                selected,
                identity,
                control.concurrent_requests_per_domain,
                control.request_timeout_seconds,
            )

        for source in selected:
            result = results[source.id]
            status = result.get("status") or "PARSER_ERROR"
            mark_source_result(source, status, result.get("reason"))
            if status == "OK":
                item = result["item"]
                if postal_code and store_key not in {"mercado_livre"}:
                    item = enrich_offer(item, postal_code)
                    item["shipping_zip_code"] = postal_code
                if (
                    control.playwright_shipping_fallback
                    and postal_code
                    and item.get("shipping_cost") is None
                    and item.get("delivery_max_days") is None
                ):
                    print(f"[FALLBACK][{store_key.upper()}] Playwright acionado para logistica")
                    item.update(
                        collect_shipping_with_playwright(
                            item["url"],
                            postal_code,
                            control.request_timeout_seconds,
                        )
                    )
                source.seller = str(item.get("seller") or source.seller)[:120]
                source.match_confidence = result["match"].confidence
                source.match_method = result["match"].method
                collected.append(item)
            else:
                print(
                    f"[COLLECTION][{store_key.upper()}] {status}: "
                    f"{result.get('reason') or identity.sku}"
                )
        db.commit()
    return collected


def run_precise_extraction(sku, db, postal_code=None, scope="all", rediscover=False):
    from services.scraper_service import _save_result, build_summary_from_cached_results
    from routes.products import _find_sku_products, _serialize_results

    postal_code = normalize_postal_code(postal_code)
    identity, products = _identity(db, sku)
    control = get_control(db)
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
        db.commit()

    known_sources = active_sources(db, sku, selected_keys)
    collected = _collect_sources(db, identity, known_sources, postal_code, control)
    still_active_keys = {
        source.store for source in active_sources(db, sku, selected_keys)
    }
    missing_stores = [store for store in selected_stores if store["key"] not in still_active_keys]

    new_sources = []
    for store in missing_stores:
        new_sources.extend(_discover_store(db, identity, store, control))
    if new_sources:
        collected.extend(_collect_sources(db, identity, new_sources, postal_code, control))

    processed_count = 0
    for item in collected:
        item["destination_zip"] = item.pop("shipping_zip_code", None) or postal_code
        try:
            processed_count += _save_result(item, db)
        except Exception as exc:
            db.rollback()
            print(f"[COLLECTION][PERSISTENCE] PARSER_ERROR: {exc}")
    db.commit()
    cached = _serialize_results(_find_sku_products(db, sku), db)
    summary = build_summary_from_cached_results(cached, sku)
    summary["processed_count"] = processed_count
    summary["mode"] = "precise"
    summary["source_count"] = len(active_sources(db, sku, selected_keys))
    return summary
