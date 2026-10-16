"""Business KPIs computed in SQL over the star schema and marts.

Shared by the FastAPI service and the Streamlit dashboard so both always show the same
numbers. Every function takes a :class:`KpiFilter`; filters are passed as bound
parameters (never interpolated), with an empty list meaning "all".

Definitions (CLP, VAT-inclusive shelf prices):
* net sales      = gross sales - discounts
* gross margin   = net sales - cost of goods sold (cost of the product version on sale date)
* average ticket = net sales / distinct tickets
* stockout rate  = share of store x SKU positions with zero units on hand
* days of supply = units on hand / average units sold per day over the last 28 days
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import pandas as pd
from sqlalchemy import Engine, text

_FILTERS_SQL = """
    AND (cardinality(CAST(:stores AS text[])) = 0 OR s.store_code = ANY(CAST(:stores AS text[])))
    AND (cardinality(CAST(:categories AS text[])) = 0
         OR p.category = ANY(CAST(:categories AS text[])))
"""


@dataclass(frozen=True)
class KpiFilter:
    start: date
    end: date
    stores: tuple[str, ...] = field(default_factory=tuple)
    categories: tuple[str, ...] = field(default_factory=tuple)

    def params(self) -> dict[str, Any]:
        return {
            "start_key": int(self.start.strftime("%Y%m%d")),
            "end_key": int(self.end.strftime("%Y%m%d")),
            "stores": list(self.stores),
            "categories": list(self.categories),
        }

    def previous_period(self) -> KpiFilter:
        length = (self.end - self.start).days + 1
        return KpiFilter(
            start=self.start - timedelta(days=length),
            end=self.start - timedelta(days=1),
            stores=self.stores,
            categories=self.categories,
        )


def _frame(engine: Engine, sql: str, params: dict[str, Any]) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def _num(value: Any) -> float:
    return 0.0 if value is None or pd.isna(value) else float(value)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


# --------------------------------------------------------------------------- metadata
def filter_options(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        bounds = conn.execute(
            text(
                "SELECT min(d.full_date), max(d.full_date) FROM mart.daily_store_sales m"
                " JOIN dw.dim_date d USING (date_key)"
            )
        ).one()
        stores = (
            conn.execute(
                text("SELECT store_code, store_name, format, region FROM dw.dim_store ORDER BY 1")
            )
            .mappings()
            .all()
        )
        categories = (
            conn.execute(
                text("SELECT DISTINCT category FROM dw.dim_product WHERE is_current ORDER BY 1")
            )
            .scalars()
            .all()
        )
    return {
        "min_date": bounds[0],
        "max_date": bounds[1],
        "stores": [dict(s) for s in stores],
        "categories": list(categories),
    }


def default_filter(engine: Engine, days: int = 30) -> KpiFilter | None:
    """Last ``days`` days of available data, or None when the warehouse is empty."""
    options = filter_options(engine)
    if options["max_date"] is None:
        return None
    end = options["max_date"]
    start = max(options["min_date"], end - timedelta(days=days - 1))
    return KpiFilter(start=start, end=end)


# --------------------------------------------------------------------------- sales
_SUMMARY_SQL = f"""
SELECT
    coalesce(sum(f.net_amount), 0)                                   AS net_sales,
    coalesce(sum(f.gross_amount), 0)                                 AS gross_sales,
    coalesce(sum(f.discount_amount), 0)                              AS discounts,
    coalesce(sum(f.cost_amount), 0)                                  AS cost,
    coalesce(sum(f.quantity), 0)                                     AS units,
    count(DISTINCT f.transaction_id)                                 AS transactions,
    coalesce(sum(f.net_amount) FILTER (WHERE f.is_promo), 0)         AS promo_sales
