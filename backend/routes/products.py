import csv
import math
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
from sqlalchemy import and_, case, exists, func, or_, literal
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
    ScrapeCursor,
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
from services.scrape_pagination import coverage_by_store
from services.precise_extraction import run_precise_extraction
from services.product_resolver import canonical_brand
from services.structured_products import UNAVAILABLE_STATES, offer_available, seller_scope_key
from services.store_registry import TARGET_STORES
from services.extraction_queue import (
    MAX_JOB_SKUS,
    cancel_extraction_job,
    enqueue_extraction_job,
    get_active_extraction_job,
    get_extraction_job,
    update_job_progress,
)

product_router = APIRouter()
BATCH_SKU_LIMIT = 30
SKU_IMPORT_LIMIT = 5000
SKU_IMPORT_MAX_BYTES = 8 * 1024 * 1024
DB_QUERY_CHUNK_SIZE = 500
SKU_METADATA_FIELDS = (
    "description", "subfamily", "sku_category", "cluster", "market_type",
    "ean", "mpn", "model", "brand",
    "internal_code", "external_id", "reference_price",
)

SKU_IMPORT_HEADERS = {
    "codigo interno": "internal_code",
    "meu codigo": "internal_code",
    "id externo": "external_id",
    "id webglobal": "external_id",
    "meu preco": "reference_price",
    "preco de referencia": "reference_price",
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
            PriceHistory.id.label("price_id"),
            func.row_number().over(
                partition_by=PriceHistory.product_id,
                order_by=(PriceHistory.timestamp.desc(), PriceHistory.id.desc()),
            ).label("position"),
        )
        .subquery()
    )

    return (
        db.query(Product, PriceHistory)
        .join(PriceHistory, Product.id == PriceHistory.product_id)
        .join(
            subquery,
            (PriceHistory.id == subquery.c.price_id)
            & (subquery.c.position == 1),
        )
        .filter(~exists().where(
            (ProductSource.sku == Product.sku)
            & (ProductSource.url == PriceHistory.url)
            & (ProductSource.seller == PriceHistory.store)
            & (ProductSource.validation_status.in_(("invalid_match", "not_found")))
        ))
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
            "brand": canonical_brand(item.brand),
            "internal_code": item.internal_code,
            "external_id": item.external_id,
            "reference_price": item.reference_price,
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


def _serialize_results(results, db: Session = None, metadata_map=None, resolve_redirects=True, fresh_after=None):
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
                "brand": canonical_brand(product.brand),
                "category": product.category,
                "sku": product_sku,
                "image_url": product.image_url,
                "current_price": price.price,
                "store": resolved["store"],
                "source": source,
                "url": resolved["url"],
                "last_update": price.timestamp,
                "cash_price": price.cash_price or price.price,
                "is_cached": not bool(fresh_after and price.timestamp and price.timestamp >= fresh_after and not price.is_cached),
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
            offers = {(price.url, price.store) for _product, price in results}
            stores = {seller_scope_key({"source": price.source, "store": price.store}) for _product, price in results}
            last_update = max((price.timestamp for _product, price in results), default=None)
            statuses[sku] = {
                "sku": sku,
                "in_database": bool(results),
                "product_count": len(product_ids),
                "offer_count": len(offers),
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
        sku: {"products": set(), "offers": set(), "stores": set(), "last_update": None}
        for sku in all_skus
    }
    for product, price in _latest_products_query(db).all():
        row_skus = set(_extract_skus(product.name or ""))
        if product.sku:
            row_skus.add(_normalize_sku(product.sku))
        for sku in row_skus.intersection(all_skus):
            aggregate = aggregates[sku]
            aggregate["products"].add(product.id)
            aggregate["offers"].add((price.url, price.store))
            aggregate["stores"].add(seller_scope_key({"source": price.source, "store": price.store}))
            if not aggregate["last_update"] or (
                price.timestamp and price.timestamp > aggregate["last_update"]
            ):
                aggregate["last_update"] = price.timestamp

    for sku, aggregate in aggregates.items():
        statuses[sku] = {
            "sku": sku,
            "in_database": bool(aggregate["offers"]),
            "product_count": len(aggregate["products"]),
            "offer_count": len(aggregate["offers"]),
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
    return query.order_by(PriceHistory.price.asc()).all()


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
    db.query(ScrapeCursor).filter(ScrapeCursor.sku == normalized_sku).delete(
        synchronize_session=False
    )
    from models.product import OfferObservation, LeadTimeQuote
    db.query(OfferObservation).filter_by(sku=normalized_sku).delete(synchronize_session=False)
    db.query(LeadTimeQuote).filter_by(sku=normalized_sku).delete(synchronize_session=False)
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
        "Codigo interno", "ID externo", "Meu preco",
    ]
    worksheet.append(headers)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = "A1:M1"

    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0F7180")
    for column, width in zip("ABCDEFGHIJKLM", (20, 42, 24, 24, 20, 20, 18, 18, 20, 22, 20, 20, 18)):
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

        reference = values.get("reference_price", "").strip()
        if reference:
            try:
                # Spreadsheet numeric cells use '.', Brazilian CSV can use ','.
                normalized = reference.replace("R$", "").replace(" ", "")
                if "," in normalized:
                    normalized = normalized.replace(".", "").replace(",", ".")
                parsed_reference = float(normalized)
                if not math.isfinite(parsed_reference) or parsed_reference < 0:
                    raise ValueError()
            except ValueError:
                errors.append({"row": spreadsheet_row, "message": "Meu preco deve ser um valor monetario nao negativo."})
                continue
            values["reference_price"] = parsed_reference

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
            "description", "subfamily", "category", "cluster", "ean", "mpn", "model", "brand",
            "internal_code", "external_id",
        ):
            value = values.get(field, "").strip()
            if value:
                setattr(known_sku, field, canonical_brand(value[:250]) if field == "brand" else value[:250])
        if market_type:
            known_sku.market_type = market_type
        if isinstance(values.get("reference_price"), (int, float)):
            # Zero means no own price supplied, as in the reference JSON.
            known_sku.reference_price = values["reference_price"] or None
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
        "only_stale": schedule.only_stale,
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
    schedule.only_stale = payload.only_stale
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


