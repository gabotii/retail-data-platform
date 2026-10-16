from datetime import date

import pandas as pd
import pytest

from retail_platform.calendar_cl import easter_sunday, holiday_name, is_holiday
from retail_platform.synthetic.catalog import build_products, chilean_price
from retail_platform.synthetic.simulation import simulate
from retail_platform.synthetic.writer import (
    DEFECT_KINDS,
    SALES_COLUMNS,
    inject_noise,
    sales_file_name,
    write_landing,
)


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_easter_sunday(year, expected):
    assert easter_sunday(year) == expected


def test_holidays():
    assert holiday_name(date(2026, 9, 18)) == "Independencia Nacional"
    assert holiday_name(date(2026, 4, 3)) == "Viernes Santo"
    assert not is_holiday(date(2026, 9, 17))


def test_chilean_price_endings():
    assert chilean_price(1234) == 1190
    assert chilean_price(1260) == 1290
    assert chilean_price(10) == 90


def test_products_are_deterministic_and_unique():
    a, b = build_products(1), build_products(1)
    pd.testing.assert_frame_equal(a, b)
    assert a["sku"].is_unique
    assert (a["unit_cost"] < a["list_price"]).all()
    assert not build_products(2).equals(a)


def test_world_shape_and_invariants(small_world):
    sales = small_world.sales
    assert not sales.empty
    assert not sales.duplicated(["transaction_id", "line_number"]).any()
    assert (sales["quantity"] > 0).all()
    assert (sales["discount_amount"] <= sales["quantity"] * sales["unit_price"]).all()
    assert set(sales["sold_at"].dt.hour) <= set(range(9, 22))
    inv = small_world.inventory
    assert len(inv) == small_world.days * 8 * len(small_world.products)
    assert (inv["on_hand"] >= 0).all()
    assert (inv["on_order"] >= 0).all()


def test_longer_horizon_reproduces_the_prefix_exactly():
    short = simulate(3, date(2026, 7, 1), 4, 0.05)
    longer = simulate(3, date(2026, 7, 1), 5, 0.05)
    prefix = longer.sales[longer.sales["sold_at"] < pd.Timestamp("2026-07-05")]
    pd.testing.assert_frame_equal(short.sales, prefix.reset_index(drop=True))
    pd.testing.assert_frame_equal(short.inventory, longer.inventory.iloc[: len(short.inventory)])


def test_price_changes_create_history_and_master_reflects_as_of():
    world = simulate(11, date(2026, 1, 1), 95, 0.01)
    changes = world.price_history[world.price_history["effective_from"] > world.start_date]
    assert not changes.empty
    sku = changes.iloc[0]["sku"]
    before = world.products_as_of(date(2026, 1, 15)).set_index("sku").loc[sku]
    after = world.products_as_of(world.end_date).set_index("sku").loc[sku]
    assert after["list_price"] > before["list_price"]
    assert after["updated_at"] > before["updated_at"]


def test_inject_noise_counts_and_types(small_world):
    day = small_world.start_date
    day_sales = small_world.sales[small_world.sales["sold_at"].dt.date == day]
    noisy, counts = inject_noise(day_sales, seed=1, day=day, dirty_rate=0.2)
    assert list(noisy.columns) == SALES_COLUMNS
    assert set(counts) == set(DEFECT_KINDS)
    assert len(noisy) == len(day_sales) + counts["duplicate"]
    assert (noisy["unit_price"] == "N/A").sum() == counts["bad_price"]
    assert (noisy["sku"] == "SKU-999999").sum() == counts["unknown_sku"]
    assert noisy.map(lambda v: isinstance(v, str)).all().all()


def test_inject_noise_disabled_returns_clean_strings(small_world):
    day = small_world.start_date
    day_sales = small_world.sales[small_world.sales["sold_at"].dt.date == day]
    clean, counts = inject_noise(day_sales, seed=1, day=day, dirty_rate=0)
    assert sum(counts.values()) == 0
    assert clean["sold_at"].iloc[0].startswith("2026-07-01 ")


def test_write_landing_is_idempotent(tmp_path, small_world, small_sim):
    first = write_landing(small_world, tmp_path, small_sim.seed, small_sim.dirty_rate)
    assert first.sales_files_written == small_world.days
    assert (tmp_path / "master" / "products.csv").exists()
    assert (tmp_path / "sales" / sales_file_name(small_world.start_date)).exists()
    assert not list(tmp_path.rglob("*.tmp"))
    second = write_landing(small_world, tmp_path, small_sim.seed, small_sim.dirty_rate)
    assert second.sales_files_written == 0
    assert second.sales_files_unchanged == small_world.days
