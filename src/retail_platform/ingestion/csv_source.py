"""Landing-zone CSV discovery and raw reading.

Files are always read with every column as text: typing is the contract's job, so a
malformed value in one row can be rejected without failing the whole file.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from retail_platform.errors import SchemaValidationError

logger = logging.getLogger(__name__)

_SALES_FILE = re.compile(r"sales_(\d{4}-\d{2}-\d{2})\.csv")


@dataclass(frozen=True)
class LandingFile:
    path: Path
    checksum: str
    size_bytes: int
    business_date: date | None = None

    @property
    def name(self) -> str:
        return self.path.name


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(path: Path, business_date: date | None = None) -> LandingFile:
    return LandingFile(path, file_checksum(path), path.stat().st_size, business_date)


def discover_sales_files(landing_dir: Path) -> list[LandingFile]:
    """Return sales files ordered by business date; badly named files are skipped with a warning."""
    found: list[LandingFile] = []
    for path in sorted((landing_dir / "sales").glob("*.csv")):
        match = _SALES_FILE.fullmatch(path.name)
        if not match:
            logger.warning("skipping unexpected file in landing zone", extra={"file": path.name})
            continue
        try:
            business_date = date.fromisoformat(match.group(1))
        except ValueError:
            logger.warning("skipping file with invalid date in name", extra={"file": path.name})
            continue
        found.append(describe(path, business_date))
    return sorted(found, key=lambda f: (f.business_date or date.min, f.name))


def read_raw_csv(path: Path) -> pd.DataFrame:
    """Read a CSV as all-text columns. Unreadable files raise SchemaValidationError."""
    try:
        with warnings.catch_warnings():
            # Rows with more fields than the header would otherwise be silently truncated.
            warnings.simplefilter("error", pd.errors.ParserWarning)
            df = pd.read_csv(
                path,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8",
                quoting=csv.QUOTE_MINIMAL,
                index_col=False,
                on_bad_lines="error",
            )
    except (
        pd.errors.ParserError,
        pd.errors.ParserWarning,
        pd.errors.EmptyDataError,
        UnicodeDecodeError,
    ) as exc:
        raise SchemaValidationError(f"{path.name}: unreadable CSV ({exc})") from exc
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df
