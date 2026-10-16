"""Demo "warehouse management system" API that exposes daily inventory snapshots.

It plays the role of a third-party source system, with the rough edges the ingestion
layer must cope with: bearer-token auth, pagination, randomly injected transient
failures (HTTP 503 / 429, controlled by ``DEMO_API_FAILURE_RATE``) and a small share of
invalid records (negative stock, empty SKUs).
"""

from __future__ import annotations

import random
from datetime import date
from typing import Annotated, Any

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from retail_platform.config import Settings, SimulationSettings, get_settings
from retail_platform.config import get_simulation_settings as _default_sim
from retail_platform.synthetic.simulation import World, simulate_cached

_RNG_API_DEFECTS = 98
_bearer = HTTPBearer(auto_error=False)


def create_app(
    settings: Settings | None = None,
    simulation: SimulationSettings | None = None,
    failure_rng: random.Random | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    sim = simulation or _default_sim()
    rng = failure_rng or random.Random()
    app = FastAPI(title="Demo WMS inventory API", version="1.0.0")

    def world() -> World:
        return simulate_cached(sim.seed, sim.start_date, sim.days, sim.traffic_scale)

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        expected = settings.demo_api_token.get_secret_value()
        if credentials is None or credentials.credentials != expected:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    @app.middleware("http")
    async def chaos(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/v1/") and rng.random() < settings.demo_api_failure_rate:
            if rng.random() < 0.5:
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            return JSONResponse(
                {"detail": "rate limited"}, status_code=429, headers={"Retry-After": "0"}
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/inventory/dates", dependencies=[Depends(authorize)])
    def available_dates() -> dict[str, date]:
        return {"min_date": sim.start_date, "max_date": sim.end_date}

    @app.get("/v1/inventory/snapshots", dependencies=[Depends(authorize)])
    def snapshots(
        snapshot_date: date,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=5000)] = 1000,
    ) -> dict[str, Any]:
        if not sim.start_date <= snapshot_date <= sim.end_date:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no snapshot for that date")
        day = _snapshot_with_defects(world(), snapshot_date, sim.seed, sim.dirty_rate)
        total = len(day)
        chunk = day.iloc[(page - 1) * page_size : page * page_size]
        return {
            "snapshot_date": snapshot_date,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
            "data": chunk.to_dict(orient="records"),
        }

    return app


def _snapshot_with_defects(world: World, day: date, seed: int, dirty_rate: float) -> pd.DataFrame:
    inv = world.inventory
    snap = inv[inv["snapshot_date"] == day].drop(columns="snapshot_date").reset_index(drop=True)
    snap = snap.astype({"on_hand": "int64", "on_order": "int64"}).astype(object)
    if dirty_rate > 0:
        rng = np.random.default_rng([seed, _RNG_API_DEFECTS, day.toordinal()])
        bad = np.flatnonzero(rng.random(len(snap)) < dirty_rate / 2)
        for row in bad:
            if rng.random() < 0.5:
                snap.loc[row, "on_hand"] = -int(rng.integers(1, 50))
            else:
                snap.loc[row, "sku"] = None
    snap.insert(0, "warehouse_ts", f"{day.isoformat()}T23:59:00-03:00")
    return snap
