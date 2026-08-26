"""Schedule and timezone rules (checks 59-67).

Timezone validity is resolved through the standard library's IANA database via
``zoneinfo``. If the host has no tzdata available, these rules return UNKNOWN
rather than guessing -- an unverifiable timezone is not a valid one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Campaign,
    CampaignSchedule,
    Capability,
    PreflightContext,
    RuleCategory,
    RuleResult,
    SendingWindow,
    Severity,
)
from ..normalization import parse_clock_time
from .base import Rule, register

__all__: list[str] = []

WEEKEND_DAYS = frozenset({0, 6})  # 0 = Sunday, 6 = Saturday
_DAY_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _zone(name: str | None) -> ZoneInfo | None:
    """Resolve an IANA timezone name, or ``None`` if it is not resolvable."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


class _ScheduleRule(Rule):
    category = RuleCategory.SCHEDULE
    requires: ClassVar[tuple[Capability, ...]] = (Capability.CAMPAIGN,)

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples

    def schedule_or_na(self, ctx: PreflightContext) -> CampaignSchedule | RuleResult:
        campaign: Campaign | None = ctx.campaign
        assert campaign is not None
        if campaign.schedule is None:
            return self.unknown("The campaign has no schedule to inspect.")
        return campaign.schedule

    @staticmethod
    def window_zones(
        schedule: CampaignSchedule, campaign: Campaign
    ) -> list[tuple[SendingWindow, str | None]]:
        return [
            (w, w.timezone_name or schedule.timezone_name or campaign.timezone_name)
            for w in schedule.windows
        ]


@register
class MissingTimezone(_ScheduleRule):
    rule_id = "schedule.missing_timezone"
    title = "The schedule declares a timezone"
    category = RuleCategory.SCHEDULE
    severity = Severity.HIGH
    description = (
        "Without a declared timezone the provider picks one, and a 9am send "
        "lands at whatever hour that turns out to be for your recipients."
    )
    remediation = "Set an explicit timezone on the campaign schedule."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        campaign = ctx.campaign
        assert campaign is not None
        if not schedule.windows:
            if schedule.timezone_name or campaign.timezone_name:
                return self.passed(
                    f"Campaign timezone is {schedule.timezone_name or campaign.timezone_name}."
                )
            return self.failed("No timezone is declared anywhere on the campaign.")
        missing = [w.name for w, tz in self.window_zones(schedule, campaign) if not tz]
        if not missing:
            return self.passed(f"All {len(schedule.windows)} sending window(s) declare a timezone.")
        return self.failed(
            f"{len(missing)} of {len(schedule.windows)} sending window(s) declare no timezone.",
            affected=len(missing),
            samples=self.sample(missing, self.limit(config)),
        )


@register
class InvalidTimezone(_ScheduleRule):
    rule_id = "schedule.invalid_timezone"
    title = "Declared timezones are valid IANA zones"
    category = RuleCategory.SCHEDULE
    severity = Severity.BLOCKER
    description = (
        "A timezone name the system cannot resolve ('EST5EDT typo', 'PST', "
        "'America/New York') will be rejected or silently defaulted by the provider."
    )
    remediation = "Use a valid IANA name such as America/New_York or Europe/London."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        campaign = ctx.campaign
        assert campaign is not None
        names: dict[str, str] = {}
        for window, tz in self.window_zones(schedule, campaign):
            if tz:
                names[tz] = window.name
        if campaign.timezone_name:
            names.setdefault(campaign.timezone_name, "campaign")
        if not names:
            return self.not_applicable("No timezone is declared (see schedule.missing_timezone).")
        if _zone("UTC") is None:  # pragma: no cover - only on a broken tzdata install
            return self.unknown(
                "No IANA timezone database is available on this machine, so "
                "timezone names cannot be validated."
            )
        invalid = sorted(
            f"{name} (on {where})" for name, where in names.items() if _zone(name) is None
        )
        if not invalid:
            return self.passed(f"All {len(names)} declared timezone(s) are valid IANA zones.")
        return self.failed(
            f"{len(invalid)} declared timezone(s) are not valid IANA zones.",
            affected=len(invalid),
            samples=self.sample(invalid, self.limit(config)),
        )


