"""Behavioural tests for the sender readiness rules."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import (
    Capability,
    CapabilityStatus,
    RuleStatus,
    Severity,
)
from helpers import make_campaign, make_context, make_sender, run_rule


class TestAttachment:
    def test_no_senders_is_a_blocker(self) -> None:
        result = run_rule("senders.none_attached", make_context(senders=[]))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_senders_attached_passes(self) -> None:
        assert run_rule("senders.none_attached", make_context()).status is RuleStatus.PASS


class TestEnabledState:
    def test_all_enabled_passes(self) -> None:
        assert run_rule("senders.disabled", make_context()).status is RuleStatus.PASS

    def test_all_disabled_is_a_blocker(self) -> None:
        senders = [make_sender(enabled=False, status_label="paused")]
        result = run_rule("senders.disabled", make_context(senders=senders))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_some_disabled_warns(self) -> None:
        senders = [
            make_sender(),
            make_sender("b@example.com", enabled=False, status_label="paused"),
        ]
        result = run_rule("senders.disabled", make_context(senders=senders))
        assert result.status is RuleStatus.WARN
        assert result.affected_record_count == 1

    def test_no_state_reported_is_unknown(self) -> None:
        senders = [make_sender(enabled=None, status_label=None)]
        assert (
            run_rule("senders.disabled", make_context(senders=senders)).status is RuleStatus.UNKNOWN
        )


class TestHealth:
    def test_healthy_senders_pass(self) -> None:
        assert run_rule("senders.health_below_threshold", make_context()).status is RuleStatus.PASS

    def test_all_below_threshold_is_a_blocker(self) -> None:
        senders = [make_sender(health_score=41.0)]
        result = run_rule("senders.health_below_threshold", make_context(senders=senders))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER
        assert "No attached sender meets" in result.summary

    def test_some_below_threshold_warns(self) -> None:
        senders = [make_sender(), make_sender("b@example.com", health_score=41.0)]
        result = run_rule("senders.health_below_threshold", make_context(senders=senders))
        assert result.status is RuleStatus.WARN
        assert result.severity is Severity.HIGH

    def test_threshold_is_configurable(self) -> None:
        senders = [make_sender(health_score=41.0)]
        config = PreflightConfig(rules={"senders.health_below_threshold": {"minimum_score": 30}})
        assert (
            run_rule("senders.health_below_threshold", make_context(senders=senders), config).status
            is RuleStatus.PASS
        )

    def test_unscored_senders_are_not_assumed_healthy(self) -> None:
        senders = [make_sender(), make_sender("b@example.com", health_score=None)]
        result = run_rule("senders.health_below_threshold", make_context(senders=senders))
        assert result.status is RuleStatus.WARN
        assert "not assessed" in result.summary

    def test_no_scores_at_all_is_unknown(self) -> None:
        senders = [make_sender(health_score=None)]
        assert (
            run_rule("senders.health_below_threshold", make_context(senders=senders)).status
            is RuleStatus.UNKNOWN
        )

    def test_capability_unavailable_yields_unknown(self) -> None:
        ctx = make_context(
            capabilities={Capability.SENDER_HEALTH: CapabilityStatus.UNAVAILABLE_PERMISSIONS}
        )
        assert run_rule("senders.health_below_threshold", ctx).status is RuleStatus.UNKNOWN


class TestCapacity:
    def test_within_capacity_passes(self) -> None:
        assert run_rule("senders.daily_capacity", make_context()).status is RuleStatus.PASS

    def test_over_per_sender_limit_fails(self) -> None:
        ctx = make_context(
            campaign=make_campaign(daily_limit=200), senders=[make_sender(daily_limit=50)]
        )
        result = run_rule("senders.daily_capacity", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.metadata["per_sender_share"] == 200

    def test_near_limit_warns(self) -> None:
        ctx = make_context(
            campaign=make_campaign(daily_limit=57), senders=[make_sender(daily_limit=60)]
        )
        assert run_rule("senders.daily_capacity", ctx).status is RuleStatus.WARN

    def test_unknown_campaign_limit_is_unknown(self) -> None:
        ctx = make_context(campaign=make_campaign(daily_limit=None))
        assert run_rule("senders.daily_capacity", ctx).status is RuleStatus.UNKNOWN

    def test_aggregate_shortfall_fails(self) -> None:
        ctx = make_context(
            campaign=make_campaign(daily_limit=500),
            senders=[make_sender(daily_limit=60), make_sender("b@example.com", daily_limit=60)],
        )
        result = run_rule("senders.aggregate_capacity", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.metadata["aggregate_capacity"] == 120

    def test_partial_limits_are_unknown_not_a_partial_sum(self) -> None:
        """Summing only the senders that report a limit would understate capacity."""
        ctx = make_context(
            campaign=make_campaign(daily_limit=100),
            senders=[make_sender(daily_limit=60), make_sender("b@example.com", daily_limit=None)],
        )
        result = run_rule("senders.aggregate_capacity", ctx)
        assert result.status is RuleStatus.UNKNOWN
        assert result.metadata["senders_without_limit"] == 1


class TestAvailabilityAndErrors:
    def test_at_least_one_usable_passes(self) -> None:
        senders = [
            make_sender(),
            make_sender("b@example.com", enabled=False, status_label="paused"),
        ]
        assert (
            run_rule("senders.all_unavailable", make_context(senders=senders)).status
            is RuleStatus.PASS
        )

    def test_all_unusable_is_a_blocker(self) -> None:
        senders = [
            make_sender(enabled=False, status_label="paused"),
            make_sender("b@example.com", status_label="connection_error", status_is_error=True),
        ]
        result = run_rule("senders.all_unavailable", make_context(senders=senders))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_setup_pending_counts_as_unusable(self) -> None:
        senders = [make_sender(setup_pending=True)]
        assert (
            run_rule("senders.all_unavailable", make_context(senders=senders)).status
            is RuleStatus.FAIL
        )

    @pytest.mark.parametrize(
        "label", ["connection_error", "soft_bounce_error", "sending_error", "suspended"]
    )
    def test_error_states_are_reported(self, label: str) -> None:
        senders = [
            make_sender(status_label=label, status_is_error=True),
            make_sender("b@example.com"),
        ]
        result = run_rule("senders.error_state", make_context(senders=senders))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.HIGH

    def test_all_errored_escalates_to_blocker(self) -> None:
        senders = [make_sender(status_label="connection_error", status_is_error=True)]
        result = run_rule("senders.error_state", make_context(senders=senders))
        assert result.severity is Severity.BLOCKER

    @pytest.mark.parametrize("warmup", ["banned", "issue", "spam_folder"])
    def test_warmup_problems_are_reported(self, warmup: str) -> None:
        senders = [make_sender(warmup_status=warmup), make_sender("b@example.com")]
        assert (
            run_rule("senders.error_state", make_context(senders=senders)).status is RuleStatus.FAIL
        )

    def test_healthy_senders_report_no_errors(self) -> None:
        assert run_rule("senders.error_state", make_context()).status is RuleStatus.PASS


class TestHealthAvailability:
    def test_full_coverage_passes(self) -> None:
        assert run_rule("senders.health_unavailable", make_context()).status is RuleStatus.PASS

    def test_partial_coverage_warns(self) -> None:
        senders = [make_sender(), make_sender("b@example.com", health_score=None)]
        result = run_rule("senders.health_unavailable", make_context(senders=senders))
        assert result.status is RuleStatus.WARN
        assert result.affected_record_count == 1

    def test_unsupported_capability_is_unknown(self) -> None:
        ctx = make_context(capabilities={Capability.SENDER_HEALTH: CapabilityStatus.UNSUPPORTED})
        result = run_rule("senders.health_unavailable", ctx)
        assert result.status is RuleStatus.UNKNOWN
        assert "No deliverability score is being invented" in result.explanation.replace("\n", " ")
