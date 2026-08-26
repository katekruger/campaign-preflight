"""Behavioural tests for the schedule and timezone rules."""

from __future__ import annotations

from datetime import date, time

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import RuleStatus, Severity
from helpers import make_campaign, make_context, make_schedule, make_window, run_rule


def with_schedule(**overrides):
    return make_context(campaign=make_campaign(schedule=make_schedule(**overrides)))


class TestTimezone:
    def test_declared_timezone_passes(self) -> None:
        assert run_rule("schedule.missing_timezone", make_context()).status is RuleStatus.PASS

    def test_window_without_timezone_fails(self) -> None:
        ctx = make_context(
            campaign=make_campaign(
                timezone_name=None,
                schedule=make_schedule(
                    timezone_name=None, windows=(make_window(timezone_name=None),)
                ),
            )
        )
        assert run_rule("schedule.missing_timezone", ctx).status is RuleStatus.FAIL

    @pytest.mark.parametrize(
        "name", ["America/New_York", "Europe/London", "Asia/Tokyo", "UTC", "America/Phoenix"]
    )
    def test_valid_iana_zones_pass(self, name: str) -> None:
        ctx = with_schedule(timezone_name=name, windows=(make_window(timezone_name=name),))
        assert run_rule("schedule.invalid_timezone", ctx).status is RuleStatus.PASS

    @pytest.mark.parametrize("name", ["PST", "America/New York", "Not/AZone", "EST5EDT typo"])
    def test_invalid_zones_are_blockers(self, name: str) -> None:
        ctx = with_schedule(timezone_name=name, windows=(make_window(timezone_name=name),))
        result = run_rule("schedule.invalid_timezone", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER


class TestBusinessHours:
    def test_window_inside_business_hours_passes(self) -> None:
        assert (
            run_rule("schedule.outside_business_hours", make_context()).status is RuleStatus.PASS
        )

    def test_early_start_warns(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(5, 30)),))
        assert run_rule("schedule.outside_business_hours", ctx).status is RuleStatus.WARN

    def test_late_end_warns(self) -> None:
        ctx = with_schedule(windows=(make_window(end=time(22, 0)),))
        assert run_rule("schedule.outside_business_hours", ctx).status is RuleStatus.WARN

    def test_bounds_are_configurable(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(5, 30)),))
        config = PreflightConfig(settings={"business_hours_start": "05:00"})
        assert (
            run_rule("schedule.outside_business_hours", ctx, config).status is RuleStatus.PASS
        )

    def test_midnight_crossing_window_is_reported(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(22, 0), end=time(6, 0)),))
        result = run_rule("schedule.outside_business_hours", ctx)
        assert result.status is RuleStatus.WARN
        assert "crosses midnight" in result.affected_record_samples[0]


class TestDays:
    def test_weekday_only_passes(self) -> None:
        assert run_rule("schedule.weekend_sending", make_context()).status is RuleStatus.PASS

    @pytest.mark.parametrize("day", [0, 6])
    def test_weekend_days_warn(self, day: int) -> None:
        ctx = with_schedule(windows=(make_window(days=frozenset({day})),))
        assert run_rule("schedule.weekend_sending", ctx).status is RuleStatus.WARN

    def test_weekend_can_be_allowed(self) -> None:
        ctx = with_schedule(windows=(make_window(days=frozenset({6})),))
        config = PreflightConfig(settings={"allow_weekend_sending": True})
        assert (
            run_rule("schedule.weekend_sending", ctx, config).status is RuleStatus.NOT_APPLICABLE
        )

    def test_no_active_days_is_a_blocker(self) -> None:
        ctx = with_schedule(windows=(make_window(days=frozenset()),))
        result = run_rule("schedule.no_active_days", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_one_dead_window_among_several_warns(self) -> None:
        windows = (make_window(), make_window(name="Dead", days=frozenset()))
        ctx = with_schedule(windows=windows)
        assert run_rule("schedule.no_active_days", ctx).status is RuleStatus.WARN


class TestDateOrdering:
    def test_start_after_end_fails(self) -> None:
        ctx = with_schedule(start_date=date(2027, 2, 1), end_date=date(2027, 1, 1))
        assert run_rule("schedule.start_after_end", ctx).status is RuleStatus.FAIL

    def test_correct_order_passes(self) -> None:
        ctx = with_schedule(start_date=date(2027, 1, 1), end_date=date(2027, 2, 1))
        assert run_rule("schedule.start_after_end", ctx).status is RuleStatus.PASS

    def test_zero_length_window_is_a_blocker(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(9, 0), end=time(9, 0)),))
        result = run_rule("schedule.window_start_after_end", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_inverted_window_fails_by_default(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(17, 0), end=time(9, 0)),))
        assert run_rule("schedule.window_start_after_end", ctx).status is RuleStatus.FAIL

    def test_overnight_window_can_be_allowed(self) -> None:
        ctx = with_schedule(windows=(make_window(start=time(22, 0), end=time(6, 0)),))
        config = PreflightConfig(
            rules={"schedule.window_start_after_end": {"allow_overnight": True}}
        )
        assert (
            run_rule("schedule.window_start_after_end", ctx, config).status is RuleStatus.PASS
        )


class TestDaylightSaving:
    def test_no_dst_zone_reports_nothing(self) -> None:
        ctx = with_schedule(
            start_date=date(2027, 1, 1),
            end_date=date(2027, 12, 1),
            timezone_name="America/Phoenix",
            windows=(make_window(timezone_name="America/Phoenix"),),
        )
        assert run_rule("schedule.dst_transition", ctx).status is RuleStatus.PASS

    def test_transition_inside_the_window_is_reported(self) -> None:
        ctx = with_schedule(
            start_date=date(2027, 1, 1),
            end_date=date(2027, 12, 1),
            timezone_name="America/New_York",
            windows=(make_window(timezone_name="America/New_York"),),
        )
        result = run_rule("schedule.dst_transition", ctx)
        assert result.status is RuleStatus.WARN
        assert result.severity is Severity.INFO
        assert result.affected_record_count == 2

    def test_unresolvable_zone_is_unknown_not_a_pass(self) -> None:
        ctx = with_schedule(
            timezone_name="Not/AZone", windows=(make_window(timezone_name="Not/AZone"),)
        )
        assert run_rule("schedule.dst_transition", ctx).status is RuleStatus.UNKNOWN


class TestTimezoneMismatch:
    def test_unconfigured_target_is_not_applicable(self) -> None:
        assert (
            run_rule("schedule.timezone_mismatch", make_context()).status
            is RuleStatus.NOT_APPLICABLE
        )

    def test_matching_target_passes(self) -> None:
        config = PreflightConfig(settings={"target_timezone": "America/Phoenix"})
        assert run_rule("schedule.timezone_mismatch", make_context(), config).status is RuleStatus.PASS

    def test_different_target_warns(self) -> None:
        config = PreflightConfig(settings={"target_timezone": "Europe/London"})
        result = run_rule("schedule.timezone_mismatch", make_context(), config)
        assert result.status is RuleStatus.WARN
        assert result.metadata["target"] == "Europe/London"

    def test_equivalent_zones_do_not_warn(self) -> None:
        """Different name, same wall clock: not a mismatch worth reporting."""
        ctx = with_schedule(
            start_date=date(2027, 1, 15),
            timezone_name="America/New_York",
            windows=(make_window(timezone_name="America/New_York"),),
        )
        config = PreflightConfig(settings={"target_timezone": "America/Toronto"})
        assert run_rule("schedule.timezone_mismatch", ctx, config).status is RuleStatus.PASS
