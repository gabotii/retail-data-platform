"""KPI module and FastAPI service against a warehouse loaded once per module."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from retail_platform.analytics import kpis
from retail_platform.api.app import create_app
from retail_platform.config import Settings
from tests.integration.conftest import Env, count


@pytest.fixture(scope="module")
def loaded(engine, tmp_path_factory, small_sim):
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables"
                " WHERE schemaname IN ('etl', 'dw', 'mart')"
            )
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    settings = Settings(
        app_env="test",
        data_dir=tmp_path_factory.mktemp("api") / "data",
        demo_api_token="test-token",
        http_backoff_seconds=0,
        api_key="secret-key",
        log_format="console",
    )
    env = Env(engine, settings, small_sim)
    assert env.pipeline().run().status == "success"
    return env


@pytest.fixture(scope="module")
def client(loaded):
    return TestClient(create_app(loaded.settings, loaded.engine, loaded.pipeline))


@pytest.fixture(scope="module")
def full_filter(loaded, small_sim):
    return kpis.KpiFilter(small_sim.start_date, small_sim.end_date)


# ------------------------------------------------------------------------ KPI module
def test_summary_matches_raw_sql(loaded, full_filter):
    summary = kpis.sales_summary(loaded.engine, full_filter)["current"]
    net = count(loaded.engine, "SELECT round(sum(net_amount)) FROM dw.fact_sales")
    tickets = count(loaded.engine, "SELECT count(DISTINCT transaction_id) FROM dw.fact_sales")
    assert round(summary["net_sales"]) == net
    assert summary["transactions"] == tickets
    assert summary["avg_ticket"] == pytest.approx(summary["net_sales"] / tickets)
    assert 0 < summary["gross_margin_pct"] < 1


def test_previous_period_is_adjacent_and_empty_before_data(loaded, full_filter):
    previous = full_filter.previous_period()
    assert previous.end == full_filter.start - timedelta(days=1)
    assert (previous.end - previous.start) == (full_filter.end - full_filter.start)
    result = kpis.sales_summary(loaded.engine, full_filter)
    assert result["previous"]["net_sales"] == 0
    assert result["change"]["net_sales"] is None


def test_breakdowns_add_up(loaded, full_filter):
    total = kpis.sales_summary(loaded.engine, full_filter)["current"]["net_sales"]
    assert kpis.daily_sales(loaded.engine, full_filter)["net_sales"].sum() == pytest.approx(total)
    by_cat = kpis.sales_by_category(loaded.engine, full_filter)
    assert by_cat["net_sales"].sum() == pytest.approx(total)
    assert by_cat["share"].sum() == pytest.approx(1)
    assert kpis.sales_by_store(loaded.engine, full_filter)["net_sales"].sum() == pytest.approx(
        total
    )
    assert kpis.payment_mix(loaded.engine, full_filter)["share"].sum() == pytest.approx(1)
    assert kpis.sales_heatmap(loaded.engine, full_filter)["net_sales"].sum() == pytest.approx(total)


def test_filters_restrict_results(loaded, full_filter):
    flt = kpis.KpiFilter(full_filter.start, full_filter.end, ("ST001",), ("Lácteos",))
    stores = kpis.sales_by_store(loaded.engine, flt)
    assert stores["store_code"].tolist() == ["ST001"]
    cats = kpis.sales_by_category(loaded.engine, flt)
    assert cats["category"].tolist() == ["Lácteos"]
    summary = kpis.sales_summary(loaded.engine, flt)["current"]["net_sales"]
    assert summary == pytest.approx(cats["net_sales"].sum())
    top = kpis.top_products(loaded.engine, flt, limit=50)
    assert set(top["category"]) == {"Lácteos"}


def test_inventory_kpis(loaded, full_filter, small_world):
    health = kpis.inventory_health(loaded.engine, full_filter)
    assert health["snapshot_date"] == full_filter.end
    assert health["positions"] <= 8 * len(small_world.products)
    assert health["stockouts"] <= health["positions"]
    risk = kpis.inventory_at_risk(loaded.engine, full_filter, limit=500)
    assert (risk["on_hand_qty"] < 3 * risk["avg_daily_units"] + 0.01).all()
    trend = kpis.stockout_trend(loaded.engine, full_filter)
    assert len(trend) == (full_filter.end - full_filter.start).days + 1


# ------------------------------------------------------------------------ HTTP API
def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["last_run"]["status"] == "success"


def test_filters_endpoint(client, small_sim):
    body = client.get("/api/v1/filters").json()
    assert body["min_date"] == small_sim.start_date.isoformat()
    assert len(body["stores"]) == 8
    assert "Lácteos" in body["categories"]


def test_summary_endpoint_defaults_and_filters(client, small_sim):
    default = client.get("/api/v1/kpis/summary").json()
    assert default["period"]["end"] == small_sim.end_date.isoformat()
    filtered = client.get(
        "/api/v1/kpis/summary",
        params={"start": "2026-07-01", "end": "2026-07-03", "store": ["ST001", "ST002"]},
    ).json()
    assert filtered["period"] == {"start": "2026-07-01", "end": "2026-07-03"}
    assert 0 < filtered["current"]["net_sales"] < default["current"]["net_sales"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/kpis/daily-sales",
        "/api/v1/kpis/by-category",
        "/api/v1/kpis/by-store",
        "/api/v1/kpis/top-products?limit=5&metric=units",
        "/api/v1/kpis/payment-mix",
        "/api/v1/kpis/inventory-health",
        "/api/v1/kpis/inventory-at-risk",
        "/api/v1/quality/rejections",
        "/api/v1/quality/rejections/samples?limit=3",
        "/api/v1/pipeline/runs",
    ],
)
def test_read_endpoints(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()


def test_invalid_ranges_are_422(client):
    assert client.get("/api/v1/kpis/summary?start=2026-07-05&end=2026-07-01").status_code == 422
    assert client.get("/api/v1/kpis/summary?start=2020-01-01&end=2026-07-01").status_code == 422
    assert client.get("/api/v1/kpis/top-products?metric=bogus").status_code == 422


def test_run_detail(client):
    runs = client.get("/api/v1/pipeline/runs").json()
    detail = client.get(f"/api/v1/pipeline/runs/{runs[0]['run_id']}").json()
    assert detail["batches"]
    assert {b["source"] for b in detail["batches"]} >= {"sales", "inventory"}
    assert client.get("/api/v1/pipeline/runs/999999").status_code == 404


def test_trigger_requires_valid_key(client):
    assert client.post("/api/v1/pipeline/runs").status_code == 401
    bad = client.post("/api/v1/pipeline/runs", headers={"X-API-Key": "nope"})
    assert bad.status_code == 401


def test_trigger_disabled_without_configured_key(loaded):
    no_key = loaded.settings.model_copy(update={"api_key": None})
    response = TestClient(create_app(no_key, loaded.engine)).post(
        "/api/v1/pipeline/runs", headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 403


def test_trigger_runs_in_background_and_rejects_overlap(loaded):
    started, release = threading.Event(), threading.Event()

    class SlowPipeline:
        def run(self, **kwargs):
            started.set()
            release.wait(5)

    app = create_app(loaded.settings, loaded.engine, pipeline_factory=SlowPipeline)
    api = TestClient(app)
    headers = {"X-API-Key": "secret-key"}
    first = api.post("/api/v1/pipeline/runs", headers=headers)
    assert first.status_code == 202
    assert started.wait(5)
    assert api.post("/api/v1/pipeline/runs", headers=headers).status_code == 409
    release.set()


def test_database_outage_returns_503(loaded):
    from retail_platform.warehouse.db import make_engine

    dead = make_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    api = TestClient(create_app(loaded.settings, dead))
    assert api.get("/health").status_code == 503
    assert api.get("/api/v1/filters").status_code == 503
