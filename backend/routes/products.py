from datetime import datetime, timedelta
from typing import Optional
import re
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from database import get_db
from models.product import ExtractionSchedule, KnownSku, PriceHistory, Product
from schemas.product import (
    AnalyticsExportRequest,
    ExtractionScheduleRequest,
    ProductCreate,
    SkuBatchRequest,
)
from scrapers.comparison import _lead_redirect_info
from services.scraper_service import (
    OFFER_DETAIL_FIELDS,
    _extract_skus,
    _primary_sku,
    build_summary_from_cached_results,
    run_all_scrapers,
)
from services.analytics_export import build_analytics_workbook
from services.offer_enrichment import normalize_postal_code

product_router = APIRouter()
BATCH_SKU_LIMIT = 30


def _seller_from_url(store, source, url):
    source_name = source or store
    store_name = store or source_name
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)

    if source_name == "Magazine Luiza" and store_name == "Magazine Luiza":
        seller_id = (query.get("seller_id") or [""])[0].strip()
        if seller_id:
            return f"{seller_id}-Magalu"

    if source_name == "Mercado Livre" and store_name == "Mercado Livre":
        match = re.search(r"/(MLB-\d+)", unquote(parsed.path), re.I)
        if match:
            return f"{match.group(1).upper()}-ML"
        match = re.search(r"/((?:MLB|MLBU)\d+)", unquote(parsed.path), re.I)
        if match:
            return f"{match.group(1).upper()}-ML"

    if source_name == "Shopee" and store_name == "Shopee":
        match = re.search(r"/product/(\d+)/\d+|-i\.(\d+)\.\d+", unquote(parsed.path))
        shop_id = next((group for group in match.groups() if group), None) if match else None
        if shop_id:
            return f"Shopee Loja {shop_id}"

    return store_name


def _resolve_cached_offer(store, source, url):
    resolved_url = url
    resolved_store = store

    if "/lead?" in (url or ""):
        info = _lead_redirect_info(url)
        resolved_url = info.get("url") or url
        resolved_store = info.get("seller") or store
        if (
            source == "Magazine Luiza"
            and resolved_store != source
            and not resolved_store.lower().endswith("-magalu")
        ):
            resolved_store = f"{resolved_store}-Magalu"

    return {
        "store": _seller_from_url(resolved_store, source, resolved_url),
        "url": resolved_url,
    }


def _latest_products_query(db: Session):
    subquery = (
        db.query(
            PriceHistory.product_id,
            func.max(PriceHistory.timestamp).label("latest_timestamp"),
        )
        .group_by(PriceHistory.product_id)
        .subquery()
    )

    return (
        db.query(Product, PriceHistory)
        .join(PriceHistory, Product.id == PriceHistory.product_id)
        .join(
            subquery,
            (PriceHistory.product_id == subquery.c.product_id)
            & (PriceHistory.timestamp == subquery.c.latest_timestamp),
        )
    )


def _query_terms(search_text: str):
    return [term.strip() for term in search_text.split() if len(term.strip()) >= 2]


def _normalize_sku(value: str):
    value = (value or "").strip().upper()
    return _primary_sku(value) or value


def _normalize_sku_list(values):
    normalized = []
    seen = set()

    for value in values or []:
        sku = _normalize_sku(value)
        if sku and sku not in seen:
            normalized.append(sku)
            seen.add(sku)

    return normalized


def _apply_search_filter(query, search_text: str, match_all: bool):
    terms = _query_terms(search_text)
    if not terms:
        return query

    clauses = [Product.name.ilike(f"%{term}%") for term in terms]
    return query.filter(and_(*clauses) if match_all else or_(*clauses))


def _serialize_results(results):
    serialized = []

    for product, price in results:
        source = price.source or price.store
        resolved = _resolve_cached_offer(price.store, source, price.url)
        item = {
                "id": product.id,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "sku": product.sku,
                "image_url": product.image_url,
                "current_price": price.price,
                "store": resolved["store"],
                "source": source,
                "url": resolved["url"],
                "last_update": price.timestamp,
                "cash_price": price.cash_price or price.price,
                "is_cached": True,
            }
        for field in OFFER_DETAIL_FIELDS:
            value = getattr(price, field, None)
            if value is not None and field not in {"cash_price", "is_cached"}:
                item[field] = value
        serialized.append(item)

    return serialized


