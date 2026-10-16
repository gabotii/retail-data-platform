from __future__ import annotations

from datetime import timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import text

from retail_platform.demo_api.app import create_app
from retail_platform.errors import PipelineAlreadyRunningError
from retail_platform.ingestion.api_source import InventoryApiClient
from retail_platform.pipeline import runner as runner_module
from retail_platform.pipeline.runner import PIPELINE_LOCK_KEY, Pipeline
from retail_platform.synthetic.writer import sales_file_name
from retail_platform.warehouse.db import MigrationDriftError, migrate
from tests.helpers import app_transport
from tests.integration.conftest import count

FACT_SALES = "SELECT count(*) FROM dw.fact_sales"
FACT_INV = "SELECT count(*) FROM dw.fact_inventory_snapshot"


def test_migrations_are_idempotent_and_detect_drift(db):
    assert migrate(db) == []
    with db.begin() as conn:
        conn.execute(
            text("UPDATE public.schema_migrations SET checksum = 'x' WHERE version = '002'")
        )
    try:
        with pytest.raises(MigrationDriftError):
            migrate(db)
    finally:
        from retail_platform.warehouse.db import available_migrations

        real = {m.version: m.checksum for m in available_migrations()}
        with db.begin() as conn:
            conn.execute(
                text("UPDATE public.schema_migrations SET checksum = :c WHERE version = '002'"),
                {"c": real["002"]},
            )


def test_full_load_is_consistent(make_env, small_sim, small_world):
    env = make_env(small_sim)
    result = env.pipeline().run()

    assert result.status == "success", result
    sales = result.sources["sales"]
    assert sales.batches_loaded == small_sim.days
    assert sales.rows_rejected > 0, "dirty data should have been quarantined"
    assert count(env.engine, FACT_SALES) == sales.rows_loaded
    assert count(env.engine, "SELECT count(*) FROM dw.dim_store") == 8
    assert count(env.engine, "SELECT count(*) FROM dw.dim_product") == len(small_world.products)
    expected_inv = small_sim.days * 8 * len(small_world.products)
    inv = result.sources["inventory"]
    assert count(env.engine, FACT_INV) == expected_inv - inv.rows_rejected
    assert inv.rows_rejected > 0

    # every loaded clean line matches the simulated truth (no silent corruption)
    with env.engine.connect() as conn:
        loaded = pd.read_sql(
            "SELECT transaction_id, line_number, net_amount FROM dw.fact_sales", conn
        )
    truth = small_world.sales.assign(
        net=lambda d: d["quantity"] * d["unit_price"] - d["discount_amount"]
    )
    merged = loaded.merge(truth, on=["transaction_id", "line_number"], how="left")
    assert merged["net"].notna().all()
    assert (merged["net"] == merged["net_amount"].astype(float)).all()

    # marts agree with the fact table
    mart_total = count(env.engine, "SELECT round(sum(net_sales)) FROM mart.daily_store_sales")
    fact_total = count(env.engine, "SELECT round(sum(net_amount)) FROM dw.fact_sales")
    assert mart_total == fact_total > 0
    cat_total = count(env.engine, "SELECT round(sum(net_sales)) FROM mart.daily_category_sales")
    assert cat_total == fact_total

    # quality bookkeeping is complete
    rejected = count(env.engine, "SELECT count(*) FROM etl.rejected_record")
    batches_rejected = count(env.engine, "SELECT sum(rows_rejected) FROM etl.batch")
    assert rejected == batches_rejected == sales.rows_rejected + inv.rows_rejected
    assert count(env.engine, "SELECT count(*) FROM etl.quality_check") > 0
    assert count(env.engine, "SELECT count(*) FROM etl.pipeline_run WHERE status='success'") == 1


def test_second_run_is_a_no_op(make_env, small_sim):
    env = make_env(small_sim)
    env.pipeline().run()
    before = count(env.engine, FACT_SALES), count(env.engine, FACT_INV)
    batches_before = count(env.engine, "SELECT count(*) FROM etl.batch")

    result = env.pipeline().run()

    assert result.status == "success"
    assert result.sources["sales"].batches_skipped == small_sim.days
    assert result.sources["sales"].batches_loaded == 0
    assert result.sources["inventory"].batches_loaded == 0
    assert result.marts_refreshed_days == 0
    assert (count(env.engine, FACT_SALES), count(env.engine, FACT_INV)) == before
    assert count(env.engine, "SELECT count(*) FROM etl.batch") == batches_before


