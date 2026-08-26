"""Markdown rendering, aimed at a pull request or a saved approval artifact.

The structure is chosen so a reviewer reading top-to-bottom stops at the first
thing that matters: verdict, then blockers, then everything else. Tables are
used only where the data is genuinely tabular; findings stay as prose because a
remediation sentence does not survive being squeezed into a cell.
"""

from __future__ import annotations

from ..models import PreflightReport, Readiness, RuleResult, RuleStatus, Severity
from .redaction import redact_samples, redact_text

__all__ = ["render_markdown"]

_BADGE = {
    Readiness.READY: "READY",
    Readiness.READY_WITH_WARNINGS: "READY WITH WARNINGS",
    Readiness.NOT_READY: "NOT READY",
    Readiness.INCOMPLETE: "INCOMPLETE",
}

_SEVERITY_ORDER = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


def _escape(text: str) -> str:
    """Escape the pipe character so free text cannot break a table row."""
    return text.replace("|", "\\|")


def _sorted(results: list[RuleResult]) -> list[RuleResult]:
    return sorted(results, key=lambda r: (_SEVERITY_ORDER.get(r.severity, 9), r.rule_id))


def _finding_block(result: RuleResult, *, redacted: bool, max_samples: int) -> list[str]:
    heuristic = " _(heuristic)_" if result.heuristic else ""
    lines = [
        f"#### `{result.rule_id}` - {result.severity.value}{heuristic}",
        "",
        redact_text(result.summary, redacted=redacted),
        "",
    ]
    samples = redact_samples(
        result.affected_record_samples, redacted=redacted, limit=max_samples
    )
    if samples:
        more = result.affected_record_count - len(samples)
        suffix = f" _(+{more} more)_" if more > 0 else ""
        lines.append(
            "- **Affected:** " + ", ".join(f"`{s}`" for s in samples) + suffix
        )
    for item in redact_samples(result.evidence, redacted=redacted, limit=max_samples):
        lines.append(f"- **Evidence:** {redact_text(item, redacted=redacted)}")
    if result.remediation:
        lines.append(f"- **Remediation:** {redact_text(result.remediation, redacted=redacted)}")
    lines.append("")
    return lines


def render_markdown(
    report: PreflightReport, *, max_samples: int = 5, verbose: bool = False
) -> str:
    """Render a report as GitHub-flavoured Markdown."""
    redacted = report.redacted
    out: list[str] = []
    add = out.append

    campaign = report.campaign_name or report.campaign_id or "unknown campaign"
    add(f"# Campaign Preflight: {_escape(campaign)}")
    add("")
    add(f"**{_BADGE[report.readiness]}** - score {report.score}/100, "
        f"confidence {report.confidence.value}")
    add("")
    add("| | |")
    add("|---|---|")
    add(f"| Provider | `{report.provider}` (read-only) |")
    add(f"| Campaign status | {report.campaign_status or 'unknown'} |")
    add(f"| Leads checked | {report.lead_count}{'+' if report.lead_count_is_partial else ''} |")
    add(f"| Senders checked | {report.sender_count} |")
    add(f"| Suppression entries | {report.suppression_count} |")
    add(f"| Blockers / failures / warnings | "
        f"{report.blocker_count} / {report.failure_count} / {report.warning_count} |")
    add(f"| Unknown / passed / n-a | "
        f"{report.unknown_count} / {report.passed_count} / {report.not_applicable_count} |")
    add(f"| Generated | {report.generated_at.isoformat().replace('+00:00', 'Z')} |")
    add(f"| Tool version | {report.tool_version} |")
    add("")
    add(f"> {report.snapshot_note}")
    add("")

    sections = [
        ("Blockers", [r for r in report.results if r.is_blocking]),
        (
            "Failures",
            [
                r
                for r in report.results
                if r.status is RuleStatus.FAIL and not r.is_blocking
            ],
        ),
        ("Warnings", [r for r in report.results if r.status is RuleStatus.WARN]),
        ("Unknown", [r for r in report.results if r.status is RuleStatus.UNKNOWN]),
    ]
    for heading, results in sections:
        if not results:
            continue
        add(f"## {heading} ({len(results)})")
        add("")
        if heading == "Unknown":
            add(
                "These checks could not run. They are **not** passes -- the "
                "underlying question is unanswered."
            )
            add("")
        for result in _sorted(results):
            out.extend(_finding_block(result, redacted=redacted, max_samples=max_samples))

    passed = [r for r in report.results if r.status is RuleStatus.PASS]
    if passed:
        add(f"<details><summary>Passed checks ({len(passed)})</summary>")
        add("")
        for result in sorted(passed, key=lambda r: r.rule_id):
            add(f"- `{result.rule_id}` - {_escape(redact_text(result.summary, redacted=redacted))}")
        add("")
        add("</details>")
        add("")

    add("## How this score was computed")
    add("")
    add("```")
    add(report.score_breakdown.explanation)
    add("```")
    add("")
    if report.score_breakdown.deductions:
        add("| Rule | Status | Severity | Points |")
        add("|---|---|---|---|")
        for deduction in report.score_breakdown.deductions:
            add(
                f"| `{deduction.rule_id}` | {deduction.status.value} | "
                f"{deduction.severity.value} | -{deduction.points:g} |"
            )
        add("")
    if report.score_breakdown.critical_unknown_rule_ids:
        add("**Critical checks that could not run:** "
            + ", ".join(f"`{r}`" for r in report.score_breakdown.critical_unknown_rule_ids))
        add("")

    if report.limitations:
        add("## Limitations of this run")
        add("")
        for item in report.limitations:
            add(f"- {_escape(redact_text(item, redacted=redacted))}")
        add("")

    add("---")
    add("")
    add(
        "_Campaign Preflight is a read-only linter. It does not guarantee "
        "deliverability, does not provide legal advice, and never activates a "
        "campaign._"
    )
    if not redacted:
        add("")
        add("> **This report is UNREDACTED and contains contact email addresses.**")
    add("")
    return "\n".join(out)