def _sku_status_map(db: Session, skus=None):
    requested_skus = set(_normalize_sku_list(skus))

    if requested_skus:
        statuses = {}
        for sku in requested_skus:
            results = _find_sku_products(db, sku)
            product_ids = {product.id for product, _price in results}
            urls = {price.url for _product, price in results}
            stores = {price.store for _product, price in results}
            last_update = max((price.timestamp for _product, price in results), default=None)
            statuses[sku] = {
                "sku": sku,
                "in_database": bool(results),
                "product_count": len(product_ids),
                "offer_count": len(urls),
                "store_count": len(stores),
                "last_update": last_update,
            }
        return statuses

    known_skus = {
        _normalize_sku(row[0])
        for row in db.query(KnownSku.sku).all()
        if row[0]
    }
    product_skus = {
        _normalize_sku(row[0])
        for row in db.query(Product.sku).filter(Product.sku.isnot(None)).distinct().all()
        if row[0]
    }
    name_skus = set()
    for row in db.query(Product.name).all():
        name_skus.update(_extract_skus(row[0] or ""))
    all_skus = known_skus | product_skus | name_skus
    statuses = {
        sku: {
            "sku": sku,
            "in_database": False,
            "product_count": 0,
            "offer_count": 0,
            "store_count": 0,
            "last_update": None,
        }
        for sku in all_skus
    }

    if not all_skus:
        return statuses

    aggregates = {
        sku: {"products": set(), "urls": set(), "stores": set(), "last_update": None}
        for sku in all_skus
    }
    for product, price in _latest_products_query(db).all():
        row_skus = set(_extract_skus(product.name or ""))
        if product.sku:
            row_skus.add(_normalize_sku(product.sku))
        for sku in row_skus.intersection(all_skus):
            aggregate = aggregates[sku]
            aggregate["products"].add(product.id)
            aggregate["urls"].add(price.url)
            aggregate["stores"].add(price.store)
            if not aggregate["last_update"] or (
                price.timestamp and price.timestamp > aggregate["last_update"]
            ):
                aggregate["last_update"] = price.timestamp

    for sku, aggregate in aggregates.items():
        statuses[sku] = {
            "sku": sku,
            "in_database": bool(aggregate["urls"]),
            "product_count": len(aggregate["products"]),
            "offer_count": len(aggregate["urls"]),
            "store_count": len(aggregate["stores"]),
            "last_update": aggregate["last_update"],
        }

    return statuses


def _find_products(db: Session, search_text: Optional[str] = None):
    query = _latest_products_query(db)

    if search_text:
        strict_results = (
            _apply_search_filter(query, search_text, match_all=True)
            .order_by(PriceHistory.price.asc())
            .limit(30)
            .all()
        )
        if strict_results:
            return strict_results

        return (
            _apply_search_filter(query, search_text, match_all=False)
            .order_by(PriceHistory.price.asc())
            .limit(30)
            .all()
        )

    return query.order_by(PriceHistory.timestamp.desc()).limit(50).all()


def _find_sku_products(db: Session, sku: str):
    query = _latest_products_query(db).filter(
        or_(Product.sku == sku, Product.name.ilike(f"%{sku}%"))
    )
    return query.order_by(PriceHistory.price.asc()).limit(250).all()


@product_router.get("/skus/status")
def list_sku_status(
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    requested_skus = _normalize_sku_list(re.split(r"[\s,;]+", q or "")) if q else None
    statuses = _sku_status_map(db, requested_skus)
    items = sorted(
        statuses.values(),
        key=lambda item: (
            item["last_update"] is None,
            item["sku"],
        ),
    )
    return {"items": items}


def _schedule_response(schedule):
    days = []
    for value in (schedule.days or "1").split(","):
        try:
            day = int(value)
        except ValueError:
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)

    return {
        "enabled": schedule.enabled,
        "postal_code": schedule.postal_code,
        "start_time": schedule.start_time,
        "end_time": schedule.end_time,
        "days": days or [1],
        "repeat_every_hours": schedule.repeat_every_hours,
        "force_refresh": schedule.force_refresh,
        "last_run_key": schedule.last_run_key,
        "last_run": schedule.last_run,
        "updated_at": schedule.updated_at,
    }


@product_router.get("/schedule")
def get_extraction_schedule(db: Session = Depends(get_db)):
    schedule = db.query(ExtractionSchedule).filter(ExtractionSchedule.id == 1).first()
    if not schedule:
        schedule = ExtractionSchedule(id=1, policy="custom")
        db.add(schedule)
        db.commit()
        db.refresh(schedule)
    return _schedule_response(schedule)