def test_incremental_run_loads_only_the_new_day(make_env, small_sim):
    env = make_env(small_sim)
    env.pipeline().run()
    sales_before = count(env.engine, FACT_SALES)

    env.write(small_sim.model_copy(update={"days": small_sim.days + 1}))
    result = env.pipeline().run()

    assert result.sources["sales"].batches_loaded == 1
    assert result.sources["sales"].batches_skipped == small_sim.days
    assert result.sources["inventory"].batches_loaded == 1
    assert result.marts_refreshed_days == 1
    new_day = small_sim.end_date + timedelta(days=1)
    new_rows = count(
        env.engine, "SELECT count(*) FROM dw.fact_sales WHERE sold_at::date = :d", d=new_day
    )
    assert count(env.engine, FACT_SALES) == sales_before + new_rows > sales_before
    with env.engine.connect() as conn:
        watermark = conn.execute(
            text("SELECT value FROM etl.watermark WHERE source='inventory'")
        ).scalar_one()
    assert watermark == new_day.isoformat()


def test_redelivered_file_replaces_its_rows(make_env, small_sim):
    env = make_env(small_sim)
    env.pipeline().run()
    path = env.settings.landing_dir / "sales" / sales_file_name(small_sim.start_date)
    original = pd.read_csv(path, dtype=str, keep_default_na=False)
    first_ticket = original["transaction_id"].iloc[0]
    corrected = original[original["transaction_id"] != first_ticket]
    corrected.to_csv(path, index=False, lineterminator="\n")
    day_key = int(small_sim.start_date.strftime("%Y%m%d"))
    mart_before = count(
        env.engine, "SELECT sum(lines) FROM mart.daily_store_sales WHERE date_key = :k", k=day_key
    )

    result = env.pipeline().run()

    assert result.sources["sales"].batches_loaded == 1
    remaining = count(
        env.engine, "SELECT count(*) FROM dw.fact_sales WHERE transaction_id = :t", t=first_ticket
    )
    assert remaining == 0
    mart_after = count(
        env.engine, "SELECT sum(lines) FROM mart.daily_store_sales WHERE date_key = :k", k=day_key
    )
    assert mart_after < mart_before
    owned = count(
        env.engine, "SELECT count(*) FROM dw.fact_sales WHERE source_file = :f", f=path.name
    )
    assert mart_after == owned


def test_quality_gate_rejects_bad_file_once(make_env, small_sim):
    env = make_env(small_sim)
    good = env.settings.landing_dir / "sales" / sales_file_name(small_sim.start_date)
    df = pd.read_csv(good, dtype=str, keep_default_na=False)
    df.loc[: len(df) // 2, "unit_price"] = "N/A"
    bad_day = small_sim.end_date + timedelta(days=5)
    bad = good.with_name(sales_file_name(bad_day))
    df.to_csv(bad, index=False, lineterminator="\n")

    result = env.pipeline().run()

    assert result.status == "partial"
    assert result.sources["sales"].batches_rejected == 1
    assert (
        count(env.engine, "SELECT count(*) FROM dw.fact_sales WHERE source_file = :f", f=bad.name)
        == 0
    )
    with env.engine.connect() as conn:
        status, error = conn.execute(
            text("SELECT status, error FROM etl.batch WHERE batch_name = :n"), {"n": bad.name}
        ).one()
    assert status == "rejected"
    assert "threshold" in error
    # the same broken file is not retried on the next run
    again = env.pipeline().run()
    assert again.sources["sales"].batches_rejected == 0
    assert again.status == "success"


def test_structurally_broken_file_is_rejected(make_env, small_sim):
    env = make_env(small_sim)
    good = env.settings.landing_dir / "sales" / sales_file_name(small_sim.start_date)
    df = pd.read_csv(good, dtype=str, keep_default_na=False).drop(columns=["sku"])
    df.to_csv(good, index=False, lineterminator="\n")

    result = env.pipeline().run()

    assert result.status == "partial"
    assert result.sources["sales"].batches_rejected == 1
    assert result.sources["sales"].batches_loaded == small_sim.days - 1


def test_inventory_outage_mid_feed_keeps_watermark_and_resumes(make_env, small_sim):
    env = make_env(small_sim)
    cutoff = small_sim.start_date + timedelta(days=2)

    def flaky_client() -> InventoryApiClient:
        inner = app_transport(create_app(env.settings, env.sim))

        def handler(request: httpx.Request) -> httpx.Response:
            day = request.url.params.get("snapshot_date")
            if day and day >= cutoff.isoformat():
                return httpx.Response(503)
            return inner.handle_request(request)

        return InventoryApiClient.from_settings(
            env.settings, transport=httpx.MockTransport(handler)
        )

    result = Pipeline(env.engine, env.settings, api_client_factory=flaky_client).run()

    assert result.status == "partial"
    assert result.sources["sales"].batches_loaded == small_sim.days
    inventory = result.sources["inventory"]
    assert inventory.batches_loaded == 2
    assert inventory.batches_failed == 1
    assert "503" in inventory.error
    watermark_sql = "SELECT value FROM etl.watermark WHERE source='inventory'"
    with env.engine.connect() as conn:
        assert conn.execute(text(watermark_sql)).scalar_one() == "2026-07-02"

    # once the API recovers, the next run resumes exactly where it stopped
    resumed = env.pipeline().run()
    assert resumed.status == "success"
    assert resumed.sources["inventory"].batches_loaded == small_sim.days - 2
    with env.engine.connect() as conn:
        assert conn.execute(text(watermark_sql)).scalar_one() == small_sim.end_date.isoformat()


def test_inventory_api_fully_down_does_not_block_other_sources(make_env, small_sim):
    env = make_env(small_sim)
    no_retry = env.settings.model_copy(update={"http_max_retries": 0})

    def dead_client() -> InventoryApiClient:
        return InventoryApiClient.from_settings(
            no_retry, transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )

    result = Pipeline(env.engine, env.settings, api_client_factory=dead_client).run()

    assert result.status == "partial"
    assert result.sources["sales"].batches_loaded == small_sim.days
    assert result.sources["inventory"].batches_loaded == 0
    assert "failed after 1 attempts" in result.sources["inventory"].error
    assert count(env.engine, FACT_INV) == 0


def test_concurrent_runs_are_prevented(make_env, small_sim):
    env = make_env(small_sim)
    with env.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": PIPELINE_LOCK_KEY})
        with pytest.raises(PipelineAlreadyRunningError):
            env.pipeline().run()
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PIPELINE_LOCK_KEY})
    assert env.pipeline().run().status == "success"


