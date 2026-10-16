"""End-to-end smoke test against a running stack (``docker compose up``).

Waits for the scheduled pipeline to finish its first run, then checks that the KPI API
and the dashboard serve consistent, non-empty data. Uses only the standard library so it
can run on a bare CI runner.

    python scripts/smoke_test.py --api http://localhost:8000 --dashboard http://localhost:8501
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

FAILURES: list[str] = []


def get(url: str, headers: dict[str, str] | None = None, method: str = "GET") -> tuple[int, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(name)


def wait_for_first_run(api: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, runs = get(f"{api}/api/v1/pipeline/runs")
            if status == 200 and runs and runs[-1]["status"] != "running":
                return dict(runs[-1])
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(3)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--dashboard", default="http://localhost:8501")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()

    first = wait_for_first_run(args.api, args.timeout)
    check("first pipeline run finished", first is not None)
    if first is None:
        return 1
    check("first run succeeded", first["status"] in ("success", "partial"), first["status"])
    check("rows loaded", first["rows_loaded"] > 100_000, first["rows_loaded"])
    check("dirty rows quarantined", first["rows_rejected"] > 0, first["rows_rejected"])

    status, health = get(f"{args.api}/health")
    check("api health", status == 200 and health["database"] == "ok", health)

    status, filters = get(f"{args.api}/api/v1/filters")
    check("filters", status == 200 and len(filters["stores"]) == 8, status)

    status, summary = get(f"{args.api}/api/v1/kpis/summary")
    current = summary.get("current", {}) if isinstance(summary, dict) else {}
    check("summary net sales > 0", status == 200 and current.get("net_sales", 0) > 0, current)
    check("margin in (0, 1)", 0 < (current.get("gross_margin_pct") or 0) < 1)

    status, cats = get(f"{args.api}/api/v1/kpis/by-category")
    total = sum(c["net_sales"] for c in cats) if status == 200 else -1
    check(
        "category breakdown adds up",
        abs(total - current.get("net_sales", 0)) < 1,
        f"{total:.0f} vs {current.get('net_sales', 0):.0f}",
    )

    status, inventory = get(f"{args.api}/api/v1/kpis/inventory-health")
    check("inventory snapshot", status == 200 and inventory["positions"] > 0, inventory)

    status, rules = get(f"{args.api}/api/v1/quality/rejections")
    check("quality rules reported", status == 200 and len(rules) >= 5, len(rules))

    status, _ = get(f"{args.api}/api/v1/pipeline/runs", method="POST")
    check("trigger without key refused", status in (401, 403), status)
    if args.api_key:
        status, _ = get(
            f"{args.api}/api/v1/pipeline/runs", headers={"X-API-Key": args.api_key}, method="POST"
        )
        check("trigger with key accepted", status in (202, 409), status)

    status, _ = get(f"{args.dashboard}/_stcore/health")
    check("dashboard health", status == 200, status)

    print(f"\n{'SMOKE TEST PASSED' if not FAILURES else f'{len(FAILURES)} CHECK(S) FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
