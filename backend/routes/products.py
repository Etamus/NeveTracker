import csv
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from typing import Optional
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from models.auth import AuthUser
from models.product import (
    ExtractionJob,
    ExtractionJobItem,
    ExtractionSchedule,
    KnownSku,
    PriceHistory,
    Product,
    ProductSource,
)
from schemas.product import (
    AnalyticsExportRequest,
    ExtractionDatabaseJobRequest,
    ExtractionScheduleRequest,
    CrawlerControlRequest,
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
from services.crawler_control import get_control, serialize_control, update_control
from services.auth_service import require_developer, require_operator
from services.product_sources import serialize_source
from services.precise_extraction import run_precise_extraction
from services.extraction_queue import (
    MAX_JOB_SKUS,
    cancel_extraction_job,
    enqueue_extraction_job,
    get_active_extraction_job,
    get_extraction_job,
)

product_router = APIRouter()
BATCH_SKU_LIMIT = 30
SKU_IMPORT_LIMIT = 5000
SKU_IMPORT_MAX_BYTES = 8 * 1024 * 1024
DB_QUERY_CHUNK_SIZE = 500
SKU_METADATA_FIELDS = (
    "description", "subfamily", "sku_category", "cluster", "market_type",
    "ean", "mpn", "model", "brand",
)

SKU_IMPORT_HEADERS = {
    "sku": "sku",
    "codigo": "sku",
    "codigo sku": "sku",
    "descricao": "description",
    "produto": "description",
    "nome": "description",
    "subfamilia": "subfamily",
    "sub familia": "subfamily",
    "categoria": "category",
    "cluster": "cluster",
    "mercado": "market_type",
    "tipo de mercado": "market_type",
    "ean": "ean",
    "gtin": "ean",
    "mpn": "mpn",
    "codigo fabricante": "mpn",
    "modelo": "model",
    "marca": "brand",
}


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


def _chunked(values, size=DB_QUERY_CHUNK_SIZE):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _apply_search_filter(query, search_text: str, match_all: bool):
    terms = _query_terms(search_text)
    if not terms:
        return query

    clauses = [Product.name.ilike(f"%{term}%") for term in terms]
    return query.filter(and_(*clauses) if match_all else or_(*clauses))


def _known_sku_metadata_map(db: Session, skus=None):
    normalized_skus = set(_normalize_sku_list(skus or []))
    if normalized_skus:
        items = []
        for sku_batch in _chunked(normalized_skus):
            items.extend(
                db.query(KnownSku).filter(KnownSku.sku.in_(sku_batch)).all()
            )
    else:
        items = db.query(KnownSku).all()

    return {
        _normalize_sku(item.sku): {
            "description": item.description,
            "subfamily": item.subfamily,
            "sku_category": item.category,
            "cluster": item.cluster,
            "market_type": item.market_type,
            "ean": item.ean,
            "mpn": item.mpn,
            "model": item.model,
            "brand": item.brand,
        }
        for item in items
    }


def _apply_item_metadata(item, metadata_map, fallback_sku=None):
    sku = _normalize_sku(item.get("sku") or fallback_sku or "")
    metadata = metadata_map.get(sku, {})
    for field in SKU_METADATA_FIELDS:
        value = metadata.get(field)
        if value is not None:
            item[field] = value
    return item


def _apply_summary_metadata(summary, db: Session, fallback_sku=None, metadata_map=None):
    skus = set()
    if fallback_sku:
        skus.add(_normalize_sku(fallback_sku))
    for item in summary.get("results") or []:
        if item.get("sku"):
            skus.add(_normalize_sku(item["sku"]))
    for row in summary.get("comparison") or []:
        if row.get("sku"):
            skus.add(_normalize_sku(row["sku"]))

    if metadata_map is None:
        metadata_map = _known_sku_metadata_map(db, skus)
    for item in summary.get("results") or []:
        _apply_item_metadata(item, metadata_map, fallback_sku)
    for row in summary.get("comparison") or []:
        _apply_item_metadata(row, metadata_map, fallback_sku)
        for offer in row.get("offers") or []:
            _apply_item_metadata(offer, metadata_map, row.get("sku") or fallback_sku)
    for store in summary.get("stores") or []:
        if store.get("product"):
            _apply_item_metadata(store["product"], metadata_map, fallback_sku)
        for product in store.get("products") or []:
            _apply_item_metadata(product, metadata_map, fallback_sku)
    return summary


def _serialize_results(results, db: Session = None, metadata_map=None, resolve_redirects=True):
    if metadata_map is None:
        metadata_map = _known_sku_metadata_map(
            db,
            [
                product.sku or _primary_sku(product.name or "")
                for product, _price in results
                if product.sku or _primary_sku(product.name or "")
            ],
        ) if db else {}
    serialized = []

    for product, price in results:
        product_sku = product.sku or _primary_sku(product.name or "")
        source = price.source or price.store
        resolved = (
            _resolve_cached_offer(price.store, source, price.url)
            if resolve_redirects
            else {"store": price.store, "url": price.url}
        )
        item = {
                "id": product.id,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "sku": product_sku,
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
        _apply_item_metadata(item, metadata_map)
        serialized.append(item)

    return serialized


def _sku_status_map(db: Session, skus=None):
    requested_skus = set(_normalize_sku_list(skus))
    metadata_map = _known_sku_metadata_map(db, requested_skus or None)

    if requested_skus and len(requested_skus) <= 20:
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
                **metadata_map.get(sku, {}),
            }
        return statuses

    if requested_skus:
        all_skus = requested_skus
    else:
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
            **metadata_map.get(sku, {}),
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
            **metadata_map.get(sku, {}),
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


def _product_belongs_to_sku(product, sku):
    if _normalize_sku(product.sku or "") == sku:
        return True
    return bool(
        re.search(
            rf"(?<![A-Z0-9]){re.escape(sku)}(?![A-Z0-9])",
            (product.name or "").upper(),
        )
    )


@product_router.delete("/skus/{sku:path}")
def delete_local_sku(
    sku: str,
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    normalized_sku = _normalize_sku(sku)
    if not normalized_sku:
        raise HTTPException(status_code=400, detail="SKU invalido.")

    active_item = (
        db.query(ExtractionJobItem)
        .join(ExtractionJob, ExtractionJob.id == ExtractionJobItem.job_id)
        .filter(
            ExtractionJobItem.sku == normalized_sku,
            ExtractionJobItem.status.in_(("queued", "running")),
            ExtractionJob.status.in_(("queued", "running")),
        )
        .first()
    )
    if active_item:
        raise HTTPException(
            status_code=409,
            detail="Este SKU esta em uma fila ativa. Aguarde ou cancele a extracao antes de excluir.",
        )

    candidates = (
        db.query(Product)
        .filter(
            or_(
                Product.sku == normalized_sku,
                Product.name.ilike(f"%{normalized_sku}%"),
            )
        )
        .all()
    )
    products = [
        product for product in candidates if _product_belongs_to_sku(product, normalized_sku)
    ]
    product_ids = [product.id for product in products]
    deleted_prices = 0
    source_filter = ProductSource.sku == normalized_sku
    if product_ids:
        source_filter = or_(source_filter, ProductSource.product_id.in_(product_ids))
    db.query(ProductSource).filter(source_filter).delete(synchronize_session=False)
    if product_ids:
        deleted_prices = (
            db.query(PriceHistory)
            .filter(PriceHistory.product_id.in_(product_ids))
            .delete(synchronize_session=False)
        )
        db.query(Product).filter(Product.id.in_(product_ids)).delete(
            synchronize_session=False
        )

    deleted_known = (
        db.query(KnownSku)
        .filter(KnownSku.sku == normalized_sku)
        .delete(synchronize_session=False)
    )
    if not products and not deleted_known:
        db.rollback()
        raise HTTPException(status_code=404, detail="SKU nao encontrado no banco local.")

    db.commit()
    return {
        "sku": normalized_sku,
        "deleted_products": len(products),
        "deleted_prices": deleted_prices,
    }


def _normalize_header(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", text.strip().lower().replace("_", " "))


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _tabular_import_rows(filename, content):
    extension = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if extension in {"xlsx", "xlsm"}:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        worksheet = workbook.active
        raw_rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        workbook.close()
    elif extension == "csv":
        decoded = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                decoded = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise HTTPException(status_code=400, detail="Nao foi possivel ler o CSV.")
        try:
            dialect = csv.Sniffer().sniff(decoded[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        raw_rows = [list(row) for row in csv.reader(StringIO(decoded), dialect)]
    else:
        raise HTTPException(
            status_code=400,
            detail="Formato nao suportado. Use XLSX, XLSM ou CSV.",
        )

    while raw_rows and not any(_cell_text(value) for value in raw_rows[0]):
        raw_rows.pop(0)
    if not raw_rows:
        raise HTTPException(status_code=400, detail="O arquivo esta vazio.")

    mapped_headers = [SKU_IMPORT_HEADERS.get(_normalize_header(value)) for value in raw_rows[0]]
    if "sku" not in mapped_headers:
        raise HTTPException(
            status_code=400,
            detail="A planilha precisa ter uma coluna chamada SKU.",
        )

    rows = []
    for spreadsheet_row, values in enumerate(raw_rows[1:], start=2):
        mapped = {
            header: _cell_text(values[index])
            for index, header in enumerate(mapped_headers)
            if header and index < len(values)
        }
        if any(mapped.values()):
            rows.append((spreadsheet_row, mapped))
    return rows


def _normalize_market_type(value):
    normalized = _normalize_header(value)
    if normalized == "cativo":
        return "Cativo"
    if normalized == "competitivo":
        return "Competitivo"
    return None


@product_router.get("/import/skus/template")
def download_sku_import_template():
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "SKUs"
    headers = [
        "SKU", "Descricao", "Subfamilia", "Categoria", "Cluster", "Mercado",
        "EAN", "MPN", "Modelo", "Marca",
    ]
    worksheet.append(headers)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = "A1:J1"

    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F7180")
    for column, width in zip("ABCDEFGHIJ", (20, 42, 24, 24, 20, 20, 18, 18, 20, 22)):
        worksheet.column_dimensions[column].width = width

    market_validation = DataValidation(
        type="list",
        formula1='"Cativo,Competitivo"',
        allow_blank=True,
    )
    worksheet.add_data_validation(market_validation)
    market_validation.add("F2:F5000")

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="modelo-importacao-skus.xlsx"'},
    )


@product_router.post("/import/skus")
async def import_skus_file(
    file: UploadFile = File(...),
    _actor: AuthUser = Depends(require_operator),
    db: Session = Depends(get_db),
):
    filename = (file.filename or "").strip()
    content = await file.read(SKU_IMPORT_MAX_BYTES + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="O arquivo esta vazio.")
    if len(content) > SKU_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="O arquivo excede o limite de 8 MB.")

    rows = _tabular_import_rows(filename, content)
    if len(rows) > SKU_IMPORT_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {SKU_IMPORT_LIMIT} linhas por arquivo.",
        )

    imported_skus = []
    seen = set()
    errors = []
    valid_rows = []

    for spreadsheet_row, values in rows:
        sku = _normalize_sku(values.get("sku", ""))
        if not sku or len(sku) > 80 or not re.fullmatch(r"[A-Z0-9._/-]+", sku):
            errors.append({"row": spreadsheet_row, "message": "SKU invalido."})
            continue
        if sku in seen:
            continue

        market_value = values.get("market_type", "")
        market_type = _normalize_market_type(market_value) if market_value else None
        if market_value and not market_type:
            errors.append(
                {
                    "row": spreadsheet_row,
                    "message": "Mercado deve ser Cativo ou Competitivo.",
                }
            )
            continue

        seen.add(sku)
        valid_rows.append((sku, values, market_type))
        imported_skus.append(sku)

    if not imported_skus:
        raise HTTPException(
            status_code=400,
            detail=errors[0]["message"] if errors else "Nenhum SKU valido foi encontrado.",
        )

    existing_by_sku = {}
    for sku_batch in _chunked(imported_skus):
        for item in db.query(KnownSku).filter(KnownSku.sku.in_(sku_batch)).all():
            existing_by_sku[item.sku] = item

    created_count = 0
    updated_count = 0
    for sku, values, market_type in valid_rows:
        known_sku = existing_by_sku.get(sku)
        if not known_sku:
            known_sku = KnownSku(sku=sku)
            db.add(known_sku)
            existing_by_sku[sku] = known_sku
            created_count += 1
        else:
            updated_count += 1

        for field in (
            "description", "subfamily", "category", "cluster", "ean", "mpn", "model", "brand"
        ):
            value = values.get(field, "").strip()
            if value:
                setattr(known_sku, field, value[:250])
        if market_type:
            known_sku.market_type = market_type
        known_sku.updated_at = datetime.utcnow()

    db.commit()
    status_map = _sku_status_map(db, imported_skus)
    return {
        "filename": filename,
        "imported_count": len(imported_skus),
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_count": len(rows) - len(imported_skus),
        "errors": errors[:100],
        "skus": imported_skus,
        "items": [status_map[sku] for sku in imported_skus if sku in status_map],
    }


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
def get_extraction_schedule(
    _actor: AuthUser = Depends(require_operator),
    db: Session = Depends(get_db),
):
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
    _actor: AuthUser = Depends(require_operator),
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
            items.append(_sku_batch_result(db, sku, summary, status))
        except Exception as exc:
            db.rollback()
            items.append(_sku_error_result(db, sku, exc))

    return {"items": items}


def _register_skus(db: Session, skus):
    skus = list(dict.fromkeys(skus))
    existing = set()
    for sku_batch in _chunked(skus):
        existing.update(
            row[0]
            for row in db.query(KnownSku.sku).filter(KnownSku.sku.in_(sku_batch)).all()
        )
    for sku in skus:
        if sku not in existing:
            db.add(KnownSku(sku=sku))
    db.commit()


def _search_sku_summary(sku, payload, db, refreshed_after=None):
    cached_results = _serialize_results(_find_sku_products(db, sku), db)
    latest = max(
        (item.get("last_update") for item in cached_results if item.get("last_update")),
        default=None,
    )
    stale_hours = 1 if payload.scope == "sites" else 8
    stale = not latest or latest < datetime.utcnow() - timedelta(hours=stale_hours)
    should_refresh = (
        not (latest and refreshed_after and latest >= refreshed_after)
        and (
            payload.force_refresh
            or not cached_results
            or (payload.refresh_if_stale and stale)
        )
    )
    if not should_refresh:
        summary = build_summary_from_cached_results(cached_results, sku)
        return _apply_summary_metadata(summary, db, sku), "cached"
    if payload.mode == "precise":
        summary = run_precise_extraction(
            sku,
            db,
            postal_code=payload.postal_code,
            scope=payload.scope,
        )
    else:
        summary = run_all_scrapers(
            sku,
            db,
            postal_code=payload.postal_code,
            scope=payload.scope,
        )
    return _apply_summary_metadata(summary, db, sku), "updated"


def _sku_batch_result(db, sku, summary, status):
    sku_status = _sku_status_map(db, [sku]).get(sku, {})
    return {
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
        **{field: sku_status.get(field) for field in SKU_METADATA_FIELDS},
    }


def _sku_error_result(db, sku, exc):
    sku_status = _sku_status_map(db, [sku]).get(sku, {})
    return {
        "sku": sku,
        "status": "error",
        "message": str(exc) or exc.__class__.__name__,
        "results_count": 0,
        "stores_count": 0,
        "comparison_count": 0,
        "in_database": sku_status.get("in_database", False),
        "offer_count": sku_status.get("offer_count", 0),
        "store_count": sku_status.get("store_count", 0),
        "last_update": sku_status.get("last_update"),
        **{field: sku_status.get(field) for field in SKU_METADATA_FIELDS},
    }


def process_extraction_queue_item(sku, options):
    db = SessionLocal()
    try:
        payload = SkuBatchRequest(
            skus=[sku],
            force_refresh=bool(options.get("force_refresh")),
            refresh_if_stale=bool(options.get("refresh_if_stale")),
            postal_code=options.get("postal_code"),
            scope=options.get("scope") or "all",
            mode=options.get("mode") or "quick",
        )
        summary, status = _search_sku_summary(
            sku,
            payload,
            db,
            refreshed_after=options.get("job_created_at"),
        )
        return _sku_batch_result(db, sku, summary, status)
    except Exception as exc:
        db.rollback()
        print(f" ERROR: Fila falhou ao processar {sku}: {exc}")
        return _sku_error_result(db, sku, exc)
    finally:
        db.close()


@product_router.post("/extraction-jobs")
def create_extraction_job(
    payload: SkuBatchRequest,
    _actor: AuthUser = Depends(require_operator),
    db: Session = Depends(get_db),
):
    skus = _normalize_sku_list(payload.skus)
    if not skus:
        raise HTTPException(status_code=400, detail="Informe ao menos um SKU valido.")
    if len(skus) > MAX_JOB_SKUS:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {MAX_JOB_SKUS} SKUs por fila.",
        )

    _register_skus(db, skus)
    return enqueue_extraction_job(
        skus,
        kind="single" if len(skus) == 1 else "manual",
        force_refresh=payload.force_refresh,
        refresh_if_stale=payload.refresh_if_stale,
        postal_code=normalize_postal_code(payload.postal_code),
        scope=payload.scope,
        mode=get_control(db).default_mode,
    )


@product_router.post("/extraction-jobs/database")
def create_database_extraction_job(
    payload: ExtractionDatabaseJobRequest,
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    skus = _normalize_sku_list(
        row[0]
        for row in db.query(KnownSku.sku).order_by(KnownSku.sku.asc()).all()
    )
    if not skus:
        raise HTTPException(status_code=400, detail="O banco local nao possui SKUs.")
    return enqueue_extraction_job(
        skus,
        kind="database",
        force_refresh=payload.force_refresh,
        refresh_if_stale=payload.refresh_if_stale,
        postal_code=normalize_postal_code(payload.postal_code),
        scope=payload.scope,
        mode=get_control(db).default_mode,
    )


@product_router.get("/extraction-jobs/active")
def active_extraction_job(_actor: AuthUser = Depends(require_operator)):
    return {"job": get_active_extraction_job()}


@product_router.get("/crawler-control")
def crawler_control_settings(
    _actor: AuthUser = Depends(require_operator),
    db: Session = Depends(get_db),
):
    return serialize_control(get_control(db))


@product_router.put("/crawler-control")
def save_crawler_control(
    payload: CrawlerControlRequest,
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    return serialize_control(update_control(db, payload))


@product_router.get("/sources")
def list_product_sources(
    sku: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    query = db.query(ProductSource)
    if sku:
        query = query.filter(ProductSource.sku == _normalize_sku(sku))
    if active is not None:
        query = query.filter(ProductSource.active.is_(active))
    items = query.order_by(
        ProductSource.last_collected_at.desc(),
        ProductSource.first_seen_at.desc(),
    ).limit(500).all()
    return {
        "items": [serialize_source(item) for item in items],
        "active_count": db.query(ProductSource).filter(ProductSource.active.is_(True)).count(),
        "invalid_count": db.query(ProductSource).filter(ProductSource.active.is_(False)).count(),
    }


@product_router.post("/sources/{sku}/rediscover")
def rediscover_product_sources(
    sku: str,
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    normalized_sku = _normalize_sku(sku)
    known = db.query(KnownSku).filter(KnownSku.sku == normalized_sku).first()
    if not known:
        raise HTTPException(status_code=404, detail="SKU nao encontrado no banco local.")
    db.query(ProductSource).filter(ProductSource.sku == normalized_sku).update(
        {
            ProductSource.active: False,
            ProductSource.validation_status: "rediscovery_requested",
            ProductSource.failure_reason: "Redescoberta solicitada pelo usuario",
            ProductSource.invalidated_at: datetime.utcnow(),
        },
        synchronize_session=False,
    )
    db.commit()
    return enqueue_extraction_job(
        [normalized_sku],
        kind="single",
        force_refresh=True,
        refresh_if_stale=False,
        mode="precise",
    )


@product_router.get("/extraction-jobs/{job_id}")
def extraction_job_status(
    job_id: str,
    include_items: bool = False,
    _actor: AuthUser = Depends(require_operator),
):
    job = get_extraction_job(job_id, include_items=include_items)
    if not job:
        raise HTTPException(status_code=404, detail="Fila de extracao nao encontrada.")
    return job


@product_router.post("/extraction-jobs/{job_id}/cancel")
def request_extraction_job_cancel(
    job_id: str,
    _actor: AuthUser = Depends(require_operator),
):
    job = cancel_extraction_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Fila de extracao nao encontrada.")
    return job


def _combine_summaries(summaries, skus):
    results = []
    seen_results = set()
    comparison = []
    stores = {}
    source_health = {}

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

        for source in summary.get("source_health") or []:
            aggregate_source = source_health.setdefault(
                source["key"],
                {**source, "offer_count": 0},
            )
            aggregate_source["offer_count"] = (
                aggregate_source.get("offer_count", 0) + source.get("offer_count", 0)
            )
            if source.get("status") == "available":
                aggregate_source["status"] = "available"

    return {
        "message": (
            f"Busca conjunta concluida para {len(skus)} SKUs: "
            f"{len(results)} ofertas em {len(comparison)} produtos."
        ),
        "results": results,
        "stores": list(stores.values()),
        "comparison": comparison,
        "skus": skus,
        "source_health": list(source_health.values()),
    }


@product_router.post("/search/batch")
def search_sku_batch(payload: SkuBatchRequest, db: Session = Depends(get_db)):
    skus = _normalize_sku_list(payload.skus)
    if not skus:
        raise HTTPException(status_code=400, detail="Informe ao menos um SKU valido.")
    search_limit = SKU_IMPORT_LIMIT if payload.local_only else BATCH_SKU_LIMIT
    if len(skus) > search_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {search_limit} SKUs por pesquisa.",
        )

    if not payload.local_only:
        _register_skus(db, skus)
    summaries = []
    local_rows = None
    metadata_map = None
    if payload.local_only:
        requested_skus = set(skus)
        local_rows = {sku: [] for sku in skus}
        metadata_map = _known_sku_metadata_map(db, requested_skus)
        for product, price in _latest_products_query(db).all():
            product_skus = set(_extract_skus(product.name or ""))
            if product.sku:
                product_skus.add(_normalize_sku(product.sku))
            for sku in product_skus.intersection(requested_skus):
                local_rows[sku].append((product, price))

    for sku in skus:
        try:
            if payload.local_only:
                sku_rows = sorted(
                    local_rows[sku],
                    key=lambda row: row[1].price,
                )[:250]
                cached = _serialize_results(
                    sku_rows,
                    metadata_map=metadata_map,
                    resolve_redirects=False,
                )
                summary = _apply_summary_metadata(
                    build_summary_from_cached_results(cached, sku),
                    db,
                    sku,
                    metadata_map=metadata_map,
                )
            else:
                summary, _status = _search_sku_summary(sku, payload, db)
            summaries.append(summary)
        except Exception as exc:
            db.rollback()
            print(f" ERROR: Pesquisa de {sku} falhou: {exc}")
            cached = _serialize_results(_find_sku_products(db, sku), db)
            if cached:
                summaries.append(build_summary_from_cached_results(cached, sku))

    return _combine_summaries(summaries, skus)


@product_router.post("/export/analytics")
def export_analytics(payload: AnalyticsExportRequest):
    if not payload.rows:
        raise HTTPException(status_code=400, detail="Nao ha dados para exportar.")

    workbook = build_analytics_workbook(payload)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"nevetracker-analytics-{timestamp}.xlsx"
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
    return _serialize_results(_find_products(db, q), db)


@product_router.post("/refresh")
def refresh_prices(
    q: str,
    postal_code: Optional[str] = None,
    scope: str = "all",
    db: Session = Depends(get_db),
):
    summary = _apply_summary_metadata(
        run_all_scrapers(q, db, postal_code=postal_code, scope=scope),
        db,
        _primary_sku(q),
    )
    return {
        "message": (
            "Busca finalizada. "
            f"{summary['processed_count']} preços novos ou atualizados"
        ),
        "results": summary["results"],
        "stores": summary["stores"],
        "comparison": summary["comparison"],
        "source_health": summary.get("source_health", []),
    }


@product_router.get("/search")
def search_and_update(q: str = Query(..., min_length=2), db: Session = Depends(get_db)):
    search_text = q.strip()
    sku = _primary_sku(search_text)

    if sku:
        cached_results = _serialize_results(_find_sku_products(db, sku), db)
        if cached_results:
            summary = _apply_summary_metadata(
                build_summary_from_cached_results(cached_results, sku), db, sku
            )
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
                "source_health": summary.get("source_health", []),
            }

    summary = _apply_summary_metadata(
        run_all_scrapers(search_text, db), db, _primary_sku(search_text)
    )
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
        "source_health": summary.get("source_health", []),
    }
