"""HTTP client for the demo inventory API, with retries and pagination.

Retry policy: transport errors (connection refused, timeouts), HTTP 429 and HTTP 5xx are
retried with exponential backoff plus jitter, honouring ``Retry-After``. Any other 4xx is
a permanent error and fails fast. After the last retry a :class:`SourceUnavailableError`
is raised so the orchestrator can mark the source as failed without crashing the run.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import date
from types import TracebackType
from typing import Any, Self

import httpx
import pandas as pd

from retail_platform.config import Settings
from retail_platform.errors import SourceUnavailableError

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class InventoryApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 4,
        backoff_seconds: float = 0.5,
        page_size: int = 1000,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "retail-platform/1.0"},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.page_size = page_size
        self._sleep = sleep
        self.retries_performed = 0

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> InventoryApiClient:
        return cls(
            settings.demo_api_url,
            settings.demo_api_token.get_secret_value(),
            timeout=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            backoff_seconds=settings.http_backoff_seconds,
            page_size=settings.api_page_size,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and "Retry-After" in response.headers:
            try:
                return float(response.headers["Retry-After"])
            except ValueError:
                pass
        base = self.backoff_seconds * (2**attempt)
        return base + random.uniform(0, self.backoff_seconds)  # noqa: S311 - jitter, not crypto

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        last_error = ""
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    return response
                last_error = f"HTTP {response.status_code}"
            if attempt == self.max_retries:
                break
            delay = self._delay(attempt, response)
            self.retries_performed += 1
            logger.warning(
                "inventory API call failed, retrying",
                extra={"path": path, "attempt": attempt + 1, "error": last_error, "delay": delay},
            )
            self._sleep(delay)
        raise SourceUnavailableError(
            f"inventory API {path} failed after {self.max_retries + 1} attempts: {last_error}"
        )

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._get(path, params)
        if response.status_code in (401, 403):
            raise SourceUnavailableError(
                f"inventory API rejected credentials ({response.status_code})"
            )
        if response.is_error:
            raise SourceUnavailableError(f"inventory API {path} returned {response.status_code}")
        payload: dict[str, Any] = response.json()
        return payload

    def available_dates(self) -> tuple[date, date]:
        body = self._get_json("/v1/inventory/dates")
        return date.fromisoformat(body["min_date"]), date.fromisoformat(body["max_date"])

    def fetch_snapshot(self, snapshot_date: date) -> pd.DataFrame:
        """Fetch every page of one day's snapshot. Rows keep the API's raw JSON values."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body = self._get_json(
                "/v1/inventory/snapshots",
                {
                    "snapshot_date": snapshot_date.isoformat(),
                    "page": page,
                    "page_size": self.page_size,
                },
            )
            rows.extend(body["data"])
            if not body["has_more"]:
                break
            page += 1
        if len(rows) != body["total"]:
            raise SourceUnavailableError(
                f"inventory snapshot {snapshot_date}: got {len(rows)} rows, "
                f"API announced {body['total']}"
            )
        df = pd.DataFrame(
            rows, columns=["warehouse_ts", "store_code", "sku", "on_hand", "on_order"]
        )
        df.insert(0, "snapshot_date", snapshot_date.isoformat())
        return df