@dataclass(frozen=True)
class BusinessHoursOptions(RuleOptions):
    allow_partial_overlap: bool = True
    """Warn when a window merely extends past business hours, rather than failing."""


@register
class OutsideBusinessHours(_ScheduleRule):
    rule_id = "schedule.outside_business_hours"
    title = "Sending windows fall within recipient-friendly hours"
    category = RuleCategory.SCHEDULE
    severity = Severity.MEDIUM
    options_model = BusinessHoursOptions
    description = (
        "Compares each sending window against settings.business_hours_start/end. "
        "A window that starts at 5am or runs past 8pm reaches inboxes at hours "
        "that read as automated."
    )
    remediation = "Move the sending windows inside your configured business hours."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, BusinessHoursOptions)
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        start_bound = parse_clock_time(config.settings.business_hours_start)
        end_bound = parse_clock_time(config.settings.business_hours_end)
        if start_bound is None or end_bound is None:
            return self.unknown("settings.business_hours_start/end are not parseable as HH:MM.")
        usable = [w for w in schedule.windows if w.start and w.end]
        if not usable:
            return self.not_applicable("No complete sending window to check.")
        outside: list[str] = []
        for window in usable:
            assert window.start is not None and window.end is not None
            if window.crosses_midnight:
                outside.append(
                    f"{window.name}: {window.start:%H:%M}-{window.end:%H:%M} crosses midnight"
                )
                continue
            if window.start < start_bound or window.end > end_bound:
                outside.append(
                    f"{window.name}: {window.start:%H:%M}-{window.end:%H:%M} extends outside "
                    f"{start_bound:%H:%M}-{end_bound:%H:%M}"
                )
        if not outside:
            return self.passed(
                f"All {len(usable)} sending window(s) fall within "
                f"{start_bound:%H:%M}-{end_bound:%H:%M}."
            )
        summary = (
            f"{len(outside)} of {len(usable)} sending window(s) fall outside "
            f"configured business hours."
        )
        if options.allow_partial_overlap:
            return self.warn(
                summary,
                affected=len(outside),
                samples=self.sample(outside, self.limit(config)),
            )
        return self.failed(
            summary, affected=len(outside), samples=self.sample(outside, self.limit(config))
        )


@register
class WeekendSending(_ScheduleRule):
    rule_id = "schedule.weekend_sending"
    title = "Weekend sending matches configured policy"
    category = RuleCategory.SCHEDULE
    severity = Severity.MEDIUM
    description = (
        "Reports sending windows active on Saturday or Sunday when "
        "settings.allow_weekend_sending is false."
    )
    remediation = "Disable weekend days, or set settings.allow_weekend_sending: true."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        if config.settings.allow_weekend_sending:
            return self.not_applicable("Weekend sending is allowed by configuration.")
        offenders = []
        for window in schedule.windows:
            weekend = sorted(window.days & WEEKEND_DAYS)
            if weekend:
                labels = ", ".join(_DAY_LABELS[d] for d in weekend)
                offenders.append(f"{window.name}: active on {labels}")
        if not offenders:
            return self.passed("No sending window is active on a weekend.")
        return self.warn(
            f"{len(offenders)} sending window(s) are active on a weekend.",
            affected=len(offenders),
            samples=self.sample(offenders, self.limit(config)),
        )


