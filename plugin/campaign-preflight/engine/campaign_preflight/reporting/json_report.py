"""JSON rendering plus the versioned schema the output conforms to.

Field order is fixed and lists are already sorted by the engine, so two runs
over identical input produce byte-identical JSON. That makes the output usable
as a committed artifact whose diffs mean something.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import PreflightReport
from .redaction import redact_samples, redact_text

__all__ = ["SCHEMA_PATH", "load_schema", "render_json", "report_to_dict"]

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "report-1.0.0.json"


def load_schema() -> dict[str, Any]:
    """The JSON Schema the rendered report validates against."""
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def report_to_dict(report: PreflightReport, *, max_samples: int | None = None) -> dict[str, Any]:
    """Convert a report to a plain, redaction-applied dictionary."""
    redacted = report.redacted

    results = [
        {
            "rule_id": r.rule_id,
            "rule_version": r.rule_version,
            "title": r.title,
            "category": r.category.value,
            "severity": r.severity.value,
            "status": r.status.value,
            "heuristic": r.heuristic,
            "summary": redact_text(r.summary, redacted=redacted),
            "explanation": redact_text(r.explanation, redacted=redacted),
            "affected_record_count": r.affected_record_count,
            "affected_record_samples": list(
                redact_samples(r.affected_record_samples, redacted=redacted, limit=max_samples)
            ),
            "evidence": list(redact_samples(r.evidence, redacted=redacted, limit=max_samples)),
            "remediation": redact_text(r.remediation, redacted=redacted),
            "metadata": r.metadata,
        }
        for r in report.results
    ]

    return {
        "report_schema_version": report.report_schema_version,
        "tool_version": report.tool_version,
        "generated_at": report.generated_at.isoformat().replace("+00:00", "Z"),
        "snapshot_note": report.snapshot_note,
        "provider": {
            "name": report.provider,
            "read_only": report.provider_read_only,
            "errors": [redact_text(e, redacted=redacted) for e in report.provider_errors],
        },
        "campaign": {
            "id": report.campaign_id,
            "name": report.campaign_name,
            "status": report.campaign_status,
        },
        "readiness": report.readiness.value,
        "score": report.score,
        "confidence": report.confidence.value,
        "score_breakdown": {
            "starting_score": report.score_breakdown.starting_score,
            "final_score": report.score_breakdown.final_score,
            "explanation": report.score_breakdown.explanation,
            "deductions": [
                {
                    "rule_id": d.rule_id,
                    "status": d.status.value,
                    "severity": d.severity.value,
                    "points": d.points,
                    "reason": redact_text(d.reason, redacted=redacted),
                }
                for d in report.score_breakdown.deductions
            ],
            "excluded_rule_ids": list(report.score_breakdown.excluded_rule_ids),
            "critical_unknown_rule_ids": list(report.score_breakdown.critical_unknown_rule_ids),
        },
        "counts": {
            "leads": report.lead_count,
            "leads_partial": report.lead_count_is_partial,
            "senders": report.sender_count,
            "suppressions": report.suppression_count,
            "blockers": report.blocker_count,
            "failures": report.failure_count,
            "warnings": report.warning_count,
            "unknown": report.unknown_count,
            "passed": report.passed_count,
            "not_applicable": report.not_applicable_count,
        },
        "results": results,
        "limitations": [redact_text(x, redacted=redacted) for x in report.limitations],
        "redacted": redacted,
        "duration_seconds": report.duration_seconds,
    }


def render_json(report: PreflightReport, *, max_samples: int | None = None, indent: int = 2) -> str:
    """Render a report as deterministic JSON."""
    payload = report_to_dict(report, max_samples=max_samples)
    # sort_keys is deliberately off: the hand-written key order above is more
    # readable, and it is already deterministic.
    return json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
