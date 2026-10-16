"""Deterministic simulation of a supermarket chain: tickets, prices and inventory.

Reproducibility contract: every random draw is keyed by ``(seed, purpose, day, store)``,
never by the length of the horizon. Simulating 61 days therefore reproduces the first
60 days bit for bit, which is what lets the pipeline's incremental loads be demonstrated
by simply extending ``SIM_DAYS``.

Sales are constrained by stock on hand (unsatisfied demand is lost), and stores are
replenished with a reorder-point policy with lead times and occasional supplier delays,
so stockouts appear naturally in the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from retail_platform.calendar_cl import is_holiday
from retail_platform.synthetic.catalog import STORES, build_products, chilean_price, stores_frame

PRICE_PERIOD_DAYS = 30
PRICE_CHANGE_PROBABILITY = 0.12
LEAD_TIME_DAYS = 2
SUPPLIER_DELAY_PROBABILITY = 0.07
SUPPLIER_DELAY_DAYS = 5
SAFETY_DAYS = 3
ORDER_UP_TO_DAYS = 12
MEAN_EXTRA_LINES = 2.2
MEAN_EXTRA_UNITS = 0.4
PROMO_PROBABILITY = 0.12
PROMO_DISCOUNTS = np.array([0.10, 0.15, 0.20, 0.30])

WEEKDAY_FACTOR = np.array([0.85, 0.90, 0.95, 1.00, 1.20, 1.35, 1.10])
OPEN_HOURS = np.arange(9, 22)
HOUR_WEIGHTS = np.array([3, 4, 5, 7, 8, 6, 5, 6, 8, 10, 11, 9, 6], dtype=float)
PAYMENT_METHODS = np.array(["Débito", "Crédito", "Efectivo", "Billetera digital"])
PAYMENT_WEIGHTS = np.array([0.45, 0.25, 0.18, 0.12])

# Purpose tags keep the random streams independent of one another.
_RNG_STORE_MIX, _RNG_INITIAL_STOCK, _RNG_PRICES, _RNG_DELAYS, _RNG_TICKETS = 2, 3, 4, 5, 10


@dataclass(frozen=True)
class World:
    start_date: date
    days: int
    stores: pd.DataFrame
    products: pd.DataFrame
    price_history: pd.DataFrame
    sales: pd.DataFrame
    inventory: pd.DataFrame

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.days - 1)

    def products_as_of(self, as_of: date) -> pd.DataFrame:
        """Product master as the source system would export it on ``as_of``."""
        history = self.price_history[self.price_history["effective_from"] <= as_of]
        current = history.sort_values("effective_from").groupby("sku", as_index=False).last()
        master = self.products.drop(columns=["list_price", "unit_cost", "popularity"])
        master = master.merge(current, on="sku", how="inner")
        return master.rename(columns={"effective_from": "updated_at"})


def _price_history(products: pd.DataFrame, seed: int, start: date, days: int) -> pd.DataFrame:
    base_price = products["list_price"].to_numpy(dtype=float)
    base_cost = products["unit_cost"].to_numpy(dtype=float)
    factor = np.ones(len(products))
    frames = [
        pd.DataFrame(
            {
                "sku": products["sku"],
                "effective_from": start,
                "list_price": base_price.astype(int),
                "unit_cost": base_cost.astype(int),
            }
        )
    ]
    for period in range(1, (days - 1) // PRICE_PERIOD_DAYS + 1):
        rng = np.random.default_rng([seed, _RNG_PRICES, period])
        changed = rng.random(len(products)) < PRICE_CHANGE_PROBABILITY
        bump = 1 + rng.uniform(0.03, 0.08, len(products))
        factor = np.where(changed, factor * bump, factor)
        if not changed.any():
            continue
        frames.append(
            pd.DataFrame(
                {
                    "sku": products["sku"][changed],
                    "effective_from": start + timedelta(days=period * PRICE_PERIOD_DAYS),
                    "list_price": [chilean_price(p) for p in (base_price * factor)[changed]],
                    "unit_cost": np.round((base_cost * factor)[changed]).astype(int),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _daily_prices(history: pd.DataFrame, skus: pd.Series, day: date) -> np.ndarray:
    valid = history[history["effective_from"] <= day]
    latest = valid.sort_values("effective_from").groupby("sku")["list_price"].last()
    return latest.reindex(skus).to_numpy(dtype=np.int64)


def _store_ticket_lines(
    rng: np.random.Generator, day: date, store_idx: int, expected_tickets: float, probs: np.ndarray
) -> pd.DataFrame:
    n_tickets = int(rng.poisson(expected_tickets))
    lines_per_ticket = 1 + rng.poisson(MEAN_EXTRA_LINES, n_tickets)
    seconds = np.sort(
        rng.choice(OPEN_HOURS, n_tickets, p=HOUR_WEIGHTS / HOUR_WEIGHTS.sum()) * 3600
        + rng.integers(0, 3600, n_tickets)
    )
    payment = rng.choice(len(PAYMENT_METHODS), n_tickets, p=PAYMENT_WEIGHTS)
    ticket = np.repeat(np.arange(n_tickets), lines_per_ticket)
    n_lines = len(ticket)
    store_code = STORES[store_idx].code
    return pd.DataFrame(
        {
            "ticket": ticket,
            "store_code": store_code,
            "product_idx": rng.choice(len(probs), n_lines, p=probs),
            "quantity": 1 + np.minimum(rng.poisson(MEAN_EXTRA_UNITS, n_lines), 5),
            "seconds": seconds[ticket],
            "payment_method": PAYMENT_METHODS[payment[ticket]],
            "promo_pct": np.where(
                rng.random(n_lines) < PROMO_PROBABILITY, rng.choice(PROMO_DISCOUNTS, n_lines), 0.0
            ),
        }
    )


def simulate(seed: int, start_date: date, days: int, traffic_scale: float = 0.5) -> World:
    products = build_products(seed)
    stores = stores_frame()
    n_stores, n_products = len(STORES), len(products)
    skus = products["sku"]
    history = _price_history(products, seed, start_date, days)

    # Each store has its own taste: a fixed multiplicative perturbation of popularity.
    popularity = products["popularity"].to_numpy()
    probs = np.empty((n_stores, n_products))
    for s in range(n_stores):
        mix = np.random.default_rng([seed, _RNG_STORE_MIX, s]).lognormal(0, 0.25, n_products)
        weights = popularity * mix
        probs[s] = weights / weights.sum()

    base_tickets = np.array([st.base_traffic for st in STORES]) * traffic_scale
    mean_units_per_ticket = (1 + MEAN_EXTRA_LINES) * (1 + MEAN_EXTRA_UNITS)
    expected_daily_units = (
        base_tickets[:, None] * WEEKDAY_FACTOR.mean() * mean_units_per_ticket * probs
    )
    reorder_point = expected_daily_units * (LEAD_TIME_DAYS + SAFETY_DAYS)
    order_up_to = expected_daily_units * ORDER_UP_TO_DAYS

    stock_rng = np.random.default_rng([seed, _RNG_INITIAL_STOCK])
    on_hand = np.ceil(expected_daily_units * stock_rng.uniform(6, 14, (n_stores, n_products)))
    on_hand = on_hand.astype(np.int64)
    pending: dict[int, np.ndarray] = {}

    sales_frames: list[pd.DataFrame] = []
    inventory_frames: list[pd.DataFrame] = []
    for i in range(days):
        day = start_date + timedelta(days=i)
        on_hand += pending.pop(i, 0)
        prices = _daily_prices(history, skus, day)
        demand_factor = WEEKDAY_FACTOR[day.weekday()] * (1 + 0.002 * i)
        if is_holiday(day):
            demand_factor *= 0.7
        elif is_holiday(day + timedelta(days=1)):
            demand_factor *= 1.25

        for s in range(n_stores):
            rng = np.random.default_rng([seed, _RNG_TICKETS, day.toordinal(), s])
            lines = _store_ticket_lines(rng, day, s, base_tickets[s] * demand_factor, probs[s])
            # Serve tickets in time order; once a product runs out, later demand is lost.
            cumulative = lines.groupby("product_idx")["quantity"].cumsum().to_numpy()
            lines = lines[cumulative <= on_hand[s, lines["product_idx"].to_numpy()]]
            on_hand[s] -= np.bincount(
                lines["product_idx"], weights=lines["quantity"], minlength=n_products
            ).astype(np.int64)
            sales_frames.append(_finalize_lines(lines, day, skus, prices))

        position = on_hand + sum(pending.values(), np.zeros_like(on_hand))
        order_qty = np.where(position < reorder_point, np.ceil(order_up_to - position), 0)
        if order_qty.any():
            delay_rng = np.random.default_rng([seed, _RNG_DELAYS, day.toordinal()])
            delayed = delay_rng.random(order_qty.shape) < SUPPLIER_DELAY_PROBABILITY
            for extra in (0, SUPPLIER_DELAY_DAYS):
                mask = delayed if extra else ~delayed
                arrival = i + 1 + LEAD_TIME_DAYS + extra
                qty = np.where(mask, order_qty, 0).astype(np.int64)
                pending[arrival] = pending.get(arrival, np.zeros_like(on_hand)) + qty

        on_order = sum(pending.values(), np.zeros_like(on_hand))
        inventory_frames.append(
            pd.DataFrame(
                {
                    "snapshot_date": day,
                    "store_code": np.repeat([st.code for st in STORES], n_products),
                    "sku": np.tile(skus.to_numpy(), n_stores),
                    "on_hand": on_hand.ravel().copy(),
                    "on_order": on_order.ravel(),
                }
            )
        )

    return World(
        start_date=start_date,
        days=days,
        stores=stores,
        products=products,
        price_history=history,
        sales=pd.concat(sales_frames, ignore_index=True),
        inventory=pd.concat(inventory_frames, ignore_index=True),
    )


def _finalize_lines(
    lines: pd.DataFrame, day: date, skus: pd.Series, prices: np.ndarray
) -> pd.DataFrame:
    product_idx = lines["product_idx"].to_numpy()
    unit_price = prices[product_idx]
    quantity = lines["quantity"].to_numpy()
    ticket_seq = (lines["ticket"] + 1).astype(str).str.zfill(5)
    stamp = day.strftime("%Y%m%d")
    return pd.DataFrame(
        {
            "transaction_id": lines["store_code"] + "-" + stamp + "-" + ticket_seq,
            "line_number": lines.groupby("ticket").cumcount().to_numpy() + 1,
            "sold_at": pd.Timestamp(day) + pd.to_timedelta(lines["seconds"].to_numpy(), unit="s"),
            "store_code": lines["store_code"].to_numpy(),
            "sku": skus.to_numpy()[product_idx],
            "quantity": quantity,
            "unit_price": unit_price,
            "discount_amount": np.round(
                quantity * unit_price * lines["promo_pct"].to_numpy()
            ).astype(np.int64),
            "payment_method": lines["payment_method"].to_numpy(),
        }
    )


@lru_cache(maxsize=4)
def simulate_cached(seed: int, start_date: date, days: int, traffic_scale: float) -> World:
    """Shared, memoised world for the demo API (simulation takes a few seconds)."""
    return simulate(seed, start_date, days, traffic_scale)
