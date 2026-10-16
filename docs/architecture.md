# Arquitectura

## Vista general

```mermaid
flowchart LR
    subgraph Fuentes["Fuentes (simuladas, reproducibles)"]
        GEN["retail generate<br/>simulador determinista"]
        CSV[("Landing zone<br/>master/*.csv<br/>sales/sales_YYYY-MM-DD.csv")]
        WMS["API demo WMS<br/>FastAPI :8001<br/>inventario paginado<br/>fallos 503/429 inyectados"]
        GEN --> CSV
    end

    subgraph Pipeline["Pipeline ETL (retail run / schedule)"]
        direction TB
        ING["Ingesta<br/>CSV como texto + sha256<br/>cliente HTTP con reintentos"]
        VAL["Contratos de esquema<br/>tipos · nulos · rangos · patrones<br/>reglas entre campos"]
        DQ["Calidad<br/>deduplicación · integridad referencial<br/>quality gate · volumen"]
        TRF["Transformación pandas<br/>normalización · montos · claves de fecha"]
        LOAD["Carga SQL<br/>COPY → tabla temporal → MERGE<br/>SCD1 tiendas · SCD2 productos"]
        ING --> VAL --> DQ --> TRF --> LOAD
    end

    subgraph PG["PostgreSQL 17"]
        ETL[("etl<br/>runs · batches · watermarks<br/>quality_check · rejected_record")]
        DW[("dw<br/>dim_date · dim_store · dim_product<br/>fact_sales · fact_inventory_snapshot")]
        MART[("mart<br/>daily_store_sales<br/>daily_category_sales · vistas")]
    end

    CSV --> ING
    WMS --> ING
    DQ -. filas rechazadas .-> ETL
    LOAD --> DW
    LOAD -- refresco incremental --> MART
    LOAD -. registro / watermark .-> ETL

    KPI["analytics.kpis<br/>SQL compartido"]
    API["API KPI FastAPI :8000<br/>/api/v1/kpis/* · /pipeline/runs"]
    DASH["Dashboard Streamlit :8501"]
    DW --> KPI
    MART --> KPI
    ETL --> KPI
    KPI --> API
    KPI --> DASH
    API -- POST /pipeline/runs<br/>X-API-Key --> Pipeline
```

## Flujo de un lote

Un **lote** es la unidad atómica de carga: un archivo CSV o un día de snapshot de inventario.

```mermaid
sequenceDiagram
    participant R as Runner
    participant S as Fuente
    participant Q as Calidad
    participant DB as PostgreSQL
    R->>DB: pg_try_advisory_lock (una ejecución a la vez)
    R->>DB: INSERT etl.pipeline_run
    loop por cada archivo nuevo/cambiado o día > watermark
        R->>DB: INSERT etl.batch (running)
        R->>S: leer CSV / GET paginado (reintentos + backoff)
        R->>Q: limpiar → validar contrato → deduplicar → RI → quality gate
        alt lote válido
            R->>DB: BEGIN; COPY → MERGE en dw; batch=loaded; registro/watermark; COMMIT
        else contrato roto o >5 % filas malas
            R->>DB: batch=rejected + quality_check + rejected_record
        else fuente caída
            R->>DB: batch=failed; watermark NO avanza; se detiene esa fuente
        end
    end
    R->>DB: refrescar marts solo para las fechas tocadas
    R->>DB: UPDATE pipeline_run (success | partial | failed, métricas JSON)
```

## Modelo dimensional

```mermaid
erDiagram
    dim_date ||--o{ fact_sales : date_key
    dim_store ||--o{ fact_sales : store_key
    dim_product ||--o{ fact_sales : product_key
    dim_date ||--o{ fact_inventory_snapshot : date_key
    dim_store ||--o{ fact_inventory_snapshot : store_key
    dim_product ||--o{ fact_inventory_snapshot : product_key

    dim_date {
        int date_key PK "YYYYMMDD"
        date full_date
        text month_name "español"
        text day_name
        bool is_weekend
        bool is_holiday "feriados de Chile"
    }
    dim_store {
        int store_key PK
        text store_code UK "SCD tipo 1"
        text format "Hiper | Super | Express"
        text region
    }
    dim_product {
        int product_key PK
        text sku "SCD tipo 2"
        text category
        numeric list_price
        numeric unit_cost
        date valid_from
        date valid_to
        bool is_current
    }
    fact_sales {
        bigint sales_line_id PK
        text transaction_id "UK con line_number"
        int quantity
        numeric net_amount
        numeric cost_amount "costo de la versión vigente"
        text source_file "dueño de la fila"
    }
    fact_inventory_snapshot {
        int date_key PK
        int store_key PK
        int product_key PK
        int on_hand_qty
        bool is_stockout
        numeric inventory_cost
    }
```

