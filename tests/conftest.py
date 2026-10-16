from __future__ import annotations

from datetime import date

import pytest

from retail_platform.config import Settings, SimulationSettings
from retail_platform.synthetic.simulation import World, simulate_cached

SMALL_SIM = SimulationSettings(
    seed=7, start_date=date(2026, 7, 1), days=6, traffic_scale=0.05, dirty_rate=0.02
)


@pytest.fixture(scope="session")
def small_sim() -> SimulationSettings:
    return SMALL_SIM


@pytest.fixture(scope="session")
def small_world(small_sim: SimulationSettings) -> World:
    return simulate_cached(
        small_sim.seed, small_sim.start_date, small_sim.days, small_sim.traffic_scale
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path / "data",
        demo_api_url="http://demo-api",
        demo_api_token="test-token",
        demo_api_failure_rate=0.0,
        http_backoff_seconds=0.0,
        api_page_size=200,
        log_format="console",
    )
