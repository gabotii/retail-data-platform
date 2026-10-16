"""Static reference data of the fictitious supermarket chain "Mercado Austral".

Store and product names are invented; prices are in Chilean pesos (CLP).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Store:
    code: str
    name: str
    city: str
    region: str
    format: str
    opened_on: date
    size_m2: int
    base_traffic: int  # expected tickets on an average weekday


STORES: tuple[Store, ...] = (
    Store("ST001", "Mercado Austral Providencia", "Santiago", "Metropolitana", "Hiper",
          date(2012, 3, 15), 9800, 420),
    Store("ST002", "Mercado Austral Las Condes", "Santiago", "Metropolitana", "Super",
          date(2015, 8, 1), 4200, 300),
    Store("ST003", "Mercado Austral Maipú", "Santiago", "Metropolitana", "Hiper",
          date(2010, 11, 20), 11000, 450),
    Store("ST004", "Mercado Austral Viña del Mar", "Viña del Mar", "Valparaíso", "Super",
          date(2016, 5, 10), 3900, 260),
    Store("ST005", "Mercado Austral Concepción", "Concepción", "Biobío", "Hiper",
          date(2013, 9, 2), 8700, 380),
    Store("ST006", "Mercado Austral La Serena", "La Serena", "Coquimbo", "Super",
          date(2018, 4, 23), 3500, 220),
    Store("ST007", "Mercado Austral Express Temuco", "Temuco", "Araucanía", "Express",
          date(2021, 1, 18), 900, 140),
    Store("ST008", "Mercado Austral Express Antofagasta", "Antofagasta", "Antofagasta",
          "Express", date(2022, 6, 6), 850, 160),
)  # fmt: skip

# (category, subcategory, item, min_price, max_price, gross_margin, base_popularity)
_ITEMS: tuple[tuple[str, str, str, int, int, float, float], ...] = (
    ("Lácteos", "Leches", "Leche entera 1 L", 990, 1390, 0.18, 9.0),
    ("Lácteos", "Leches", "Leche descremada 1 L", 1050, 1450, 0.18, 5.0),
    ("Lácteos", "Yogures", "Yogur batido 120 g", 290, 490, 0.30, 7.0),
    ("Lácteos", "Quesos", "Queso gauda laminado 250 g", 2990, 4290, 0.27, 4.0),
    ("Lácteos", "Mantequillas", "Mantequilla con sal 250 g", 2490, 3290, 0.24, 3.0),
    ("Abarrotes", "Arroz y legumbres", "Arroz grado 1 1 kg", 1290, 1890, 0.20, 6.0),
    ("Abarrotes", "Arroz y legumbres", "Lentejas 1 kg", 1990, 2790, 0.22, 2.5),
    ("Abarrotes", "Pastas", "Fideos spaghetti 400 g", 790, 1290, 0.25, 6.0),
    ("Abarrotes", "Aceites", "Aceite maravilla 1 L", 2390, 3290, 0.15, 4.0),
    ("Abarrotes", "Endulzantes", "Azúcar granulada 1 kg", 1190, 1590, 0.14, 4.0),
    ("Abarrotes", "Desayuno", "Café instantáneo 170 g", 4990, 6990, 0.28, 3.0),
    ("Abarrotes", "Desayuno", "Té 100 bolsitas", 2490, 3490, 0.30, 2.5),
    ("Bebidas", "Gaseosas", "Bebida cola 1,5 L", 1590, 2190, 0.25, 8.0),
    ("Bebidas", "Aguas", "Agua mineral sin gas 1,6 L", 790, 1190, 0.35, 6.0),
    ("Bebidas", "Jugos", "Jugo néctar durazno 1,5 L", 1290, 1790, 0.30, 4.0),
    ("Bebidas", "Cervezas", "Cerveza lager lata 470 cc", 990, 1490, 0.22, 5.0),
    ("Carnes", "Pollo", "Pechuga de pollo 1 kg", 4990, 6490, 0.20, 5.0),
    ("Carnes", "Vacuno", "Carne molida 5% grasa 500 g", 4290, 5990, 0.21, 4.0),
    ("Carnes", "Cerdo", "Chuleta de cerdo 1 kg", 5490, 7290, 0.21, 2.5),
    ("Frutas y Verduras", "Frutas", "Plátano 1 kg", 990, 1490, 0.32, 7.0),
    ("Frutas y Verduras", "Frutas", "Manzana roja 1 kg", 1190, 1790, 0.33, 5.0),
    ("Frutas y Verduras", "Verduras", "Palta hass 1 kg", 3990, 5990, 0.30, 5.0),
    ("Frutas y Verduras", "Verduras", "Tomate 1 kg", 1290, 2190, 0.34, 6.0),
    ("Frutas y Verduras", "Verduras", "Papa malla 2 kg", 1990, 2790, 0.31, 4.0),
    ("Panadería", "Pan", "Marraqueta 1 kg", 1990, 2590, 0.40, 9.0),
    ("Panadería", "Pan", "Pan de molde 600 g", 2190, 2990, 0.33, 4.0),
    ("Limpieza", "Lavado", "Detergente líquido 3 L", 7990, 10990, 0.26, 2.5),
    ("Limpieza", "Cocina", "Lavaloza 750 ml", 1590, 2290, 0.30, 3.0),
    ("Limpieza", "Papeles", "Papel higiénico 12 rollos", 5990, 8490, 0.24, 3.5),
    ("Cuidado Personal", "Cabello", "Shampoo 400 ml", 2990, 4490, 0.36, 2.0),
    ("Cuidado Personal", "Higiene bucal", "Pasta dental 90 g", 1290, 2190, 0.38, 2.5),
    ("Snacks", "Salados", "Papas fritas 230 g", 1990, 2790, 0.35, 4.5),
    ("Snacks", "Dulces", "Galletas de chocolate 120 g", 690, 1090, 0.34, 5.0),
    ("Snacks", "Dulces", "Chocolate de leche 100 g", 1290, 1990, 0.36, 3.5),
    ("Congelados", "Postres", "Helado de vainilla 1 L", 2990, 4290, 0.33, 2.5),
    ("Congelados", "Vegetales", "Papas prefritas 1 kg", 2490, 3290, 0.29, 2.5),
)

_BRANDS: dict[str, tuple[str, ...]] = {
    "Lácteos": ("Campo Sureño", "Valle Lechero", "Austral Selección"),
    "Abarrotes": ("Granero Andino", "La Despensa", "Austral Selección"),
    "Bebidas": ("Cordillera", "Fresca Vida", "Austral Selección"),
    "Carnes": ("Fundo Los Robles", "Austral Selección"),
    "Frutas y Verduras": ("Huerto del Maule", "Austral Selección"),
    "Panadería": ("Horno Austral",),
    "Limpieza": ("Brillo Total", "Hogar Limpio", "Austral Selección"),
    "Cuidado Personal": ("Natural Andes", "Dermacare"),
    "Snacks": ("Crujiente", "Dulce Sur", "Austral Selección"),
    "Congelados": ("Polar Sur", "Austral Selección"),
}

PRIVATE_LABEL = "Austral Selección"


def chilean_price(value: float) -> int:
    """Round to the typical "x.x90" retail price ending (e.g. 1290, 4990)."""
    return max(90, round(value / 100) * 100 - 10)


def build_products(seed: int) -> pd.DataFrame:
    """Return the product master with one row per SKU (deterministic for a seed)."""
    rng = np.random.default_rng([seed, 0])
    rows = []
    sku_number = 1
    for category, subcategory, item, lo, hi, margin, popularity in _ITEMS:
        brands = _BRANDS[category]
        count = int(rng.integers(1, len(brands) + 1))
        chosen = rng.choice(len(brands), size=count, replace=False)
        for brand_idx in sorted(int(i) for i in chosen):
            brand = brands[brand_idx]
            price_factor = 0.88 if brand == PRIVATE_LABEL else 1.0
            price = chilean_price(rng.uniform(lo, hi) * price_factor)
            item_margin = float(np.clip(margin + rng.normal(0, 0.03), 0.08, 0.5))
            rows.append(
                {
                    "sku": f"SKU-{sku_number:06d}",
                    "product_name": f"{item} {brand}",
                    "category": category,
                    "subcategory": subcategory,
                    "brand": brand,
                    "list_price": price,
                    "unit_cost": round(price * (1 - item_margin)),
                    "popularity": popularity * float(rng.lognormal(0, 0.35)),
                }
            )
            sku_number += 1
    return pd.DataFrame(rows)


def stores_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "store_code": s.code,
                "store_name": s.name,
                "city": s.city,
                "region": s.region,
                "format": s.format,
                "opened_on": s.opened_on,
                "size_m2": s.size_m2,
            }
            for s in STORES
        ]
    )
