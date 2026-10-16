from __future__ import annotations

import pytest
from sqlalchemy import text

from retail_platform import cli
from retail_platform.config import get_settings, get_simulation_settings
from retail_platform.pipeline.runner import PIPELINE_LOCK_KEY
from tests.integration.conftest import _test_database_url, count


@pytest.fixture
def cli_env(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _test_database_url())
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEMO_API_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setenv("HTTP_MAX_RETRIES", "0")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    get_simulation_settings.cache_clear()
    yield db
    get_settings.cache_clear()
    get_simulation_settings.cache_clear()


def test_generate_migrate_run_and_exit_codes(cli_env, tmp_path, capsys):
    gen = ["generate", "--days", "2", "--traffic-scale", "0.02", "--seed", "3", "--dirty-rate", "0"]
    assert cli.main(gen) == cli.EXIT_OK
    assert "2 sales files written" in capsys.readouterr().out
    assert len(list((tmp_path / "data" / "landing" / "sales").glob("*.csv"))) == 2

    assert cli.main(["migrate"]) == cli.EXIT_OK
    assert cli.main(["run", "--only", "master", "sales"]) == cli.EXIT_OK
    assert count(cli_env, "SELECT count(*) FROM dw.fact_sales") > 0

    # inventory API unreachable -> partial run -> exit code 2, sales untouched
    assert cli.main(["run"]) == cli.EXIT_PARTIAL

    with cli_env.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": PIPELINE_LOCK_KEY})
        assert cli.main(["run"]) == cli.EXIT_LOCKED
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PIPELINE_LOCK_KEY})


def test_command_crash_returns_failure(cli_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1:1/z?connect_timeout=1")
    get_settings.cache_clear()
    assert cli.main(["migrate"]) == cli.EXIT_FAILED


def test_parser_rejects_unknown_source():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--only", "weather"])
