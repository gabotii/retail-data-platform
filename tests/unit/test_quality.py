import json

import pandas as pd
import pytest

from retail_platform.errors import QualityGateError, SchemaValidationError
from retail_platform.quality.checks import (
    check_volume,
    deduplicate,
    enforce_reject_threshold,
    require_known,
)
from retail_platform.quality.contracts import (
    INVENTORY_CONTRACT,
    SALES_CONTRACT,
    Column,
    Contract,
    validate,
)
from retail_platform.quality.report import BatchReport

GOOD = {
    "transaction_id": "ST001-20260701-00001",
    "line_number": "1",
    "sold_at": "2026-07-01 10:00:00",
    "store_code": "ST001",
    "sku": "SKU-000001",
    "quantity": "2",
    "unit_price": "990",
    "discount_amount": "0",
    "payment_method": "DEBIT",
}


def sales(*overrides: dict) -> pd.DataFrame:
    return pd.DataFrame([{**GOOD, "line_number": str(i + 1), **o} for i, o in enumerate(overrides)])


def report(rows: int = 0) -> BatchReport:
    return BatchReport("sales", "test", rows_in=rows)


def test_valid_rows_are_typed():
    rep = report(1)
    out = validate(sales({}), SALES_CONTRACT, rep)
    assert len(out) == 1
    assert out["quantity"].dtype == "Int64"
    assert out["sold_at"].dtype.kind == "M"
    assert rep.rejected_count == 0


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"quantity": "-2"}, "range:quantity"),
        ({"quantity": "1.5"}, "type:quantity"),
        ({"unit_price": "N/A"}, "type:unit_price"),
        ({"store_code": ""}, "not_null:store_code"),
        ({"sku": "SKU-1"}, "pattern:sku"),
        ({"sold_at": "2026-13-45 25:61:00"}, "type:sold_at"),
        ({"payment_method": "CHEQUE"}, "allowed_values:payment_method"),
        ({"discount_amount": "5000"}, "discount_exceeds_gross"),
        ({"store_code": "ST002"}, "store_mismatch_transaction"),
    ],
)
def test_row_level_rejections(override, rule):
    rep = report(2)
    out = validate(sales({}, override), SALES_CONTRACT, rep)
    assert len(out) == 1
    assert rep.rejected_by_rule() == {rule: 1}
    record = json.loads(rep.rejected.iloc[0]["record"])
    key = next(iter(override))
    assert record[key] == override[key]


def test_first_broken_rule_wins():
    rep = report(1)
    validate(sales({"quantity": "", "sku": "bad"}), SALES_CONTRACT, rep)
    assert rep.rejected_by_rule() == {"pattern:sku": 1}


def test_missing_column_is_structural_error():
    with pytest.raises(SchemaValidationError, match="sku"):
        validate(sales({}).drop(columns="sku"), SALES_CONTRACT, report())


def test_extra_columns_are_ignored_with_warning():
    rep = report(1)
    out = validate(sales({}).assign(cashier="x"), SALES_CONTRACT, rep)
    assert "cashier" not in out.columns
    assert any(c.check == "unexpected_columns" and c.severity == "warning" for c in rep.checks)


def test_nullable_column_accepts_none():
    contract = Contract("t", (Column("a", "int"), Column("b", "datetime", nullable=True)), ("a",))
    rep = report(1)
    out = validate(pd.DataFrame({"a": [1], "b": [None]}), contract, rep)
    assert len(out) == 1


def test_inventory_contract_handles_json_values():
    raw = pd.DataFrame(
        {
            "snapshot_date": ["2026-07-01"] * 3,
            "store_code": ["ST001"] * 3,
            "sku": ["SKU-000001", None, "SKU-000003"],
            "on_hand": [5, 3, -4],
            "on_order": [0, 0, 10],
            "warehouse_ts": ["2026-07-01T23:59:00-03:00"] * 3,
        }
    )
    rep = report(3)
    out = validate(raw, INVENTORY_CONTRACT, rep)
    assert len(out) == 1
    assert rep.rejected_by_rule() == {"not_null:sku": 1, "range:on_hand": 1}


def test_deduplicate_drops_exact_and_rejects_conflicts():
    df = pd.DataFrame({"id": [1, 1, 2, 2], "v": ["a", "a", "b", "c"]})
    rep = report(4)
    out = deduplicate(df, ["id"], rep)
    assert out["v"].tolist() == ["a", "b"]
    assert rep.rejected_by_rule() == {"duplicate_key": 1}
    exact = next(c for c in rep.checks if c.check == "exact_duplicates")
    assert exact.failed_rows == 1
    assert exact.severity == "warning"


def test_require_known():
    df = pd.DataFrame({"sku": ["A", "B", "Z"]})
    rep = report(3)
    out = require_known(df, "sku", {"A", "B"}, rep, "unknown_sku")
    assert out["sku"].tolist() == ["A", "B"]
    assert rep.rejected.iloc[0]["detail"] == "Z"


def test_quality_gate():
    rep = report(10)
    rep.reject(pd.DataFrame({"x": [1]}), "r")
    enforce_reject_threshold(rep, 0.1)  # 10% is not above 10%
    rep.reject(pd.DataFrame({"x": [2]}), "r")
    with pytest.raises(QualityGateError) as info:
        enforce_reject_threshold(rep, 0.1)
    assert info.value.ratio == pytest.approx(0.2)


def test_check_volume():
    rep = report(100)
    check_volume(rep, 100, None)
    check_volume(rep, 100, 90)
    check_volume(rep, 300, 100)
    results = [c.passed for c in rep.checks]
    assert results == [True, True, False]


def test_empty_report_has_no_rejections():
    rep = report(0)
    assert rep.reject_ratio == 0
    assert rep.rejected.empty
    assert rep.rejected_by_rule() == {}
