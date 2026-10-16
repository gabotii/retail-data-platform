"""Domain exceptions. Each one maps to a well-defined pipeline behaviour."""

from __future__ import annotations


class RetailPlatformError(Exception):
    """Base class for all expected platform errors."""


class SourceUnavailableError(RetailPlatformError):
    """A source system could not be reached after all retries. The run fails for that source."""


class SchemaValidationError(RetailPlatformError):
    """A batch does not match its contract structurally (e.g. a required column is missing).

    The whole batch is rejected and quarantined; other batches keep loading.
    """


class QualityGateError(RetailPlatformError):
    """A batch's rejected-row ratio exceeds the configured threshold; the batch is not loaded."""

    def __init__(self, batch: str, ratio: float, threshold: float) -> None:
        self.batch = batch
        self.ratio = ratio
        self.threshold = threshold
        super().__init__(f"{batch}: {ratio:.1%} of rows rejected (threshold {threshold:.1%})")


class PipelineAlreadyRunningError(RetailPlatformError):
    """Another pipeline run holds the advisory lock."""
