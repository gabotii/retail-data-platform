-- V002: analyst-friendly views on top of the star schema.

-- Ticket lines with every dimension attribute resolved (for ad-hoc SQL / BI tools).
CREATE VIEW mart.v_sales_detail AS
SELECT
    f.transaction_id,
    f.line_number,
    f.sold_at,
    d.full_date,
    d.year,
    d.month,
    d.month_name,
    d.day_name,
    d.is_weekend,
    d.is_holiday,
    s.store_code,
    s.store_name,
    s.city,
    s.region,
    s.format AS store_format,
    p.sku,
    p.product_name,
    p.category,
    p.subcategory,
    p.brand,
    f.quantity,
    f.unit_price,
    f.gross_amount,
    f.discount_amount,
    f.net_amount,
    f.cost_amount,
    f.net_amount - f.cost_amount AS margin_amount,
    f.is_promo,
    f.payment_method
FROM dw.fact_sales f
JOIN dw.dim_date d ON d.date_key = f.date_key
JOIN dw.dim_store s ON s.store_key = f.store_key
JOIN dw.dim_product p ON p.product_key = f.product_key;

-- Inventory position per store and day.
CREATE VIEW mart.v_inventory_daily AS
SELECT
    i.date_key,
    i.store_key,
    count(*)                                   AS sku_count,
    count(*) FILTER (WHERE i.is_stockout)      AS stockout_skus,
    sum(i.on_hand_qty)                         AS on_hand_units,
    sum(i.on_order_qty)                        AS on_order_units,
    sum(i.inventory_cost)                      AS inventory_cost
FROM dw.fact_inventory_snapshot i
GROUP BY i.date_key, i.store_key;

-- Data quality overview per batch.
CREATE VIEW etl.v_batch_quality AS
SELECT
    b.batch_id,
    b.run_id,
    b.source,
    b.batch_name,
    b.status,
    b.rows_in,
    b.rows_loaded,
    b.rows_rejected,
    CASE WHEN b.rows_in > 0 THEN round(b.rows_rejected::numeric / b.rows_in, 4) END AS reject_ratio,
    b.error,
    b.started_at,
    b.finished_at
FROM etl.batch b;