def test_unexpected_error_marks_run_and_batch_failed(make_env, small_sim, monkeypatch):
    env = make_env(small_sim)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(runner_module.loaders, "load_sales", boom)
    result = env.pipeline().run()

    assert result.status == "failed"
    assert "disk on fire" in result.error
    with env.engine.connect() as conn:
        run = conn.execute(text("SELECT status, error FROM etl.pipeline_run")).one()
        failed = conn.execute(
            text("SELECT count(*) FROM etl.batch WHERE status = 'failed'")
        ).scalar_one()
    assert run.status == "failed"
    assert failed == 1
    assert count(env.engine, FACT_SALES) == 0


def test_full_refresh_reprocesses_without_duplicates(make_env, small_sim):
    env = make_env(small_sim)
    env.pipeline().run()
    before = count(env.engine, FACT_SALES), count(env.engine, FACT_INV)
    result = env.pipeline().run(full_refresh=True)
    assert result.sources["sales"].batches_loaded == small_sim.days
    assert (count(env.engine, FACT_SALES), count(env.engine, FACT_INV)) == before


def test_scd2_product_versions(make_env, small_sim):
    long_sim = small_sim.model_copy(update={"days": 40, "traffic_scale": 0.02})
    env = make_env(long_sim.model_copy(update={"days": 25}))
    env.pipeline().run(sources=("master", "sales"))
    assert count(env.engine, "SELECT count(*) FROM dw.dim_product WHERE NOT is_current") == 0

    env.write(long_sim)  # price changes become effective on day 31
    env.pipeline().run(sources=("master", "sales"))

    with env.engine.connect() as conn:
        versions = pd.read_sql(
            "SELECT sku, valid_from, valid_to, is_current, unit_cost FROM dw.dim_product"
            " WHERE sku IN (SELECT sku FROM dw.dim_product WHERE NOT is_current)"
            " ORDER BY sku, valid_from",
            conn,
        )
        overlaps = conn.execute(
            text(
                "SELECT count(*) FROM dw.dim_product a JOIN dw.dim_product b"
                " ON a.sku = b.sku AND a.product_key < b.product_key"
                " AND a.valid_from <= b.valid_to AND b.valid_from <= a.valid_to"
            )
        ).scalar_one()
        cost_check = conn.execute(
            text(
                "SELECT count(*) FROM dw.fact_sales f JOIN dw.dim_product p USING (product_key)"
                " WHERE f.sold_at::date NOT BETWEEN p.valid_from AND p.valid_to"
            )
        ).scalar_one()
    assert not versions.empty, "seed should produce at least one price change"
    change_day = long_sim.start_date + timedelta(days=30)
    closed = versions[~versions["is_current"]]
    assert (closed["valid_to"] == change_day - timedelta(days=1)).all()
    current = versions[versions["is_current"]]
    assert (current["valid_from"] == change_day).all()
    assert overlaps == 0
    assert cost_check == 0
