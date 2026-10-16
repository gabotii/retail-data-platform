import json
import logging
from datetime import date

from retail_platform.config import Settings, SimulationSettings
from retail_platform.logging_setup import ConsoleFormatter, JsonFormatter, run_id_var


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": msg})
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_extras_and_run_id():
    token = run_id_var.set(7)
    try:
        line = JsonFormatter().format(_record(rows=12, source="sales"))
    finally:
        run_id_var.reset(token)
    payload = json.loads(line)
    assert payload["msg"] == "hello"
    assert payload["run_id"] == 7
    assert payload["rows"] == 12
    assert payload["source"] == "sales"


def test_json_formatter_without_run_id_omits_it():
    payload = json.loads(JsonFormatter().format(_record()))
    assert "run_id" not in payload


def test_console_formatter_is_single_readable_line():
    line = ConsoleFormatter().format(_record("loaded", rows=3))
    assert "loaded" in line
    assert "rows=3" in line


def test_simulation_end_date_is_inclusive():
    sim = SimulationSettings(start_date=date(2026, 1, 30), days=3)
    assert sim.end_date == date(2026, 2, 1)


def test_landing_dir_is_under_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.landing_dir == tmp_path / "landing"
