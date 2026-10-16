from datetime import date

import pandas as pd
import pytest

from retail_platform.errors import QualityGateError
from retail_platform.pipeline.stages import (
    prepare_inventory,
    prepare_products,
    prepare_sales,
    prepare_stores,
)
from retail_platform.quality.report import BatchReport
from retail_platform.synthetic.writer import inject_noise
from retail_platform.transform.cleaning import clean_sales, normalize_payment_method
from retail_platform.transform.enrich import build_dim_date, date_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Débito", "DEBIT"),
        (" DEBITO ", "DEBIT"),
        ("Credito ", "CREDIT"),
        ("billetera   Digital", "DIGITAL_WALLET"),
        ("Efectivo", "CASH"),
        (" cheque ", "cheque"),
        (None, None),
    ],
)
def test_normalize_payment_method(raw, expected):
    assert normalize_payment_method(raw) == expected


def test_clean_sales_standardises_codes():
    raw = pd.DataFrame(
        {"store_code": [" st001"], "sku": ["sku-000001 "], "payment_method": ["débito"]}
    )
    out = clean_sales(raw)
    assert out.iloc[0].tolist() == ["ST001", "SKU-000001", "DEBIT"]


def test_date_key():
    assert date_key(pd.Series(["2026-07-01", "2026-12-31"])).tolist() == [20260701, 20261231]


def test_dim_date():
    dim = build_dim_date(date(2026, 9, 17), date(2026, 9, 20))
    assert dim["date_key"].tolist() == [20260917, 20260918, 20260919, 20260920]
    assert dim["is_holiday"].tolist() == [False, True, True, False]
    assert dim.loc[3, "day_name"] == "domingo"
    assert dim.loc[0, "month_name"] == "septiembre"
    assert bool(dim.loc[3, "is_weekend"])


def _known(world):
    return set(world.stores["store_code"]), set(world.products["sku"])


def test_prepare_sales_on_noisy_day(small_world):
    day = small_world.start_date
    clean_day = small_world.sales[small_world.sales["sold_at"].dt.date == day]
    raw, defects = inject_noise(clean_day, seed=5, day=day, dirty_rate=0.03)
    stores, skus = _known(small_world)
    rep = BatchReport("sales", "day1")
    out = prepare_sales(raw, rep, known_stores=stores, known_skus=skus, max_reject_ratio=0.2)

    rejected = rep.rejected_by_rule()
    assert rejected.get("range:quantity", 0) == defects["negative_quantity"]
    assert rejected.get("type:unit_price", 0) == defects["bad_price"]
    assert rejected.get("unknown_sku", 0) == defects["unknown_sku"]
    assert rejected.get("type:sold_at", 0) == defects["bad_timestamp"]
    # every row is either loaded, rejected or an exact duplicate
    exact = next(c.failed_rows for c in rep.checks if c.check == "exact_duplicates")
    assert len(out) + rep.rejected_count + exact == len(raw)
    # cosmetic noise is normalised, never rejected
    assert set(out["payment_method"]) <= {"DEBIT", "CREDIT", "CASH", "DIGITAL_WALLET"}
    assert out["store_code"].str.fullmatch(r"ST\d{3}").all()
    # money is preserved for untouched rows
    assert (out["net_amount"] == out["gross_amount"] - out["discount_amount"]).all()
    assert out["date_key"].unique().tolist() == [20260701]


def test_prepare_sales_gate_blocks_garbage(small_world):
    day = small_world.start_date
    clean_day = small_world.sales[small_world.sales["sold_at"].dt.date == day]
    raw, _ = inject_noise(clean_day, seed=5, day=day, dirty_rate=0.4)
    stores, skus = _known(small_world)
    with pytest.raises(QualityGateError):
        prepare_sales(
            raw,
            BatchReport("sales", "x"),
            known_stores=stores,
            known_skus=skus,
            max_reject_ratio=0.05,
        )


def test_prepare_inventory(small_world):
    stores, skus = _known(small_world)
    snap = small_world.inventory[small_world.inventory["snapshot_date"] == small_world.start_date]
    raw = snap.astype({"snapshot_date": str}).assign(warehouse_ts=None)
    raw.loc[raw.index[0], "on_hand"] = -3
    rep = BatchReport("inventory", "d1")
    out = prepare_inventory(raw, rep, known_stores=stores, known_skus=skus, max_reject_ratio=0.05)
    assert len(out) == len(raw) - 1
    assert out["is_stockout"].dtype == bool
    assert rep.rejected_by_rule() == {"range:on_hand": 1}


def test_prepare_master_data(small_world):
    products = small_world.products_as_of(small_world.end_date).astype(str)
    out = prepare_products(products, BatchReport("products", "p"), 0.0)
    assert len(out) == len(products)
    assert isinstance(out.loc[0, "updated_at"], date)
    stores = prepare_stores(small_world.stores.astype(str), BatchReport("stores", "s"), 0.0)
    assert stores["size_m2"].dtype == "int64"
    assert isinstance(stores.loc[0, "opened_on"], date)
