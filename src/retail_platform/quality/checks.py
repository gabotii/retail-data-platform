"""Batch-level quality checks applied after contract validation."""

from __future__ import annotations

from collections.abc import Collection, Sequence

import pandas as pd

from retail_platform.errors import QualityGateError
from retail_platform.quality.report import BatchReport


def deduplicate(df: pd.DataFrame, key: Sequence[str], report: BatchReport) -> pd.DataFrame:
    """Drop exact duplicate rows (re-sent records) and quarantine conflicting key duplicates.

    Exact duplicates are harmless re-deliveries, so they are only counted (warning).
    Two *different* rows sharing a business key are ambiguous: the first one wins and the
    others are rejected so a human can look at them.
    """
    exact = df.duplicated(keep="first")
    report.add_check("exact_duplicates", int(exact.sum()), severity="warning")
    unique = df[~exact]
    conflicting = unique.duplicated(subset=list(key), keep="first")
    report.reject(unique[conflicting], "duplicate_key", detail=f"key={'+'.join(key)}")
    report.add_check("duplicate_key", int(conflicting.sum()))
    return unique[~conflicting]


def require_known(
    df: pd.DataFrame, column: str, known: Collection[str], report: BatchReport, rule: str
) -> pd.DataFrame:
    """Referential integrity: reject rows whose ``column`` is not in ``known``."""
    unknown = ~df[column].isin(known)
    report.reject(df[unknown], rule, detail=df.loc[unknown, column].astype(str))
    report.add_check(rule, int(unknown.sum()))
    return df[~unknown]


def enforce_reject_threshold(report: BatchReport, threshold: float) -> None:
    """Quality gate: a batch with too many rejected rows is not loaded at all."""
    ratio = report.reject_ratio
    report.add_check(
        "reject_ratio_gate",
        failed_rows=report.rejected_count if ratio > threshold else 0,
        detail=f"ratio={ratio:.4f} threshold={threshold:.4f}",
    )
    if ratio > threshold:
        raise QualityGateError(f"{report.source}:{report.batch}", ratio, threshold)


def check_volume(
    report: BatchReport, rows: int, baseline: float | None, tolerance: float = 0.5
) -> None:
    """Warn when a batch's volume deviates more than ``tolerance`` from the recent baseline."""
    if not baseline:
        report.add_check("volume_anomaly", 0, "info", detail="no baseline yet")
        return
    deviation = abs(rows - baseline) / baseline
    report.add_check(
        "volume_anomaly",
        failed_rows=0 if deviation <= tolerance else rows,
        severity="warning",
        detail=f"rows={rows} baseline={baseline:.0f} deviation={deviation:.1%}",
    )
