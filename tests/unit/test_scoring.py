"""Scoring and readiness. These are the invariants the whole verdict rests on."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import (
    Confidence,
    Readiness,
    RuleCategory,
    RuleResult,
    RuleStatus,
    Severity,
)
from campaign_preflight.scoring import decide_readiness, score_results


def result(
    rule_id: str = "campaign.daily_volume",
    status: RuleStatus = RuleStatus.PASS,
    severity: Severity = Severity.MEDIUM,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        rule_version="1.0.0",
        title="t",
        category=RuleCategory.CAMPAIGN,
        severity=severity,
        status=status,
        summary=f"{rule_id} {status.value}",
    )


def verdict(results, config: PreflightConfig | None = None) -> tuple[int, Readiness, Confidence]:
    resolved = config or PreflightConfig()
    breakdown = score_results(results, resolved)
    return (
        breakdown.final_score,
        decide_readiness(results, breakdown, resolved),
        breakdown.confidence,
    )


class TestScore:
    def test_all_passing_scores_100(self) -> None:
        score, readiness, confidence = verdict([result(), result("campaign.has_leads")])
        assert (score, readiness, confidence) == (100, Readiness.READY, Confidence.HIGH)

    def test_no_results_scores_100(self) -> None:
        assert verdict([])[0] == 100

    @pytest.mark.parametrize(
        ("severity", "expected"),
        [
            (Severity.BLOCKER, 70),
            (Severity.HIGH, 85),
            (Severity.MEDIUM, 93),
            (Severity.LOW, 97),
            (Severity.INFO, 100),
        ],
    )
    def test_failure_weights(self, severity: Severity, expected: int) -> None:
        assert verdict([result(status=RuleStatus.FAIL, severity=severity)])[0] == expected

    @pytest.mark.parametrize(
        ("severity", "expected"),
        [(Severity.HIGH, 94), (Severity.MEDIUM, 97), (Severity.LOW, 99), (Severity.INFO, 100)],
    )
    def test_warning_weights(self, severity: Severity, expected: int) -> None:
        assert verdict([result(status=RuleStatus.WARN, severity=severity)])[0] == expected

    def test_score_never_goes_below_zero(self) -> None:
        results = [
            result(f"campaign.r{i}", RuleStatus.FAIL, Severity.BLOCKER) for i in range(20)
        ]
        assert verdict(results)[0] == 0

    def test_unknown_deducts_nothing(self) -> None:
        """A provider outage must not look like a bad campaign."""
        assert verdict([result(status=RuleStatus.UNKNOWN, severity=Severity.BLOCKER)])[0] == 100

    def test_not_applicable_deducts_nothing(self) -> None:
        assert verdict([result(status=RuleStatus.NOT_APPLICABLE)])[0] == 100

    def test_weights_are_configurable(self) -> None:
        config = PreflightConfig(scoring={"fail_weights": {"MEDIUM": 50.0}})
        assert verdict([result(status=RuleStatus.FAIL)], config)[0] == 50

    def test_order_does_not_affect_the_score(self) -> None:
        a = result("campaign.a", RuleStatus.FAIL, Severity.HIGH)
        b = result("campaign.b", RuleStatus.WARN, Severity.LOW)
        assert verdict([a, b])[0] == verdict([b, a])[0]

    def test_breakdown_itemizes_every_deduction(self) -> None:
        results = [
            result("campaign.a", RuleStatus.FAIL, Severity.HIGH),
            result("campaign.b", RuleStatus.WARN, Severity.LOW),
            result("campaign.c"),
        ]
        breakdown = score_results(results, PreflightConfig())
        assert [d.rule_id for d in breakdown.deductions] == ["campaign.a", "campaign.b"]
        assert sum(d.points for d in breakdown.deductions) == 16.0
        assert "= 84" in breakdown.explanation

    def test_explanation_is_arithmetic_a_human_can_check(self) -> None:
        breakdown = score_results(
            [result(status=RuleStatus.FAIL, severity=Severity.HIGH)], PreflightConfig()
        )
        assert breakdown.explanation.startswith("100 - (")


class TestReadiness:
    def test_blocker_always_produces_not_ready(self) -> None:
        results = [result("campaign.x", RuleStatus.FAIL, Severity.BLOCKER)]
        assert verdict(results)[1] is Readiness.NOT_READY

    def test_high_numeric_score_cannot_override_a_blocker(self) -> None:
        """A single BLOCKER at score 70 is still NOT_READY."""
        score, readiness, _ = verdict([result("campaign.x", RuleStatus.FAIL, Severity.BLOCKER)])
        assert score == 70
        assert readiness is Readiness.NOT_READY

    def test_high_failure_blocks_by_default(self) -> None:
        results = [result("campaign.x", RuleStatus.FAIL, Severity.HIGH)]
        assert verdict(results)[1] is Readiness.NOT_READY

    def test_high_failure_blocking_can_be_disabled(self) -> None:
        config = PreflightConfig(scoring={"high_failure_blocks": False})
        results = [result("campaign.x", RuleStatus.FAIL, Severity.HIGH)]
        assert verdict(results, config)[1] is Readiness.READY_WITH_WARNINGS

    def test_critical_unknown_produces_incomplete(self) -> None:
        results = [result("campaign.has_leads", RuleStatus.UNKNOWN)]
        assert verdict(results)[1] is Readiness.INCOMPLETE

    def test_blocker_outranks_incomplete(self) -> None:
        results = [
            result("campaign.has_leads", RuleStatus.UNKNOWN),
            result("campaign.x", RuleStatus.FAIL, Severity.BLOCKER),
        ]
        assert verdict(results)[1] is Readiness.NOT_READY

    def test_non_critical_unknown_does_not_produce_incomplete(self) -> None:
        results = [result("campaign.start_in_past", RuleStatus.UNKNOWN)]
        assert verdict(results)[1] is Readiness.READY

    def test_warnings_produce_ready_with_warnings(self) -> None:
        results = [result("campaign.x", RuleStatus.WARN, Severity.LOW)]
        assert verdict(results)[1] is Readiness.READY_WITH_WARNINGS

    def test_low_severity_failure_is_not_ready_with_warnings(self) -> None:
        config = PreflightConfig(scoring={"high_failure_blocks": False})
        results = [result("campaign.x", RuleStatus.FAIL, Severity.LOW)]
        assert verdict(results, config)[1] is Readiness.READY_WITH_WARNINGS

    def test_critical_rule_list_is_configurable(self) -> None:
        config = PreflightConfig(scoring={"critical_rules": ["campaign.start_in_past"]})
        results = [result("campaign.start_in_past", RuleStatus.UNKNOWN)]
        assert verdict(results, config)[1] is Readiness.INCOMPLETE


class TestConfidence:
    def test_no_unknowns_is_high(self) -> None:
        assert verdict([result()])[2] is Confidence.HIGH

    def test_non_critical_unknown_is_medium(self) -> None:
        assert verdict([result("campaign.start_in_past", RuleStatus.UNKNOWN)])[2] is Confidence.MEDIUM

    def test_critical_unknown_is_low(self) -> None:
        assert verdict([result("campaign.has_leads", RuleStatus.UNKNOWN)])[2] is Confidence.LOW

    def test_excluded_rules_are_listed(self) -> None:
        results = [
            result("campaign.a", RuleStatus.UNKNOWN),
            result("campaign.b", RuleStatus.NOT_APPLICABLE),
        ]
        breakdown = score_results(results, PreflightConfig())
        assert breakdown.excluded_rule_ids == ("campaign.a", "campaign.b")
