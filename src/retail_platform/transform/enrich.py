"""Post-validation transformations: derived measures and keys ready for the warehouse."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from retail_platform.calendar_cl import MONTHS_ES, WEEKDAYS_ES, holiday_name


def date_key(values: pd.Series) -> pd.Series:
    """YYYYMMDD integer surrogate key used by ``dw.dim_date``."""
    stamps = pd.to_datetime(values)
    return (stamps.dt.year * 10000 + stamps.dt.month * 100 + stamps.dt.day).astype("int64")


def enrich_sales(typed: pd.DataFrame) -> pd.DataFrame:
    """Add sale date, date key, gross/net amounts and a promotion flag to typed sales lines."""
    df = typed.copy()
    df["quantity"] = df["quantity"].astype("int64")
    df["line_number"] = df["line_number"].astype("int64")
    df["unit_price"] = df["unit_price"].astype("float64").round(2)
    df["discount_amount"] = df["discount_amount"].astype("float64").round(2)
    df["sale_date"] = df["sold_at"].dt.normalize()
    df["date_key"] = date_key(df["sale_date"])
    df["gross_amount"] = (df["quantity"] * df["unit_price"]).round(2)
    df["net_amount"] = (df["gross_amount"] - df["discount_amount"]).round(2)
    df["is_promo"] = df["discount_amount"] > 0
    return df[
        [
            "transaction_id",
            "line_number",
            "sold_at",
            "sale_date",
            "date_key",
            "store_code",
            "sku",
            "quantity",
            "unit_price",
            "gross_amount",
            "discount_amount",
            "net_amount",
            "is_promo",
            "payment_method",
        ]
    ].reset_index(drop=True)


def enrich_inventory(typed: pd.DataFrame) -> pd.DataFrame:
    df = typed.copy()
    df["on_hand"] = df["on_hand"].astype("int64")
    df["on_order"] = df["on_order"].astype("int64")
    df["date_key"] = date_key(df["snapshot_date"])
    df["is_stockout"] = df["on_hand"] == 0
    return df[
        ["snapshot_date", "date_key", "store_code", "sku", "on_hand", "on_order", "is_stockout"]
    ].reset_index(drop=True)


def build_dim_date(start: date, end: date) -> pd.DataFrame:
    """Calendar rows for every day in [start, end] (Spanish labels, Chilean holidays)."""
    days = pd.date_range(start, end, freq="D")
    iso = days.isocalendar()
    holidays = [holiday_name(d.date()) for d in days]
    return pd.DataFrame(
        {
            "date_key": date_key(pd.Series(days)).to_numpy(),
            "full_date": days.date,
            "year": days.year,
            "quarter": days.quarter,
            "month": days.month,
            "month_name": [MONTHS_ES[m - 1] for m in days.month],
            "iso_week": iso["week"].to_numpy(dtype=np.int64),
            "day_of_month": days.day,
            "day_of_week": days.dayofweek + 1,
            "day_name": [WEEKDAYS_ES[d] for d in days.dayofweek],
            "is_weekend": days.dayofweek >= 5,
            "is_holiday": [h is not None for h in holidays],
            "holiday_name": holidays,
        }
    )
