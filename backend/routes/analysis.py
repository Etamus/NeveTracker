from datetime import date, datetime, timedelta
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from models.product import OfferObservation
from services.price_observations import history_report, compare_groups, local_day
from services.analysis_workbooks import history_workbook, comparison_workbook

from services.auth_service import get_current_user
analysis_router = APIRouter(dependencies=[Depends(get_current_user)])


def excel(stream, filename):
    return StreamingResponse(stream, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                             headers={'Content-Disposition': f'attachment; filename="{filename}"'})


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
            db: Session = Depends(get_db)):
    end = end or local_day(datetime.utcnow()); start = start or end-timedelta(days=29)
    try:
        result = history_report(db, start, end, split_skus(skus), source, brand, carry, page, None if export else page_size)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return excel(history_workbook(result), 'historico-precos.xlsx') if export else result


@analysis_router.get('/comparison')
def comparison(dimension: str = 'source', left: str = '', right: str = '', skus: str = '',
               source: list[str] | None = Query(None), brand: list[str] | None = Query(None), export: bool = False,
               db: Session = Depends(get_db)):
    try:
        result = compare_groups(db, dimension, left, right, split_skus(skus), source, brand)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return excel(comparison_workbook(result), 'comparacao-lojas-marcas.xlsx') if export else result
