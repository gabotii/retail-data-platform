"""Writes the simulated world to the landing zone as the source systems would export it.

Realistic noise is injected on purpose so the validation and quality layers have
something to catch:

* *defects* (rows the pipeline must reject or deduplicate): duplicated lines, negative
  quantities, unknown SKUs, missing store codes, non-numeric prices, impossible timestamps;
* *cosmetic noise* (rows the pipeline must normalise, not reject): inconsistent casing,
  stray whitespace and missing accents in payment methods and store codes.

Files are written atomically (temp file + rename) so a concurrent pipeline run never
reads a half-written CSV, and the output is byte-identical for the same settings, so
re-generating does not change checksums of files that were already ingested.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from retail_platform.synthetic.simulation import World

logger = logging.getLogger(__name__)

SALES_COLUMNS = [
    "transaction_id",
    "line_number",
    "sold_at",
    "store_code",
    "sku",
    "quantity",
    "unit_price",
    "discount_amount",
    "payment_method",
]
DEFECT_KINDS = (
    "duplicate",
    "negative_quantity",
    "unknown_sku",
    "missing_store",
    "bad_price",
    "bad_timestamp",
)
_RNG_DEFECTS = 99
_PAYMENT_NOISE = {
    "Débito": ("debito", "DEBITO", " Débito "),
    "Crédito": ("credito", "CRÉDITO", "Credito "),
    "Efectivo": ("efectivo", " EFECTIVO"),
    "Billetera digital": ("billetera digital", "Billetera Digital "),
}


@dataclass(frozen=True)
class LandingSummary:
    sales_files_written: int
    sales_files_unchanged: int
    sales_rows: int
    defects: dict[str, int]


def sales_file_name(day: date) -> str:
    return f"sales_{day.isoformat()}.csv"


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> bool:
    """Write ``df`` to ``path`` unless an identical file exists. Returns True if written."""
    content = df.to_csv(index=False, lineterminator="\n").encode("utf-8")
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)
    return True


def _as_source_strings(sales: pd.DataFrame) -> pd.DataFrame:
    out = sales[SALES_COLUMNS].copy()
    out["sold_at"] = out["sold_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return out.astype(str)


def inject_noise(
    day_sales: pd.DataFrame, seed: int, day: date, dirty_rate: float
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Return a string-typed copy of one day of sales with defects and cosmetic noise."""
    df = _as_source_strings(day_sales).reset_index(drop=True)
    counts = dict.fromkeys(DEFECT_KINDS, 0)
    if df.empty or dirty_rate == 0:
        return df, counts
    rng = np.random.default_rng([seed, _RNG_DEFECTS, day.toordinal()])

    n_defects = int(rng.binomial(len(df), dirty_rate))
    rows = rng.choice(len(df), size=n_defects, replace=False)
    kinds = rng.choice(len(DEFECT_KINDS), size=n_defects)
    duplicates = []
    for row, kind_idx in zip(rows, kinds, strict=True):
        kind = DEFECT_KINDS[kind_idx]
        counts[kind] += 1
        if kind == "duplicate":
            duplicates.append(row)
        elif kind == "negative_quantity":
            df.loc[row, "quantity"] = f"-{df.loc[row, 'quantity']}"
        elif kind == "unknown_sku":
            df.loc[row, "sku"] = "SKU-999999"
        elif kind == "missing_store":
            df.loc[row, "store_code"] = ""
        elif kind == "bad_price":
            df.loc[row, "unit_price"] = "N/A"
        else:
            df.loc[row, "sold_at"] = f"{day.year}-13-45 25:61:00"

    cosmetic = rng.random(len(df)) < dirty_rate * 3
    for row in np.flatnonzero(cosmetic):
        variants = _PAYMENT_NOISE.get(df.loc[row, "payment_method"])
        if variants:
            df.loc[row, "payment_method"] = variants[int(rng.integers(len(variants)))]
    lower_store = rng.random(len(df)) < dirty_rate
    df.loc[lower_store, "store_code"] = " " + df.loc[lower_store, "store_code"].str.lower()

    if duplicates:
        df = pd.concat([df, df.loc[duplicates]]).sort_index(kind="stable")
    return df.reset_index(drop=True), counts


def write_landing(world: World, landing_dir: Path, seed: int, dirty_rate: float) -> LandingSummary:
    """Export master data and one sales CSV per day into ``landing_dir``."""
    master_dir = landing_dir / "master"
    _atomic_write_csv(world.stores, master_dir / "stores.csv")
    _atomic_write_csv(world.products_as_of(world.end_date), master_dir / "products.csv")

    sales_dir = landing_dir / "sales"
    written = unchanged = total_rows = 0
    defects = dict.fromkeys(DEFECT_KINDS, 0)
    day_of_sale = world.sales["sold_at"].dt.date
    for offset in range(world.days):
        day = world.start_date + timedelta(days=offset)
        noisy, day_defects = inject_noise(world.sales[day_of_sale == day], seed, day, dirty_rate)
        for kind, n in day_defects.items():
            defects[kind] += n
        total_rows += len(noisy)
        if _atomic_write_csv(noisy, sales_dir / sales_file_name(day)):
            written += 1
        else:
            unchanged += 1

    summary = LandingSummary(written, unchanged, total_rows, defects)
    logger.info(
        "landing zone generated",
        extra={
            "landing_dir": str(landing_dir),
            "files_written": written,
            "files_unchanged": unchanged,
            "rows": total_rows,
            "defects": defects,
        },
    )
    return summary
