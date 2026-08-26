"""Optional CSV export of the affected records, for fixing a list in bulk.

Every value written here is passed through
:func:`~campaign_preflight.normalization.neutralize_formula` first. Campaign
Preflight reports formula injection as a finding, so writing an export that
re-introduces it would be indefensible: the export is opened in a spreadsheet
by definition.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import PreflightReport, RuleStatus
from ..normalization import neutralize_formula
from .redaction import redact_text

__all__ = ["write_affected_csv", "AFFECTED_COLUMNS"]

AFFECTED_COLUMNS = ("rule_id", "severity", "status", "affected_record", "remediation")


def write_affected_csv(
    report: PreflightReport,
    path: Path | str,
    *,
    redacted: bool = True,
    max_samples: int | None = None,
) -> int:
    """Write one row per affected-record sample. Returns the row count."""
    rows: list[tuple[str, ...]] = []
    for result in report.results:
        if result.status not in {RuleStatus.FAIL, RuleStatus.WARN}:
            continue
        samples = result.affected_record_samples
        if max_samples is not None:
            samples = samples[:max_samples]
        for sample in samples:
            rows.append(
                (
                    result.rule_id,
                    result.severity.value,
                    result.status.value,
                    redact_text(sample, redacted=redacted),
                    redact_text(result.remediation, redacted=redacted),
                )
            )

    target = Path(path)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(AFFECTED_COLUMNS)
        for row in rows:
            writer.writerow([neutralize_formula(value) for value in row])
    return len(rows)
