"""Declarative data contracts and row-level validation.

A contract describes the columns a source must deliver, their types and value rules.
Validation is two-tiered:

* **structural** problems (a required column is missing) make the whole batch unusable
  and raise :class:`SchemaValidationError`;
* **row-level** problems (bad type, null, out of range, pattern, cross-field rule)
  quarantine only the offending rows, tagged with the first rule they broke.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from retail_platform.errors import SchemaValidationError
from retail_platform.quality.report import BatchReport

logger = logging.getLogger(__name__)

DType = Literal["string", "int", "decimal", "date", "datetime"]


@dataclass(frozen=True)
class Column:
    name: str
    dtype: DType
    nullable: bool = False
    pattern: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    allowed: frozenset[str] | None = None
    datetime_format: str | None = None


@dataclass(frozen=True)
class RowRule:
    """Cross-field rule on the *typed* frame; ``failing`` returns True for bad rows."""

    name: str
    failing: Callable[[pd.DataFrame], pd.Series]
    detail: str = ""


@dataclass(frozen=True)
class Contract:
    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    row_rules: tuple[RowRule, ...] = field(default_factory=tuple)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def _blank(raw: pd.Series) -> pd.Series:
    as_text = raw.astype(object).where(raw.notna(), "").astype(str).str.strip()
    return raw.isna() | (as_text == "")


def _coerce(raw: pd.Series, column: Column) -> pd.Series:
    if column.dtype == "string":
        return (
            raw.astype(object)
            .where(raw.notna(), None)
            .map(lambda v: None if v is None else str(v).strip())
        )
    if column.dtype in ("int", "decimal"):
        numbers = pd.to_numeric(raw, errors="coerce")
        if column.dtype == "int":
            numbers = numbers.where(numbers.isna() | (numbers % 1 == 0))
            return numbers.astype("Int64")
        return numbers.astype("Float64")
    if column.dtype == "date":
        return pd.to_datetime(raw, format="%Y-%m-%d", errors="coerce").dt.normalize()
    fmt = column.datetime_format or "ISO8601"
    return pd.to_datetime(raw, format=fmt, errors="coerce")


def _value_failures(typed: pd.Series, column: Column) -> tuple[pd.Series, str]:
    """Return (mask of rows violating value rules, rule name) for non-null typed values."""
    present = typed.notna()
    if column.pattern is not None:
        ok = typed.where(present, "").astype(str).str.fullmatch(column.pattern)
        return present & ~ok.astype(bool), f"pattern:{column.name}"
    if column.allowed is not None:
        return present & ~typed.isin(column.allowed), f"allowed_values:{column.name}"
    mask = pd.Series(False, index=typed.index)
    if column.min_value is not None:
        mask |= (present & (typed < column.min_value)).fillna(False).astype(bool)
    if column.max_value is not None:
        mask |= (present & (typed > column.max_value)).fillna(False).astype(bool)
    return mask, f"range:{column.name}"


def validate(df: pd.DataFrame, contract: Contract, report: BatchReport) -> pd.DataFrame:
    """Validate and type ``df``; bad rows go to ``report``, the typed clean frame is returned."""
    missing = [name for name in contract.column_names if name not in df.columns]
    if missing:
        raise SchemaValidationError(f"{contract.name}: missing required columns {missing}")
    extra = sorted(set(df.columns) - set(contract.column_names))
    if extra:
        logger.warning(
            "ignoring unexpected columns", extra={"contract": contract.name, "columns": extra}
        )
        report.add_check("unexpected_columns", 0, "warning", detail=", ".join(extra))

    raw = df[contract.column_names]
    typed = pd.DataFrame(index=raw.index)
    failed_rule = pd.Series(None, index=raw.index, dtype=object)

    def flag(mask: pd.Series, rule: str) -> None:
        fresh = mask & failed_rule.isna()
        failed_rule[fresh] = rule

    for column in contract.columns:
        is_blank = _blank(raw[column.name])
        typed[column.name] = _coerce(raw[column.name].where(~is_blank), column)
        if not column.nullable:
            flag(is_blank, f"not_null:{column.name}")
        flag(~is_blank & typed[column.name].isna(), f"type:{column.name}")
        bad_values, rule = _value_failures(typed[column.name], column)
        flag(bad_values, rule)

    still_ok = failed_rule.isna()
    for row_rule in contract.row_rules:
        failing = row_rule.failing(typed[still_ok]).reindex(raw.index, fill_value=False)
        flag(failing.fillna(False).astype(bool), row_rule.name)

    rejected = failed_rule.notna()
    for rule_name, group in raw[rejected].groupby(failed_rule[rejected]):
        report.reject(group, str(rule_name))
    for rule_name, count in failed_rule[rejected].value_counts().items():
        report.add_check(str(rule_name), int(count))
    report.add_check(f"contract:{contract.name}", int(rejected.sum()))
    return typed[~rejected].copy()


# --------------------------------------------------------------------------- contracts

PAYMENT_METHODS = frozenset({"DEBIT", "CREDIT", "CASH", "DIGITAL_WALLET"})
STORE_FORMATS = frozenset({"Hiper", "Super", "Express"})
STORE_CODE = r"ST\d{3}"
SKU = r"SKU-\d{6}"

SALES_CONTRACT = Contract(
    name="sales_line",
    key=("transaction_id", "line_number"),
    columns=(
        Column("transaction_id", "string", pattern=rf"{STORE_CODE}-\d{{8}}-\d{{5}}"),
        Column("line_number", "int", min_value=1, max_value=500),
        Column("sold_at", "datetime", datetime_format="%Y-%m-%d %H:%M:%S"),
        Column("store_code", "string", pattern=STORE_CODE),
        Column("sku", "string", pattern=SKU),
        Column("quantity", "int", min_value=1, max_value=1000),
        Column("unit_price", "decimal", min_value=1, max_value=10_000_000),
        Column("discount_amount", "decimal", min_value=0),
        Column("payment_method", "string", allowed=PAYMENT_METHODS),
    ),
    row_rules=(
        RowRule(
            "discount_exceeds_gross",
            lambda d: d["discount_amount"] > d["quantity"] * d["unit_price"],
        ),
        RowRule(
            "store_mismatch_transaction",
            lambda d: d["transaction_id"].str[:5] != d["store_code"],
        ),
    ),
)

INVENTORY_CONTRACT = Contract(
    name="inventory_snapshot",
    key=("snapshot_date", "store_code", "sku"),
    columns=(
        Column("snapshot_date", "date"),
        Column("store_code", "string", pattern=STORE_CODE),
        Column("sku", "string", pattern=SKU),
        Column("on_hand", "int", min_value=0),
        Column("on_order", "int", min_value=0),
        Column("warehouse_ts", "datetime", nullable=True),
    ),
)

PRODUCT_CONTRACT = Contract(
    name="product",
    key=("sku",),
    columns=(
        Column("sku", "string", pattern=SKU),
        Column("product_name", "string"),
        Column("category", "string"),
        Column("subcategory", "string"),
        Column("brand", "string"),
        Column("list_price", "decimal", min_value=1),
        Column("unit_cost", "decimal", min_value=0),
        Column("updated_at", "date"),
    ),
)

STORE_CONTRACT = Contract(
    name="store",
    key=("store_code",),
    columns=(
        Column("store_code", "string", pattern=STORE_CODE),
        Column("store_name", "string"),
        Column("city", "string"),
        Column("region", "string"),
        Column("format", "string", allowed=STORE_FORMATS),
        Column("opened_on", "date"),
        Column("size_m2", "int", min_value=1),
    ),
)
