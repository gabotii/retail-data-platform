"""Set-based loads into the dimensional model.

Pattern for every load: ``COPY`` the prepared pandas frame into a transaction-scoped
temporary table, then let PostgreSQL do the joins and merges in SQL. All functions run
inside the caller's transaction, so a batch is either fully loaded or not at all.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import Connection, text

from retail_platform.transform.enrich import build_dim_date

_COPY_CHUNK = 1 << 20


class LoadIntegrityError(RuntimeError):
    """Rows were lost while resolving dimension keys; the batch transaction is rolled back."""


@dataclass(frozen=True)
class MergeResult:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    date_keys: frozenset[int] = frozenset()

    @property
    def loaded(self) -> int:
        return self.inserted + self.updated


def copy_frame(conn: Connection, table: str, df: pd.DataFrame, ddl: str) -> None:
    """Create temp table ``table`` with ``ddl`` columns and bulk-load ``df`` via COPY (CSV)."""
    conn.execute(text(f"CREATE TEMP TABLE {table} ({ddl}) ON COMMIT DROP"))
    payload = df.to_csv(
        index=False, header=False, lineterminator="\n", date_format="%Y-%m-%d %H:%M:%S"
    )
    columns = ", ".join(df.columns)
    driver_conn = conn.connection.driver_connection
    assert driver_conn is not None  # noqa: S101
    with (
        driver_conn.cursor() as cur,
        cur.copy(f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv)") as copy,
    ):
        buffer = io.StringIO(payload)
        while chunk := buffer.read(_COPY_CHUNK):
            copy.write(chunk)


# ===================================================================== dim_date
def ensure_dates(conn: Connection, start: date, end: date) -> int:
    """Make sure ``dw.dim_date`` covers [start, end] (facts reference it by FK)."""
    dim = build_dim_date(start, end)
    copy_frame(
        conn,
        "tmp_dim_date",
        dim,
        "date_key integer, full_date date, year smallint, quarter smallint, month smallint,"
        " month_name text, iso_week smallint, day_of_month smallint, day_of_week smallint,"
        " day_name text, is_weekend boolean, is_holiday boolean, holiday_name text",
    )
    result = conn.execute(
        text("INSERT INTO dw.dim_date SELECT * FROM tmp_dim_date ON CONFLICT (date_key) DO NOTHING")
    )
    conn.execute(text("DROP TABLE tmp_dim_date"))
    return result.rowcount


# ===================================================================== dimensions
def merge_stores(conn: Connection, stores: pd.DataFrame) -> MergeResult:
    """SCD1 upsert: attributes are overwritten; untouched rows are not rewritten."""
    copy_frame(
        conn,
        "tmp_stores",
        stores,
        "store_code text, store_name text, city text, region text, format text,"
        " opened_on date, size_m2 integer",
    )
    rows = conn.execute(
        text(
            """
            INSERT INTO dw.dim_store AS d
                (store_code, store_name, city, region, format, opened_on, size_m2)
            SELECT store_code, store_name, city, region, format, opened_on, size_m2
            FROM tmp_stores
            ON CONFLICT (store_code) DO UPDATE SET
                store_name = EXCLUDED.store_name, city = EXCLUDED.city,
                region = EXCLUDED.region, format = EXCLUDED.format,
                opened_on = EXCLUDED.opened_on, size_m2 = EXCLUDED.size_m2, updated_at = now()
            WHERE (d.store_name, d.city, d.region, d.format, d.opened_on, d.size_m2)
                IS DISTINCT FROM (EXCLUDED.store_name, EXCLUDED.city, EXCLUDED.region,
                                  EXCLUDED.format, EXCLUDED.opened_on, EXCLUDED.size_m2)
            RETURNING (xmax = 0) AS inserted
            """
        )
    ).all()
    inserted = sum(1 for r in rows if r.inserted)
    return MergeResult(inserted=inserted, updated=len(rows) - inserted)


_PRODUCT_ATTRS = ("product_name", "category", "subcategory", "brand", "list_price", "unit_cost")


def product_row_hash(df: pd.DataFrame) -> pd.Series:
    def digest(row: pd.Series) -> str:
        parts = [
            f"{row[c]:.2f}" if c in ("list_price", "unit_cost") else str(row[c])
            for c in _PRODUCT_ATTRS
        ]
        return hashlib.md5("|".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()

    return df.apply(digest, axis=1)


def merge_products(conn: Connection, products: pd.DataFrame) -> MergeResult:
    """SCD2 merge of the product master.

    * new SKU -> first version valid since 1900-01-01 (covers history loaded before it);
    * changed attributes with a newer ``updated_at`` -> close the current version the day
      before and open a new one;
    * changed attributes without a newer ``updated_at`` -> correction, overwrite in place.
    """
    staged = products[["sku", *_PRODUCT_ATTRS, "updated_at"]].copy()
    staged["row_hash"] = product_row_hash(staged)
    copy_frame(
        conn,
        "tmp_products",
        staged,
        "sku text, product_name text, category text, subcategory text, brand text,"
        " list_price numeric(12,2), unit_cost numeric(12,2), updated_at date, row_hash text",
    )
    corrected = conn.execute(
        text(
            """
            UPDATE dw.dim_product d SET
                product_name = t.product_name, category = t.category,
                subcategory = t.subcategory, brand = t.brand, list_price = t.list_price,
                unit_cost = t.unit_cost, row_hash = t.row_hash
            FROM tmp_products t
            WHERE d.sku = t.sku AND d.is_current
              AND d.row_hash <> t.row_hash AND t.updated_at <= d.valid_from
            """
        )
    ).rowcount
    closed = conn.execute(
        text(
            """
            UPDATE dw.dim_product d SET valid_to = t.updated_at - 1, is_current = false
            FROM tmp_products t
            WHERE d.sku = t.sku AND d.is_current
              AND d.row_hash <> t.row_hash AND t.updated_at > d.valid_from
            """
        )
    ).rowcount
    inserted = conn.execute(
        text(
            """
            INSERT INTO dw.dim_product
                (sku, product_name, category, subcategory, brand, list_price, unit_cost,
                 valid_from, row_hash)
            SELECT t.sku, t.product_name, t.category, t.subcategory, t.brand, t.list_price,
                   t.unit_cost,
                   CASE WHEN EXISTS (SELECT 1 FROM dw.dim_product x WHERE x.sku = t.sku)
                        THEN t.updated_at ELSE DATE '1900-01-01' END,
                   t.row_hash
            FROM tmp_products t
            WHERE NOT EXISTS (
                SELECT 1 FROM dw.dim_product d WHERE d.sku = t.sku AND d.is_current
            )
            """
        )
    ).rowcount
    return MergeResult(inserted=inserted, updated=corrected + closed)


def known_codes(conn: Connection) -> tuple[set[str], set[str]]:
    stores = set(conn.execute(text("SELECT store_code FROM dw.dim_store")).scalars())
    skus = set(conn.execute(text("SELECT sku FROM dw.dim_product WHERE is_current")).scalars())
    return stores, skus


# ===================================================================== facts
def load_sales(
    conn: Connection, sales: pd.DataFrame, batch_id: int, source_file: str
) -> MergeResult:
    """Replace the rows owned by ``source_file`` with the prepared ``sales`` frame.

    A re-delivered (corrected) file therefore fully replaces its previous version,
    including lines that disappeared, while unchanged files are never touched.
    """
    if not sales.empty:
        ensure_dates(conn, sales["sale_date"].min().date(), sales["sale_date"].max().date())
    previous_keys = (
        conn.execute(
            text("DELETE FROM dw.fact_sales WHERE source_file = :f RETURNING date_key"),
            {"f": source_file},
        )
        .scalars()
        .all()
    )
    touched = frozenset(int(k) for k in previous_keys) | frozenset(
        int(k) for k in sales["date_key"].unique()
    )
    if sales.empty:
        return MergeResult(deleted=len(previous_keys), date_keys=touched)
    staged = sales.drop(columns=["sale_date"])
    copy_frame(
        conn,
        "tmp_sales",
        staged,
        "transaction_id text, line_number smallint, sold_at timestamp, date_key integer,"
        " store_code text, sku text, quantity integer, unit_price numeric(12,2),"
        " gross_amount numeric(14,2), discount_amount numeric(14,2), net_amount numeric(14,2),"
        " is_promo boolean, payment_method text",
    )
    rows = conn.execute(
        text(
            """
            INSERT INTO dw.fact_sales AS f
                (date_key, store_key, product_key, transaction_id, line_number, sold_at,
                 quantity, unit_price, gross_amount, discount_amount, net_amount, cost_amount,
                 is_promo, payment_method, source_file, batch_id)
            SELECT t.date_key, s.store_key, p.product_key, t.transaction_id, t.line_number,
                   t.sold_at, t.quantity, t.unit_price, t.gross_amount, t.discount_amount,
                   t.net_amount, round(t.quantity * p.unit_cost, 2), t.is_promo,
                   t.payment_method, :source_file, :batch_id
            FROM tmp_sales t
            JOIN dw.dim_store s ON s.store_code = t.store_code
            JOIN dw.dim_product p
              ON p.sku = t.sku AND t.sold_at::date BETWEEN p.valid_from AND p.valid_to
            ON CONFLICT (transaction_id, line_number) DO UPDATE SET
                date_key = EXCLUDED.date_key, store_key = EXCLUDED.store_key,
                product_key = EXCLUDED.product_key, sold_at = EXCLUDED.sold_at,
                quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price,
                gross_amount = EXCLUDED.gross_amount, discount_amount = EXCLUDED.discount_amount,
                net_amount = EXCLUDED.net_amount, cost_amount = EXCLUDED.cost_amount,
                is_promo = EXCLUDED.is_promo, payment_method = EXCLUDED.payment_method,
                source_file = EXCLUDED.source_file, batch_id = EXCLUDED.batch_id,
                loaded_at = now()
            RETURNING (xmax = 0) AS inserted
            """
        ),
        {"source_file": source_file, "batch_id": batch_id},
    ).all()
    if len(rows) != len(staged):
        raise LoadIntegrityError(
            f"{source_file}: {len(staged) - len(rows)} sales rows could not be matched to "
            "store/product dimensions"
        )
    inserted = sum(1 for r in rows if r.inserted)
    return MergeResult(
        inserted=inserted,
        updated=len(rows) - inserted,
        deleted=len(previous_keys),
        date_keys=touched,
    )


def load_inventory(conn: Connection, inventory: pd.DataFrame, batch_id: int) -> MergeResult:
    if inventory.empty:
        return MergeResult()
    days = pd.to_datetime(inventory["snapshot_date"])
    ensure_dates(conn, days.min().date(), days.max().date())
    copy_frame(
        conn,
        "tmp_inventory",
        inventory,
        "snapshot_date date, date_key integer, store_code text, sku text, on_hand integer,"
        " on_order integer, is_stockout boolean",
    )
    rows = conn.execute(
        text(
            """
            INSERT INTO dw.fact_inventory_snapshot AS f
                (date_key, store_key, product_key, on_hand_qty, on_order_qty, is_stockout,
                 inventory_cost, batch_id)
            SELECT t.date_key, s.store_key, p.product_key, t.on_hand, t.on_order,
                   t.is_stockout, round(t.on_hand * p.unit_cost, 2), :batch_id
            FROM tmp_inventory t
            JOIN dw.dim_store s ON s.store_code = t.store_code
            JOIN dw.dim_product p
              ON p.sku = t.sku AND t.snapshot_date BETWEEN p.valid_from AND p.valid_to
            ON CONFLICT (date_key, store_key, product_key) DO UPDATE SET
                on_hand_qty = EXCLUDED.on_hand_qty, on_order_qty = EXCLUDED.on_order_qty,
                is_stockout = EXCLUDED.is_stockout, inventory_cost = EXCLUDED.inventory_cost,
                batch_id = EXCLUDED.batch_id, loaded_at = now()
            RETURNING (xmax = 0) AS inserted
            """
        ),
        {"batch_id": batch_id},
    ).all()
    if len(rows) != len(inventory):
        raise LoadIntegrityError(
            f"inventory: {len(inventory) - len(rows)} rows could not be matched to dimensions"
        )
    inserted = sum(1 for r in rows if r.inserted)
    return MergeResult(inserted=inserted, updated=len(rows) - inserted)


# ===================================================================== marts
def refresh_sales_marts(conn: Connection, date_keys: Iterable[int]) -> int:
    """Recompute the daily aggregates only for ``date_keys`` (incremental refresh)."""
    keys: Sequence[int] = sorted(set(date_keys))
    if not keys:
        return 0
    params = {"keys": list(keys)}
    conn.execute(text("DELETE FROM mart.daily_store_sales WHERE date_key = ANY(:keys)"), params)
    conn.execute(text("DELETE FROM mart.daily_category_sales WHERE date_key = ANY(:keys)"), params)
    conn.execute(
        text(
            """
            INSERT INTO mart.daily_store_sales
            SELECT date_key, store_key, sum(net_amount), sum(gross_amount), sum(discount_amount),
                   sum(cost_amount), sum(quantity), count(*), count(DISTINCT transaction_id)
            FROM dw.fact_sales WHERE date_key = ANY(:keys)
            GROUP BY date_key, store_key
            """
        ),
        params,
    )
    conn.execute(
        text(
            """
            INSERT INTO mart.daily_category_sales
            SELECT f.date_key, f.store_key, p.category, sum(f.net_amount), sum(f.cost_amount),
                   sum(f.quantity)
            FROM dw.fact_sales f JOIN dw.dim_product p ON p.product_key = f.product_key
            WHERE f.date_key = ANY(:keys)
            GROUP BY f.date_key, f.store_key, p.category
            """
        ),
        params,
    )
    return len(keys)
