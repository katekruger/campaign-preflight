"""Readiness scoring. The formula is public and every deduction is itemized.

Design commitments, in order of precedence:

1. A BLOCKER failure always produces ``NOT_READY``. The numeric score cannot
   override it, no matter how high.
2. An ``UNKNOWN`` on a critical check produces ``INCOMPLETE``. "We could not
   check" is its own verdict, not a passing grade with an asterisk.
3. ``UNKNOWN`` reduces *confidence*, not score. Deducting points for a check
   that did not run would let a provider outage look like a bad campaign.
4. ``NOT_APPLICABLE`` affects nothing.

The arithmetic is deterministic: the same results in any order produce the same
score, because deductions are sorted by rule id before they are summed.
"""

from __future__ import annotations

from .config import PreflightConfig
from .models import (
    Confidence,
    Readiness,
    RuleResult,
    RuleStatus,
    ScoreBreakdown,
    ScoreDeduction,
    Severity,
)

__all__ = ["score_results", "decide_readiness", "SCORING_FORMULA"]

SCORING_FORMULA = """\
score = 100 - sum(weight[status][severity] for every FAIL and WARN)
clamped to [0, 100]; UNKNOWN and NOT_APPLICABLE deduct nothing.

readiness:
  NOT_READY    if any BLOCKER FAIL, or (high_failure_blocks and any HIGH FAIL)
  INCOMPLETE   else if any critical rule is UNKNOWN
  READY_WITH_WARNINGS  else if any FAIL or WARN
  READY        otherwise

confidence:
  HIGH    no UNKNOWN results
  MEDIUM  UNKNOWN results present, none of them critical
  LOW     one or more critical rules are UNKNOWN\
"""


def score_results(
    results: tuple[RuleResult, ...] | list[RuleResult], config: PreflightConfig
) -> ScoreBreakdown:
    """Compute the score, confidence, and the full derivation."""
    scoring = config.scoring
    critical = frozenset(scoring.critical_rules)

    deductions: list[ScoreDeduction] = []
    excluded: list[str] = []
    critical_unknowns: list[str] = []

    for result in sorted(results, key=lambda r: r.rule_id):
        if result.status is RuleStatus.NOT_APPLICABLE:
            excluded.append(result.rule_id)
            continue
        if result.status is RuleStatus.UNKNOWN:
            excluded.append(result.rule_id)
            if result.rule_id in critical:
                critical_unknowns.append(result.rule_id)
            continue
        if result.status is RuleStatus.PASS:
            continue

        weights = (
            scoring.fail_weights
            if result.status is RuleStatus.FAIL
            else scoring.warn_weights
        )
        points = float(weights.get(result.severity, 0.0))
        if points <= 0:
            continue
        deductions.append(
            ScoreDeduction(
                rule_id=result.rule_id,
                status=result.status,
                severity=result.severity,
                points=points,
                reason=result.summary,
            )
        )

    total = sum(d.points for d in deductions)
    final = int(round(max(0.0, min(100.0, 100.0 - total))))

    unknown_ids = [
        r.rule_id for r in results if r.status is RuleStatus.UNKNOWN
    ]
    if critical_unknowns:
        confidence = Confidence.LOW
    elif unknown_ids:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.HIGH

    if deductions:
        lines = ", ".join(
            f"{d.rule_id} {d.status.value}/{d.severity.value} -{d.points:g}"
            for d in deductions
        )
        explanation = f"100 - ({lines}) = {final}"
    else:
        explanation = "No FAIL or WARN results: the score stays at 100."
    if unknown_ids:
        explanation += (
            f" {len(unknown_ids)} check(s) could not run and deduct no points; "
            f"they lower confidence instead."
        )

    return ScoreBreakdown(
        starting_score=100.0,
        deductions=tuple(deductions),
        final_score=final,
        confidence=confidence,
        excluded_rule_ids=tuple(sorted(excluded)),
        critical_unknown_rule_ids=tuple(sorted(critical_unknowns)),
        explanation=explanation,
    )


def decide_readiness(
    results: tuple[RuleResult, ...] | list[RuleResult],
    breakdown: ScoreBreakdown,
    config: PreflightConfig,
) -> Readiness:
    """Map results to a verdict. Blockers win over everything else."""
    failures = [r for r in results if r.status is RuleStatus.FAIL]

    if any(r.severity is Severity.BLOCKER for r in failures):
        return Readiness.NOT_READY
    if config.scoring.high_failure_blocks and any(
        r.severity is Severity.HIGH for r in failures
    ):
        return Readiness.NOT_READY
    if breakdown.critical_unknown_rule_ids:
        return Readiness.INCOMPLETE
    if failures or any(r.status is RuleStatus.WARN for r in results):
        return Readiness.READY_WITH_WARNINGS
    return Readiness.READY
