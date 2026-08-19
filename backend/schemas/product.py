from pydantic import BaseModel, HttpUrl
from datetime import datetime
from typing import Optional, List

class PriceHistoryCreate(BaseModel):
    price: float
    store: str
    url: str
    source: Optional[str] = None

class ProductCreate(BaseModel):
    name: str
    brand: str
    description: Optional[str] = None
    category: str
    image_url: Optional[str] = None
    sku: Optional[str] = None

    initial_price: PriceHistoryCreate


class SkuBatchRequest(BaseModel):
    skus: List[str]
    force_refresh: bool = True


class AnalyticsOfferExport(BaseModel):
    store: str
    source: Optional[str] = None
    current_price: float
    url: str
    last_update: Optional[datetime] = None


class AnalyticsRowExport(BaseModel):
    sku: Optional[str] = None
    name: str
    store_count: int
    offer_count: int
    pma: float
    cheapest_price: float
    cheapest_store: str
    difference_value: float = 0
    difference_percent: float = 0
    offers: List[AnalyticsOfferExport]


class AnalyticsExportRequest(BaseModel):
    query: Optional[str] = None
    rows: List[AnalyticsRowExport]
