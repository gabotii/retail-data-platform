"""Pure (database-free) preparation stages: raw frame -> clean, validated, enriched frame.

Each ``prepare_*`` function runs the same sequence:
standardise -> validate contract -> deduplicate -> referential integrity -> quality gate -> enrich.
They record everything in a :class:`BatchReport` and raise on batch-level failures.
"""

from __future__ import annotations

from collections.abc import Collection

import pandas as pd

from retail_platform.quality.checks import deduplicate, enforce_reject_threshold, require_known
from retail_platform.quality.contracts import (
    INVENTORY_CONTRACT,
    PRODUCT_CONTRACT,
    SALES_CONTRACT,
    STORE_CONTRACT,
    validate,
)
from retail_platform.quality.report import BatchReport
from retail_platform.transform.cleaning import clean_inventory, clean_master, clean_sales
from retail_platform.transform.enrich import enrich_inventory, enrich_sales


def prepare_sales(
    raw: pd.DataFrame,
    report: BatchReport,
    *,
    known_stores: Collection[str],
    known_skus: Collection[str],
    max_reject_ratio: float,
) -> pd.DataFrame:
    report.rows_in = len(raw)
    typed = validate(clean_sales(raw), SALES_CONTRACT, report)
    typed = deduplicate(typed, SALES_CONTRACT.key, report)
    typed = require_known(typed, "store_code", known_stores, report, "unknown_store")
    typed = require_known(typed, "sku", known_skus, report, "unknown_sku")
    enforce_reject_threshold(report, max_reject_ratio)
    return enrich_sales(typed)


def prepare_inventory(
    raw: pd.DataFrame,
    report: BatchReport,
    *,
    known_stores: Collection[str],
    known_skus: Collection[str],
    max_reject_ratio: float,
) -> pd.DataFrame:
    report.rows_in = len(raw)
    typed = validate(clean_inventory(raw), INVENTORY_CONTRACT, report)
    typed = deduplicate(typed, INVENTORY_CONTRACT.key, report)
    typed = require_known(typed, "store_code", known_stores, report, "unknown_store")
    typed = require_known(typed, "sku", known_skus, report, "unknown_sku")
    enforce_reject_threshold(report, max_reject_ratio)
    return enrich_inventory(typed)


def prepare_products(
    raw: pd.DataFrame, report: BatchReport, max_reject_ratio: float
) -> pd.DataFrame:
    report.rows_in = len(raw)
    typed = validate(clean_master(raw, "sku"), PRODUCT_CONTRACT, report)
    typed = deduplicate(typed, PRODUCT_CONTRACT.key, report)
    enforce_reject_threshold(report, max_reject_ratio)
    above_price = typed["unit_cost"] > typed["list_price"]
    report.add_check("cost_above_price", int(above_price.sum()), severity="warning")
    typed["updated_at"] = typed["updated_at"].dt.date
    return typed.reset_index(drop=True)


def prepare_stores(raw: pd.DataFrame, report: BatchReport, max_reject_ratio: float) -> pd.DataFrame:
    report.rows_in = len(raw)
    typed = validate(clean_master(raw, "store_code"), STORE_CONTRACT, report)
    typed = deduplicate(typed, STORE_CONTRACT.key, report)
    enforce_reject_threshold(report, max_reject_ratio)
    typed["opened_on"] = typed["opened_on"].dt.date
    typed["size_m2"] = typed["size_m2"].astype("int64")
    return typed.reset_index(drop=True)
