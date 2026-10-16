from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from retail_platform.config import get_settings
from tests.integration.conftest import _test_database_url
from tests.integration.test_api_and_kpis import loaded  # noqa: F401 - shared fixture

APP = Path(__file__).resolve().parents[2] / "src" / "retail_platform" / "dashboard" / "app.py"


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _test_database_url())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_dashboard_renders_kpis_and_tabs(loaded, app_env):  # noqa: F811
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    labels = [m.label for m in at.metric]
    assert {"Venta neta", "Tickets", "Ticket promedio", "Margen bruto"} <= set(labels)
    assert [t.label for t in at.tabs] == ["Ventas", "Inventario", "Calidad de datos"]
    net = next(m for m in at.metric if m.label == "Venta neta")
    assert net.value.startswith("$")


def test_dashboard_filters_rerun_without_errors(loaded, app_env):  # noqa: F811
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    at.multiselect[0].select("ST001").run()
    at.multiselect[1].select("Lácteos").run()
    assert not at.exception, at.exception
    at.selectbox[0].select(at.selectbox[0].options[-1]).run()
    assert not at.exception, at.exception
