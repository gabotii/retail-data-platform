"""Response models of the KPI API (also documented in the OpenAPI schema at /docs)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class Period(BaseModel):
    start: date
    end: date


class SalesKpis(BaseModel):
    net_sales: float
    gross_sales: float
    discounts: float
    gross_margin: float
    gross_margin_pct: float | None
    units: float
    transactions: float
    avg_ticket: float | None
    units_per_ticket: float | None
    discount_rate: float | None
    promo_share: float | None


class SalesChange(BaseModel):
    net_sales: float | None
    transactions: float | None
    avg_ticket: float | None
    units: float | None
    gross_margin_pct_points: float | None


class SalesSummary(BaseModel):
    period: Period
    previous_period: Period
    current: SalesKpis
    previous: SalesKpis
    change: SalesChange


class StoreOption(BaseModel):
    store_code: str
    store_name: str
    format: str
    region: str


class FilterOptions(BaseModel):
    min_date: date | None
    max_date: date | None
    stores: list[StoreOption]
    categories: list[str]


class InventoryHealth(BaseModel):
    snapshot_date: date | None
    positions: int
    stockouts: int
    stockout_rate: float | None
    on_hand_units: float
    inventory_cost: float
    days_of_supply: float | None


class RunSummary(BaseModel):
    run_id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    trigger: str
    full_refresh: bool
    error: str | None
    batches_loaded: int
    batches_with_errors: int
    rows_loaded: int
    rows_rejected: int


class BatchSummary(BaseModel):
    batch_id: int
    source: str
    batch_name: str
    status: str
    rows_in: int
    rows_loaded: int
    rows_rejected: int
    reject_ratio: float | None
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    batches: list[BatchSummary]


class RunAccepted(BaseModel):
    status: str
    detail: str


class Health(BaseModel):
    status: str
    database: str
    last_run: RunSummary | None
