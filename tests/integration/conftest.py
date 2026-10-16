from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import Engine, text

from retail_platform.config import Settings, SimulationSettings
from retail_platform.demo_api.app import create_app
from retail_platform.ingestion.api_source import InventoryApiClient
from retail_platform.pipeline.runner import Pipeline
from retail_platform.synthetic.simulation import simulate_cached
from retail_platform.synthetic.writer import write_landing
from retail_platform.warehouse.db import make_engine, migrate
from tests.helpers import app_transport

ROOT = Path(__file__).resolve().parents[2]


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_database_url()
    if not url:
        pytest.skip("TEST_DATABASE_URL not configured")
    eng = make_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS mart, dw, etl CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS public.schema_migrations"))
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Engine:
    """A migrated database with every table emptied before the test."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables"
                " WHERE schemaname IN ('etl', 'dw', 'mart')"
            )
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    return engine


def count(engine: Engine, sql: str, **params: object) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one() or 0)


class Env:
    """A landing zone + demo API + pipeline wired together for one test."""

    def __init__(self, engine: Engine, settings: Settings, sim: SimulationSettings) -> None:
        self.engine = engine
        self.settings = settings
        self.sim = sim
        self.api_failure_rate = 0.0
        self.write(sim)

    def write(self, sim: SimulationSettings) -> None:
        self.sim = sim
        world = simulate_cached(sim.seed, sim.start_date, sim.days, sim.traffic_scale)
        write_landing(world, self.settings.landing_dir, sim.seed, sim.dirty_rate)

    def client(self) -> InventoryApiClient:
        api_settings = self.settings.model_copy(
            update={"demo_api_failure_rate": self.api_failure_rate}
        )
        transport = app_transport(create_app(api_settings, self.sim))
        return InventoryApiClient.from_settings(self.settings, transport=transport)

    def pipeline(self) -> Pipeline:
        return Pipeline(self.engine, self.settings, api_client_factory=self.client)


@pytest.fixture
def make_env(db: Engine, settings: Settings) -> Callable[[SimulationSettings], Env]:
    def factory(sim: SimulationSettings) -> Env:
        return Env(db, settings.model_copy(update={"http_max_retries": 1}), sim)

    return factory
