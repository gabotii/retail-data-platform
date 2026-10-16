"""Pre-validation standardisation of raw (all-text) source frames.

Only lossless, unambiguous fixes belong here (whitespace, casing, accent-insensitive
label mapping). Anything that requires a judgement call is left for the contract to reject.
"""

from __future__ import annotations

import unicodedata

import pandas as pd

PAYMENT_ALIASES = {
    "debito": "DEBIT",
    "tarjeta de debito": "DEBIT",
    "credito": "CREDIT",
    "tarjeta de credito": "CREDIT",
    "efectivo": "CASH",
    "billetera digital": "DIGITAL_WALLET",
    "billetera": "DIGITAL_WALLET",
}


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_payment_method(value: object) -> object:
    if not isinstance(value, str):
        return value
    key = " ".join(strip_accents(value).lower().split())
    return PAYMENT_ALIASES.get(key, value.strip())


def _is_arrow_text(values: pd.Series) -> bool:
    return values.dtype != object and pd.api.types.is_string_dtype(values)


def _strip_text(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column in out.columns:
        values = out[column]
        if _is_arrow_text(values):
            out[column] = values.str.strip()
        elif values.dtype == object:
            out[column] = values.map(lambda v: v.strip() if isinstance(v, str) else v)
    return out


def _upper(series: pd.Series) -> pd.Series:
    if _is_arrow_text(series):
        return series.str.upper()
    return series.map(lambda v: v.upper() if isinstance(v, str) else v)


def _normalize_payments(series: pd.Series) -> pd.Series:
    # Few distinct labels, many rows: normalise each distinct value once.
    mapping = {value: normalize_payment_method(value) for value in series.dropna().unique()}
    return series.map(mapping)


def clean_sales(raw: pd.DataFrame) -> pd.DataFrame:
    df = _strip_text(raw)
    for column in ("transaction_id", "store_code", "sku"):
        if column in df.columns:
            df[column] = _upper(df[column])
    if "payment_method" in df.columns:
        df["payment_method"] = _normalize_payments(df["payment_method"])
    return df


def clean_inventory(raw: pd.DataFrame) -> pd.DataFrame:
    df = _strip_text(raw)
    for column in ("store_code", "sku"):
        if column in df.columns:
            df[column] = _upper(df[column])
    return df


def clean_master(raw: pd.DataFrame, code_column: str) -> pd.DataFrame:
    df = _strip_text(raw)
    if code_column in df.columns:
        df[code_column] = _upper(df[code_column])
    return df