def _search_sku_summary(sku, payload, db, refreshed_after=None, collection_after=None, resume=False, progress=None):
    cached_results = _serialize_results(_find_sku_products(db, sku), db)
    latest = max(
        (item.get("last_update") for item in cached_results if item.get("last_update")),
        default=None,
    )
    stale_hours = 1 if payload.scope == "sites" else 8
    stale = not latest or latest < datetime.utcnow() - timedelta(hours=stale_hours)
    should_refresh = (
        (payload.mode == "precise" and payload.force_refresh)
        or
        (resume and payload.mode == "precise")
        or (
            not (latest and refreshed_after and latest >= refreshed_after)
            and (
                payload.force_refresh
                or not cached_results
                or (payload.refresh_if_stale and stale)
            )
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
            collection_after=collection_after,
            resume=resume,
            progress=progress,
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
    if status == "cached":
        message = f"Cache local consultado; {len(summary['results'])} ofertas disponíveis."
    elif not summary.get("fresh_count"):
        message = (
            "Coleta concluída sem ofertas novas nesta execução; "
            f"{len(summary['results'])} ofertas permanecem no cache."
        )
    else:
        fresh_count = summary["fresh_count"]
        fresh_label = "oferta verificada" if fresh_count == 1 else "ofertas verificadas"
        message = (
            f"{fresh_count} {fresh_label}; "
            f"{summary['processed_count']} ofertas novas ou com preço alterado. "
            f"{len(summary['results'])} ofertas disponíveis no total."
        )
    return {
        "sku": sku,
        "status": status,
        "message": message,
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
            refreshed_after=(
                None if payload.mode == "precise" and options.get("attempt", 1) > 1
                else options.get("job_created_at")
            ),
            collection_after=options.get("job_created_at") if payload.mode == "precise" else None,
            resume=payload.mode == "precise" and options.get("attempt", 1) > 1,
            progress=(
                lambda message: update_job_progress(
                    options["job_id"], sku,
                    f"Rodada {options.get('attempt', 1)}: {message}", db,
                )
                if options.get("job_id") else None
            ),
        )
        result = _sku_batch_result(db, sku, summary, status)
        if payload.mode == "precise" and status == "updated" and summary.get("more_work"):
            result["status"] = "partial"
            result["message"] = (
                f"Lote coletado; {summary.get('pending_sources', 0)} fontes aguardam coleta "
                f"e {summary.get('pending_stores', 0)} lojas aguardam descoberta, "
                "alem das paginas de vendedores pendentes."
            )
        elif payload.mode == "precise":
            limitations = []
            if summary.get("pending_pages"):
                limitations.append(
                    f"{summary['pending_pages']} paginas de loja nao avancaram"
                )
            if summary.get("failed_sources"):
                limitations.append(
                    f"{summary['failed_sources']} paginas de produto falharam "
                    f"({summary.get('failed_sources_without_price', 0)} sem preco de listagem)"
                )
            if summary.get('limited_sources'):
                limitations.append(f"{summary['limited_sources']} paginas nao expuseram todas as ofertas de vendedores")
            if limitations:
                result["message"] += (
                    " " + "; ".join(limitations)
                    + " por bloqueio ou falha de leitura; a cobertura pode estar incompleta."
                )
                affected = [
                    f"{row['label']} ({row.get('failed_pages', 0)} paginas, "
                    f"{row.get('failed_sources', 0)} produtos)"
                    for row in summary.get("coverage_by_store") or []
                    if row.get("failed_pages") or row.get("failed_sources") or row.get('limited_sources')
                ]
                if affected:
                    result["message"] += " Lojas afetadas: " + ", ".join(affected) + "."
        return result
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
        mode=(payload.mode if "mode" in payload.model_fields_set else get_control(db).default_mode),
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
        mode=(payload.mode if "mode" in payload.model_fields_set else get_control(db).default_mode),
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
        "coverage_by_store": coverage_by_store(db, _normalize_sku(sku), TARGET_STORES) if sku else [],
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
    db.query(ScrapeCursor).filter(ScrapeCursor.sku == normalized_sku).update(
        {
            ScrapeCursor.completed: True,
            ScrapeCursor.updated_at: datetime.min,
            ScrapeCursor.last_status: None,
            ScrapeCursor.failure_reason: None,
            ScrapeCursor.last_attempted_at: None,
            ScrapeCursor.browser_attempted_at: None,
            ScrapeCursor.browser_attempted_url: None,
            ScrapeCursor.visited_urls: "[]",
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
                key=lambda product: product["current_price"] if product["current_price"] is not None else float("inf"),
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


@product_router.get("/analytics/sheet/options")
def analytics_sheet_options(
    source: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    latest = _latest_products_query(db)
    sources = sorted({value for (value,) in latest.with_entities(PriceHistory.source).distinct().all() if value})
    selected_sources = _analytics_filter_values(source)
    if selected_sources:
        latest = latest.filter(PriceHistory.source.in_(selected_sources))
    sellers = sorted({value for (value,) in latest.with_entities(PriceHistory.store).distinct().all() if value})
    return {"sources": sources, "sellers": sellers}


def _analytics_filter_values(values):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(value.strip() for value in (values or []) if value and value.strip()))


def _analytics_sheet_query(db, skus, search, source, seller, fulfillment=None):
    query = _latest_products_query(db)
    requested_skus = _normalize_sku_list(re.split(r"[,;\s]+", skus))
    if requested_skus:
        query = query.filter(Product.sku.in_(requested_skus))
    if search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(Product.name.ilike(term), Product.sku.ilike(term)))
    selected_sources = _analytics_filter_values(source)
    selected_sellers = _analytics_filter_values(seller)
    if selected_sources:
        query = query.filter(PriceHistory.source.in_(selected_sources))
    if selected_sellers:
        query = query.filter(PriceHistory.store.in_(selected_sellers))
    selected_fulfillment = _analytics_filter_values(fulfillment)
    if selected_fulfillment:
        conditions = [PriceHistory.fulfillment_type.in_([value for value in selected_fulfillment if value in {'2P', '3P'}])]
        if 'unknown' in selected_fulfillment:
            conditions.append(PriceHistory.fulfillment_type.is_(None))
        query = query.filter(or_(*conditions))
    return query


@product_router.get("/analytics/sheet/summary")
def analytics_sheet_summary(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    query = _analytics_sheet_query(db, skus, search, source, seller, fulfillment)
    unavailable = func.lower(PriceHistory.availability).in_(tuple(UNAVAILABLE_STATES))
    # The provider's min/max are cash prices; fall back only when cash is unknown.
    priced = case((or_(PriceHistory.availability.is_(None), ~unavailable),
                   func.coalesce(PriceHistory.cash_price, PriceHistory.price)), else_=None)
    grouped = (
        query.filter(Product.sku.isnot(None), Product.sku != "")
        .with_entities(
            Product.sku.label("sku"),
            func.min(Product.name).label("name"),
            func.count(Product.id).label("offer_count"),
            func.count(func.distinct(func.coalesce(PriceHistory.source, PriceHistory.store))).label("store_count"),
            func.count(func.distinct(func.coalesce(PriceHistory.source, PriceHistory.store) + "|" + PriceHistory.store)).label("seller_count"),
            func.avg(priced).label("average_price"),
            func.min(priced).label("lowest_price"),
            func.max(priced).label("highest_price"),
            func.sum(case((unavailable, 1), else_=0)).label('unavailable_count'),
        )
        .group_by(Product.sku)
    )
    if not _analytics_filter_values(source) and not _analytics_filter_values(seller) and not _analytics_filter_values(fulfillment):
        # Keep requested/catalogued materials visible even when no storefront
        # exposed an offer. Unknown availability is not a stock-out assertion.
        missing = db.query(
            KnownSku.sku.label("sku"), func.coalesce(KnownSku.description, KnownSku.sku).label("name"),
            literal(0).label("offer_count"), literal(0).label("store_count"), literal(0).label("seller_count"),
            literal(None).label("average_price"), literal(None).label("lowest_price"),
            literal(None).label("highest_price"), literal(0).label("unavailable_count"),
        ).filter(~query.filter(Product.sku == KnownSku.sku).exists())
        requested = _normalize_sku_list(re.split(r"[,;\s]+", skus))
        if requested:
            missing = missing.filter(KnownSku.sku.in_(requested))
        if search.strip():
            term = f"%{search.strip()}%"
            missing = missing.filter(or_(KnownSku.sku.ilike(term), KnownSku.description.ilike(term)))
        grouped = grouped.union_all(missing)
    total = grouped.count()
    page_rows = grouped.order_by("sku").offset((page - 1) * page_size).limit(page_size).all()
    metadata = _known_sku_metadata_map(db, [row.sku for row in page_rows])
    items = []
    for row in page_rows:
        cheapest = (
            query.filter(Product.sku == row.sku, priced == row.lowest_price)
            .with_entities(PriceHistory.store)
            .order_by(PriceHistory.store)
            .first()
        )
        average = float(row.average_price) if row.average_price is not None else None
        lowest = float(row.lowest_price) if row.lowest_price is not None else None
        items.append({
            "sku": row.sku,
            "name": row.name,
            **metadata.get(_normalize_sku(row.sku), {}),
            "store_count": row.store_count,
            "seller_count": row.seller_count,
            "offer_count": row.offer_count,
            "pma": average,
            "cheapest_price": lowest,
            "highest_price": float(row.highest_price) if row.highest_price is not None else None,
            "reference_difference": (
                metadata.get(_normalize_sku(row.sku), {}).get("reference_price") - lowest
                if lowest is not None and metadata.get(_normalize_sku(row.sku), {}).get("reference_price") is not None
                else None
            ),
            "cheapest_store": cheapest[0] if cheapest and lowest is not None else "",
            "difference_value": average - lowest if average is not None and lowest is not None else None,
            "difference_percent": (average - lowest) / lowest * 100 if lowest else None,
            "availability": 'out_of_stock' if row.offer_count and row.unavailable_count == row.offer_count else None,
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@product_router.get("/analytics/sheet/export")
def export_analytics_sheet(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    """Export every matching saved offer, independently of the visible page."""
    matches = _analytics_sheet_query(db, skus, search, source, seller, fulfillment).order_by(
        Product.sku, PriceHistory.price, Product.id,
    ).all()
    grouped = {}
    for offer in _serialize_results(matches, db, resolve_redirects=False):
        key = offer.get("sku") or offer["name"]
        group = grouped.setdefault(key, {"first": offer, "offers": []})
        group["offers"].append(offer)

    rows = []
    for group in grouped.values():
        offers = group["offers"]
        first = group["first"]
        available = [offer for offer in offers if offer_available(offer) and offer.get('current_price') is not None]
        def cash_value(offer):
            return offer["cash_price"] if offer.get("cash_price") is not None else offer["current_price"]
        prices = [cash_value(offer) for offer in available]
        cheapest = min(available, key=cash_value, default=None)
        average = sum(prices) / len(prices) if prices else None
        lowest = cash_value(cheapest) if cheapest else None
        rows.append({
            "sku": first.get("sku"), "name": first["name"],
            "description": first.get("description"),
            "subfamily": first.get("subfamily"),
            "sku_category": first.get("sku_category"),
            "cluster": first.get("cluster"),
            "market_type": first.get("market_type"),
            "store_count": len({offer.get("source") or offer["store"] for offer in available}),
            "offer_count": len(available), "pma": average,
            "cheapest_price": lowest, "cheapest_store": cheapest["store"] if cheapest else "",
            "highest_price": max(prices) if prices else None,
            "internal_code": first.get("internal_code"),
            "external_id": first.get("external_id"),
            "reference_price": first.get("reference_price"),
            "reference_difference": first["reference_price"] - lowest if first.get("reference_price") is not None and lowest is not None else None,
            "difference_value": average - lowest if average is not None and lowest is not None else 0,
            "difference_percent": (average - lowest) / lowest * 100 if lowest else 0,
            "offers": offers,
        })

    if not _analytics_filter_values(source) and not _analytics_filter_values(seller) and not _analytics_filter_values(fulfillment):
        requested = _normalize_sku_list(re.split(r"[,;\s]+", skus))
        known = db.query(KnownSku)
        if requested:
            known = known.filter(KnownSku.sku.in_(requested))
        if search.strip():
            term = f"%{search.strip()}%"
            known = known.filter(or_(KnownSku.sku.ilike(term), KnownSku.description.ilike(term)))
        exported = {row.get('sku') for row in rows}
        for material in known.order_by(KnownSku.sku).all():
            if material.sku not in exported:
                rows.append({'sku': material.sku, 'name': material.description or material.sku,
                             'store_count': 0, 'offer_count': 0, 'pma': None,
                             'cheapest_price': None, 'cheapest_store': '',
                             'difference_value': None, 'difference_percent': None, 'offers': [],
                             'internal_code': material.internal_code, 'external_id': material.external_id,
                             'reference_price': material.reference_price})

    if not rows:
        raise HTTPException(status_code=400, detail="Nao ha ofertas disponiveis para comparar.")
    workbook = build_analytics_workbook(AnalyticsExportRequest(query=skus or search, rows=rows))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="nevetracker-analytics-{timestamp}.xlsx"'},
    )


@product_router.get("/analytics/sheet")
def analytics_sheet(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    """A paginated view of the latest saved offer for each product listing."""
    query = _analytics_sheet_query(db, skus, search, source, seller, fulfillment)

    total = query.count()
    rows = (query.order_by(Product.sku.asc(), PriceHistory.price.asc(), Product.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "items": _serialize_results(rows, db, resolve_redirects=False),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@product_router.post("/", status_code=201)
def create_product(product_data: ProductCreate, db: Session = Depends(get_db)):
    new_product = Product(
        name=product_data.name,
        brand=canonical_brand(product_data.brand),
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
    db.flush()
    from services.price_observations import record_observation
    record_observation(db, {
        **product_data.initial_price.model_dump(), 'sku': new_product.sku,
        'name': new_product.name, 'brand': new_product.brand,
    }, new_price.timestamp, origin='manual', legacy_price_id=new_price.id)
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
