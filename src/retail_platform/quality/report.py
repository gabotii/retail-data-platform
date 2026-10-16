"""Per-batch quality bookkeeping: check results and quarantined (rejected) rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class CheckResult:
    check: str
    severity: Severity
    passed: bool
    failed_rows: int
    total_rows: int
    detail: str = ""


@dataclass
class BatchReport:
    """Everything the quality layer learned about one batch (a file or a snapshot date)."""

    source: str
    batch: str
    rows_in: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    _rejected: list[pd.DataFrame] = field(default_factory=list, repr=False)

    def add_check(
        self,
        check: str,
        failed_rows: int,
        severity: Severity = "error",
        detail: str = "",
        total_rows: int | None = None,
    ) -> None:
        self.checks.append(
            CheckResult(
                check=check,
                severity=severity,
                passed=failed_rows == 0,
                failed_rows=int(failed_rows),
                total_rows=int(self.rows_in if total_rows is None else total_rows),
                detail=detail,
            )
        )

    def reject(self, rows: pd.DataFrame, rule: str, detail: str | pd.Series = "") -> None:
        """Quarantine ``rows`` (any dtype) under ``rule``; the original values are kept as JSON."""
        if rows.empty:
            return
        records = rows.to_json(
            orient="records", lines=True, date_format="iso", force_ascii=False
        ).splitlines()
        details = detail if isinstance(detail, pd.Series) else pd.Series(detail, index=rows.index)
        self._rejected.append(
            pd.DataFrame(
                {"rule": rule, "detail": details.loc[rows.index].to_numpy(), "record": records}
            )
        )

    @property
    def rejected(self) -> pd.DataFrame:
        if not self._rejected:
            return pd.DataFrame(columns=["rule", "detail", "record"])
        return pd.concat(self._rejected, ignore_index=True)

    @property
    def rejected_count(self) -> int:
        return sum(len(df) for df in self._rejected)

    @property
    def reject_ratio(self) -> float:
        return self.rejected_count / self.rows_in if self.rows_in else 0.0

    def rejected_by_rule(self) -> dict[str, int]:
        rejected = self.rejected
        if rejected.empty:
            return {}
        return {str(k): int(v) for k, v in rejected["rule"].value_counts().items()}