Granos: `fact_sales` = una línea de ticket; `fact_inventory_snapshot` = un producto en una
tienda al cierre de un día (snapshot periódico).

## Decisiones de diseño (ADR)

**ADR-1 · pandas para transformar, SQL para unir y fusionar.** pandas limpia, tipa y
deriva columnas por lote (miles de filas). Las búsquedas de claves sustitutas, el SCD2 y los
upserts se hacen en PostgreSQL con operaciones de conjunto (`COPY` a tabla temporal +
`INSERT … ON CONFLICT`), que son más rápidas, atómicas y verificables que iterar en Python.

**ADR-2 · Contratos propios en lugar de Pandera/Great Expectations.** Un módulo de ~200
líneas (`quality/contracts.py`) cubre tipos, nulos, rangos, patrones, valores permitidos y
reglas entre campos, y registra la *primera* regla rota de cada fila. Evita una dependencia
pesada y es fácil de explicar; migrar a Pandera sería directo porque los contratos son
declarativos.

**ADR-3 · Validación en dos niveles.** Problemas estructurales (columna faltante, CSV
ilegible) rechazan el lote completo; problemas por fila ponen en cuarentena solo esas
filas (`etl.rejected_record`, con los valores originales en JSONB). El *quality gate*
rechaza el lote si más del 5 % (`QUALITY_MAX_REJECT_RATIO`) de las filas son inválidas: un
archivo así suele indicar un problema de la fuente, no ruido.

**ADR-4 · Incrementalidad por tipo de fuente.**
- CSV: registro `(archivo, sha256)` en `etl.processed_file`. Un archivo sin cambios se
  omite; uno re-entregado con otro contenido **reemplaza** las filas que le pertenecen
  (`fact_sales.source_file`), incluso líneas eliminadas.
- API: *watermark* por fecha en `etl.watermark`; se avanza día a día dentro de la misma
  transacción que carga ese día. Si la API cae, el watermark queda en el último día
  confirmado y la siguiente ejecución retoma desde ahí.
- Marts: se recalculan solo las fechas tocadas por la ejecución.

**ADR-5 · Idempotencia.** Toda carga es un upsert por clave natural y cada lote es una
transacción, así que reintentar o usar `--full-refresh` nunca duplica datos (probado).

**ADR-6 · SCD2 en productos, SCD1 en tiendas.** Precio, costo y clasificación cambian y el
margen histórico debe usar el costo vigente en la fecha de venta. Una SKU nueva abre su
primera versión en `1900-01-01` para cubrir ventas cargadas antes que el maestro; un
cambio sin `updated_at` posterior se trata como corrección (sobrescritura).

**ADR-7 · Una ejecución a la vez.** `pg_try_advisory_lock` a nivel de sesión: si el proceso
muere, PostgreSQL libera el lock solo. La API además evita dos disparos simultáneos en el
mismo proceso (HTTP 409).

**ADR-8 · Datos sintéticos deterministas.** Cada número aleatorio depende de
`(semilla, propósito, día, tienda)`, nunca del largo del horizonte; simular 61 días
reproduce los primeros 60 byte a byte. Por eso basta subir `SIM_DAYS` para demostrar la
carga incremental, y los totales son idénticos en Windows y en Linux (Docker).

**ADR-9 · Un módulo de KPI compartido.** `analytics/kpis.py` contiene el SQL de todas las
métricas; la API y el dashboard lo importan, así nunca muestran cifras distintas.

**ADR-10 · Códigos de salida para orquestadores.** `retail run` devuelve 0 (éxito),
2 (parcial: lotes rechazados o fuente caída), 3 (otra ejecución en curso) o 1 (fallo). El
programador incluido (`retail schedule`) es para la demo; en producción se usaría cron,
Airflow o un job programado de Kubernetes.

## Trabajo futuro

- Particionar `fact_sales` por mes cuando supere decenas de millones de filas.
- Reemplazar el *scheduler* de demo por Airflow/Dagster y publicar métricas (Prometheus).
- Reprocesar registros en cuarentena desde la API tras corregirlos.
- dbt para documentar y testear los marts SQL.
