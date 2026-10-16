"""Streamlit dashboard: commercial and inventory KPIs plus data-quality monitoring.

Run with ``streamlit run src/retail_platform/dashboard/app.py``. All numbers come from
``retail_platform.analytics.kpis``, the same module that backs the REST API.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import Engine

from retail_platform.analytics import kpis
from retail_platform.config import get_settings
from retail_platform.warehouse.db import make_engine

st.set_page_config(page_title="Retail KPIs · Mercado Austral", page_icon="🛒", layout="wide")

# Single-series charts use categorical slot 1; the heatmap uses the blue sequential ramp.
# Light/dark steps come from the validated reference palette.
_DARK = getattr(getattr(st.context, "theme", None), "type", None) == "dark"
SERIES = "#3987e5" if _DARK else "#2a78d6"
SEQUENTIAL = (
    ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"]
    if _DARK
    else ["#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#0d366b"]
)
GRID = "#383835" if _DARK else "#e6e5e0"
CATEGORY_LABELS = {
    "DEBIT": "Débito",
    "CREDIT": "Crédito",
    "CASH": "Efectivo",
    "DIGITAL_WALLET": "Billetera digital",
}


# --------------------------------------------------------------------------- helpers
def clp(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return "$" + f"{value:,.0f}".replace(",", ".")


def compact_clp(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    if abs(value) >= 1e6:
        return (
            "$"
            + f"{value / 1e6:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
            + " MM"
        )
    return clp(value)


def pct(value: float | None, digits: int = 1) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value * 100:.{digits}f} %".replace(".", ",")


def delta_pct(value: float | None) -> str | None:
    return None if value is None else f"{value * 100:+.1f} % vs periodo anterior".replace(".", ",")


@st.cache_resource
def engine() -> Engine:
    return make_engine(get_settings().database_url)


def _flt(start: date, end: date, stores: tuple[str, ...], cats: tuple[str, ...]) -> kpis.KpiFilter:
    return kpis.KpiFilter(start, end, stores, cats)


@st.cache_data(ttl=60, show_spinner=False)
def load(name: str, start: date, end: date, stores: tuple[str, ...], cats: tuple[str, ...]) -> Any:
    flt = _flt(start, end, stores, cats)
    fn = {
        "summary": kpis.sales_summary,
        "daily": kpis.daily_sales,
        "category": kpis.sales_by_category,
        "store": kpis.sales_by_store,
        "payments": kpis.payment_mix,
        "heatmap": kpis.sales_heatmap,
        "inventory": kpis.inventory_health,
        "risk": kpis.inventory_at_risk,
        "stockouts": kpis.stockout_trend,
        "top": kpis.top_products,
    }[name]
    return fn(engine(), flt)


@st.cache_data(ttl=60, show_spinner=False)
def options() -> dict[str, Any]:
    return kpis.filter_options(engine())


# Axis labels in Chilean style: "12 MM" for millions, "%d/%m" for dates (no English months).
MONEY_LABELS = (
    "abs(datum.value) >= 1e6 ? format(datum.value / 1e6, ',.0f') + ' MM'"
    " : abs(datum.value) >= 1e3 ? format(datum.value / 1e3, ',.0f') + ' mil'"
    " : format(datum.value, ',.0f')"
)


def axis(
    title: str | None = None,
    fmt: str | None = None,
    grid: bool = True,
    label_expr: str | None = None,
) -> alt.Axis:
    extra: dict[str, Any] = {"format": fmt, "labelExpr": label_expr}
    return alt.Axis(
        title=title,
        grid=grid,
        gridColor=GRID,
        domain=False,
        ticks=False,
        labelPadding=6,
        **{k: v for k, v in extra.items() if v is not None},
    )


def money_axis(title: str | None) -> alt.Axis:
    return axis(title, label_expr=MONEY_LABELS)


def date_axis() -> alt.Axis:
    return axis(None, "%d/%m", grid=False)


def hbar(df: pd.DataFrame, y: str, x: str, x_title: str, tooltip: list[alt.Tooltip]) -> alt.Chart:
    """Horizontal single-series bar chart, sorted by value, thin bars with rounded data end."""
    height = max(160, 30 * len(df))
    return (
        alt.Chart(df, height=height)
        .mark_bar(color=SERIES, cornerRadiusEnd=4, size=16)
        .encode(
            x=alt.X(f"{x}:Q", axis=money_axis(x_title)),
            y=alt.Y(
                f"{y}:N",
                sort="-x",
                axis=alt.Axis(title=None, domain=False, ticks=False, labelLimit=240),
            ),
            tooltip=tooltip,
        )
    )


# --------------------------------------------------------------------------- layout
st.title("Mercado Austral · KPIs de retail")
opts = options()
if opts["max_date"] is None:
    st.info("Aún no hay datos cargados. Ejecuta `retail generate` y luego `retail run`.")
    st.stop()

min_d, max_d = opts["min_date"], opts["max_date"]
store_names = {s["store_code"]: f"{s['store_code']} · {s['store_name']}" for s in opts["stores"]}

# Filters live in one row above the charts.
col_range, col_store, col_cat = st.columns([1.2, 2, 2])
with col_range:
    picked = st.date_input(
        "Periodo",
        value=(max(min_d, max_d - timedelta(days=29)), max_d),
        min_value=min_d,
        max_value=max_d,
        format="DD/MM/YYYY",
    )
with col_store:
    stores = st.multiselect(
        "Tiendas",
        list(store_names),
        format_func=lambda code: store_names[code],
        placeholder="Todas",
    )
with col_cat:
    cats = st.multiselect("Categorías", opts["categories"], placeholder="Todas")

if not isinstance(picked, tuple) or len(picked) != 2:
    st.warning("Selecciona fecha de inicio y de término.")
    st.stop()
start, end = picked
args = (start, end, tuple(stores), tuple(cats))

summary = load("summary", *args)
cur, chg = summary["current"], summary["change"]
inv = load("inventory", *args)

tiles = st.columns(6)
tiles[0].metric("Venta neta", compact_clp(cur["net_sales"]), delta_pct(chg["net_sales"]))
tiles[1].metric(
    "Tickets", f"{cur['transactions']:,.0f}".replace(",", "."), delta_pct(chg["transactions"])
)
tiles[2].metric("Ticket promedio", clp(cur["avg_ticket"]), delta_pct(chg["avg_ticket"]))
pp = chg["gross_margin_pct_points"]
tiles[3].metric(
    "Margen bruto",
    pct(cur["gross_margin_pct"]),
    None if pp is None else f"{pp:+.1f} pp".replace(".", ","),
)
tiles[4].metric(
    "Quiebre de stock",
    pct(inv["stockout_rate"]),
    help=f"Posiciones tienda×SKU sin stock al {inv['snapshot_date']}",
)
tiles[5].metric(
    "Días de inventario",
    "—" if inv["days_of_supply"] is None else f"{inv['days_of_supply']:.1f}".replace(".", ","),
    help="Unidades en stock / venta diaria promedio de los últimos 28 días",
)
st.caption(
    f"Periodo {start:%d/%m/%Y}–{end:%d/%m/%Y} comparado con "
    f"{summary['previous_period']['start']:%d/%m/%Y}–{summary['previous_period']['end']:%d/%m/%Y}. "
    "Montos en CLP."
)

tab_sales, tab_inventory, tab_quality = st.tabs(["Ventas", "Inventario", "Calidad de datos"])

# --------------------------------------------------------------------------- sales tab
with tab_sales:
    daily = load("daily", *args)
    daily["day"] = pd.to_datetime(daily["day"])
    hover = alt.selection_point(fields=["day"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(daily, height=260).encode(x=alt.X("day:T", axis=date_axis()))
    line = base.mark_line(color=SERIES, strokeWidth=2).encode(
        y=alt.Y("net_sales:Q", axis=money_axis("Venta neta diaria (CLP)"))
    )
    rule = (
        base.mark_rule(color=GRID, strokeWidth=1)
        .encode(
            opacity=alt.condition(hover, alt.value(1), alt.value(0)),
            tooltip=[
                alt.Tooltip("day:T", title="Día", format="%A %d/%m"),
                alt.Tooltip("net_sales:Q", title="Venta neta", format=",.0f"),
                alt.Tooltip("gross_margin:Q", title="Margen", format=",.0f"),
                alt.Tooltip("units:Q", title="Unidades", format=",.0f"),
            ],
        )
        .add_params(hover)
    )
    points = line.mark_point(color=SERIES, size=64, filled=True).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    st.subheader("Venta neta diaria")
    st.altair_chart(line + rule + points, width="stretch")

    left, right = st.columns(2)
    with left:
        st.subheader("Venta por categoría")
        by_cat = load("category", *args)
        st.altair_chart(
            hbar(
                by_cat,
                "category",
                "net_sales",
                "Venta neta (CLP)",
                [
                    alt.Tooltip("category:N", title="Categoría"),
                    alt.Tooltip("net_sales:Q", title="Venta neta", format=",.0f"),
                    alt.Tooltip("share:Q", title="Participación", format=".1%"),
                    alt.Tooltip("gross_margin_pct:Q", title="Margen", format=".1%"),
                ],
            ),
            width="stretch",
        )
    with right:
        st.subheader("Venta por tienda")
        by_store = load("store", *args)
        by_store["store_name"] = by_store["store_name"].str.replace("Mercado Austral ", "")
        st.altair_chart(
            hbar(
                by_store,
                "store_name",
                "net_sales",
                "Venta neta (CLP)",
                [
                    alt.Tooltip("store_name:N", title="Tienda"),
                    alt.Tooltip("format:N", title="Formato"),
                    alt.Tooltip("net_sales:Q", title="Venta neta", format=",.0f"),
                    alt.Tooltip("gross_margin_pct:Q", title="Margen", format=".1%"),
                ],
            ),
            width="stretch",
        )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Venta por día y hora")
        heat = load("heatmap", *args)
        st.altair_chart(
            alt.Chart(heat, height=alt.Step(28))
            .mark_rect(cornerRadius=2)
            .encode(
                x=alt.X(
                    "hour:O", title="Hora", axis=alt.Axis(labelAngle=0, ticks=False, domain=False)
                ),
                y=alt.Y(
                    "day_name:N",
                    title=None,
                    sort=["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
                    axis=alt.Axis(ticks=False, domain=False),
                ),
                color=alt.Color(
                    "net_sales:Q",
                    title="Venta neta",
                    scale=alt.Scale(range=SEQUENTIAL),
                    legend=alt.Legend(orient="bottom", labelExpr=MONEY_LABELS),
                ),
                tooltip=[
                    alt.Tooltip("day_name:N", title="Día"),
                    alt.Tooltip("hour:O", title="Hora"),
                    alt.Tooltip("net_sales:Q", title="Venta neta", format=",.0f"),
                ],
            ),
            width="stretch",
        )
    with right:
        st.subheader("Medios de pago")
        pay = load("payments", *args)
        pay["label"] = pay["payment_method"].map(CATEGORY_LABELS).fillna(pay["payment_method"])
        st.altair_chart(
            hbar(
                pay,
                "label",
                "share",
                "Participación en venta neta",
                [
                    alt.Tooltip("label:N", title="Medio"),
                    alt.Tooltip("share:Q", title="Participación", format=".1%"),
                    alt.Tooltip("transactions:Q", title="Tickets", format=",.0f"),
                ],
            ).encode(x=alt.X("share:Q", axis=axis("Participación en venta neta", ".0%"))),
            width="stretch",
        )

    st.subheader("Productos más vendidos")
    top = load("top", *args)
    st.dataframe(
        top.rename(
            columns={
                "sku": "SKU",
                "product_name": "Producto",
                "category": "Categoría",
                "brand": "Marca",
                "net_sales": "Venta neta (CLP)",
                "units": "Unidades",
                "gross_margin": "Margen (CLP)",
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Venta neta (CLP)": st.column_config.NumberColumn(format="localized"),
            "Unidades": st.column_config.NumberColumn(format="localized"),
            "Margen (CLP)": st.column_config.NumberColumn(format="localized"),
        },
    )

# --------------------------------------------------------------------------- inventory tab
with tab_inventory:
    a, b, c = st.columns(3)
    a.metric("Valor del inventario (costo)", compact_clp(inv["inventory_cost"]))
    b.metric("Unidades en stock", f"{inv['on_hand_units']:,.0f}".replace(",", "."))
    c.metric("Posiciones sin stock", f"{inv['stockouts']} de {inv['positions']}")

    trend = load("stockouts", *args)
    if not trend.empty:
        trend["day"] = pd.to_datetime(trend["day"])
        st.subheader("Tasa de quiebre de stock diaria")
        st.altair_chart(
            alt.Chart(trend, height=220)
            .mark_line(color=SERIES, strokeWidth=2, point=alt.OverlayMarkDef(size=36, color=SERIES))
            .encode(
                x=alt.X("day:T", axis=date_axis()),
                y=alt.Y("stockout_rate:Q", axis=axis("Posiciones sin stock", ".1%")),
                tooltip=[
                    alt.Tooltip("day:T", title="Día", format="%d/%m"),
                    alt.Tooltip("stockout_rate:Q", title="Quiebre", format=".2%"),
                    alt.Tooltip("inventory_cost:Q", title="Inventario (costo)", format=",.0f"),
                ],
            ),
            width="stretch",
        )

    st.subheader("Productos en riesgo (menos de 3 días de cobertura)")
    risk = load("risk", *args)
    if risk.empty:
        st.success("Ninguna posición bajo 3 días de cobertura.")
    else:
        st.dataframe(
            risk.rename(
                columns={
                    "store_code": "Tienda",
                    "store_name": "Nombre tienda",
                    "sku": "SKU",
                    "product_name": "Producto",
                    "category": "Categoría",
                    "on_hand_qty": "Stock",
                    "on_order_qty": "En tránsito",
                    "avg_daily_units": "Venta diaria",
                    "days_of_supply": "Días de cobertura",
                }
            ),
            hide_index=True,
            width="stretch",
        )

# --------------------------------------------------------------------------- quality tab
with tab_quality:
    runs = kpis.recent_runs(engine(), limit=15)
    st.subheader("Últimas ejecuciones del pipeline")
    st.dataframe(
        runs.rename(
            columns={
                "run_id": "Ejecución",
                "started_at": "Inicio",
                "finished_at": "Fin",
                "status": "Estado",
                "trigger": "Origen",
                "full_refresh": "Recarga completa",
                "error": "Error",
                "batches_loaded": "Lotes cargados",
                "batches_with_errors": "Lotes con error",
                "rows_loaded": "Filas cargadas",
                "rows_rejected": "Filas rechazadas",
            }
        ),
        hide_index=True,
        width="stretch",
    )

    left, right = st.columns([2, 3])
    rules = kpis.rejections_by_rule(engine())
    with left:
        st.subheader("Filas en cuarentena por regla")
        if rules.empty:
            st.success("Sin filas rechazadas.")
        else:
            rules["regla"] = rules["source"] + " · " + rules["rule"]
            st.altair_chart(
                hbar(
                    rules,
                    "regla",
                    "rows",
                    "Filas rechazadas",
                    [
                        alt.Tooltip("regla:N", title="Regla"),
                        alt.Tooltip("rows:Q", title="Filas", format=",.0f"),
                    ],
                ),
                width="stretch",
            )
    with right:
        st.subheader("Muestras de registros rechazados")
        rule_filter = st.selectbox(
            "Regla",
            ["(todas)", *sorted(rules["rule"].unique())] if not rules.empty else ["(todas)"],
        )
        samples = kpis.rejected_samples(
            engine(), None if rule_filter == "(todas)" else rule_filter, limit=25
        )
        st.dataframe(samples, hide_index=True, width="stretch")
