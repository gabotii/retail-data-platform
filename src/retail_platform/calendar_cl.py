"""Chilean calendar helpers shared by the simulator and ``dim_date``.

The holiday list is simplified: fixed-date national holidays plus Good Friday and
Holy Saturday. Rules that move some holidays to Mondays are intentionally ignored.
"""

from __future__ import annotations

from datetime import date, timedelta

_FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "Año Nuevo",
    (5, 1): "Día del Trabajo",
    (5, 21): "Día de las Glorias Navales",
    (6, 20): "Día de los Pueblos Indígenas",
    (6, 29): "San Pedro y San Pablo",
    (7, 16): "Virgen del Carmen",
    (8, 15): "Asunción de la Virgen",
    (9, 18): "Independencia Nacional",
    (9, 19): "Glorias del Ejército",
    (10, 12): "Encuentro de Dos Mundos",
    (10, 31): "Iglesias Evangélicas",
    (11, 1): "Todos los Santos",
    (12, 8): "Inmaculada Concepción",
    (12, 25): "Navidad",
}

MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip
WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def holiday_name(day: date) -> str | None:
    fixed = _FIXED_HOLIDAYS.get((day.month, day.day))
    if fixed:
        return fixed
    easter = easter_sunday(day.year)
    if day == easter - timedelta(days=2):
        return "Viernes Santo"
    if day == easter - timedelta(days=1):
        return "Sábado Santo"
    return None


def is_holiday(day: date) -> bool:
    return holiday_name(day) is not None
