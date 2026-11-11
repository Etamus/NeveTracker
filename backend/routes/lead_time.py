from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from services.auth_service import require_operator, get_current_user
from services.lead_time_destinations import BY_UF
from services.lead_time_service import get_matrix, get_shipping_map, enqueue_quotes
from models.product import LeadTimeQuote
from services.delivery_destinations import destinations, save_destinations

lead_time_router = APIRouter(dependencies=[Depends(get_current_user)])


class QuoteRequest(BaseModel):
    states: list[str] = Field(default_factory=lambda: list(BY_UF), max_length=27)
    force_refresh: bool = False
    source: str | None = None
    sources: list[str] | None = Field(default=None, max_length=15)
    postal_codes: list[str] | None = Field(default=None, max_length=200)


class DestinationRequest(BaseModel):
    postal_codes: list[str] = Field(default_factory=list, max_length=200)


@lead_time_router.get('/destinations')
def list_destinations(db: Session = Depends(get_db)):
    chosen = destinations(db)
    return {'custom': any(dest.get('custom') for dest in chosen), 'destinations': chosen}


@lead_time_router.put('/destinations')
def configure_destinations(payload: DestinationRequest, actor=Depends(require_operator), db: Session = Depends(get_db)):
    try:
        return save_destinations(db, payload.postal_codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def normalized_sku(sku):
    from routes.products import _normalize_sku
    import re
    normalized = _normalize_sku(sku)
    if not normalized or len(normalized) > 80 or not re.fullmatch(r'[A-Z0-9._-]+', normalized):
        raise HTTPException(400, 'SKU inválido.')
    return normalized


@lead_time_router.get('/{sku}')
def matrix(sku: str, db: Session = Depends(get_db)):
    return get_matrix(db, normalized_sku(sku))


@lead_time_router.get('/{sku}/shipping')
def shipping_map(sku: str, source: list[str] | None = Query(default=None), db: Session = Depends(get_db)):
    return get_shipping_map(db, normalized_sku(sku), sources=source)


@lead_time_router.get('/{sku}/export')
def export_quotes(sku: str, region: str | None = None, db: Session = Depends(get_db)):
    from services.analysis_workbooks import lead_time_workbook
    from routes.analysis import excel
    normalized = normalized_sku(sku)
    return excel(lead_time_workbook(get_matrix(db, normalized), region), f'lead-time-{normalized}.xlsx')


@lead_time_router.post('/{sku}/refresh')
def refresh(sku: str, payload: QuoteRequest, actor=Depends(require_operator), db: Session = Depends(get_db)):
    states = list(dict.fromkeys(value.upper() for value in payload.states))
    if not states or any(value not in BY_UF for value in states):
        raise HTTPException(400, 'Selecione estados válidos.')
    try:
        return enqueue_quotes(db, normalized_sku(sku), states, payload.force_refresh, source=payload.source, sources=payload.sources,
                              postal_codes=payload.postal_codes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@lead_time_router.post('/{sku}/cancel')
def cancel(sku: str, actor=Depends(require_operator), db: Session = Depends(get_db)):
    count = db.query(LeadTimeQuote).filter(LeadTimeQuote.sku == normalized_sku(sku), LeadTimeQuote.status.in_(['queued', 'running'])).update({'status': 'cancelled'})
    db.commit()
    return {'cancelled': count}