@product_router.put("/schedule")
def update_extraction_schedule(
    payload: ExtractionScheduleRequest,
    db: Session = Depends(get_db),
):
    schedule = db.query(ExtractionSchedule).filter(ExtractionSchedule.id == 1).first()
    if not schedule:
        schedule = ExtractionSchedule(id=1, policy="custom")
        db.add(schedule)
    schedule.enabled = payload.enabled
    schedule.policy = "custom"
    schedule.postal_code = normalize_postal_code(payload.postal_code)
    schedule.start_time = payload.start_time
    schedule.end_time = payload.end_time
    schedule.days = ",".join(str(day) for day in payload.days)
    schedule.repeat_every_hours = payload.repeat_every_hours
    schedule.force_refresh = payload.force_refresh
    schedule.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(schedule)
    return _schedule_response(schedule)


@product_router.post("/batch-search")
def batch_search_products(payload: SkuBatchRequest, db: Session = Depends(get_db)):
    skus = _normalize_sku_list(payload.skus)
    if not skus:
        raise HTTPException(status_code=400, detail="Informe ao menos um SKU valido.")
    if len(skus) > BATCH_SKU_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {BATCH_SKU_LIMIT} SKUs por extracao.",
        )

    _register_skus(db, skus)
    items = []
    for sku in skus:
        try:
            summary, status = _search_sku_summary(sku, payload, db)

            sku_status = _sku_status_map(db, [sku]).get(sku, {})
            items.append(
                {
                    "sku": sku,
                    "status": status,
                    "message": (
                        f"{len(summary['results'])} ofertas analisadas em "
                        f"{len(summary['comparison'])} linhas comparativas."
                    ),
                    "results_count": len(summary["results"]),
                    "stores_count": len(
                        [store for store in summary["stores"] if store["available"]]
                    ),
                    "comparison_count": len(summary["comparison"]),
                    "in_database": sku_status.get("in_database", False),
                    "offer_count": sku_status.get("offer_count", 0),
                    "store_count": sku_status.get("store_count", 0),
                    "last_update": sku_status.get("last_update"),
                }
            )
        except Exception as exc:
            db.rollback()
            sku_status = _sku_status_map(db, [sku]).get(sku, {})
            items.append(
                {
                    "sku": sku,
                    "status": "error",
                    "message": str(exc),
                    "results_count": 0,
                    "stores_count": 0,
                    "comparison_count": 0,
                    "in_database": sku_status.get("in_database", False),
                    "offer_count": sku_status.get("offer_count", 0),
                    "store_count": sku_status.get("store_count", 0),
                    "last_update": sku_status.get("last_update"),
                }
            )

    return {"items": items}


def _register_skus(db: Session, skus):
    existing = {
        row[0]
        for row in db.query(KnownSku.sku).filter(KnownSku.sku.in_(skus)).all()
    }
    for sku in skus:
        if sku not in existing:
            db.add(KnownSku(sku=sku))
    db.commit()


def _search_sku_summary(sku, payload, db):
    cached_results = _serialize_results(_find_sku_products(db, sku))
    latest = max(
        (item.get("last_update") for item in cached_results if item.get("last_update")),
        default=None,
    )
    stale_hours = 1 if payload.scope == "sites" else 8
    stale = not latest or latest < datetime.utcnow() - timedelta(hours=stale_hours)
    should_refresh = (
        payload.force_refresh
        or not cached_results
        or (payload.refresh_if_stale and stale)
    )
    if not should_refresh:
        return build_summary_from_cached_results(cached_results, sku), "cached"
    return (
        run_all_scrapers(
            sku,
            db,
            postal_code=payload.postal_code,
            scope=payload.scope,
        ),
        "updated",
    )


def _combine_summaries(summaries, skus):
    results = []
    seen_results = set()
    comparison = []
    stores = {}

    for summary in summaries:
        for item in summary["results"]:
            identity = (item.get("sku"), item.get("url"))
            if identity not in seen_results:
                seen_results.add(identity)
                results.append(item)

        for row in summary["comparison"]:
            comparison.append({**row, "id": len(comparison) + 1})

        for store in summary["stores"]:
            aggregate = stores.setdefault(
                store["key"],
                {
                    **store,
                    "available": False,
                    "product": None,
                    "products": [],
                },
            )
            known_urls = {product["url"] for product in aggregate["products"]}
            for product in store.get("products") or []:
                if product["url"] not in known_urls:
                    aggregate["products"].append(product)
                    known_urls.add(product["url"])
            aggregate["available"] = bool(aggregate["products"])
            aggregate["product"] = min(
                aggregate["products"],
                key=lambda product: product["current_price"],
                default=None,
            )
            aggregate["message"] = None if aggregate["available"] else "Nao disponivel"

    return {
        "message": (
            f"Busca conjunta concluida para {len(skus)} SKUs: "
            f"{len(results)} ofertas em {len(comparison)} produtos."
        ),
        "results": results,
        "stores": list(stores.values()),
        "comparison": comparison,
        "skus": skus,
    }


