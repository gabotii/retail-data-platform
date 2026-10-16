import random

import pytest
from fastapi.testclient import TestClient

from retail_platform.demo_api.app import create_app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(settings, small_sim):
    return TestClient(create_app(settings, small_sim))


def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_rejects_missing_or_wrong_token(client):
    assert client.get("/v1/inventory/dates").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/v1/inventory/dates", headers=bad).status_code == 401


def test_available_dates(client, small_sim):
    body = client.get("/v1/inventory/dates", headers=AUTH).json()
    assert body == {
        "min_date": small_sim.start_date.isoformat(),
        "max_date": small_sim.end_date.isoformat(),
    }


def test_pagination_covers_every_row_once(client, small_world):
    rows, page = [], 1
    while True:
        body = client.get(
            "/v1/inventory/snapshots",
            params={"snapshot_date": "2026-07-02", "page": page, "page_size": 100},
            headers=AUTH,
        ).json()
        rows.extend(body["data"])
        if not body["has_more"]:
            break
        page += 1
    assert len(rows) == body["total"] == 8 * len(small_world.products)
    assert set(rows[0]) == {"warehouse_ts", "store_code", "sku", "on_hand", "on_order"}


def test_snapshot_outside_range_is_404(client):
    resp = client.get(
        "/v1/inventory/snapshots", params={"snapshot_date": "2020-01-01"}, headers=AUTH
    )
    assert resp.status_code == 404


def test_injects_invalid_records_deterministically(client):
    params = {"snapshot_date": "2026-07-03", "page_size": 5000}
    first = client.get("/v1/inventory/snapshots", params=params, headers=AUTH).json()["data"]
    second = client.get("/v1/inventory/snapshots", params=params, headers=AUTH).json()["data"]
    assert first == second
    invalid = [r for r in first if r["sku"] is None or r["on_hand"] < 0]
    assert invalid, "dirty_rate=0.02 should produce some invalid inventory records"


def test_chaos_mode_returns_transient_errors(settings, small_sim):
    chaotic = settings.model_copy(update={"demo_api_failure_rate": 0.9})
    client = TestClient(create_app(chaotic, small_sim, failure_rng=random.Random(0)))  # noqa: S311
    codes = {client.get("/v1/inventory/dates", headers=AUTH).status_code for _ in range(30)}
    assert {503, 429} <= codes
    assert client.get("/health").status_code == 200
