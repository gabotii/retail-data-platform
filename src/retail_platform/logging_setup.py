"""Structured logging.

Every record is emitted as one JSON line (or a readable console line in development)
and automatically carries the current pipeline ``run_id`` when one is active, so the
logs of a run can be correlated with the ``etl.pipeline_run`` table.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

run_id_var: ContextVar[int | None] = ContextVar("run_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = run_id_var.get()
        if run_id is not None:
            payload["run_id"] = run_id
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        run_id = run_id_var.get()
        prefix = f"[run {run_id}] " if run_id is not None else ""
        extras = " ".join(f"{k}={v}" for k, v in _extra_fields(record).items())
        line = f"{ts} {record.levelname:<7} {record.name}: {prefix}{record.getMessage()}"
        if extras:
            line = f"{line} | {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Third-party request logs are noise at INFO level.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
