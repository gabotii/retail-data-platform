"""KPI and operations API.

Read endpoints expose the same KPIs as the dashboard; ``POST /api/v1/pipeline/runs``
triggers a pipeline run in the background and is protected by an API key.
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from retail_platform import __version__
from retail_platform.analytics import kpis
from retail_platform.api import schemas
from retail_platform.config import Settings, get_settings
from retail_platform.errors import PipelineAlreadyRunningError
from retail_platform.pipeline.runner import Pipeline
from retail_platform.warehouse.db import make_engine

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 366


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame -> JSON-safe records (NaN -> None, numpy scalars -> Python)."""
    clean = df.astype(object).where(df.notna(), None)
    return [
        {str(k): (v.item() if hasattr(v, "item") else v) for k, v in row.items()}
        for row in clean.to_dict(orient="records")
    ]


def _engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def kpi_filter(
    request: Request,
    start: Annotated[date | None, Query(description="Inicio (incl.); por defecto 30 días")] = None,
    end: Annotated[date | None, Query(description="Fin (incl.)")] = None,
    store: Annotated[list[str] | None, Query(description="Código de tienda, repetible")] = None,
    category: Annotated[list[str] | None, Query(description="Categoría, repetible")] = None,
) -> kpis.KpiFilter:
    default = kpis.default_filter(_engine(request))
    if default is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no data loaded yet")
    span = default.end - default.start
    end = end or (default.end if start is None else start + span)
    start = start or end - span
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "start must be <= end")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"range limited to {MAX_RANGE_DAYS} days"
        )
    return kpis.KpiFilter(start, end, tuple(store or ()), tuple(category or ()))


Filter = Annotated[kpis.KpiFilter, Depends(kpi_filter)]


def require_api_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.api_key.get_secret_value() if settings.api_key else ""
    if not expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "pipeline trigger disabled (no API_KEY)")
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    pipeline_factory: Callable[[], Pipeline] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    make_pipeline = pipeline_factory or (lambda: Pipeline(engine, settings))
    run_guard = threading.Lock()

    app = FastAPI(
        title="Retail Data Platform API",
        version=__version__,
        description="KPIs de ventas e inventario y operación del pipeline ETL.",
    )
    app.state.engine = engine
    app.state.settings = settings

    @app.exception_handler(OperationalError)
    async def database_unavailable(_: Request, exc: OperationalError) -> JSONResponse:
        logger.error("database unavailable", extra={"error": str(exc.orig)})
        return JSONResponse({"detail": "database unavailable"}, status_code=503)

    # ------------------------------------------------------------------ health
    @app.get("/health", response_model=schemas.Health, tags=["ops"])
    def health() -> dict[str, Any]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError:
            return JSONResponse(  # type: ignore[return-value]
                {"status": "degraded", "database": "unavailable", "last_run": None},
                status_code=503,
            )
        runs = _records(kpis.recent_runs(engine, limit=1))
        return {"status": "ok", "database": "ok", "last_run": runs[0] if runs else None}

    # ------------------------------------------------------------------ KPIs
    router = APIRouter(prefix="/api/v1")

    @router.get("/filters", response_model=schemas.FilterOptions, tags=["kpis"])
    def filters() -> dict[str, Any]:
        return kpis.filter_options(engine)

    @router.get("/kpis/summary", response_model=schemas.SalesSummary, tags=["kpis"])
    def summary(flt: Filter) -> dict[str, Any]:
        return kpis.sales_summary(engine, flt)

    @router.get("/kpis/daily-sales", tags=["kpis"])
    def daily(flt: Filter) -> list[dict[str, Any]]:
        return _records(kpis.daily_sales(engine, flt))

    @router.get("/kpis/by-category", tags=["kpis"])
    def by_category(flt: Filter) -> list[dict[str, Any]]:
        return _records(kpis.sales_by_category(engine, flt))

    @router.get("/kpis/by-store", tags=["kpis"])
    def by_store(flt: Filter) -> list[dict[str, Any]]:
        return _records(kpis.sales_by_store(engine, flt))

    @router.get("/kpis/top-products", tags=["kpis"])
    def top(
        flt: Filter,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        metric: kpis.TopMetric = "net_sales",
    ) -> list[dict[str, Any]]:
        return _records(kpis.top_products(engine, flt, limit, metric))

    @router.get("/kpis/payment-mix", tags=["kpis"])
    def payments(flt: Filter) -> list[dict[str, Any]]:
        return _records(kpis.payment_mix(engine, flt))

    @router.get("/kpis/inventory-health", response_model=schemas.InventoryHealth, tags=["kpis"])
    def inventory(flt: Filter) -> dict[str, Any]:
        return kpis.inventory_health(engine, flt)

    @router.get("/kpis/inventory-at-risk", tags=["kpis"])
    def at_risk(
        flt: Filter, limit: Annotated[int, Query(ge=1, le=500)] = 20
    ) -> list[dict[str, Any]]:
        return _records(kpis.inventory_at_risk(engine, flt, limit))

    # ------------------------------------------------------------------ pipeline ops
    @router.get("/pipeline/runs", response_model=list[schemas.RunSummary], tags=["pipeline"])
    def runs(limit: Annotated[int, Query(ge=1, le=200)] = 20) -> list[dict[str, Any]]:
        return _records(kpis.recent_runs(engine, limit))

    @router.get("/pipeline/runs/{run_id}", response_model=schemas.RunDetail, tags=["pipeline"])
    def run_detail(run_id: int) -> dict[str, Any]:
        match = kpis.recent_runs(engine, limit=1, run_id=run_id)
        if match.empty:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return {**_records(match)[0], "batches": _records(kpis.run_batches(engine, run_id))}

    @router.get("/quality/rejections", tags=["quality"])
    def rejections(run_id: int | None = None) -> list[dict[str, Any]]:
        return _records(kpis.rejections_by_rule(engine, run_id))

    @router.get("/quality/rejections/samples", tags=["quality"])
    def rejection_samples(
        rule: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 20
    ) -> list[dict[str, Any]]:
        return _records(kpis.rejected_samples(engine, rule, limit))

    @router.post(
        "/pipeline/runs",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=schemas.RunAccepted,
        dependencies=[Depends(require_api_key)],
        tags=["pipeline"],
    )
    def trigger_run(full_refresh: bool = False) -> dict[str, str]:
        if not run_guard.acquire(blocking=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "a run is already in progress")

        def work() -> None:
            try:
                make_pipeline().run(full_refresh=full_refresh, trigger="api")
            except PipelineAlreadyRunningError:
                logger.warning("API-triggered run skipped: another run holds the lock")
            except Exception:
                logger.exception("API-triggered run crashed")
            finally:
                run_guard.release()

        threading.Thread(target=work, name="pipeline-run", daemon=True).start()
        return {"status": "accepted", "detail": "pipeline run started; see /api/v1/pipeline/runs"}

    app.include_router(router)
    return app
