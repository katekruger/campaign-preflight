"""Terminal rendering, with no third-party dependencies.

Written for a working terminal, not a demo screenshot: no box drawing, no
emoji, status carried by a word rather than only by a colour, and every line
readable when piped to a file or read aloud by a screen reader.

Colour is raw ANSI, applied only when the caller says the destination is a TTY.
Wrapping uses :mod:`textwrap` so long findings stay readable at 100 columns.
"""

from __future__ import annotations

import textwrap
from typing import Dict, List, Optional

from ..models import PreflightReport, Readiness, RuleResult, RuleStatus, Severity
from .redaction import redact_samples, redact_text

__all__ = ["render_terminal", "READINESS_STYLES"]

RESET = "\033[0m"

READINESS_STYLES: Dict[Readiness, str] = {
    Readiness.READY: "\033[1;32m",  # bold green
    Readiness.READY_WITH_WARNINGS: "\033[1;33m",  # bold yellow
    Readiness.NOT_READY: "\033[1;31m",  # bold red
    Readiness.INCOMPLETE: "\033[1;35m",  # bold magenta
}

SECTION_STYLES: Dict[str, str] = {
    "BLOCKERS": "\033[1;31m",
    "FAILURES": "\033[31m",
    "WARNINGS": "\033[33m",
    "UNKNOWN": "\033[35m",
}

BOLD = "\033[1m"
DIM = "\033[2m"

_SEVERITY_ORDER = {
    Severity.BLOCKER: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class _Writer:
    """Accumulates lines, applying colour only when enabled."""

    def __init__(self, *, color: bool, width: int) -> None:
        self.color = color
        self.width = width
        self.lines: List[str] = []

    def line(self, text: str = "", style: Optional[str] = None) -> None:
        if style and self.color:
            self.lines.append(f"{style}{text}{RESET}")
        else:
            self.lines.append(text)

    def wrapped(self, text: str, *, indent: str = "", style: Optional[str] = None) -> None:
        """Emit ``text`` wrapped to the writer's width, preserving an indent."""
        if not text:
            return
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                self.line()
                continue
            wrapped = textwrap.wrap(
                paragraph,
                width=max(40, self.width),
                initial_indent=indent,
                subsequent_indent=indent + "  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            for entry in wrapped or [indent + paragraph]:
                self.line(entry, style)

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def _readiness_label(readiness: Readiness) -> str:
    return readiness.value.replace("_", " ")


def _sort_key(result: RuleResult):
    return (_SEVERITY_ORDER.get(result.severity, 9), result.rule_id)


def _finding_lines(
    result: RuleResult, *, redacted: bool, max_samples: int, verbose: bool
) -> List[tuple]:
    """The lines describing one finding, as ``(text, indent)`` pairs."""
    marker = " (heuristic)" if result.heuristic else ""
    lines: List[tuple] = [
        (f"[{result.rule_id}]", ""),
        (f"{redact_text(result.summary, redacted=redacted)}{marker}", ""),
    ]

    samples = redact_samples(
        result.affected_record_samples, redacted=redacted, limit=max_samples
    )
    if samples:
        shown = ", ".join(samples)
        more = result.affected_record_count - len(samples)
        suffix = f" (+{more} more)" if more > 0 else ""
        lines.append((f"Affected: {shown}{suffix}", "  "))
    if verbose and result.evidence:
        for item in redact_samples(result.evidence, redacted=redacted, limit=max_samples):
            lines.append((f"Evidence: {item}", "  "))
    if verbose and result.explanation:
        lines.append((f"Why: {redact_text(result.explanation, redacted=redacted)}", "  "))
    if result.remediation:
        lines.append(
            (f"Remediation: {redact_text(result.remediation, redacted=redacted)}", "  ")
        )
    return lines


def render_terminal(
    report: PreflightReport,
    *,
    max_samples: int = 5,
    verbose: bool = False,
    quiet: bool = False,
    color: bool = True,
    width: Optional[int] = None,
) -> str:
    """Render a report as plain text. ANSI colour is applied only if ``color``."""
    out = _Writer(color=color, width=width or 100)
    redacted = report.redacted

    # -- header ------------------------------------------------------------
    out.line("CAMPAIGN PREFLIGHT", BOLD)
    out.line(f"Campaign: {report.campaign_name or report.campaign_id or 'unknown'}")
    out.line(f"Provider: {report.provider}")
    style = READINESS_STYLES.get(report.readiness, BOLD)
    if color:
        out.lines.append(f"Readiness: {style}{_readiness_label(report.readiness)}{RESET}")
    else:
        out.line(f"Readiness: {_readiness_label(report.readiness)}")
    out.line(f"Score: {report.score}/100")
    out.line(f"Confidence: {report.confidence.value}")

    if quiet:
        out.line(
            f"{report.blocker_count} blockers, {report.warning_count} warnings, "
            f"{report.unknown_count} unknown"
        )
        return out.render()

    # -- findings ----------------------------------------------------------
    sections = [
        ("BLOCKERS", [r for r in report.results if r.is_blocking]),
        (
            "FAILURES",
            [r for r in report.results if r.status is RuleStatus.FAIL and not r.is_blocking],
        ),
        ("WARNINGS", [r for r in report.results if r.status is RuleStatus.WARN]),
        ("UNKNOWN", [r for r in report.results if r.status is RuleStatus.UNKNOWN]),
    ]

    for heading, results in sections:
        if not results:
            continue
        out.line()
        out.line(heading, SECTION_STYLES.get(heading))
        for result in sorted(results, key=_sort_key):
            out.line()
            entries = _finding_lines(
                result, redacted=redacted, max_samples=max_samples, verbose=verbose
            )
            for index, (text, indent) in enumerate(entries):
                out.wrapped(
                    text, indent=indent, style=SECTION_STYLES.get(heading) if index == 0 else None
                )

    # -- limitations -------------------------------------------------------
    if report.limitations:
        out.line()
        out.line("LIMITATIONS", DIM)
        for item in report.limitations[:10]:
            out.wrapped(redact_text(item, redacted=redacted), indent="  ", style=DIM)
        if len(report.limitations) > 10:
            out.line(f"  ... and {len(report.limitations) - 10} more", DIM)

    # -- summary -----------------------------------------------------------
    out.line()
    out.line("-" * min(out.width, 78), DIM)
    out.line("Summary:")
    out.line(
        f"{report.blocker_count} blockers, {report.failure_count} failures, "
        f"{report.warning_count} warnings, {report.unknown_count} unknown, "
        f"{report.passed_count} passed"
    )
    lead_note = "+" if report.lead_count_is_partial else ""
    out.line(
        f"{report.lead_count}{lead_note} leads and {report.sender_count} sender(s) "
        f"checked in {report.duration_seconds:.1f}s"
    )
    if verbose:
        out.wrapped(f"Score derivation: {report.score_breakdown.explanation}")
    if report.confidence is not report.confidence.HIGH:
        out.line(
            f"Confidence is {report.confidence.value}: "
            f"{report.unknown_count} check(s) could not run.",
            DIM,
        )
    if not redacted:
        out.line("Output is UNREDACTED and contains contact addresses.", "\033[1;33m")
    out.line(report.snapshot_note, DIM)
    return out.render()
