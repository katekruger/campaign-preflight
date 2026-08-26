"""Campaign configuration rules (checks 1-10).

These read only the campaign object. Anything that depends on lead or sender
data lives in the other rule modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from datetime import timedelta

from ..config import PreflightConfig, RuleOptions
from ..models import Capability, PreflightContext, RuleCategory, RuleResult, Severity
from .base import Rule, register

__all__: list[str] = []

# Statuses a campaign may hold and still be a sensible preflight target. Anything
# already sending is past the point this tool is useful, and is reported as such.
PREFLIGHT_READY_STATUSES = frozenset({"draft", "paused", "scheduled", "", "unknown"})
ALREADY_RUNNING_STATUSES = frozenset({"active", "running", "running_subsequences"})
TERMINAL_STATUSES = frozenset({"completed", "archived", "stopped"})
ERROR_STATUSES = frozenset(
    {"accounts_unhealthy", "bounce_protect", "account_suspended", "suspended"}
)


@register
class CampaignExists(Rule):
    rule_id = "campaign.exists"
    title = "Campaign is readable"
    category = RuleCategory.CAMPAIGN
    severity = Severity.BLOCKER
    requires = (Capability.CAMPAIGN,)
    description = (
        "Confirms the campaign could be retrieved and carries an identifier. "
        "Every other check depends on this one."
    )
    remediation = "Check the campaign id, the file path, and the provider credentials."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        if campaign is None:
            return self.failed("The campaign could not be read.")
        if not campaign.id and not campaign.name:
            return self.failed(
                "The campaign was retrieved but has neither an id nor a name.",
                explanation=(
                    "A campaign with no identity cannot be tracked between runs and "
                    "usually means the wrong file or endpoint was read."
                ),
            )
        label = campaign.name or campaign.id
        return self.passed(f"Campaign '{label}' was read successfully.")


@dataclass(frozen=True)
class StatusOptions(RuleOptions):
    warn_on_active: bool = True
    """Warn (rather than fail) when the campaign is already sending."""


@register
class CampaignStatusSuitable(Rule):
    rule_id = "campaign.status_suitable"
    title = "Campaign status is suitable for preflight"
    category = RuleCategory.CAMPAIGN
    severity = Severity.MEDIUM
    requires = (Capability.CAMPAIGN,)
    options_model = StatusOptions
    description = (
        "Preflight is meant to run before activation. A campaign that is already "
        "active, completed, or in a provider error state is reported so the "
        "findings are read in the right context."
    )
    remediation = "Pause the campaign before acting on these findings."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, StatusOptions)
        campaign = ctx.campaign
        assert campaign is not None
        status = (campaign.status or "").lower()

        if not status:
            return self.unknown(
                "The provider did not report a campaign status.",
                metadata={"raw_status": campaign.raw_status},
            )
        if status in ERROR_STATUSES:
            return self.failed(
                f"The campaign is in a provider error state: {status}.",
                severity=Severity.HIGH,
                explanation=(
                    "The provider has flagged this campaign. Sending is likely "
                    "blocked or degraded regardless of anything below."
                ),
                remediation="Resolve the provider-side error before launching.",
                metadata={"status": status},
            )
        if status in TERMINAL_STATUSES:
            return self.warn(
                f"The campaign is {status}; preflight findings are informational only.",
                severity=Severity.LOW,
                remediation="No action needed unless you intend to relaunch.",
                metadata={"status": status},
            )
        if status in ALREADY_RUNNING_STATUSES:
            summary = f"The campaign is already {status} - preflight is running after the fact."
            explanation = (
                "Campaign Preflight is a pre-activation check. Findings below "
                "describe a campaign that is already sending, so any blocker is "
                "affecting live traffic right now."
            )
            if options.warn_on_active:
                return self.warn(
                    summary,
                    severity=Severity.MEDIUM,
                    explanation=explanation,
                    metadata={"status": status},
                )
            return self.failed(
                summary, explanation=explanation, metadata={"status": status}
            )
        if status in PREFLIGHT_READY_STATUSES:
            return self.passed(f"Campaign status is '{status}', suitable for preflight.")
        return self.unknown(
            f"Unrecognized campaign status '{status}'.",
            explanation=(
                "The provider returned a status this version does not know about. "
                "Reporting UNKNOWN rather than guessing whether it is safe."
            ),
            metadata={"status": status, "raw_status": campaign.raw_status},
        )


@dataclass(frozen=True)
class StepsOptions(RuleOptions):
    warn_below: int = 1
    """Warn when the enabled step count is at or below this number."""


@register
class CampaignHasSteps(Rule):
    rule_id = "campaign.has_steps"
    title = "Campaign has at least one step"
    category = RuleCategory.CAMPAIGN
    severity = Severity.BLOCKER
    requires = (Capability.CAMPAIGN,)
    options_model = StepsOptions
    description = "A campaign with no enabled email step will never send anything."
    remediation = "Add at least one enabled email step to the sequence."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        enabled = [s for s in campaign.steps if not s.disabled]
        if not campaign.steps:
            return self.failed("The campaign has no steps.")
        if not enabled:
            return self.failed(
                f"All {len(campaign.steps)} step variants are disabled.",
                affected=len(campaign.steps),
            )
        distinct_steps = len({s.index for s in enabled})
        return self.passed(
            f"{distinct_steps} step(s) with {len(enabled)} enabled variant(s).",
            metadata={"step_count": distinct_steps, "variant_count": len(enabled)},
        )


@register
class CampaignHasSenders(Rule):
    rule_id = "campaign.has_senders"
    title = "Campaign has at least one sender attached"
    category = RuleCategory.CAMPAIGN
    severity = Severity.BLOCKER
    requires = (Capability.SENDERS,)
    description = "A campaign with no sending mailbox cannot deliver."
    remediation = "Attach at least one sending account to the campaign."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.senders:
            return self.failed("No sender is attached to this campaign.")
        return self.passed(
            f"{len(ctx.senders)} sender(s) attached.",
            metadata={"sender_count": len(ctx.senders)},
        )


@dataclass(frozen=True)
class DailyVolumeOptions(RuleOptions):
    warning_above: int = 100
    blocker_above: int = 250
    unset_is_warning: bool = True


@register
class CampaignDailyVolume(Rule):
    rule_id = "campaign.daily_volume"
    title = "Daily sending volume is within configured limits"
    category = RuleCategory.CAMPAIGN
    severity = Severity.HIGH
    requires = (Capability.CAMPAIGN,)
    options_model = DailyVolumeOptions
    description = (
        "High per-day volume is the single most common cause of a burned domain. "
        "The thresholds are yours to set; the defaults are conservative."
    )
    remediation = "Lower the campaign daily limit or spread volume across more senders."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DailyVolumeOptions)
        campaign = ctx.campaign
        assert campaign is not None
        limit = campaign.daily_limit

        if limit is None:
            if options.unset_is_warning:
                return self.warn(
                    "No daily sending limit is configured.",
                    severity=Severity.MEDIUM,
                    explanation=(
                        "Without an explicit cap the campaign will send at whatever "
                        "rate the provider and the attached senders allow."
                    ),
                    remediation="Set an explicit daily limit on the campaign.",
                )
            return self.not_applicable("No daily limit configured and none required.")
        if limit <= 0:
            return self.failed(
                f"The daily limit is {limit}, so the campaign will not send.",
                severity=Severity.HIGH,
                remediation="Set a positive daily limit.",
                metadata={"daily_limit": limit},
            )
        if limit > options.blocker_above:
            return self.failed(
                f"Daily limit of {limit} exceeds the blocker threshold "
                f"of {options.blocker_above}.",
                severity=Severity.BLOCKER,
                metadata={"daily_limit": limit, "threshold": options.blocker_above},
            )
        if limit > options.warning_above:
            return self.warn(
                f"Daily limit of {limit} exceeds the warning threshold "
                f"of {options.warning_above}.",
                severity=Severity.MEDIUM,
                metadata={"daily_limit": limit, "threshold": options.warning_above},
            )
        return self.passed(
            f"Daily limit of {limit} is within the configured threshold "
            f"of {options.warning_above}.",
            metadata={"daily_limit": limit},
        )


@register
class CampaignStopOnReply(Rule):
    rule_id = "campaign.stop_on_reply"
    title = "Stop-on-reply is enabled"
    category = RuleCategory.CAMPAIGN
    severity = Severity.HIGH
    requires = (Capability.CAMPAIGN,)
    description = (
        "With stop-on-reply disabled, a prospect who replies keeps receiving "
        "follow-ups. This is the most reliably damaging campaign misconfiguration."
    )
    remediation = "Enable stop-on-reply on the campaign."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        if campaign.stop_on_reply is None:
            return self.unknown(
                "The provider did not report a stop-on-reply setting.",
                explanation=(
                    "This field was absent or unparseable. It is not being treated "
                    "as enabled."
                ),
            )
        if campaign.stop_on_reply is False:
            return self.failed(
                "Stop-on-reply is disabled: repliers will keep receiving follow-ups.",
                severity=Severity.BLOCKER,
            )
        note = ""
        if campaign.stop_on_auto_reply is False:
            note = " Stop-on-auto-reply is disabled, so out-of-office replies will not pause a thread."
        return self.passed(
            f"Stop-on-reply is enabled.{note}",
            metadata={"stop_on_auto_reply": campaign.stop_on_auto_reply},
        )


@register
class CampaignHasLeads(Rule):
    rule_id = "campaign.has_leads"
    title = "Campaign has leads"
    category = RuleCategory.CAMPAIGN
    severity = Severity.BLOCKER
    requires = (Capability.LEADS,)
    description = "A campaign with zero leads will not send."
    remediation = "Import leads into the campaign before activating."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.leads:
            return self.failed("The campaign has no leads.")
        suffix = " (list was truncated)" if ctx.lead_total_hint else ""
        return self.passed(
            f"{len(ctx.leads)} lead(s) attached{suffix}.",
            metadata={"lead_count": len(ctx.leads)},
        )


@register
class CampaignDateCoherence(Rule):
    rule_id = "campaign.date_coherence"
    title = "Campaign start and end dates are coherent"
    category = RuleCategory.CAMPAIGN
    severity = Severity.HIGH
    requires = (Capability.CAMPAIGN,)
    description = "An end date on or before the start date leaves no sending days."
    remediation = "Correct the campaign start and end dates."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        schedule = campaign.schedule
        if schedule is None:
            return self.unknown("The campaign has no schedule to check.")
        start, end = schedule.start_date, schedule.end_date
        if start is None and end is None:
            return self.not_applicable("The campaign has no start or end date set.")
        if start is None or end is None:
            which = "end" if end is None else "start"
            return self.passed(
                f"Only a {'start' if end is None else 'end'} date is set; "
                f"no {which} date to conflict with it.",
                metadata={"start_date": str(start), "end_date": str(end)},
            )
        if end < start:
            return self.failed(
                f"The end date ({end}) is before the start date ({start}).",
                severity=Severity.BLOCKER,
                metadata={"start_date": str(start), "end_date": str(end)},
            )
        if end == start:
            return self.warn(
                f"Start and end dates are both {start}: the campaign has a single sending day.",
                severity=Severity.MEDIUM,
                remediation="Widen the date range if a multi-day sequence was intended.",
                metadata={"start_date": str(start), "end_date": str(end)},
            )
        return self.passed(
            f"Sending window runs {start} to {end} ({(end - start).days} days).",
            metadata={"start_date": str(start), "end_date": str(end)},
        )


@dataclass(frozen=True)
class StartInPastOptions(RuleOptions):
    grace_days: int = 1
    """How far in the past a start date may be before it is reported."""


@register
class CampaignStartInPast(Rule):
    rule_id = "campaign.start_in_past"
    title = "Campaign does not start in the past"
    category = RuleCategory.CAMPAIGN
    severity = Severity.MEDIUM
    requires = (Capability.CAMPAIGN,)
    options_model = StartInPastOptions
    description = (
        "A start date well in the past usually means a campaign was copied from an "
        "earlier one and the dates were never updated."
    )
    remediation = "Update the campaign start date, or confirm the backdate is intentional."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, StartInPastOptions)
        campaign = ctx.campaign
        assert campaign is not None
        schedule = campaign.schedule
        if schedule is None or schedule.start_date is None:
            return self.not_applicable("The campaign has no start date set.")
        today = ctx.generated_at.date()
        cutoff = today - timedelta(days=options.grace_days)
        start = schedule.start_date
        if start < cutoff:
            days = (today - start).days
            return self.warn(
                f"The campaign start date ({start}) is {days} day(s) in the past.",
                metadata={"start_date": str(start), "days_ago": days},
            )
        return self.passed(
            f"The campaign start date ({start}) is not unexpectedly in the past.",
            metadata={"start_date": str(start)},
        )


@register
class CampaignScheduleWindows(Rule):
    rule_id = "campaign.schedule_windows"
    title = "Campaign schedule has at least one sending window"
    category = RuleCategory.CAMPAIGN
    severity = Severity.BLOCKER
    requires = (Capability.CAMPAIGN,)
    description = "Without a sending window the campaign has no time in which to send."
    remediation = "Add a sending window with a start time, an end time, and active days."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        schedule = campaign.schedule
        if schedule is None:
            return self.failed("The campaign has no schedule at all.")
        if not schedule.windows:
            return self.failed("The campaign schedule contains no sending windows.")
        usable = [w for w in schedule.windows if w.start is not None and w.end is not None]
        if not usable:
            return self.failed(
                f"All {len(schedule.windows)} sending window(s) are missing a start or end time.",
                affected=len(schedule.windows),
                samples=[w.name for w in schedule.windows],
            )
        if len(usable) < len(schedule.windows):
            broken = [w.name for w in schedule.windows if w not in usable]
            return self.warn(
                f"{len(broken)} of {len(schedule.windows)} sending windows are incomplete.",
                severity=Severity.MEDIUM,
                affected=len(broken),
                samples=broken,
                remediation="Give every sending window both a start and an end time.",
            )
        return self.passed(
            f"{len(usable)} sending window(s) configured.",
            metadata={"window_count": len(usable)},
        )
