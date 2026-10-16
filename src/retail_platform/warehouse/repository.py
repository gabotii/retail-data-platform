"""Reads and writes of the ``etl`` control schema (runs, batches, registry, watermarks, quality)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, text

from retail_platform.quality.report import BatchReport


class ControlRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------ runs
    def start_run(self, trigger: str, full_refresh: bool) -> int:
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    text(
                        "INSERT INTO etl.pipeline_run (trigger, full_refresh)"
                        " VALUES (:t, :f) RETURNING run_id"
                    ),
                    {"t": trigger, "f": full_refresh},
                ).scalar_one()
            )

    def finish_run(
        self, run_id: int, status: str, metrics: dict[str, Any], error: str | None
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE etl.pipeline_run SET status = :s, metrics = CAST(:m AS jsonb),"
                    " error = :e, finished_at = now() WHERE run_id = :r"
                ),
                {"s": status, "m": json.dumps(metrics, default=str), "e": error, "r": run_id},
            )

    # ------------------------------------------------------------------ batches
    def start_batch(self, run_id: int, source: str, name: str, checksum: str | None) -> int:
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    text(
                        "INSERT INTO etl.batch (run_id, source, batch_name, checksum)"
                        " VALUES (:r, :s, :n, :c) RETURNING batch_id"
                    ),
                    {"r": run_id, "s": source, "n": name, "c": checksum},
                ).scalar_one()
            )

    @staticmethod
    def finish_batch(
        conn: Connection,
        batch_id: int,
        status: str,
        report: BatchReport | None,
        rows_loaded: int = 0,
        error: str | None = None,
    ) -> None:
        """Close a batch and persist its quality results, inside the caller's transaction."""
        conn.execute(
            text(
                "UPDATE etl.batch SET status = :s, rows_in = :i, rows_loaded = :l,"
                " rows_rejected = :x, error = :e, finished_at = now() WHERE batch_id = :b"
            ),
            {
                "s": status,
                "i": report.rows_in if report else 0,
                "l": rows_loaded,
                "x": report.rejected_count if report else 0,
                "e": error,
                "b": batch_id,
            },
        )
        if report is None:
            return
        if report.checks:
            conn.execute(
                text(
                    "INSERT INTO etl.quality_check (batch_id, check_name, severity, passed,"
                    " failed_rows, total_rows, detail)"
                    " VALUES (:b, :c, :s, :p, :f, :t, :d)"
                ),
                [
                    {
                        "b": batch_id,
                        "c": c.check,
                        "s": c.severity,
                        "p": c.passed,
                        "f": c.failed_rows,
                        "t": c.total_rows,
                        "d": c.detail,
                    }
                    for c in report.checks
                ],
            )
        rejected = report.rejected
        if not rejected.empty:
            conn.execute(
                text(
                    "INSERT INTO etl.rejected_record (batch_id, rule, detail, record)"
                    " VALUES (:b, :r, :d, CAST(:j AS jsonb))"
                ),
                [
                    {"b": batch_id, "r": row.rule, "d": str(row.detail), "j": row.record}
                    for row in rejected.itertuples(index=False)
                ],
            )

    def fail_batch(self, batch_id: int, report: BatchReport | None, error: str) -> None:
        with self.engine.begin() as conn:
            self.finish_batch(conn, batch_id, "failed", report, error=error)

    # ------------------------------------------------------------------ file registry
    def file_status(self, source: str, file_name: str, checksum: str) -> str | None:
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT status FROM etl.processed_file"
                    " WHERE source = :s AND file_name = :f AND checksum = :c"
                ),
                {"s": source, "f": file_name, "c": checksum},
            ).scalar_one_or_none()
        return None if value is None else str(value)

    @staticmethod
    def register_file(
        conn: Connection, source: str, file_name: str, checksum: str, status: str, batch_id: int
    ) -> None:
        conn.execute(
            text(
                "INSERT INTO etl.processed_file (source, file_name, checksum, status, batch_id)"
                " VALUES (:s, :f, :c, :st, :b)"
                " ON CONFLICT (source, file_name) DO UPDATE SET checksum = EXCLUDED.checksum,"
                " status = EXCLUDED.status, batch_id = EXCLUDED.batch_id, processed_at = now()"
            ),
            {"s": source, "f": file_name, "c": checksum, "st": status, "b": batch_id},
        )

    # ------------------------------------------------------------------ watermarks
    def get_watermark_date(self, source: str) -> date | None:
        with self.engine.connect() as conn:
            value = conn.execute(
                text("SELECT value FROM etl.watermark WHERE source = :s"), {"s": source}
            ).scalar_one_or_none()
        return None if value is None else date.fromisoformat(value)

    @staticmethod
    def set_watermark(conn: Connection, source: str, value: date, run_id: int) -> None:
        conn.execute(
            text(
                "INSERT INTO etl.watermark (source, value, run_id) VALUES (:s, :v, :r)"
                " ON CONFLICT (source) DO UPDATE SET value = EXCLUDED.value,"
                " run_id = EXCLUDED.run_id, updated_at = now()"
                " WHERE etl.watermark.value < EXCLUDED.value"
            ),
            {"s": source, "v": value.isoformat(), "r": run_id},
        )

    # ------------------------------------------------------------------ baselines
    def recent_rows_baseline(self, source: str, last_n: int = 7) -> float | None:
        """Average rows_in of the last ``n`` loaded batches of a source (volume anomaly check)."""
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT avg(rows_in) FROM (SELECT rows_in FROM etl.batch"
                    " WHERE source = :s AND status = 'loaded'"
                    " ORDER BY batch_id DESC LIMIT :n) recent"
                ),
                {"s": source, "n": last_n},
            ).scalar_one_or_none()
        return None if value is None else float(value)
