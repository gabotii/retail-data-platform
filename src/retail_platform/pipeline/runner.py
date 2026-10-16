"""Pipeline orchestration: master data -> sales CSVs -> inventory API -> marts.

Error-handling contract
-----------------------
* ``SchemaValidationError`` / ``QualityGateError``: the batch is *rejected* (quarantined
  with its quality report); the run continues and ends ``partial``.
* ``SourceUnavailableError``: the source stops for this run (the watermark is not moved
  past the failure); other sources continue; the run ends ``partial``.
* Anything else (bug, database outage): the current batch is marked ``failed`` and the run
  stops with status ``failed``. Already committed batches stay committed.

Incrementality
--------------
* CSV files are identified by (name, sha256); unchanged files are skipped, re-delivered
  files fully replace the rows they own.
* The inventory API is read from the day after the stored watermark up to the latest
  date the API publishes; the watermark advances one committed day at a time.
* Marts are recomputed only for the dates touched by the run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Literal

import pandas as pd
from sqlalchemy import Connection, Engine, text

from retail_platform.config import Settings
from retail_platform.errors import (
    PipelineAlreadyRunningError,
    QualityGateError,
    SchemaValidationError,
    SourceUnavailableError,
)
from retail_platform.ingestion.api_source import InventoryApiClient
from retail_platform.ingestion.csv_source import (
    LandingFile,
    describe,
    discover_sales_files,
    read_raw_csv,
)
from retail_platform.logging_setup import run_id_var
from retail_platform.pipeline.stages import (
    prepare_inventory,
    prepare_products,
    prepare_sales,
    prepare_stores,
)
from retail_platform.quality.checks import check_volume
from retail_platform.quality.report import BatchReport
from retail_platform.warehouse import loaders
from retail_platform.warehouse.repository import ControlRepository

logger = logging.getLogger(__name__)

PIPELINE_LOCK_KEY = 4242_0002
Source = Literal["master", "sales", "inventory"]
ALL_SOURCES: tuple[Source, ...] = ("master", "sales", "inventory")
RunStatus = Literal["success", "partial", "failed"]


@dataclass
class SourceStats:
    batches_loaded: int = 0
    batches_rejected: int = 0
    batches_failed: int = 0
    batches_skipped: int = 0
    rows_in: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    error: str | None = None

    @property
    def clean(self) -> bool:
        return not (self.batches_rejected or self.batches_failed or self.error)


@dataclass
class RunResult:
    run_id: int
    status: RunStatus
    duration_seconds: float = 0.0
    sources: dict[str, SourceStats] = field(default_factory=dict)
    marts_refreshed_days: int = 0
    error: str | None = None

    def metrics(self) -> dict[str, object]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "marts_refreshed_days": self.marts_refreshed_days,
            "sources": {name: asdict(stats) for name, stats in self.sources.items()},
        }


class Pipeline:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        api_client_factory: Callable[[], InventoryApiClient] | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.repo = ControlRepository(engine)
        self._api_client_factory = api_client_factory or (
            lambda: InventoryApiClient.from_settings(settings)
        )
        self._touched_date_keys: set[int] = set()

    # ------------------------------------------------------------------ public API
    def run(
        self,
        *,
        full_refresh: bool = False,
        sources: tuple[Source, ...] = ALL_SOURCES,
        trigger: str = "cli",
    ) -> RunResult:
        started = time.perf_counter()
        with self._exclusive_lock():
            run_id = self.repo.start_run(trigger, full_refresh)
            token = run_id_var.set(run_id)
            result = RunResult(run_id=run_id, status="success")
            self._touched_date_keys = set()
            logger.info(
                "pipeline run started",
                extra={"sources": list(sources), "full_refresh": full_refresh},
            )
            try:
                if "master" in sources:
                    result.sources["stores"] = self._load_master_file(
                        run_id, "stores", "stores.csv", full_refresh
                    )
                    result.sources["products"] = self._load_master_file(
                        run_id, "products", "products.csv", full_refresh
                    )
                if "sales" in sources:
                    result.sources["sales"] = self._load_sales(run_id, full_refresh)
                if "inventory" in sources:
                    result.sources["inventory"] = self._load_inventory(run_id, full_refresh)
                with self.engine.begin() as conn:
                    result.marts_refreshed_days = loaders.refresh_sales_marts(
                        conn, self._touched_date_keys
                    )
                if not all(stats.clean for stats in result.sources.values()):
                    result.status = "partial"
            except Exception as exc:
                result.status = "failed"
                result.error = f"{type(exc).__name__}: {exc}"
                logger.exception("pipeline run failed")
            finally:
                result.duration_seconds = time.perf_counter() - started
                self.repo.finish_run(run_id, result.status, result.metrics(), result.error)
                logger.info(
                    "pipeline run finished",
                    extra={"status": result.status, **result.metrics()},
                )
                run_id_var.reset(token)
        return result

    # ------------------------------------------------------------------ locking
    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        with self.engine.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": PIPELINE_LOCK_KEY}
            ).scalar_one()
            conn.commit()
            if not acquired:
                raise PipelineAlreadyRunningError("another pipeline run is in progress")
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": PIPELINE_LOCK_KEY})
                conn.commit()

    # ------------------------------------------------------------------ CSV sources
    def _should_skip(self, source: str, file: LandingFile, full_refresh: bool) -> bool:
        if full_refresh:
            return False
        status = self.repo.file_status(source, file.name, file.checksum)
        if status is not None:
            logger.debug("file already processed", extra={"file": file.name, "status": status})
            return True
        return False

    def _load_master_file(
        self, run_id: int, source: str, file_name: str, full_refresh: bool
    ) -> SourceStats:
        stats = SourceStats()
        path = self.settings.landing_dir / "master" / file_name
        if not path.exists():
            stats.error = f"{path} not found"
            logger.warning("master data file missing", extra={"file": str(path)})
            return stats
        file = describe(path)
        if self._should_skip(source, file, full_refresh):
            stats.batches_skipped += 1
            return stats

        def load(raw: pd.DataFrame, report: BatchReport, batch_id: int) -> int:
            threshold = self.settings.quality_max_reject_ratio
            with self.engine.begin() as conn:
                if source == "stores":
                    merged = loaders.merge_stores(conn, prepare_stores(raw, report, threshold))
                else:
                    merged = loaders.merge_products(conn, prepare_products(raw, report, threshold))
                self._close_file_batch(conn, source, file, batch_id, report, merged.loaded)
            return merged.loaded

        self._process_file(run_id, source, file, stats, load)
        return stats

    def _load_sales(self, run_id: int, full_refresh: bool) -> SourceStats:
        stats = SourceStats()
        files = discover_sales_files(self.settings.landing_dir)
        if not files:
            logger.warning("no sales files found", extra={"dir": str(self.settings.landing_dir)})
        with self.engine.connect() as conn:
            known_stores, known_skus = loaders.known_codes(conn)
        for file in files:
            if self._should_skip("sales", file, full_refresh):
                stats.batches_skipped += 1
                continue

            def load(
                raw: pd.DataFrame, report: BatchReport, batch_id: int, f: LandingFile = file
            ) -> int:
                baseline = self.repo.recent_rows_baseline("sales")
                prepared = prepare_sales(
                    raw,
                    report,
                    known_stores=known_stores,
                    known_skus=known_skus,
                    max_reject_ratio=self.settings.quality_max_reject_ratio,
                )
                check_volume(report, len(raw), baseline)
                with self.engine.begin() as conn:
                    merged = loaders.load_sales(conn, prepared, batch_id, f.name)
                    self._close_file_batch(conn, "sales", f, batch_id, report, merged.loaded)
                self._touched_date_keys.update(merged.date_keys)
                return merged.loaded

            self._process_file(run_id, "sales", file, stats, load)
        return stats

    def _process_file(
        self,
        run_id: int,
        source: str,
        file: LandingFile,
        stats: SourceStats,
        load: Callable[[pd.DataFrame, BatchReport, int], int],
    ) -> None:
        batch_id = self.repo.start_batch(run_id, source, file.name, file.checksum)
        report = BatchReport(source, file.name)
        try:
            raw = read_raw_csv(file.path)
            loaded = load(raw, report, batch_id)
        except (SchemaValidationError, QualityGateError) as exc:
            self._reject_file(source, file, batch_id, report, str(exc))
            stats.batches_rejected += 1
            stats.rows_in += report.rows_in
            stats.rows_rejected += report.rejected_count
            return
        except Exception as exc:
            stats.batches_failed += 1
            self.repo.fail_batch(batch_id, report, f"{type(exc).__name__}: {exc}")
            raise
        stats.batches_loaded += 1
        stats.rows_in += report.rows_in
        stats.rows_loaded += loaded
        stats.rows_rejected += report.rejected_count
        logger.info(
            "batch loaded",
            extra={
                "source": source,
                "batch": file.name,
                "rows_in": report.rows_in,
                "rows_loaded": loaded,
                "rows_rejected": report.rejected_count,
            },
        )

    def _close_file_batch(
        self,
        conn: Connection,
        source: str,
        file: LandingFile,
        batch_id: int,
        report: BatchReport,
        loaded: int,
    ) -> None:
        self.repo.finish_batch(conn, batch_id, "loaded", report, rows_loaded=loaded)
        self.repo.register_file(conn, source, file.name, file.checksum, "loaded", batch_id)

    def _reject_file(
        self, source: str, file: LandingFile, batch_id: int, report: BatchReport, error: str
    ) -> None:
        logger.warning(
            "batch rejected", extra={"source": source, "batch": file.name, "error": error}
        )
        with self.engine.begin() as conn:
            self.repo.finish_batch(conn, batch_id, "rejected", report, error=error)
            self.repo.register_file(conn, source, file.name, file.checksum, "rejected", batch_id)

    # ------------------------------------------------------------------ API source
    def _load_inventory(self, run_id: int, full_refresh: bool) -> SourceStats:
        stats = SourceStats()
        try:
            client = self._api_client_factory()
        except Exception as exc:  # pragma: no cover - construction failures are config bugs
            stats.error = str(exc)
            return stats
        with client:
            try:
                min_date, max_date = client.available_dates()
            except SourceUnavailableError as exc:
                stats.error = str(exc)
                logger.error("inventory source unavailable", extra={"error": str(exc)})
                return stats
            watermark = self.repo.get_watermark_date("inventory")
            start = min_date
            if watermark is not None and not full_refresh:
                start = max(min_date, watermark + timedelta(days=1))
            with self.engine.connect() as conn:
                known_stores, known_skus = loaders.known_codes(conn)
            logger.info(
                "inventory incremental window",
                extra={"watermark": watermark, "from": start, "to": max_date},
            )
            day = start
            while day <= max_date:
                if not self._load_inventory_day(
                    client, run_id, day, known_stores, known_skus, stats
                ):
                    break
                day += timedelta(days=1)
            if client.retries_performed:
                logger.info("inventory API retries", extra={"retries": client.retries_performed})
        return stats

    def _load_inventory_day(
        self,
        client: InventoryApiClient,
        run_id: int,
        day: date,
        known_stores: set[str],
        known_skus: set[str],
        stats: SourceStats,
    ) -> bool:
        """Load one snapshot date. Returns False when the source must stop for this run."""
        batch_id = self.repo.start_batch(run_id, "inventory", day.isoformat(), None)
        report = BatchReport("inventory", day.isoformat())
        try:
            raw = client.fetch_snapshot(day)
            prepared = prepare_inventory(
                raw,
                report,
                known_stores=known_stores,
                known_skus=known_skus,
                max_reject_ratio=self.settings.quality_max_reject_ratio,
            )
        except SourceUnavailableError as exc:
            stats.batches_failed += 1
            stats.error = str(exc)
            self.repo.fail_batch(batch_id, report, str(exc))
            logger.error("inventory source failed; watermark not advanced", extra={"day": day})
            return False
        except (SchemaValidationError, QualityGateError) as exc:
            stats.batches_rejected += 1
            stats.rows_in += report.rows_in
            stats.rows_rejected += report.rejected_count
            with self.engine.begin() as conn:
                self.repo.finish_batch(conn, batch_id, "rejected", report, error=str(exc))
                # The day is quarantined; move on so one bad day cannot block the feed.
                self.repo.set_watermark(conn, "inventory", day, run_id)
            logger.warning("inventory day rejected", extra={"day": day, "error": str(exc)})
            return True
        except Exception as exc:
            stats.batches_failed += 1
            self.repo.fail_batch(batch_id, report, f"{type(exc).__name__}: {exc}")
            raise
        try:
            with self.engine.begin() as conn:
                merged = loaders.load_inventory(conn, prepared, batch_id)
                self.repo.finish_batch(conn, batch_id, "loaded", report, rows_loaded=merged.loaded)
                self.repo.set_watermark(conn, "inventory", day, run_id)
        except Exception as exc:
            stats.batches_failed += 1
            self.repo.fail_batch(batch_id, report, f"{type(exc).__name__}: {exc}")
            raise
        stats.batches_loaded += 1
        stats.rows_in += report.rows_in
        stats.rows_loaded += merged.loaded
        stats.rows_rejected += report.rejected_count
        logger.info(
            "batch loaded",
            extra={
                "source": "inventory",
                "batch": day.isoformat(),
                "rows_in": report.rows_in,
                "rows_loaded": merged.loaded,
                "rows_rejected": report.rejected_count,
            },
        )
        return True
