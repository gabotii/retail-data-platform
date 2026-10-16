from datetime import date

import httpx
import pytest

from retail_platform.demo_api.app import create_app
from retail_platform.errors import SchemaValidationError, SourceUnavailableError
from retail_platform.ingestion.api_source import InventoryApiClient
from retail_platform.ingestion.csv_source import (
    describe,
    discover_sales_files,
    file_checksum,
    read_raw_csv,
)
from tests.helpers import app_transport

# ---------------------------------------------------------------- CSV landing zone


def test_discover_sorts_by_date_and_skips_unexpected(tmp_path):
    sales = tmp_path / "sales"
    sales.mkdir()
    for name in (
        "sales_2026-07-02.csv",
        "sales_2026-07-01.csv",
        "notes.csv",
        "sales_2026-02-30.csv",
    ):
        (sales / name).write_text("a\n1\n", encoding="utf-8")
    files = discover_sales_files(tmp_path)
    assert [f.name for f in files] == ["sales_2026-07-01.csv", "sales_2026-07-02.csv"]
    assert files[0].business_date == date(2026, 7, 1)


def test_checksum_changes_with_content(tmp_path):
    path = tmp_path / "f.csv"
    path.write_bytes(b"a\n1\n")
    first = file_checksum(path)
    path.write_bytes(b"a\n2\n")
    assert file_checksum(path) != first
    assert describe(path).size_bytes == 4


def test_read_raw_csv_keeps_everything_as_text(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text(" Quantity ,sku\n007,\n", encoding="utf-8")
    df = read_raw_csv(path)
    assert list(df.columns) == ["quantity", "sku"]
    assert df.iloc[0].tolist() == ["007", ""]


@pytest.mark.parametrize("content", [b"", b"a,b\n1,2,3,4\n", b"a\n\xff\xfe\n"])
def test_unreadable_csv_is_schema_error(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_bytes(content)
    with pytest.raises(SchemaValidationError):
        read_raw_csv(path)


# ---------------------------------------------------------------- inventory API client


def make_client(handler, retries=3):
    sleeps: list[float] = []
    client = InventoryApiClient(
        "http://api",
        "t",
        max_retries=retries,
        backoff_seconds=0.01,
        page_size=2,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_retries_transient_errors_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        if calls["n"] == 2:
            return httpx.Response(503)
        if calls["n"] == 3:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json={"min_date": "2026-07-01", "max_date": "2026-07-03"})

    client, sleeps = make_client(handler)
    assert client.available_dates() == (date(2026, 7, 1), date(2026, 7, 3))
    assert client.retries_performed == 3
    assert sleeps[2] == 1.5  # Retry-After honoured
    assert sleeps[1] > sleeps[0]  # exponential backoff


def test_gives_up_after_max_retries():
    client, sleeps = make_client(lambda r: httpx.Response(500), retries=2)
    with pytest.raises(SourceUnavailableError, match="3 attempts"):
        client.available_dates()
    assert len(sleeps) == 2


def test_auth_errors_fail_fast():
    client, sleeps = make_client(lambda r: httpx.Response(401))
    with pytest.raises(SourceUnavailableError, match="credentials"):
        client.available_dates()
    assert sleeps == []


def test_pagination_and_row_count_check():
    rows = [
        {"warehouse_ts": None, "store_code": "ST001", "sku": f"S{i}", "on_hand": i, "on_order": 0}
        for i in range(5)
    ]

    def handler(request):
        page = int(request.url.params["page"])
        size = int(request.url.params["page_size"])
        chunk = rows[(page - 1) * size : page * size]
        return httpx.Response(200, json={"data": chunk, "total": 5, "has_more": page * size < 5})

    client, _ = make_client(handler)
    df = client.fetch_snapshot(date(2026, 7, 1))
    assert len(df) == 5
    assert df["snapshot_date"].unique().tolist() == ["2026-07-01"]


def test_inconsistent_total_is_detected():
    def handler(request):
        return httpx.Response(200, json={"data": [], "total": 3, "has_more": False})

    client, _ = make_client(handler)
    with pytest.raises(SourceUnavailableError, match="announced 3"):
        client.fetch_snapshot(date(2026, 7, 1))


def test_against_demo_api_with_chaos(settings, small_sim, small_world):
    chaotic = settings.model_copy(update={"demo_api_failure_rate": 0.3})
    transport = app_transport(create_app(chaotic, small_sim))
    with InventoryApiClient.from_settings(chaotic, transport=transport) as client:
        client.max_retries = 10
        assert client.available_dates() == (small_sim.start_date, small_sim.end_date)
        df = client.fetch_snapshot(small_sim.start_date)
    assert len(df) == 8 * len(small_world.products)
