"""Shaping preflight results for an MCP client.

An agent reading a report has a different budget than a human reading a
terminal: it wants the verdict, the blockers, and the remediations, and it does
not want 76 passing checks. These helpers produce a compact structure that keeps
every load-bearing fact -- including the unknown checks, which an agent must not
mistake for passes -- and drops the rest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import PreflightReport, RuleResult, RuleStatus
from ..reporting.json_report import report_to_dict
from ..reporting.redaction import redact_samples, redact_text

__all__ = ["report_id", "summarize_report", "summarize_finding"]


def report_id(report: PreflightReport) -> str:
    """A stable identifier for a report's content.

    Derived from the findings rather than from the clock, so re-running the same
    check on unchanged data yields the same id and an agent can tell "nothing
    moved" from "something changed".
    """
    payload = report_to_dict(report)
    payload.pop("generated_at", None)
    payload.pop("duration_seconds", None)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"cpf-{digest[:12]}"


def summarize_finding(result: RuleResult, *, redacted: bool, max_samples: int) -> dict[str, Any]:
    return {
        "rule_id": result.rule_id,
        "severity": result.severity.value,
        "status": result.status.value,
        "title": result.title,
        "summary": redact_text(result.summary, redacted=redacted),
        "affected_record_count": result.affected_record_count,
        "affected_record_samples": list(
            redact_samples(result.affected_record_samples, redacted=redacted, limit=max_samples)
        ),
        "remediation": redact_text(result.remediation, redacted=redacted),
        "heuristic": result.heuristic,
    }


def summarize_report(
    report: PreflightReport, *, max_samples: int = 5, include_passed: bool = False
) -> dict[str, Any]:
    """The compact structure returned by every preflight MCP tool."""
    redacted = report.redacted

    def block(results: tuple[RuleResult, ...]) -> list[dict[str, Any]]:
        return [
            summarize_finding(r, redacted=redacted, max_samples=max_samples) for r in results
        ]

    unknown = report.results_by_status(RuleStatus.UNKNOWN)
    payload: dict[str, Any] = {
        "report_id": report_id(report),
        "report_schema_version": report.report_schema_version,
        "tool_version": report.tool_version,
        "generated_at": report.generated_at.isoformat().replace("+00:00", "Z"),
        "provider": report.provider,
        "read_only": True,
        "campaign": {
            "id": report.campaign_id,
            "name": report.campaign_name,
            "status": report.campaign_status,
        },
        "readiness": report.readiness.value,
        "score": report.score,
        "confidence": report.confidence.value,
        "score_explanation": report.score_breakdown.explanation,
        "counts": {
            "leads": report.lead_count,
            "senders": report.sender_count,
            "blockers": report.blocker_count,
            "failures": report.failure_count,
            "warnings": report.warning_count,
            "unknown": report.unknown_count,
            "passed": report.passed_count,
            "not_applicable": report.not_applicable_count,
        },
        "blockers": block(report.blockers),
        "failures": block(tuple(r for r in report.failures if not r.is_blocking)),
        "warnings": block(report.warnings),
        "unknown_checks": block(unknown),
        "unknown_checks_note": (
            "UNKNOWN means the check could not run. It is not a pass. Do not "
            "report a campaign as safe on the basis of an unknown check."
        ),
        "recommended_remediations": _remediations(report, redacted=redacted),
        "limitations": [redact_text(x, redacted=redacted) for x in report.limitations],
        "redacted": redacted,
        "snapshot_note": report.snapshot_note,
        "disclaimer": (
            "Campaign Preflight is a read-only linter. It does not guarantee "
            "deliverability, does not provide legal advice, and cannot activate, "
            "edit, or send anything."
        ),
    }
    if include_passed:
        payload["passed"] = [
            {"rule_id": r.rule_id, "summary": redact_text(r.summary, redacted=redacted)}
            for r in report.results_by_status(RuleStatus.PASS)
        ]
    return payload


def _remediations(report: PreflightReport, *, redacted: bool) -> list[str]:
    """De-duplicated remediations, most severe first."""
    order = {"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    actionable = [
        r
        for r in report.results
        if r.status in {RuleStatus.FAIL, RuleStatus.WARN} and r.remediation
    ]
    actionable.sort(key=lambda r: (order.get(r.severity.value, 9), r.rule_id))
    seen: dict[str, None] = {}
    for result in actionable:
        text = f"[{result.rule_id}] {redact_text(result.remediation, redacted=redacted)}"
        seen.setdefault(text, None)
    return list(seen)
