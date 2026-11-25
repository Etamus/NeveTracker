from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
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
    delivery_days_type: Optional[str] = None
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
    mode: Literal["quick", "precise"] = "quick"
    local_only: bool = False
    api_with_scraping: Optional[bool] = None
    postal_codes: List[str] = Field(default_factory=list, max_length=200)
    stores: List[str] = Field(default_factory=list, max_length=15)
    sellers: dict[str, List[str]] = Field(default_factory=dict, max_length=15)

    @field_validator('sellers')
    @classmethod
    def validate_sellers(cls, values):
        from services.store_registry import TARGET_STORES
        from services.seller_selection import seller_key
        known = {store['key'] for store in TARGET_STORES}
        result = {}
        for store, names in values.items():
            if store not in known:
                raise ValueError('Loja de lojista inválida.')
            if len(names) > 200:
                raise ValueError('Selecione até 200 lojistas por loja.')
            unique = {}
            for name in names:
                name = name.strip()
                if not name or len(name) > 160 or any(ord(char) < 32 for char in name):
                    raise ValueError('Informe nomes de lojistas entre 1 e 160 caracteres.')
                unique.setdefault(seller_key(store, name), name)
            if unique: result[store] = list(unique.values())
        return result

    @field_validator('stores')
    @classmethod
    def validate_stores(cls, values):
        from services.store_registry import TARGET_STORES
        known={store['key'] for store in TARGET_STORES}
        if any(value not in known for value in values):raise ValueError('Loja de extração inválida.')
        return list(dict.fromkeys(values))

    @field_validator('postal_codes')
    @classmethod
    def validate_postal_codes(cls, values):
        import re
        normalized=[]
        for value in values:
            if not re.fullmatch(r'\d{5}-?\d{3}', value.strip()):
                raise ValueError('Informe CEPs com oito dígitos.')
            value=value.strip().replace('-','')
            if value not in normalized:normalized.append(value)
        return normalized


class ExtractionDatabaseJobRequest(BaseModel):
    force_refresh: bool = True
    refresh_if_stale: bool = False
    postal_code: Optional[str] = None
    scope: Literal["all", "sites", "retail"] = "all"
    mode: Literal["quick", "precise"] = "quick"


class CrawlerControlRequest(BaseModel):
    default_mode: Literal["quick", "precise"] = "quick"
    external_discovery_fallback: bool = True
    playwright_product_fallback: bool = True
    playwright_shipping_fallback: bool = True
    api_with_scraping: bool = True
    concurrent_requests_per_domain: int = Field(default=2, ge=1, le=4)
    request_timeout_seconds: int = Field(default=15, ge=5, le=45)
    precise_sku_workers: int = Field(default=8, ge=1, le=8)
    quick_sku_workers: int = Field(default=4, ge=1, le=8)
    quote_workers: int = Field(default=12, ge=2, le=32)
    quick_timeout_seconds: int = Field(default=5, ge=3, le=10)
    discovery_stores_per_round: int = Field(default=3, ge=1, le=6)
    adaptive_parallelism: bool = True
    hardware_acceleration: bool = False
    reuse_public_browser: bool = True
    browser_failure_cooldown_seconds: int = Field(default=30, ge=15, le=300)


class ExtractionScheduleRequest(BaseModel):
    enabled: bool = False
    start_time: str = "09:00"
    end_time: str = "18:00"
    days: List[int] = Field(default_factory=lambda: [1])
    repeat_every_hours: int = Field(default=0, ge=0, le=12)
    force_refresh: bool = True
    only_stale: bool = False
    postal_code: Optional[str] = None

    @model_validator(mode='after')
    def ordered_window(self):
        if datetime.strptime(self.end_time, '%H:%M') < datetime.strptime(self.start_time, '%H:%M'):
            raise ValueError('O horário final deve ser igual ou posterior ao inicial.')
        return self

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
    current_price: Optional[float] = None
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
    delivery_days_type: Optional[str] = None
    destination_zip: Optional[str] = None
    availability: Optional[str] = None
    fulfillment_type: Optional[str] = None
    fulfilled_by: Optional[str] = None
    fulfillment_evidence: Optional[str] = None
    logistics_source: Optional[str] = None
    is_cached: bool = False


class AnalyticsRowExport(BaseModel):
    highest_price: Optional[float] = None
    reference_price: Optional[float] = None
    reference_difference: Optional[float] = None
    internal_code: Optional[str] = None
    external_id: Optional[str] = None
    sku: Optional[str] = None
    name: str
    description: Optional[str] = None
    subfamily: Optional[str] = None
    sku_category: Optional[str] = None
    cluster: Optional[str] = None
    market_type: Optional[str] = None
    store_count: int
    offer_count: int
    pma: Optional[float] = None
    cheapest_price: Optional[float] = None
    cheapest_store: str
    difference_value: Optional[float] = None
    difference_percent: Optional[float] = None
    offers: List[AnalyticsOfferExport]


class AnalyticsExportRequest(BaseModel):
    query: Optional[str] = None
    rows: List[AnalyticsRowExport]