FROM dw.fact_sales f
JOIN dw.dim_store s ON s.store_key = f.store_key
JOIN dw.dim_product p ON p.product_key = f.product_key
WHERE f.date_key BETWEEN :start_key AND :end_key
{_FILTERS_SQL}
"""


def _summary_values(engine: Engine, flt: KpiFilter) -> dict[str, float | None]:
    row = _frame(engine, _SUMMARY_SQL, flt.params()).iloc[0]
    net, gross, cost = _num(row["net_sales"]), _num(row["gross_sales"]), _num(row["cost"])
    tickets, units = _num(row["transactions"]), _num(row["units"])
    margin = net - cost
    return {
        "net_sales": net,
        "gross_sales": gross,
        "discounts": _num(row["discounts"]),
        "gross_margin": margin,
        "gross_margin_pct": _ratio(margin, net),
        "units": units,
        "transactions": tickets,
        "avg_ticket": _ratio(net, tickets),
        "units_per_ticket": _ratio(units, tickets),
        "discount_rate": _ratio(_num(row["discounts"]), gross),
        "promo_share": _ratio(_num(row["promo_sales"]), net),
    }


def sales_summary(engine: Engine, flt: KpiFilter) -> dict[str, Any]:
    """Headline KPIs for the period plus the change vs. the previous period of equal length."""
    current = _summary_values(engine, flt)
    previous = _summary_values(engine, flt.previous_period())
    change: dict[str, float | None] = {}
    for key in ("net_sales", "transactions", "avg_ticket", "units"):
        cur, prev = current[key], previous[key]
        change[key] = _ratio((cur or 0) - (prev or 0), prev or 0) if prev else None
    cur_m, prev_m = current["gross_margin_pct"], previous["gross_margin_pct"]
    change["gross_margin_pct_points"] = (
        None if cur_m is None or prev_m is None else (cur_m - prev_m) * 100
    )
    return {
        "period": {"start": flt.start, "end": flt.end},
        "previous_period": {
            "start": flt.previous_period().start,
            "end": flt.previous_period().end,
        },
        "current": current,
        "previous": previous,
        "change": change,
    }


def daily_sales(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    """Daily net sales, margin and units (from the incremental mart)."""
    return _frame(
        engine,
        f"""
        SELECT d.full_date AS day, sum(m.net_sales) AS net_sales,
               sum(m.net_sales - m.cost_amount) AS gross_margin, sum(m.units) AS units
        FROM mart.daily_category_sales m
        JOIN dw.dim_date d ON d.date_key = m.date_key
        JOIN dw.dim_store s ON s.store_key = m.store_key
        JOIN (SELECT DISTINCT category FROM dw.dim_product) p ON p.category = m.category
        WHERE m.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY d.full_date ORDER BY d.full_date
        """,
        flt.params(),
    )


def sales_by_category(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    df = _frame(
        engine,
        f"""
        SELECT m.category, sum(m.net_sales) AS net_sales,
               sum(m.net_sales - m.cost_amount) AS gross_margin, sum(m.units) AS units
        FROM mart.daily_category_sales m
        JOIN dw.dim_store s ON s.store_key = m.store_key
        JOIN (SELECT DISTINCT category FROM dw.dim_product) p ON p.category = m.category
        WHERE m.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY m.category ORDER BY net_sales DESC
        """,
        flt.params(),
    )
    total = df["net_sales"].sum()
    df["share"] = df["net_sales"] / total if total else 0.0
    df["gross_margin_pct"] = df["gross_margin"] / df["net_sales"].where(df["net_sales"] != 0)
    return df


def sales_by_store(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    df = _frame(
        engine,
        f"""
        SELECT s.store_code, s.store_name, s.format, s.region,
               sum(m.net_sales) AS net_sales, sum(m.net_sales - m.cost_amount) AS gross_margin,
               sum(m.units) AS units
        FROM mart.daily_category_sales m
        JOIN dw.dim_store s ON s.store_key = m.store_key
        JOIN (SELECT DISTINCT category FROM dw.dim_product) p ON p.category = m.category
        WHERE m.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY s.store_code, s.store_name, s.format, s.region
        ORDER BY net_sales DESC
        """,
        flt.params(),
    )
    df["gross_margin_pct"] = df["gross_margin"] / df["net_sales"].where(df["net_sales"] != 0)
    return df


TopMetric = Literal["net_sales", "units", "gross_margin"]


def top_products(
    engine: Engine, flt: KpiFilter, limit: int = 10, metric: TopMetric = "net_sales"
) -> pd.DataFrame:
    order = {"net_sales": "net_sales", "units": "units", "gross_margin": "gross_margin"}[metric]
    return _frame(
        engine,
        f"""
        SELECT cur.sku, cur.product_name, cur.category, cur.brand,
               sum(f.net_amount) AS net_sales, sum(f.quantity) AS units,
               sum(f.net_amount - f.cost_amount) AS gross_margin
        FROM dw.fact_sales f
        JOIN dw.dim_store s ON s.store_key = f.store_key
        JOIN dw.dim_product p ON p.product_key = f.product_key
        JOIN dw.dim_product cur ON cur.sku = p.sku AND cur.is_current
        WHERE f.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY cur.sku, cur.product_name, cur.category, cur.brand
        ORDER BY {order} DESC
        LIMIT :limit
        """,
        {**flt.params(), "limit": limit},
    )


def payment_mix(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    df = _frame(
        engine,
        f"""
        SELECT f.payment_method, sum(f.net_amount) AS net_sales,
               count(DISTINCT f.transaction_id) AS transactions
        FROM dw.fact_sales f
        JOIN dw.dim_store s ON s.store_key = f.store_key
        JOIN dw.dim_product p ON p.product_key = f.product_key
        WHERE f.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY f.payment_method ORDER BY net_sales DESC
        """,
        flt.params(),
    )
    total = df["net_sales"].sum()
    df["share"] = df["net_sales"] / total if total else 0.0
    return df


def sales_heatmap(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    """Net sales by weekday (1 = lunes) and hour of day, for staffing decisions."""
    return _frame(
        engine,
        f"""
        SELECT d.day_of_week, d.day_name, extract(hour FROM f.sold_at)::int AS hour,
               sum(f.net_amount) AS net_sales
        FROM dw.fact_sales f
        JOIN dw.dim_date d ON d.date_key = f.date_key
        JOIN dw.dim_store s ON s.store_key = f.store_key
        JOIN dw.dim_product p ON p.product_key = f.product_key
        WHERE f.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY d.day_of_week, d.day_name, hour ORDER BY d.day_of_week, hour
        """,
        flt.params(),
    )


# --------------------------------------------------------------------------- inventory
_INVENTORY_BASE = f"""
WITH snapshot_day AS (
    SELECT max(date_key) AS date_key FROM dw.fact_inventory_snapshot
    WHERE date_key <= :end_key
),
demand AS (
    SELECT f.store_key, p.sku, sum(f.quantity) / 28.0 AS avg_daily_units
    FROM dw.fact_sales f
    JOIN dw.dim_product p ON p.product_key = f.product_key
    WHERE f.date_key > to_char(to_date((SELECT date_key FROM snapshot_day)::text, 'YYYYMMDD')
                               - 28, 'YYYYMMDD')::int
      AND f.date_key <= (SELECT date_key FROM snapshot_day)
    GROUP BY f.store_key, p.sku
),
positions AS (
    SELECT i.date_key, s.store_code, s.store_name, p.sku, p.product_name, p.category,
           i.on_hand_qty, i.on_order_qty, i.is_stockout, i.inventory_cost,
           coalesce(dm.avg_daily_units, 0) AS avg_daily_units
    FROM dw.fact_inventory_snapshot i
    JOIN dw.dim_store s ON s.store_key = i.store_key
    JOIN dw.dim_product p ON p.product_key = i.product_key
    LEFT JOIN demand dm ON dm.store_key = i.store_key AND dm.sku = p.sku
    WHERE i.date_key = (SELECT date_key FROM snapshot_day)
    {_FILTERS_SQL}
)
"""


def inventory_health(engine: Engine, flt: KpiFilter) -> dict[str, Any]:
    df = _frame(
        engine,
        _INVENTORY_BASE
        + """
        SELECT min(date_key) AS date_key, count(*) AS positions,
               count(*) FILTER (WHERE is_stockout) AS stockouts,
               coalesce(sum(on_hand_qty), 0) AS on_hand_units,
               coalesce(sum(inventory_cost), 0) AS inventory_cost,
               coalesce(sum(avg_daily_units), 0) AS avg_daily_units
        FROM positions
        """,
        flt.params(),
    )
    row = df.iloc[0]
    positions = _num(row["positions"])
    snapshot_key = row["date_key"]
    return {
        "snapshot_date": (
            None
            if snapshot_key is None or pd.isna(snapshot_key)
            else pd.to_datetime(str(int(snapshot_key)), format="%Y%m%d").date()
        ),
        "positions": int(positions),
        "stockouts": int(_num(row["stockouts"])),
        "stockout_rate": _ratio(_num(row["stockouts"]), positions),
        "on_hand_units": _num(row["on_hand_units"]),
        "inventory_cost": _num(row["inventory_cost"]),
        "days_of_supply": _ratio(_num(row["on_hand_units"]), _num(row["avg_daily_units"])),
    }


def inventory_at_risk(engine: Engine, flt: KpiFilter, limit: int = 20) -> pd.DataFrame:
    """Positions out of stock or with under 3 days of supply, worst (highest demand) first."""
    return _frame(
        engine,
        _INVENTORY_BASE
        + """
        SELECT store_code, store_name, sku, product_name, category, on_hand_qty, on_order_qty,
               round(avg_daily_units, 2) AS avg_daily_units,
               CASE WHEN avg_daily_units > 0 THEN round(on_hand_qty / avg_daily_units, 1) END
                   AS days_of_supply
        FROM positions
        WHERE avg_daily_units > 0 AND on_hand_qty < 3 * avg_daily_units
        ORDER BY on_hand_qty / avg_daily_units, avg_daily_units DESC
        LIMIT :limit
        """,
        {**flt.params(), "limit": limit},
    )


def stockout_trend(engine: Engine, flt: KpiFilter) -> pd.DataFrame:
    return _frame(
        engine,
        f"""
        SELECT d.full_date AS day,
               avg(CASE WHEN i.is_stockout THEN 1.0 ELSE 0.0 END) AS stockout_rate,
               sum(i.inventory_cost) AS inventory_cost
        FROM dw.fact_inventory_snapshot i
        JOIN dw.dim_date d ON d.date_key = i.date_key
        JOIN dw.dim_store s ON s.store_key = i.store_key
        JOIN dw.dim_product p ON p.product_key = i.product_key
        WHERE i.date_key BETWEEN :start_key AND :end_key
        {_FILTERS_SQL}
        GROUP BY d.full_date ORDER BY d.full_date
        """,
        flt.params(),
    )


# --------------------------------------------------------------------------- data quality
def recent_runs(engine: Engine, limit: int = 20, run_id: int | None = None) -> pd.DataFrame:
    return _frame(
        engine,
        """
        SELECT r.run_id, r.started_at, r.finished_at, r.status, r.trigger, r.full_refresh,
               r.error,
               (SELECT count(*) FROM etl.batch b WHERE b.run_id = r.run_id
                  AND b.status = 'loaded') AS batches_loaded,
               (SELECT count(*) FROM etl.batch b WHERE b.run_id = r.run_id
                  AND b.status IN ('rejected', 'failed')) AS batches_with_errors,
               (SELECT coalesce(sum(rows_loaded), 0) FROM etl.batch b
                 WHERE b.run_id = r.run_id) AS rows_loaded,
               (SELECT coalesce(sum(rows_rejected), 0) FROM etl.batch b
                 WHERE b.run_id = r.run_id) AS rows_rejected
        FROM etl.pipeline_run r
        WHERE CAST(:run_id AS bigint) IS NULL OR r.run_id = :run_id
        ORDER BY r.run_id DESC
        LIMIT :limit
        """,
        {"limit": limit, "run_id": run_id},
    )


def run_batches(engine: Engine, run_id: int) -> pd.DataFrame:
    return _frame(
        engine,
        "SELECT batch_id, source, batch_name, status, rows_in, rows_loaded, rows_rejected,"
        " reject_ratio, error, started_at, finished_at"
        " FROM etl.v_batch_quality WHERE run_id = :r ORDER BY batch_id",
        {"r": run_id},
    )


def rejections_by_rule(engine: Engine, run_id: int | None = None) -> pd.DataFrame:
    return _frame(
        engine,
        """
        SELECT b.source, r.rule, count(*) AS rows
        FROM etl.rejected_record r JOIN etl.batch b ON b.batch_id = r.batch_id
        WHERE CAST(:run_id AS bigint) IS NULL OR b.run_id = :run_id
        GROUP BY b.source, r.rule ORDER BY rows DESC
        """,
        {"run_id": run_id},
    )


def rejected_samples(engine: Engine, rule: str | None = None, limit: int = 20) -> pd.DataFrame:
    return _frame(
        engine,
        """
        SELECT r.rejected_id, b.source, b.batch_name, r.rule, r.detail, r.record::text AS record
        FROM etl.rejected_record r JOIN etl.batch b ON b.batch_id = r.batch_id
        WHERE CAST(:rule AS text) IS NULL OR r.rule = :rule
        ORDER BY r.rejected_id DESC
        LIMIT :limit
        """,
        {"rule": rule, "limit": limit},
    )
