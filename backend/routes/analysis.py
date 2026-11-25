from datetime import date, datetime, timedelta
from typing import Annotated
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import begin_report_snapshot, get_db
from models.product import OfferObservation
from services.price_observations import history_report, compare_groups, local_day, report_search_predicate
from services.analysis_workbooks import history_workbook, comparison_workbook

from services.auth_service import get_current_user
analysis_router = APIRouter(dependencies=[Depends(get_current_user)])


@analysis_router.get('/export/all')
def export_all(job_id: str | None = None, db: Session = Depends(get_db), date_from: date | None = None, date_to: date | None = None):
    from services.complete_export import complete_workbook
    return excel(complete_workbook(db,job_id,date_from=date_from,date_to=date_to),'nevetracker-visoes-completas.xlsx')


def excel(stream, filename):
    # Iterating BytesIO splits a binary ZIP at newline bytes and schedules
    # thousands of tiny thread-pool reads. Send bounded binary blocks instead.
    size=stream.getbuffer().nbytes
    return StreamingResponse(iter(lambda: stream.read(64*1024), b''),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Content-Length':str(size)})


def split_skus(value):
    return list(dict.fromkeys(filter(None, re.split(r'[\s,;]+', (value or '').upper()))))


@analysis_router.get('/options')
def options(db: Session = Depends(get_db)):
    return {'sources': [row[0] for row in db.query(OfferObservation.source).distinct().order_by(OfferObservation.source) if row[0]],
            'brands': [row[0] for row in db.query(OfferObservation.brand).distinct().order_by(OfferObservation.brand) if row[0]]}


@analysis_router.get('/history')
def history(start: date | None = None, end: date | None = None, skus: str = '',
            source: list[str] | None = Query(None), brand: list[str] | None = Query(None), carry: bool = True,
            page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), export: bool = False,
            db: Session = Depends(get_db), all_dates: bool = False, search: Annotated[str, Query(max_length=250)] = ''):
    if all_dates:
        begin_report_snapshot(db)
        from services.price_observations import verified_observations, _label
        from services.product_resolver import canonical_brand
        bounds = verified_observations(db.query(func.min(OfferObservation.observed_at), func.max(OfferObservation.observed_at)))
        if split_skus(skus): bounds = bounds.filter(OfferObservation.sku.in_(split_skus(skus)))
        if source: bounds = bounds.filter(OfferObservation.source.in_([_label(value) for value in source]))
        if brand: bounds = bounds.filter(OfferObservation.brand.in_([canonical_brand(value) for value in brand]))
        matches = report_search_predicate(search)
        if matches is not None:
            keys = bounds.with_entities(OfferObservation.offer_key).filter(matches).distinct().statement
            bounds = bounds.filter(OfferObservation.offer_key.in_(keys))
        first, last = bounds.one()
        start = local_day(first) if first else local_day(datetime.utcnow())
        end = local_day(last) if last else start
    end = end or local_day(datetime.utcnow()); start = start or end-timedelta(days=29)
    try:
        result = history_report(db, start, end, split_skus(skus), source, brand, carry, page, None if export else page_size, all_dates=all_dates, search=search)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return excel(history_workbook(result), 'historico-precos.xlsx') if export else result


@analysis_router.get('/comparison')
def comparison(dimension: str = 'source', left: str = '', right: str = '', skus: str = '',
               source: list[str] | None = Query(None), brand: list[str] | None = Query(None), export: bool = False,
               db: Session = Depends(get_db), start: date | None = None, end: date | None = None,
               search: Annotated[str, Query(max_length=250)] = ''):
    try:
        result = compare_groups(db, dimension, left, right, split_skus(skus), source, brand, start=start, end=end, search=search)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return excel(comparison_workbook(result), 'comparacao-lojas-marcas.xlsx') if export else result
