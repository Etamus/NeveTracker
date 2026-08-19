from pydantic import BaseModel, Field, HttpUrl, field_validator
from datetime import datetime
from typing import Literal, Optional, List

class PriceHistoryCreate(BaseModel):
    price: float
    store: str
    url: str
    source: Optional[str] = None
    cash_price: Optional[float] = None
    installment_price: Optional[float] = None
    installment_count: Optional[int] = None
    installment_value: Optional[float] = None
    shipping_cost: Optional[float] = None
    delivery_min_days: Optional[int] = None
    delivery_max_days: Optional[int] = None
    destination_zip: Optional[str] = None

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
    refresh_if_stale: bool = False
    postal_code: Optional[str] = None
    scope: Literal["all", "sites", "retail"] = "all"


class ExtractionScheduleRequest(BaseModel):
    enabled: bool = False
    start_time: str = "09:00"
    end_time: str = "18:00"
    days: List[int] = Field(default_factory=lambda: [1])
    repeat_every_hours: int = Field(default=0, ge=0, le=12)
    force_refresh: bool = True
    postal_code: Optional[str] = None

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time(cls, value):
        try:
            datetime.strptime(value, "%H:%M")
        except (TypeError, ValueError) as exc:
            raise ValueError("Horario invalido. Use HH:MM.") from exc
        return value

    @field_validator("days")
    @classmethod
    def validate_days(cls, value):
        days = sorted(set(value))
        if not days or any(day < 0 or day > 6 for day in days):
            raise ValueError("Selecione ao menos um dia valido.")
        return days


class AnalyticsOfferExport(BaseModel):
    store: str
    source: Optional[str] = None
    current_price: float
    url: str
    last_update: Optional[datetime] = None
    discovery_source: Optional[str] = None
    cash_price: Optional[float] = None
    installment_price: Optional[float] = None
    installment_count: Optional[int] = None
    installment_value: Optional[float] = None
    payment_difference: Optional[float] = None
    shipping_cost: Optional[float] = None
    shipping_method: Optional[str] = None
    delivery_min_days: Optional[int] = None
    delivery_max_days: Optional[int] = None
    destination_zip: Optional[str] = None
    availability: Optional[str] = None
    logistics_source: Optional[str] = None
    is_cached: bool = False


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
