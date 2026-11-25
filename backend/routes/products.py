import csv
import json
import math
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from typing import Literal, Optional
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import and_, case, exists, func, or_, literal, select
from sqlalchemy.orm import Session, aliased

from database import SessionLocal, begin_report_snapshot, get_db
from models.auth import AuthUser
from models.product import (
    ExtractionJob,
    ExtractionJobItem,
    ExtractionSchedule,
    KnownSku,
    PriceHistory,
    latest_price_at_destination,
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
from services.auth_service import FULL_ACCESS_ROLES, require_developer, require_operator, get_current_user, get_optional_current_user, user_role
from services.product_sources import HIDDEN_CURRENT_SOURCE_STATUSES, serialize_source
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


def _latest_products_query(db: Session, sku_filter=None, product_filter=None):
    if db.get_bind().dialect.name == "sqlite":
        latest_id = latest_price_at_destination(PriceHistory)
        query = (
            db.query(Product, PriceHistory)
            .join(PriceHistory, Product.id == PriceHistory.product_id)
            .filter(PriceHistory.id == latest_id)
        )
        if sku_filter:
            query = query.filter(Product.sku.in_(sku_filter))
        if product_filter is not None:
            query = query.filter(Product.id.in_(product_filter))
        return _verified_latest_products(query)

    prices = (
        db.query(
            PriceHistory.id.label("price_id"),
            func.row_number().over(
                partition_by=(PriceHistory.product_id, PriceHistory.destination_zip),
                order_by=(PriceHistory.timestamp.desc(), PriceHistory.id.desc()),
            ).label("position"),
        )
    )
    if sku_filter:
        prices=prices.filter(PriceHistory.product_id.in_(select(Product.id).where(Product.sku.in_(sku_filter))))
    if product_filter is not None:
        prices=prices.filter(PriceHistory.product_id.in_(product_filter))
    subquery=prices.subquery()

    return _verified_latest_products(
        db.query(Product, PriceHistory)
        .join(PriceHistory, Product.id == PriceHistory.product_id)
        .join(
            subquery,
            (PriceHistory.id == subquery.c.price_id)
            & (subquery.c.position == 1),
        )
    )


def _verified_latest_products(query):
    return query.filter(~exists().where(
        (ProductSource.sku == Product.sku)
        & (ProductSource.url == PriceHistory.url)
        & (ProductSource.seller == PriceHistory.store)
        & (ProductSource.validation_status.in_(HIDDEN_CURRENT_SOURCE_STATUSES))
    ))


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

    clauses = [Product.name.ilike('%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', escape='\\') for term in terms]
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
    if not results:
        return []
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
    from services.material_index import material_status_map
    return material_status_map(db, _normalize_sku_list(skus) if skus else None)


def _at_destinations(query, postal_codes):
    if not postal_codes:
        return query
    destination = PriceHistory.destination_zip.in_(postal_codes)
    if "01001000" in postal_codes:
        # Legacy entries without a destination belonged to the default search.
        destination = or_(destination, PriceHistory.destination_zip.is_(None), PriceHistory.destination_zip == "")
    return query.filter(destination)


def _find_products(db: Session, search_text: Optional[str] = None, strict=False, postal_codes=None):
    query = _at_destinations(_latest_products_query(db), postal_codes)
    if strict and search_text and not _query_terms(search_text):
        return []

    if search_text:
        strict_results = (
            _apply_search_filter(query, search_text, match_all=True)
            .order_by(PriceHistory.price.asc())
            .limit(30)
            .all()
        )
        if strict_results or strict:
            return strict_results

        return (
            _apply_search_filter(query, search_text, match_all=False)
            .order_by(PriceHistory.price.asc())
            .limit(30)
            .all()
        )

    return query.order_by(PriceHistory.timestamp.desc()).limit(50).all()


def _find_sku_products(db: Session, sku: str, postal_codes=None):
    matching = or_(Product.sku == sku, (Product.sku.is_(None)) & Product.name.ilike(f"%{sku}%"))
    query = _latest_products_query(db, product_filter=select(Product.id).where(matching)).filter(matching)
    return _at_destinations(query, postal_codes).order_by(PriceHistory.price.asc()).all()


@product_router.get("/skus/status")
def list_sku_status(
    q: Optional[str] = Query(None),
    page: Optional[int] = Query(None, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    from services.material_index import material_query,material_status_map
    query=material_query(db,q or '')
    total=query.count()
    query=query.order_by('sku')
    if isinstance(page,int):query=query.offset((page-1)*page_size).limit(page_size)
    keys=[row[0] for row in query.all()]
    items=list(material_status_map(db,keys).values()) if keys else []
    return {'items':items,'total':total,'page':page if isinstance(page,int) else 1,'page_size':page_size if isinstance(page,int) else total}


@product_router.get("/skus/keys")
def list_sku_keys(q: str = Query("", max_length=250), db: Session = Depends(get_db)):
    from services.material_index import material_query

    return {"skus": [sku for (sku,) in material_query(db, q).order_by("sku")]}


def _product_belongs_to_sku(product, sku):
    if product.sku:
        return _normalize_sku(product.sku) == sku
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
            ExtractionJobItem.status.in_(("queued", "running", "waiting_access")),
            ExtractionJob.status.in_(("queued", "running", "waiting_access", "paused")),
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
    deleted_sources=db.query(ProductSource).filter(source_filter).delete(synchronize_session=False)
    deleted_cursors=db.query(ScrapeCursor).filter(ScrapeCursor.sku == normalized_sku).delete(
        synchronize_session=False
    )
    from models.product import OfferObservation, LeadTimeQuote
    deleted_observations=db.query(OfferObservation).filter_by(sku=normalized_sku).delete(synchronize_session=False)
    deleted_quotes=db.query(LeadTimeQuote).filter_by(sku=normalized_sku).delete(synchronize_session=False)
    for saved in db.query(ExtractionJobItem).filter_by(sku=normalized_sku).all():
        db.delete(saved)
    for saved in db.query(ExtractionJobItem).filter(ExtractionJobItem.results_json.isnot(None)).yield_per(100):
        try: offers=json.loads(saved.results_json or '[]')
        except (ValueError,TypeError):continue
        retained=[offer for offer in offers if offer.get('sku')!=normalized_sku]
        if len(retained)!=len(offers):
            saved.results_json=json.dumps(retained,ensure_ascii=False,default=str)
            saved.results_count=len(retained)
            saved.stores_count=len({offer.get('source') for offer in retained})
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
    if not any([products, deleted_known, deleted_quotes, deleted_sources, deleted_cursors, deleted_observations]):
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
    schedule.owner_user_id = _actor.id
    schedule.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(schedule)
    return _schedule_response(schedule)


@product_router.post("/batch-search")
def batch_search_products(payload: SkuBatchRequest, db: Session = Depends(get_db), _actor: AuthUser = Depends(require_operator)):
    return create_extraction_job(payload, _actor, db)


def _search_sku_summary(sku, payload, db, refreshed_after=None, collection_after=None, resume=False, progress=None, catalog_snapshots=None, listing_snapshots=None):
    # A forced/resumed precise pass will collect anyway. Loading every saved
    # destination first adds a full history scan and cannot change that decision.
    precise_refresh = payload.mode == 'precise' and (payload.force_refresh or resume)
    quick_refresh = payload.mode == 'quick' and payload.force_refresh
    if quick_refresh and refreshed_after:
        # Jobs may reuse a refresh completed after enqueueing. The cheap existence
        # check deliberately overmatches; normal store/seller validation follows
        # whenever a recent row could qualify, including legacy products without SKU.
        matching = or_(Product.sku == sku, (Product.sku.is_(None)) & Product.name.ilike(f"%{sku}%"))
        recent = db.query(PriceHistory.id).join(Product, Product.id == PriceHistory.product_id).filter(
            matching, PriceHistory.timestamp >= refreshed_after).first()
        quick_refresh = recent is None
    cached_results = [] if precise_refresh or quick_refresh else _serialize_results(
        _find_sku_products(db, sku), db, resolve_redirects=payload.mode != 'quick',
    )
    if payload.sellers:
        from services.seller_selection import seller_matches
        cached_results = [item for item in cached_results if seller_matches(payload.sellers,item)]
    if payload.stores:
        from services.store_registry import find_target_store
        cached_results = [item for item in cached_results
                          if (store := find_target_store(item.get('source'))) and store['key'] in payload.stores]
    if cached_results and payload.mode == 'quick':
        from services.quick_extraction import scope_cached_results
        cached_results = scope_cached_results(cached_results, payload.postal_code)
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
            api_with_scraping=payload.api_with_scraping,
            stores=payload.stores,
            **({'sellers': payload.sellers} if payload.sellers else {}),
            **({'catalog_snapshots': catalog_snapshots} if catalog_snapshots else {}),
        )
    else:
        from services.quick_extraction import run_quick_extraction
        summary = run_quick_extraction(
            sku,
            db,
            postal_code=payload.postal_code,
            scope=payload.scope,
            **({'stores': payload.stores} if payload.stores else {}),
            **({'sellers': payload.sellers} if payload.sellers else {}),
            **({'catalog_snapshots': catalog_snapshots} if catalog_snapshots else {}),
            **({'listing_snapshots': listing_snapshots} if listing_snapshots else {}),
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
    from services.extraction_resources import check_cancelled

    check_cancelled()
    db = SessionLocal()
    try:
        from services.marketplace_intervention import waiting_stores
        from services.store_registry import TARGET_STORES
        # A quick job resumes only its blocked marketplace via the resumable
        # collector; it must not repeat all other storefronts after login.
        recovery=bool(options.get('mode')=='precise' and options.get('remaining_waiting_stores') and options.get('stores') and set(options['stores'])<={'mercado_livre','shopee'})
        payload = SkuBatchRequest(
            skus=[sku],
            force_refresh=bool(options.get("force_refresh")),
            refresh_if_stale=bool(options.get("refresh_if_stale")),
            postal_code=options.get("postal_code"),
            scope=options.get("scope") or "all",
            mode="precise" if recovery else options.get("mode") or "quick",
            api_with_scraping=options.get('api_with_scraping'),
            postal_codes=options.get('postal_codes') or [],
            stores=options.get('stores') or [],
            sellers=options.get('sellers') or {},
        )
        keys=payload.stores or [store['key'] for store in TARGET_STORES if payload.scope=='all' or store.get('scope')==payload.scope]
        deferred_at_start=waiting_stores(keys)
        if payload.postal_codes:
            from services.delivery_destinations import first_quotable_cep
            payload.postal_code = first_quotable_cep(payload.postal_codes)
            if payload.postal_code is None:
                raise ValueError('Nenhum dos CEPs informados foi localizado.')
        unchanged, snapshots, listings = {}, {}, {}
        scheduled = options.get('kind') == 'scheduled'
        price_check = scheduled and payload.refresh_if_stale and len(payload.postal_codes) <= 1
        if price_check and not payload.sellers:
            from services.same_day_refresh import scheduled_price_check
            unchanged, snapshots, listings = scheduled_price_check(
                db, sku, [key for key in keys if key not in deferred_at_start],
                payload.postal_code or '01001000', payload.mode, job_created_at=options.get('job_created_at'),
            )
        elif not scheduled and payload.mode == 'precise' and payload.force_refresh and options.get('attempt', 1) == 1 and not payload.sellers:
            from services.same_day_refresh import unchanged_catalog_stores
            unchanged, snapshots = unchanged_catalog_stores(db, sku, keys,
                payload.postal_codes or [payload.postal_code or '01001000'])
        check_cancelled()
        remaining = [key for key in keys if key not in unchanged]
        if unchanged:
            payload.stores = remaining
        if not remaining:
            from services.scraper_service import build_summary_from_cached_results
            from services.store_registry import find_target_store
            cached = ([offer for offers in unchanged.values() for offer in offers] if price_check else
                      [item for item in _serialize_results(_find_sku_products(db, sku), db)
                       if (store := find_target_store(item.get('source'))) and store['key'] in unchanged])
            summary, status = build_summary_from_cached_results(cached, sku), 'cached'
        else:
            summary, status = _search_sku_summary(
                sku,
                payload,
                db,
                refreshed_after=(
                    None if price_check or (payload.mode == "precise" and options.get("attempt", 1) > 1)
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
                **({'catalog_snapshots': snapshots} if snapshots else {}),
                **({'listing_snapshots': listings} if listings else {}),
            )
        check_cancelled()
        result = _sku_batch_result(db, sku, summary, status)
        if payload.postal_codes and payload.mode == 'precise' and not summary.get('more_work'):
            from services.multi_postal_extraction import collect_destinations
            from services.marketplace_intervention import suspension as pending
            from services.store_registry import find_target_store
            available=[item for item in summary.get('results') or []
                       if (store := find_target_store(item.get('source')))
                       and store['key'] in remaining and not pending(store['key'])]
            result['_offers']=collect_destinations(db,sku,available,payload.postal_codes,
                progress=lambda message:update_job_progress(options.get('job_id'),sku,message,db) if options.get('job_id') else None,
                refreshed_after=options.get('job_created_at'), reuse_collected_quotes=status == 'updated')
            result['results_count']=len(result['_offers'])
            result['message']=f"Coleta concluída em {len(payload.postal_codes)} CEPs; {len(result['_offers'])} resultados por oferta e destino."
        elif not summary.get('more_work'):
            result['_offers']=summary.get('results') or []
        if payload.mode == 'quick' and summary.get('limitations'):
            limited = len({issue['store'] for issue in summary['limitations']})
            result['message'] += f' {limited} loja(s) com retorno parcial ou indisponível na consulta rápida.'
        if unchanged:
            if price_check and not remaining:
                result['_offers'] = cached
            else:
                result['_offers'] = [*result.get('_offers', []), *(offer for values in unchanged.values() for offer in values)]
            result['results_count'] = len(result['_offers'])
            result['stores_count'] = len({offer.get('source') for offer in result['_offers']})
            if price_check:
                checked = f"Preços conferidos sem alteração em {', '.join(unchanged)}. Registro atualizado com as cotações anteriores."
                result['message'] = f"{result['message']} {checked}" if remaining else checked
            else:
                result['message'] += ' Preços e disponibilidade conferidos no catálogo; sem alterações em ' + ', '.join(unchanged) + '. Cotações de hoje preservadas com a data original.'
        result['_waiting_stores']=(sorted(set(deferred_at_start)|set(summary.get('waiting_marketplaces') or [])|set(waiting_stores(keys)))
                                   if payload.mode=='precise' else [])
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
        check_cancelled()
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

    if payload.postal_codes:
        from services.delivery_destinations import first_quotable_cep
        if first_quotable_cep(payload.postal_codes) is None:
            raise HTTPException(422, 'Nenhum dos CEPs informados foi localizado.')

    job = enqueue_extraction_job(
        skus,
        kind="single" if len(skus) == 1 else "manual",
        force_refresh=payload.force_refresh,
        refresh_if_stale=payload.refresh_if_stale,
        postal_code=normalize_postal_code(payload.postal_code),
        scope=payload.scope,
        mode=(payload.mode if "mode" in payload.model_fields_set else get_control(db).default_mode),
        owner_user_id=_actor.id,
        api_with_scraping=get_control(db).api_with_scraping,
        postal_codes=payload.postal_codes,
        stores=payload.stores,
        sellers=payload.sellers,
        register_materials=True,
    )
    if payload.postal_codes:
        from services.delivery_destinations import save_destinations
        try:
            saved = save_destinations(db, payload.postal_codes, skip_unknown=True)
            if unknown := saved.get('unknown_postal_codes'):
                job['message'] += ' CEPs não localizados, sem cotação: ' + ', '.join(unknown) + '.'
        except ValueError as exc:
            job['message']+=' Os destinos do Lead Time não foram alterados: '+str(exc)
    return job


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
        owner_user_id=_actor.id,
        api_with_scraping=get_control(db).api_with_scraping,
    )


@product_router.get("/extraction-jobs/active")
def active_extraction_job(_actor: AuthUser = Depends(require_operator)):
    return {"job": get_active_extraction_job()}


@product_router.get("/crawler-control")
def crawler_control_settings(
    _actor: AuthUser = Depends(require_operator),
    db: Session = Depends(get_db),
):
    return {
        **serialize_control(get_control(db)),
        "last_extraction_at": db.query(func.max(PriceHistory.timestamp)).scalar(),
    }


@product_router.put("/crawler-control")
def save_crawler_control(
    payload: CrawlerControlRequest,
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
):
    return serialize_control(update_control(db, payload))


@product_router.get('/extraction-sellers')
def extraction_sellers(_actor: AuthUser = Depends(require_operator), db: Session = Depends(get_db)):
    from services.seller_selection import seller_key
    from services.store_registry import find_target_store
    values = {store['key']: {} for store in TARGET_STORES}
    for store, seller in db.query(ProductSource.store,ProductSource.seller).filter(ProductSource.seller.isnot(None)).distinct():
        if store in values and seller and seller.strip():
            values[store].setdefault(seller_key(store,seller),seller.strip())
    for source, seller in db.query(PriceHistory.source,PriceHistory.store).distinct():
        target=find_target_store(source)
        if target and seller and seller.strip():
            values[target['key']].setdefault(seller_key(target['key'],seller),seller.strip())
    return {'items':[{'store':store['key'],'label':store['label'],'sellers':sorted(values[store['key']].values(),key=str.casefold)} for store in TARGET_STORES]}


@product_router.get("/sources")
def list_product_sources(
    sku: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    _actor: AuthUser = Depends(require_developer),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 50,
):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="Paginação inválida.")
    query = db.query(ProductSource)
    if sku:
        query = query.filter(ProductSource.sku == _normalize_sku(sku))
    if active is not None:
        query = query.filter(ProductSource.active.is_(active))
    total = query.count()
    page = min(page, max(1, (total + page_size - 1) // page_size))
    items = query.order_by(
        ProductSource.last_collected_at.desc(),
        ProductSource.first_seen_at.desc(),
        ProductSource.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total, "page": page, "page_size": page_size,
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
        owner_user_id=_actor.id,
        api_with_scraping=get_control(db).api_with_scraping,
    )


@product_router.get("/extraction-jobs/{job_id}")
def extraction_job_status(
    job_id: str,
    include_items: bool = False,
    _actor: AuthUser = Depends(require_operator),
    include_metrics: bool = True,
):
    job = get_extraction_job(job_id, include_items=include_items, include_metrics=include_metrics)
    if not job:
        raise HTTPException(status_code=404, detail="Fila de extracao nao encontrada.")
    return job


@product_router.post('/extraction-jobs/{job_id}/access/{store}/{action}')
def resolve_job_access(job_id:str,store:str,action:str,_actor:AuthUser=Depends(require_operator),db:Session=Depends(get_db)):
    from services.marketplace_intervention import pending,STORES
    from services.marketplace_session import start_validation,finish_validation
    job=db.get(ExtractionJob,job_id)
    if not job:raise HTTPException(404,'Extração não encontrada.')
    if job.owner_user_id!=_actor.id:raise HTTPException(403,'Somente o usuário que iniciou a extração pode validar este acesso.')
    if store not in STORES or action not in {'open','confirm'}:raise HTTPException(400,'Ação de acesso inválida.')
    waiting={value for item in job.items for value in json.loads(item.waiting_stores_json or '[]')}
    if job.status in {'queued','running','waiting_access'}:
        requested=json.loads(job.stores_json or '[]') or [value['key'] for value in TARGET_STORES if job.scope=='all' or value.get('scope')==job.scope]
        waiting.update(value for value in requested if pending(value))
    if store not in waiting or not pending(store):raise HTTPException(409,'Esta pendência já foi liberada ou não pertence à extração.')
    try:return start_validation(store) if action=='open' else finish_validation(store)
    except ValueError as exc:raise HTTPException(409,str(exc))


@product_router.post('/extraction-jobs/{job_id}/pause')
def pause_extraction(job_id:str,_actor:AuthUser=Depends(require_operator)):
    from services.extraction_queue import set_extraction_paused
    job=set_extraction_paused(job_id,_actor.id,True)
    if not job:raise HTTPException(404,'Extração não encontrada.')
    return job


@product_router.post('/extraction-jobs/{job_id}/resume')
def resume_extraction(job_id:str,_actor:AuthUser=Depends(require_operator)):
    from services.extraction_queue import set_extraction_paused
    job=set_extraction_paused(job_id,_actor.id,False)
    if not job:raise HTTPException(404,'Extração não encontrada.')
    return job


@product_router.post("/extraction-jobs/{job_id}/cancel", summary="Solicitar cancelamento pelo responsável ou por um administrador")
def request_extraction_job_cancel(
    job_id: str,
    _actor: AuthUser = Depends(require_operator),
):
    job = cancel_extraction_job(job_id, owner_user_id=_actor.id, can_cancel_any=user_role(_actor) in FULL_ACCESS_ROLES)
    if not job:
        raise HTTPException(status_code=404, detail="Fila de extracao nao encontrada.")
    return job


def _combine_summaries(summaries, skus, include_stores=True):
    results = []
    seen_results = set()
    comparison = []
    stores = {}
    store_identities = {}
    source_health = {}

    def identity(item):
        return (item.get("sku"), item.get("source"), item.get("store"), item.get("url"), item.get("destination_zip"))

    for summary in summaries:
        for item in summary["results"]:
            key = identity(item)
            if key not in seen_results:
                seen_results.add(key)
                results.append(item)

        for row in summary["comparison"]:
            comparison.append({**row, "id": len(comparison) + 1})

        for store in summary["stores"] if include_stores else ():
            aggregate = stores.setdefault(
                store["key"],
                {
                    **store,
                    "available": False,
                    "product": None,
                    "products": [],
                },
            )
            seen = store_identities.setdefault(store["key"], set())
            for product in store.get("products") or []:
                key = identity(product)
                if key in seen:
                    continue
                aggregate["products"].append(product)
                seen.add(key)

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

    for aggregate in stores.values():
        aggregate["available"] = bool(aggregate["products"])
        aggregate["product"] = min(
            aggregate["products"],
            key=lambda product: product["current_price"] if product["current_price"] is not None else float("inf"),
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
        "source_health": list(source_health.values()),
    }


@product_router.post("/search/batch")
def search_sku_batch(payload: SkuBatchRequest, db: Session = Depends(get_db), include_stores: bool = True):
    if not payload.local_only:
        raise HTTPException(409, 'Inicie a extração pelo modal de Extrações para acompanhar a fila.')
    skus = _normalize_sku_list(payload.skus)
    if not skus:
        raise HTTPException(status_code=400, detail="Informe ao menos um SKU valido.")
    search_limit = SKU_IMPORT_LIMIT
    if len(skus) > search_limit:
        raise HTTPException(
            status_code=400,
            detail=f"Limite de {search_limit} SKUs por pesquisa.",
        )

    # Keep the navbar's per-material preview bounded before hydrating price history.
    # The Database view and exports paginate the full set of observations.
    latest = _latest_products_query(db, skus)
    if payload.postal_codes:
        latest = latest.filter(PriceHistory.destination_zip.in_(payload.postal_codes))
    sort_fields = (PriceHistory.price.asc().nulls_last(), PriceHistory.id.asc())
    if len(skus) == 1:
        rows = latest.order_by(*sort_fields).limit(250)
    else:
        ranked = latest.with_entities(
            PriceHistory.id.label("price_id"),
            func.row_number().over(partition_by=Product.sku, order_by=sort_fields).label("position"),
        ).subquery()
        rows = (db.query(Product, PriceHistory).join(PriceHistory, PriceHistory.product_id == Product.id)
                .join(ranked, ranked.c.price_id == PriceHistory.id)
                .filter(ranked.c.position <= 250).order_by(Product.sku, ranked.c.position))

    metadata = _known_sku_metadata_map(db, skus)
    by_sku = {sku: [] for sku in skus}
    for item in _serialize_results(rows, metadata_map=metadata, resolve_redirects=False):
        by_sku[item["sku"]].append(item)
    summaries = []
    for sku in skus:
        summary = build_summary_from_cached_results(by_sku[sku], sku)
        for row in summary["comparison"]:
            _apply_item_metadata(row, metadata, sku)
        summaries.append(summary)

    return _combine_summaries(summaries, skus, include_stores)


@product_router.post("/export/analytics")
def export_analytics(payload: AnalyticsExportRequest):
    if not payload.rows:
        raise HTTPException(status_code=400, detail="Nao ha dados para exportar.")

    workbook = build_analytics_workbook(payload)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"nevetracker-analytics-{timestamp}.xlsx"
    from routes.analysis import excel
    return excel(workbook,filename)


@product_router.get("/analytics/sheet/options", dependencies=[Depends(get_current_user)])
def analytics_sheet_options(
    source: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
):
    latest = _latest_products_query(db)
    selected_sources = set(_analytics_filter_values(source))
    if db.get_bind().dialect.name == 'sqlite':
        # The covering source/seller index is small. For each distinct pair,
        # stop after its first verified current row, even with dozens of CEPs.
        pairs = select(PriceHistory.source.label('source'), PriceHistory.store.label('seller')).distinct().subquery()
        current = latest.filter(PriceHistory.source.is_not_distinct_from(pairs.c.source),
            PriceHistory.store.is_not_distinct_from(pairs.c.seller)).correlate(pairs).exists()
        options = db.query(pairs.c.source, pairs.c.seller).filter(current)
    else:
        options = latest.with_entities(PriceHistory.source, PriceHistory.store).distinct()
    sources, sellers = set(), set()
    for store_source, seller in options:
        if store_source:
            sources.add(store_source)
        if seller and (not selected_sources or store_source in selected_sources):
            sellers.add(seller)
    return {"sources": sorted(sources), "sellers": sorted(sellers)}


def _analytics_filter_values(values):
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(value.strip() for value in (values or []) if value and value.strip()))


def _analytics_sheet_query(db, skus, search, source, seller, fulfillment=None, date_from=None, date_to=None):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, 'A data final deve ser igual ou posterior à data inicial.')
    requested_skus = _normalize_sku_list(re.split(r"[,;\s]+", skus))
    query = _latest_products_query(db, requested_skus)
    # Filter the latest saved update, without reviving an older price from history.
    from services.price_observations import LOCAL
    if date_from:
        start = datetime.combine(date_from, datetime.min.time(), tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        query = query.filter(PriceHistory.timestamp >= start)
    if date_to and date_to < date.max:
        end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL).astimezone(timezone.utc).replace(tzinfo=None)
        query = query.filter(PriceHistory.timestamp < end)
    if requested_skus:
        query = query.filter(Product.sku.in_(requested_skus))
    for word in search.split():
        term = f"%{word}%"
        query = query.filter(Product.sku.ilike(term) if re.fullmatch(r'(?=.*\d)[A-Za-z0-9-]+',word) else or_(Product.name.ilike(term), Product.sku.ilike(term)))
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


def _analytics_offer_order(date_order, sku, price, product_id, timestamp, destination_zip):
    dates = [] if date_order is None else [
        (timestamp.asc() if date_order == 'asc' else timestamp.desc()).nullslast(),
    ]
    return (*dates, sku.asc(), price.asc(), product_id.asc(), destination_zip.asc())


def _summary_sku_page(db, date_order, page, page_size):
    # The newest verified offer of each listing determines its SKU's sort date.
    # Seek it in listing/time order before aggregating the (much larger) set
    # of prices and destinations, which is only needed for SKUs on this page.
    candidate = aliased(PriceHistory)
    latest_at_destination = latest_price_at_destination(candidate)
    invalid = (select(ProductSource.id).where(ProductSource.sku == Product.sku,
        ProductSource.url == candidate.url, ProductSource.seller == candidate.store,
        ProductSource.validation_status.in_(HIDDEN_CURRENT_SOURCE_STATUSES)).correlate(Product, candidate).exists())
    newest = (select(candidate.id).where(candidate.product_id == Product.id,
        candidate.id == latest_at_destination, ~invalid)
        .order_by(candidate.timestamp.desc(), candidate.id.desc()).limit(1).correlate(Product).scalar_subquery())
    present = (select(Product.sku.label('sku'), PriceHistory.timestamp.label('last_update'))
        .select_from(Product).join(PriceHistory, PriceHistory.id == newest)
        .where(Product.sku.is_not(None), Product.sku != ''))
    candidates = present.union_all(select(KnownSku.sku, literal(None))).subquery()
    dates = func.max(candidates.c.last_update)
    ordering = ([dates.asc().nullslast() if date_order == 'asc' else dates.desc().nullslast()] if date_order else [])
    grouped = db.query(candidates.c.sku).group_by(candidates.c.sku)
    rows = grouped.add_columns(func.count().over().label('total')).order_by(
        *ordering, candidates.c.sku).offset((page-1)*page_size).limit(page_size).all()
    total = rows[0].total if rows else grouped.count() if page > 1 else 0
    return [row.sku for row in rows], total


@product_router.get("/analytics/sheet/summary", dependencies=[Depends(get_current_user)])
def analytics_sheet_summary(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    date_order: Optional[Literal['asc', 'desc']] = None,
):
    begin_report_snapshot(db)
    total_on_all_pages = None
    if (db.get_bind().dialect.name == 'sqlite' and not skus.strip() and not search.strip() and
        not date_from and not date_to and not any(_analytics_filter_values(values) for values in (source, seller, fulfillment))):
        selected, total_on_all_pages = _summary_sku_page(db, date_order, page, page_size)
        if not selected:
            return {"items": [], "total": total_on_all_pages, "page": page, "page_size": page_size}
    else:
        selected = None
    query = _analytics_sheet_query(db, skus, search, source, seller, fulfillment, date_from, date_to)
    if selected is not None:
        query = query.filter(Product.sku.in_(selected))
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
            func.max(PriceHistory.timestamp).label('last_update'),
        )
        .group_by(Product.sku)
    )
    if not date_from and not date_to and not _analytics_filter_values(source) and not _analytics_filter_values(seller) and not _analytics_filter_values(fulfillment):
        present = grouped.cte('present_sku_summary')
        # Keep requested/catalogued materials visible even when no storefront
        # exposed an offer. Unknown availability is not a stock-out assertion.
        missing = db.query(
            KnownSku.sku.label("sku"), func.coalesce(KnownSku.description, KnownSku.sku).label("name"),
            literal(0).label("offer_count"), literal(0).label("store_count"), literal(0).label("seller_count"),
            literal(None).label("average_price"), literal(None).label("lowest_price"),
            literal(None).label("highest_price"), literal(0).label("unavailable_count"),
            literal(None).label('last_update'),
        ).filter(~exists().where(present.c.sku == KnownSku.sku))
        requested = _normalize_sku_list(re.split(r"[,;\s]+", skus))
        if selected is not None:
            missing = missing.filter(KnownSku.sku.in_(selected))
        if requested:
            missing = missing.filter(KnownSku.sku.in_(requested))
        for word in search.split():
            term = f"%{word}%"
            missing = missing.filter(KnownSku.sku.ilike(term) if re.fullmatch(r'(?=.*\d)[A-Za-z0-9-]+',word) else or_(KnownSku.sku.ilike(term), KnownSku.description.ilike(term)))
        grouped_rows = select(*present.c).union_all(missing.statement).subquery()
    else:
        grouped_rows = grouped.subquery()
    ordering = ([grouped_rows.c.last_update.asc().nullslast() if date_order=='asc' else grouped_rows.c.last_update.desc().nullslast()] if date_order else [])
    if total_on_all_pages is None:
        page_rows = db.query(grouped_rows, func.count().over().label('row_total')).order_by(
            *ordering, grouped_rows.c.sku).offset((page-1)*page_size).limit(page_size).all()
        total = int(page_rows[0].row_total) if page_rows else db.query(grouped_rows).count() if page > 1 else 0
    else:
        page_rows = db.query(grouped_rows).order_by(*ordering, grouped_rows.c.sku).all()
        total = total_on_all_pages
    metadata = _known_sku_metadata_map(db, [row.sku for row in page_rows])
    cheapest_by_sku = {}
    page_skus = [row.sku for row in page_rows if row.lowest_price is not None]
    if page_skus:
        page_query=_analytics_sheet_query(db,'',search,source,seller,fulfillment,date_from,date_to).filter(Product.sku.in_(page_skus))
        price_matches=or_(*[(Product.sku==row.sku)&(priced==row.lowest_price) for row in page_rows if row.lowest_price is not None])
        cheapest_rows = page_query.filter(price_matches).with_entities(
            Product.sku,func.min(PriceHistory.store)).group_by(Product.sku).all()
        for code, store in cheapest_rows:
            cheapest_by_sku.setdefault(code, store)
    items = []
    for row in page_rows:
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
            "cheapest_store": cheapest_by_sku.get(row.sku, "") if lowest is not None else "",
            "difference_value": average - lowest if average is not None and lowest is not None else None,
            "difference_percent": (average - lowest) / lowest * 100 if lowest else None,
            "availability": 'out_of_stock' if row.offer_count and row.unavailable_count == row.offer_count else None,
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@product_router.get("/analytics/sheet/export", dependencies=[Depends(get_current_user)])
def export_analytics_sheet(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    date_order: Optional[Literal['asc', 'desc']] = None,
):
    """Export every matching saved offer, independently of the visible page."""
    begin_report_snapshot(db)
    matches = _analytics_sheet_query(db, skus, search, source, seller, fulfillment,date_from,date_to).order_by(
        *_analytics_offer_order(date_order, Product.sku, PriceHistory.price, Product.id,
                               PriceHistory.timestamp, PriceHistory.destination_zip),
    ).all()
    grouped = {}
    for offer in _serialize_results(matches, db, resolve_redirects=False):
        key = offer.get("sku") or offer["name"]
        group = grouped.setdefault(key, {"first": offer, "offers": []})
        group["offers"].append(offer)

    rows = []
    latest_updates = {}
    for group in grouped.values():
        offers = group["offers"]
        first = group["first"]
        available = [offer for offer in offers if offer_available(offer) and offer.get('current_price') is not None]
        def cash_value(offer):
            return offer["cash_price"] if offer.get("cash_price") is not None else offer["current_price"]
        prices = [cash_value(offer) for offer in available]
        cheapest = min(available, key=lambda offer: (cash_value(offer), offer["store"]), default=None)
        average = sum(prices) / len(prices) if prices else None
        lowest = cash_value(cheapest) if cheapest else None
        rows.append({
            "sku": first.get("sku"), "name": first["name"],
            "description": first.get("description"),
            "subfamily": first.get("subfamily"),
            "sku_category": first.get("sku_category"),
            "cluster": first.get("cluster"),
            "market_type": first.get("market_type"),
            "store_count": len({offer.get("source") or offer["store"] for offer in offers}),
            "offer_count": len(offers), "pma": average,
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
        if first.get('sku'):
            latest_updates[first['sku']] = max((offer['last_update'] for offer in offers if offer.get('last_update')), default=None)

    if not date_from and not date_to and not _analytics_filter_values(source) and not _analytics_filter_values(seller) and not _analytics_filter_values(fulfillment):
        requested = _normalize_sku_list(re.split(r"[,;\s]+", skus))
        known = db.query(KnownSku)
        if requested:
            known = known.filter(KnownSku.sku.in_(requested))
        for word in search.split():
            term = f"%{word}%"
            known = known.filter(KnownSku.sku.ilike(term) if re.fullmatch(r'(?=.*\d)[A-Za-z0-9-]+',word) else or_(KnownSku.sku.ilike(term), KnownSku.description.ilike(term)))
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
    if date_order:
        rows.sort(key=lambda row: row.get('sku') or '')
        rows.sort(key=lambda row: latest_updates.get(row.get('sku')) or
                  (datetime.max if date_order == 'asc' else datetime.min), reverse=date_order == 'desc')
    workbook = build_analytics_workbook(AnalyticsExportRequest(query=skus or search, rows=rows), date_order=date_order)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="nevetracker-analytics-{timestamp}.xlsx"'},
    )


def _unfiltered_offer_total():
    # There is exactly one current row per (listing, destination), including
    # NULL destinations. Count keys using the covering index, then subtract
    # only current rows rejected by the (usually much smaller) source table.
    destinations = (select(PriceHistory.product_id, PriceHistory.destination_zip)
        .join(Product, Product.id == PriceHistory.product_id)
        .group_by(PriceHistory.product_id, PriceHistory.destination_zip).subquery())
    excluded = (select(func.count(func.distinct(PriceHistory.id))).select_from(ProductSource)
        .join(Product, Product.sku == ProductSource.sku)
        .join(PriceHistory, (PriceHistory.product_id == Product.id) & (PriceHistory.url == ProductSource.url)
              & (PriceHistory.store == ProductSource.seller))
        .where(ProductSource.validation_status.in_(HIDDEN_CURRENT_SOURCE_STATUSES),
               PriceHistory.id == latest_price_at_destination(PriceHistory)).correlate(None).scalar_subquery())
    return select(func.count()).select_from(destinations).scalar_subquery() - excluded


@product_router.get("/analytics/sheet", dependencies=[Depends(get_current_user)])
def analytics_sheet(
    skus: str = "",
    search: str = "",
    source: Optional[list[str]] = Query(None),
    seller: Optional[list[str]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    fulfillment: Optional[list[str]] = Query(None),
    db: Session = Depends(get_db),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    date_order: Optional[Literal['asc', 'desc']] = None,
):
    """A paginated view of the latest saved offer for each product listing."""
    query = _analytics_sheet_query(db, skus, search, source, seller, fulfillment,date_from,date_to)

    # Keep count and page in a single SELECT so both see the same snapshot.
    unfiltered = (db.get_bind().dialect.name == 'sqlite' and not search.strip() and not skus.strip()
        and not date_from and not date_to and not any(_analytics_filter_values(values) for values in (source, seller, fulfillment)))
    row_total = _unfiltered_offer_total() if unfiltered else func.count().over()
    keys = (query.with_entities(
        Product.sku.label('sku'), Product.id.label('product_id'),
        PriceHistory.id.label('price_id'), PriceHistory.price.label('price'),
        PriceHistory.timestamp.label('timestamp'), PriceHistory.destination_zip.label('destination_zip'),
        row_total.label('row_total'),
    ).order_by(*_analytics_offer_order(date_order, Product.sku, PriceHistory.price, Product.id, PriceHistory.timestamp, PriceHistory.destination_zip))
        .offset((page - 1) * page_size).limit(page_size).subquery())
    page_rows = (db.query(Product, PriceHistory, keys.c.row_total).select_from(keys)
        .join(Product, Product.id == keys.c.product_id).join(PriceHistory, PriceHistory.id == keys.c.price_id)
        .order_by(*_analytics_offer_order(date_order, keys.c.sku, keys.c.price, keys.c.product_id, keys.c.timestamp, keys.c.destination_zip)).all())
    if page_rows:
        total = int(page_rows[0].row_total)
    elif page > 1:
        total = db.scalar(select(row_total)) if unfiltered else query.count()
    else:
        total = 0
    rows=[(row[0],row[1]) for row in page_rows]
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
    _actor: AuthUser = Depends(require_operator),
):
    return create_extraction_job(SkuBatchRequest(skus=_extract_skus(q), postal_code=postal_code, scope=scope), _actor, db)


@product_router.get("/search")
def search_and_update(q: str = Query(..., min_length=2, max_length=250), db: Session = Depends(get_db), actor=Depends(get_optional_current_user), postal_codes: Optional[list[str]] = Query(None), include_stores: bool = True):
    search_text = q.strip()
    sku = _primary_sku(search_text)
    try:requested=SkuBatchRequest(skus=[sku or search_text],postal_codes=postal_codes if isinstance(postal_codes,list) else []).postal_codes or ['01001000']
    except ValueError as exc:raise HTTPException(422,'Informe CEPs válidos com oito dígitos.') from exc
    destinations = requested if actor is not None else None
    rows = ([row for row in _find_sku_products(db, sku, postal_codes=destinations) if _product_belongs_to_sku(row[0], sku)]
            if sku else _find_products(db, search_text, strict=True, postal_codes=destinations))
    skus_in_result = {product.sku or _primary_sku(product.name or "") for product, _ in rows}
    skus_in_result.discard(None)
    metadata = _known_sku_metadata_map(db, skus_in_result) if skus_in_result else {}
    cached = _serialize_results(rows, metadata_map=metadata, resolve_redirects=False)
    if actor is not None and not cached:
        active_job = get_active_extraction_job()
        if active_job and active_job.get('owner_user_id') != actor.id:
            raise HTTPException(409, 'Outro usuário está realizando uma extração. Aguarde a conclusão para pesquisar novas ofertas.')
        summary = run_all_scrapers(search_text, db, postal_code=requested[0], scope='all', persist=False)
        if len(requested)>1 and sku:
            from services.multi_postal_extraction import collect_destinations
            quoted=collect_destinations(db,sku,summary.get('results') or [],requested,persist=False)
            summary=build_summary_from_cached_results([{**item,'current_price':item.get('price',item.get('current_price'))} for item in quoted],search_text)
        summary = _apply_summary_metadata(summary, db, sku)
        message = 'Pesquisa nas lojas concluída.'
    else:
        summary = build_summary_from_cached_results(cached, search_text)
        for row in summary['comparison']:
            _apply_item_metadata(row, metadata, sku)
        message = 'Consulta ao banco local.'
    if not include_stores:
        summary = {**summary, 'stores': []}
    return {'message': message, **summary}