@product_router.post("/search/batch")
def search_sku_batch(payload: SkuBatchRequest, db: Session = Depends(get_db)):
    skus = _normalize_sku_list(payload.skus)
    if not skus:
        raise HTTPException(status_code=400, detail="Informe ao menos um SKU valido.")
    if len(skus) > BATCH_SKU_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {BATCH_SKU_LIMIT} SKUs por pesquisa.",
        )

    _register_skus(db, skus)
    summaries = []
    for sku in skus:
        try:
            summary, _status = _search_sku_summary(sku, payload, db)
            summaries.append(summary)
        except Exception as exc:
            db.rollback()
            print(f" ERROR: Pesquisa de {sku} falhou: {exc}")
            cached = _serialize_results(_find_sku_products(db, sku))
            if cached:
                summaries.append(build_summary_from_cached_results(cached, sku))

    return _combine_summaries(summaries, skus)


@product_router.post("/export/analytics")
def export_analytics(payload: AnalyticsExportRequest):
    if not payload.rows:
        raise HTTPException(status_code=400, detail="Nao ha dados para exportar.")

    workbook = build_analytics_workbook(payload)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"neveprice-analytics-{timestamp}.xlsx"
    return StreamingResponse(
        workbook,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@product_router.post("/", status_code=201)
def create_product(product_data: ProductCreate, db: Session = Depends(get_db)):
    new_product = Product(
        name=product_data.name,
        brand=product_data.brand,
        description=product_data.description,
        category=product_data.category,
        image_url=product_data.image_url,
        sku=product_data.sku,
    )

    db.add(new_product)
    db.commit()
    db.refresh(new_product)

    new_price = PriceHistory(
        product_id=new_product.id,
        price=product_data.initial_price.price,
        store=product_data.initial_price.store,
        source=product_data.initial_price.source or product_data.initial_price.store,
        url=product_data.initial_price.url,
        cash_price=(
            product_data.initial_price.cash_price or product_data.initial_price.price
        ),
        installment_price=product_data.initial_price.installment_price,
        installment_count=product_data.initial_price.installment_count,
        installment_value=product_data.initial_price.installment_value,
        shipping_cost=product_data.initial_price.shipping_cost,
        delivery_min_days=product_data.initial_price.delivery_min_days,
        delivery_max_days=product_data.initial_price.delivery_max_days,
        destination_zip=product_data.initial_price.destination_zip,
    )

    db.add(new_price)
    db.commit()

    return {"message": "Produto e preço inicial cadastrados!", "id": new_product.id}


@product_router.get("/")
def search_products(
    q: Optional[str] = Query(None, min_length=2), db: Session = Depends(get_db)
):
    return _serialize_results(_find_products(db, q))


@product_router.post("/refresh")
def refresh_prices(
    q: str,
    postal_code: Optional[str] = None,
    scope: str = "all",
    db: Session = Depends(get_db),
):
    summary = run_all_scrapers(q, db, postal_code=postal_code, scope=scope)
    return {
        "message": (
            "Busca finalizada. "
            f"{summary['processed_count']} preços novos ou atualizados"
        ),
        "results": summary["results"],
        "stores": summary["stores"],
        "comparison": summary["comparison"],
    }


@product_router.get("/search")
def search_and_update(q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    search_text = q.strip()
    sku = _primary_sku(search_text)

    if sku:
        cached_results = _serialize_results(_find_sku_products(db, sku))
        if cached_results:
            summary = build_summary_from_cached_results(cached_results, sku)
            unavailable_count = len(
                [store for store in summary["stores"] if not store["available"]]
            )
            return {
                "message": (
                    f"Resultado carregado do banco local. {len(cached_results)} "
                    f"ofertas analisadas para {sku}; {unavailable_count} "
                    "lojas sem disponibilidade."
                ),
                "results": summary["results"],
                "stores": summary["stores"],
                "comparison": summary["comparison"],
            }

    summary = run_all_scrapers(search_text, db)
    results = summary["results"]
    comparison = summary["comparison"]
    unavailable_count = len(
        [store for store in summary["stores"] if not store["available"]]
    )

    if results:
        message = (
            f"Busca concluída. {len(results)} ofertas analisadas em "
            f"{len(comparison)} linhas comparativas; {unavailable_count} "
            "lojas sem disponibilidade."
        )
    else:
        message = (
            "Nenhuma das lojas alvo retornou oferta nas fontes gratuitas agora. "
            "Cada loja foi marcada como Não disponível."
        )

    return {
        "message": message,
        "results": results,
        "stores": summary["stores"],
        "comparison": comparison,
    }