@register
class NoActiveDays(_ScheduleRule):
    rule_id = "schedule.no_active_days"
    title = "The schedule has at least one active sending day"
    category = RuleCategory.SCHEDULE
    severity = Severity.BLOCKER
    description = "A schedule with every day disabled will never send."
    remediation = "Enable at least one sending day."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        if not schedule.windows:
            return self.not_applicable(
                "The campaign has no sending windows (see campaign.schedule_windows)."
            )
        active = {day for window in schedule.windows for day in window.days}
        if not active:
            return self.failed(
                f"All {len(schedule.windows)} sending window(s) have zero active days.",
                affected=len(schedule.windows),
                samples=[w.name for w in schedule.windows],
            )
        empty = [w.name for w in schedule.windows if not w.days]
        if empty:
            return self.warn(
                f"{len(empty)} of {len(schedule.windows)} sending window(s) have no "
                f"active days and will never fire.",
                affected=len(empty),
                samples=self.sample(empty, self.limit(config)),
                remediation="Enable days on the affected windows, or remove them.",
            )
        labels = ", ".join(_DAY_LABELS[d] for d in sorted(active))
        return self.passed(f"Active sending days: {labels}.")


@register
class StartAfterEnd(_ScheduleRule):
    rule_id = "schedule.start_after_end"
    title = "The schedule start date is not after its end date"
    category = RuleCategory.SCHEDULE
    severity = Severity.BLOCKER
    description = (
        "The schedule-level view of the same problem campaign.date_coherence "
        "reports, kept separate so the schedule section stands alone."
    )
    remediation = "Correct the schedule start and end dates."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        start, end = schedule.start_date, schedule.end_date
        if start is None or end is None:
            return self.not_applicable("The schedule does not set both a start and an end date.")
        if start > end:
            return self.failed(
                f"The schedule starts on {start} and ends on {end}, leaving no sending days.",
                metadata={"start_date": str(start), "end_date": str(end)},
            )
        return self.passed(f"The schedule runs {start} to {end}.")


@dataclass(frozen=True)
class WindowOrderOptions(RuleOptions):
    allow_overnight: bool = False
    """Set true if you intentionally send across midnight."""


@register
class WindowStartAfterEnd(_ScheduleRule):
    rule_id = "schedule.window_start_after_end"
    title = "Sending windows start before they end"
    category = RuleCategory.SCHEDULE
    severity = Severity.HIGH
    options_model = WindowOrderOptions
    description = (
        "A window whose end time is at or before its start time either sends "
        "nothing or wraps across midnight, depending on the provider. Both are "
        "worth knowing before launch."
    )
    remediation = "Correct the window times, or set allow_overnight: true if intended."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, WindowOrderOptions)
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        usable = [w for w in schedule.windows if w.start and w.end]
        if not usable:
            return self.not_applicable("No complete sending window to check.")
        equal: list[str] = []
        inverted: list[str] = []
        for window in usable:
            assert window.start is not None and window.end is not None
            if window.end == window.start:
                equal.append(f"{window.name}: start and end are both {window.start:%H:%M}")
            elif window.end < window.start:
                inverted.append(f"{window.name}: {window.start:%H:%M} to {window.end:%H:%M}")
        if equal:
            return self.failed(
                f"{len(equal)} sending window(s) have a zero-length duration.",
                severity=Severity.BLOCKER,
                affected=len(equal),
                samples=self.sample(equal, self.limit(config)),
            )
        if inverted and not options.allow_overnight:
            return self.failed(
                f"{len(inverted)} sending window(s) end before they start.",
                affected=len(inverted),
                samples=self.sample(inverted, self.limit(config)),
            )
        if inverted:
            return self.passed(
                f"{len(inverted)} overnight window(s), allowed by configuration.",
                metadata={"overnight_windows": inverted},
            )
        return self.passed(f"All {len(usable)} sending window(s) are correctly ordered.")


@dataclass(frozen=True)
class DstOptions(RuleOptions):
    lookahead_days: int = 45


