"""Behavioural tests for the campaign configuration rules."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import RuleStatus, Severity
from helpers import (
    FIXED_NOW,
    make_campaign,
    make_context,
    make_schedule,
    make_sender,
    make_step,
    make_window,
    run_rule,
)


class TestCampaignExists:
    def test_readable_campaign_passes(self) -> None:
        result = run_rule("campaign.exists", make_context())
        assert result.status is RuleStatus.PASS

    def test_missing_campaign_fails(self) -> None:
        ctx = make_context(campaign=None)
        result = run_rule("campaign.exists", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_campaign_with_no_identity_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(id=None, name=None))
        assert run_rule("campaign.exists", ctx).status is RuleStatus.FAIL


class TestCampaignStatus:
    @pytest.mark.parametrize("status", ["draft", "paused", "scheduled"])
    def test_preflight_ready_statuses_pass(self, status: str) -> None:
        ctx = make_context(campaign=make_campaign(status=status))
        assert run_rule("campaign.status_suitable", ctx).status is RuleStatus.PASS

    def test_active_campaign_warns_by_default(self) -> None:
        ctx = make_context(campaign=make_campaign(status="active"))
        result = run_rule("campaign.status_suitable", ctx)
        assert result.status is RuleStatus.WARN
        assert "already active" in result.summary

    def test_active_campaign_can_be_configured_to_fail(self) -> None:
        ctx = make_context(campaign=make_campaign(status="active"))
        config = PreflightConfig(
            rules={"campaign.status_suitable": {"warn_on_active": False}}
        )
        assert run_rule("campaign.status_suitable", ctx, config).status is RuleStatus.FAIL

    def test_provider_error_state_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(status="accounts_unhealthy"))
        result = run_rule("campaign.status_suitable", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.HIGH

    def test_completed_campaign_is_informational(self) -> None:
        ctx = make_context(campaign=make_campaign(status="completed"))
        result = run_rule("campaign.status_suitable", ctx)
        assert result.status is RuleStatus.WARN
        assert result.severity is Severity.LOW

    def test_unrecognized_status_is_unknown_not_a_guess(self) -> None:
        ctx = make_context(campaign=make_campaign(status="unknown:77"))
        result = run_rule("campaign.status_suitable", ctx)
        assert result.status is RuleStatus.UNKNOWN

    def test_absent_status_is_unknown(self) -> None:
        ctx = make_context(campaign=make_campaign(status=None))
        assert run_rule("campaign.status_suitable", ctx).status is RuleStatus.UNKNOWN


class TestCampaignSteps:
    def test_no_steps_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(steps=()))
        result = run_rule("campaign.has_steps", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_all_variants_disabled_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(steps=(make_step(disabled=True),)))
        assert run_rule("campaign.has_steps", ctx).status is RuleStatus.FAIL

    def test_counts_distinct_steps_not_variants(self) -> None:
        steps = (make_step(0), make_step(0, variant_index=1), make_step(1))
        ctx = make_context(campaign=make_campaign(steps=steps))
        result = run_rule("campaign.has_steps", ctx)
        assert result.status is RuleStatus.PASS
        assert result.metadata["step_count"] == 2
        assert result.metadata["variant_count"] == 3


class TestCampaignSenders:
    def test_no_senders_fails(self) -> None:
        assert run_rule("campaign.has_senders", make_context(senders=[])).status is RuleStatus.FAIL

    def test_senders_present_passes(self) -> None:
        assert run_rule("campaign.has_senders", make_context()).status is RuleStatus.PASS


class TestDailyVolume:
    @pytest.mark.parametrize(
        ("limit", "expected", "severity"),
        [
            (50, RuleStatus.PASS, None),
            (100, RuleStatus.PASS, None),
            (150, RuleStatus.WARN, Severity.MEDIUM),
            (250, RuleStatus.WARN, Severity.MEDIUM),
            (400, RuleStatus.FAIL, Severity.BLOCKER),
        ],
    )
    def test_thresholds(self, limit: int, expected: RuleStatus, severity) -> None:
        ctx = make_context(campaign=make_campaign(daily_limit=limit))
        result = run_rule("campaign.daily_volume", ctx)
        assert result.status is expected
        if severity:
            assert result.severity is severity

    def test_zero_limit_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(daily_limit=0))
        assert run_rule("campaign.daily_volume", ctx).status is RuleStatus.FAIL

    def test_unset_limit_warns_by_default(self) -> None:
        ctx = make_context(campaign=make_campaign(daily_limit=None))
        assert run_rule("campaign.daily_volume", ctx).status is RuleStatus.WARN

    def test_thresholds_are_configurable(self) -> None:
        ctx = make_context(campaign=make_campaign(daily_limit=150))
        config = PreflightConfig(
            rules={"campaign.daily_volume": {"warning_above": 500, "blocker_above": 900}}
        )
        assert run_rule("campaign.daily_volume", ctx, config).status is RuleStatus.PASS


class TestStopOnReply:
    def test_enabled_passes(self) -> None:
        assert run_rule("campaign.stop_on_reply", make_context()).status is RuleStatus.PASS

    def test_disabled_is_a_blocker(self) -> None:
        ctx = make_context(campaign=make_campaign(stop_on_reply=False))
        result = run_rule("campaign.stop_on_reply", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_null_is_unknown_not_disabled(self) -> None:
        """The distinction that keeps this tool honest: null is not False."""
        ctx = make_context(campaign=make_campaign(stop_on_reply=None))
        assert run_rule("campaign.stop_on_reply", ctx).status is RuleStatus.UNKNOWN

    def test_auto_reply_disabled_is_noted_on_a_pass(self) -> None:
        ctx = make_context(campaign=make_campaign(stop_on_auto_reply=False))
        result = run_rule("campaign.stop_on_reply", ctx)
        assert result.status is RuleStatus.PASS
        assert "auto-reply" in result.summary


class TestCampaignLeads:
    def test_zero_leads_fails(self) -> None:
        result = run_rule("campaign.has_leads", make_context(leads=[]))
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER


class TestDateCoherence:
    def test_end_before_start_fails(self) -> None:
        schedule = make_schedule(start_date=date(2027, 2, 1), end_date=date(2027, 1, 1))
        ctx = make_context(campaign=make_campaign(schedule=schedule))
        result = run_rule("campaign.date_coherence", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_same_day_warns(self) -> None:
        day = date(2027, 1, 1)
        schedule = make_schedule(start_date=day, end_date=day)
        ctx = make_context(campaign=make_campaign(schedule=schedule))
        assert run_rule("campaign.date_coherence", ctx).status is RuleStatus.WARN

    def test_no_dates_is_not_applicable(self) -> None:
        result = run_rule("campaign.date_coherence", make_context())
        assert result.status is RuleStatus.NOT_APPLICABLE


class TestStartInPast:
    def test_recent_start_passes(self) -> None:
        schedule = make_schedule(start_date=FIXED_NOW.date())
        ctx = make_context(campaign=make_campaign(schedule=schedule))
        assert run_rule("campaign.start_in_past", ctx).status is RuleStatus.PASS

    def test_old_start_warns(self) -> None:
        schedule = make_schedule(start_date=FIXED_NOW.date() - timedelta(days=200))
        ctx = make_context(campaign=make_campaign(schedule=schedule))
        result = run_rule("campaign.start_in_past", ctx)
        assert result.status is RuleStatus.WARN
        assert result.metadata["days_ago"] == 200

    def test_grace_period_is_configurable(self) -> None:
        schedule = make_schedule(start_date=FIXED_NOW.date() - timedelta(days=200))
        ctx = make_context(campaign=make_campaign(schedule=schedule))
        config = PreflightConfig(rules={"campaign.start_in_past": {"grace_days": 365}})
        assert run_rule("campaign.start_in_past", ctx, config).status is RuleStatus.PASS


class TestScheduleWindows:
    def test_no_schedule_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(schedule=None))
        assert run_rule("campaign.schedule_windows", ctx).status is RuleStatus.FAIL

    def test_no_windows_fails(self) -> None:
        ctx = make_context(campaign=make_campaign(schedule=make_schedule(windows=())))
        assert run_rule("campaign.schedule_windows", ctx).status is RuleStatus.FAIL

    def test_window_without_times_fails(self) -> None:
        broken = make_window(start=None, end=None)
        ctx = make_context(campaign=make_campaign(schedule=make_schedule(windows=(broken,))))
        assert run_rule("campaign.schedule_windows", ctx).status is RuleStatus.FAIL

    def test_partially_broken_windows_warn(self) -> None:
        windows = (make_window(), make_window(name="Broken", end=None))
        ctx = make_context(campaign=make_campaign(schedule=make_schedule(windows=windows)))
        result = run_rule("campaign.schedule_windows", ctx)
        assert result.status is RuleStatus.WARN
        assert result.affected_record_count == 1
