-- V001: control (etl), dimensional model (dw) and aggregate marts (mart).

CREATE SCHEMA IF NOT EXISTS etl;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS mart;

-- ===================================================================== etl: run control
CREATE TABLE etl.pipeline_run (
    run_id        bigserial PRIMARY KEY,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'success', 'partial', 'failed')),
    trigger       text NOT NULL DEFAULT 'cli',
    full_refresh  boolean NOT NULL DEFAULT false,
    metrics       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error         text
);
CREATE INDEX pipeline_run_started_idx ON etl.pipeline_run (started_at DESC);

-- One batch = one unit of atomic loading: a CSV file or one inventory snapshot date.
CREATE TABLE etl.batch (
    batch_id       bigserial PRIMARY KEY,
    run_id         bigint NOT NULL REFERENCES etl.pipeline_run (run_id),
    source         text NOT NULL CHECK (source IN ('stores', 'products', 'sales', 'inventory')),
    batch_name     text NOT NULL,
    checksum       text,
    status         text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'loaded', 'rejected', 'failed')),
    rows_in        integer NOT NULL DEFAULT 0,
    rows_loaded    integer NOT NULL DEFAULT 0,
    rows_rejected  integer NOT NULL DEFAULT 0,
    error          text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);
CREATE INDEX batch_run_idx ON etl.batch (run_id);
CREATE INDEX batch_source_idx ON etl.batch (source, started_at DESC);

-- Incremental file ingestion: a (file, checksum) pair is processed at most once.
CREATE TABLE etl.processed_file (
    source        text NOT NULL,
    file_name     text NOT NULL,
    checksum      text NOT NULL,
    status        text NOT NULL CHECK (status IN ('loaded', 'rejected')),
    batch_id      bigint NOT NULL REFERENCES etl.batch (batch_id),
    processed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, file_name)
);