@register
class DaylightSavingTransition(_ScheduleRule):
    rule_id = "schedule.dst_transition"
    title = "No daylight-saving transition inside the sending window"
    category = RuleCategory.SCHEDULE
    severity = Severity.LOW
    options_model = DstOptions
    description = (
        "A DST transition during the campaign shifts every send by an hour "
        "relative to the recipient's local clock. Informational, but it explains "
        "an otherwise mysterious change in reply rate."
    )
    remediation = "No action required; be aware send times shift by an hour."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DstOptions)
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        campaign = ctx.campaign
        assert campaign is not None
        zones = {tz for _, tz in self.window_zones(schedule, campaign) if tz} or (
            {campaign.timezone_name} if campaign.timezone_name else set()
        )
        if not zones:
            return self.not_applicable("No timezone is declared, so DST cannot be assessed.")

        start = schedule.start_date or ctx.generated_at.date()
        end = schedule.end_date or (start + timedelta(days=options.lookahead_days))
        if end < start:
            return self.not_applicable(
                "The date range is incoherent (see schedule.start_after_end)."
            )
        span_days = min((end - start).days, 366)

        transitions: list[str] = []
        unresolved: list[str] = []
        for name in sorted(zones):
            zone = _zone(name)
            if zone is None:
                unresolved.append(name)
                continue
            previous = datetime.combine(start, time(12, 0), tzinfo=zone).utcoffset()
            for day_offset in range(1, span_days + 1):
                moment = datetime.combine(
                    start + timedelta(days=day_offset), time(12, 0), tzinfo=zone
                )
                current = moment.utcoffset()
                if current != previous:
                    transitions.append(f"{name}: offset changes on {moment.date()}")
                    previous = current
        if unresolved:
            return self.unknown(
                f"Could not resolve timezone(s): {', '.join(unresolved)}.",
                metadata={"unresolved": unresolved},
            )
        if not transitions:
            return self.passed(
                f"No daylight-saving transition in the next {span_days} day(s) for "
                f"{len(zones)} timezone(s)."
            )
        return self.warn(
            f"{len(transitions)} daylight-saving transition(s) fall inside the campaign window.",
            severity=Severity.INFO,
            affected=len(transitions),
            samples=self.sample(transitions, self.limit(config)),
        )


@register
class TimezoneMismatch(_ScheduleRule):
    rule_id = "schedule.timezone_mismatch"
    title = "The campaign timezone matches the configured target"
    category = RuleCategory.SCHEDULE
    severity = Severity.MEDIUM
    description = (
        "Compares the campaign's timezone with settings.target_timezone. Catches "
        "the copied-campaign case where the schedule still points at the previous "
        "region's working hours."
    )
    remediation = "Set the campaign timezone to your target, or update settings.target_timezone."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        target = config.settings.target_timezone
        if not target:
            return self.not_applicable(
                "No target timezone configured (set settings.target_timezone)."
            )
        schedule = self.schedule_or_na(ctx)
        if isinstance(schedule, RuleResult):
            return schedule
        campaign = ctx.campaign
        assert campaign is not None
        declared = {tz for _, tz in self.window_zones(schedule, campaign) if tz} or (
            {campaign.timezone_name} if campaign.timezone_name else set()
        )
        if not declared:
            return self.unknown("No timezone is declared, so it cannot be compared.")

        target_zone = _zone(target)
        mismatched: list[str] = []
        for name in sorted(declared):
            if name == target:
                continue
            zone = _zone(name)
            if zone is not None and target_zone is not None:
                probe = datetime.combine(
                    schedule.start_date or ctx.generated_at.date(), time(12, 0)
                )
                if (
                    probe.replace(tzinfo=zone).utcoffset()
                    == probe.replace(tzinfo=target_zone).utcoffset()
                ):
                    continue  # different name, same wall clock right now
            mismatched.append(name)
        if not mismatched:
            return self.passed(f"The campaign sends on {target} time as configured.")
        return self.warn(
            f"The campaign uses {', '.join(mismatched)} but the configured target is {target}.",
            affected=len(mismatched),
            samples=self.sample(mismatched, self.limit(config)),
            metadata={"declared": sorted(declared), "target": target},
        )