-- Incremental API ingestion: high-water mark per source.
CREATE TABLE etl.watermark (
    source      text PRIMARY KEY,
    value       text NOT NULL,
    run_id      bigint REFERENCES etl.pipeline_run (run_id),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE etl.quality_check (
    check_id     bigserial PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES etl.batch (batch_id),
    check_name   text NOT NULL,
    severity     text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    passed       boolean NOT NULL,
    failed_rows  integer NOT NULL,
    total_rows   integer NOT NULL,
    detail       text NOT NULL DEFAULT ''
);
CREATE INDEX quality_check_batch_idx ON etl.quality_check (batch_id);

-- Quarantine: rejected rows with their original values, for inspection and replay.
CREATE TABLE etl.rejected_record (
    rejected_id  bigserial PRIMARY KEY,
    batch_id     bigint NOT NULL REFERENCES etl.batch (batch_id),
    rule         text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    record       jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rejected_record_batch_idx ON etl.rejected_record (batch_id);
CREATE INDEX rejected_record_rule_idx ON etl.rejected_record (rule);

-- ===================================================================== dw: dimensions
CREATE TABLE dw.dim_date (
    date_key      integer PRIMARY KEY,           -- YYYYMMDD
    full_date     date NOT NULL UNIQUE,
    year          smallint NOT NULL,
    quarter       smallint NOT NULL,
    month         smallint NOT NULL,
    month_name    text NOT NULL,
    iso_week      smallint NOT NULL,
    day_of_month  smallint NOT NULL,
    day_of_week   smallint NOT NULL,             -- 1 = lunes
    day_name      text NOT NULL,
    is_weekend    boolean NOT NULL,
    is_holiday    boolean NOT NULL,
    holiday_name  text
);

-- SCD type 1: store attributes are overwritten in place.
CREATE TABLE dw.dim_store (
    store_key   serial PRIMARY KEY,
    store_code  text NOT NULL UNIQUE,
    store_name  text NOT NULL,
    city        text NOT NULL,
    region      text NOT NULL,
    format      text NOT NULL,
    opened_on   date NOT NULL,
    size_m2     integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- SCD type 2: a new version per change of price, cost or classification.
CREATE TABLE dw.dim_product (
    product_key   serial PRIMARY KEY,
    sku           text NOT NULL,
    product_name  text NOT NULL,
    category      text NOT NULL,
    subcategory   text NOT NULL,
    brand         text NOT NULL,
    list_price    numeric(12, 2) NOT NULL,
    unit_cost     numeric(12, 2) NOT NULL,
    valid_from    date NOT NULL,
    valid_to      date NOT NULL DEFAULT DATE '9999-12-31',
    is_current    boolean NOT NULL DEFAULT true,
    row_hash      text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dim_product_validity CHECK (valid_from <= valid_to),
    CONSTRAINT dim_product_version UNIQUE (sku, valid_from)
);
CREATE UNIQUE INDEX dim_product_current_uidx ON dw.dim_product (sku) WHERE is_current;

-- ===================================================================== dw: facts
-- Grain: one ticket line.
CREATE TABLE dw.fact_sales (
    sales_line_id    bigserial PRIMARY KEY,
    date_key         integer NOT NULL REFERENCES dw.dim_date (date_key),
    store_key        integer NOT NULL REFERENCES dw.dim_store (store_key),
    product_key      integer NOT NULL REFERENCES dw.dim_product (product_key),
    transaction_id   text NOT NULL,
    line_number      smallint NOT NULL,
    sold_at          timestamp NOT NULL,
    quantity         integer NOT NULL CHECK (quantity > 0),
    unit_price       numeric(12, 2) NOT NULL,
    gross_amount     numeric(14, 2) NOT NULL,
    discount_amount  numeric(14, 2) NOT NULL,
    net_amount       numeric(14, 2) NOT NULL,
    cost_amount      numeric(14, 2) NOT NULL,
    is_promo         boolean NOT NULL,
    payment_method   text NOT NULL,
    source_file      text NOT NULL,
    batch_id         bigint NOT NULL REFERENCES etl.batch (batch_id),
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fact_sales_natural_key UNIQUE (transaction_id, line_number)
);
CREATE INDEX fact_sales_date_idx ON dw.fact_sales (date_key);
CREATE INDEX fact_sales_store_date_idx ON dw.fact_sales (store_key, date_key);
CREATE INDEX fact_sales_product_idx ON dw.fact_sales (product_key);
CREATE INDEX fact_sales_source_file_idx ON dw.fact_sales (source_file);

-- Grain: one product in one store at the end of one day (periodic snapshot).
CREATE TABLE dw.fact_inventory_snapshot (
    date_key        integer NOT NULL REFERENCES dw.dim_date (date_key),
    store_key       integer NOT NULL REFERENCES dw.dim_store (store_key),
    product_key     integer NOT NULL REFERENCES dw.dim_product (product_key),
    on_hand_qty     integer NOT NULL CHECK (on_hand_qty >= 0),
    on_order_qty    integer NOT NULL CHECK (on_order_qty >= 0),
    is_stockout     boolean NOT NULL,
    inventory_cost  numeric(14, 2) NOT NULL,
    batch_id        bigint NOT NULL REFERENCES etl.batch (batch_id),
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (date_key, store_key, product_key)
);
CREATE INDEX fact_inventory_store_idx ON dw.fact_inventory_snapshot (store_key, date_key);

-- ===================================================================== mart: aggregates
-- Rebuilt incrementally, only for the dates touched by each run.
CREATE TABLE mart.daily_store_sales (
    date_key         integer NOT NULL REFERENCES dw.dim_date (date_key),
    store_key        integer NOT NULL REFERENCES dw.dim_store (store_key),
    net_sales        numeric(16, 2) NOT NULL,
    gross_sales      numeric(16, 2) NOT NULL,
    discount_amount  numeric(16, 2) NOT NULL,
    cost_amount      numeric(16, 2) NOT NULL,
    units            bigint NOT NULL,
    lines            bigint NOT NULL,
    transactions     bigint NOT NULL,
    PRIMARY KEY (date_key, store_key)
);

CREATE TABLE mart.daily_category_sales (
    date_key     integer NOT NULL REFERENCES dw.dim_date (date_key),
    store_key    integer NOT NULL REFERENCES dw.dim_store (store_key),
    category     text NOT NULL,
    net_sales    numeric(16, 2) NOT NULL,
    cost_amount  numeric(16, 2) NOT NULL,
    units        bigint NOT NULL,
    PRIMARY KEY (date_key, store_key, category)
);
