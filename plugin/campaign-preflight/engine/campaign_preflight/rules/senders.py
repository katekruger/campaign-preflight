"""Sender readiness rules (checks 68-75).

The governing constraint: this module never invents a deliverability number. If
the provider does not expose a health score, the health rules return UNKNOWN and
say which senders they could not assess. A confident-looking score derived from
nothing would be worse than no score at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Capability,
    CapabilityStatus,
    PreflightContext,
    RuleCategory,
    RuleResult,
    Sender,
    Severity,
)
from .base import Rule, register

__all__: list[str] = []

# Status strings that mean "this mailbox is not going to send right now".
DISABLED_STATUSES = frozenset({"paused", "disabled", "inactive", "stopped", "maintenance"})
ERROR_STATUSES = frozenset(
    {"error", "connection_error", "soft_bounce_error", "sending_error", "suspended", "banned"}
)
UNHEALTHY_WARMUP = frozenset({"banned", "issue", "spam_folder", "suspended", "paused"})


class _SenderRule(Rule):
    category = RuleCategory.SENDERS
    requires = (Capability.SENDERS,)

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples

    @staticmethod
    def is_disabled(sender: Sender) -> bool:
        if sender.enabled is False:
            return True
        label = (sender.status_label or "").strip().lower()
        return label in DISABLED_STATUSES

    @staticmethod
    def is_errored(sender: Sender) -> bool:
        if sender.status_is_error is True:
            return True
        label = (sender.status_label or "").strip().lower()
        return label in ERROR_STATUSES


@register
class NoSenderAttached(_SenderRule):
    rule_id = "senders.none_attached"
    title = "At least one sender is attached"
    category = RuleCategory.SENDERS
    severity = Severity.BLOCKER
    description = (
        "The sender-side view of campaign.has_senders, kept in this section so "
        "the sender readiness summary is complete on its own."
    )
    remediation = "Attach at least one sending account to the campaign."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.senders:
            return self.failed("No sending account is attached to this campaign.")
        return self.passed(f"{len(ctx.senders)} sending account(s) attached.")


@register
class SenderDisabled(_SenderRule):
    rule_id = "senders.disabled"
    title = "Attached senders are enabled"
    category = RuleCategory.SENDERS
    severity = Severity.HIGH
    description = "Paused or disabled mailboxes contribute no capacity to the campaign."
    remediation = "Re-enable the paused senders, or detach them and adjust volume."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.senders:
            return self.not_applicable("No senders attached (see senders.none_attached).")
        disabled = [s for s in ctx.senders if self.is_disabled(s)]
        unknown_state = [s for s in ctx.senders if s.enabled is None and not s.status_label]
        if not disabled:
            if len(unknown_state) == len(ctx.senders):
                return self.unknown(
                    f"No enabled/paused state was reported for any of the "
                    f"{len(ctx.senders)} senders."
                )
            note = (
                f" ({len(unknown_state)} reported no state)" if unknown_state else ""
            )
            return self.passed(f"All {len(ctx.senders)} senders are enabled{note}.")
        if len(disabled) == len(ctx.senders):
            return self.failed(
                f"All {len(ctx.senders)} attached senders are disabled or paused.",
                severity=Severity.BLOCKER,
                affected=len(disabled),
                samples=[s.email for s in disabled],
            )
        return self.warn(
            f"{len(disabled)} of {len(ctx.senders)} senders are disabled or paused.",
            affected=len(disabled),
            samples=self.sample([s.email for s in disabled], self.limit(config)),
        )


@dataclass(frozen=True)
class SenderHealthOptions(RuleOptions):
    minimum_score: float = 80.0
    """Scores are on the provider's own scale, normally 0-100."""


@register
class SenderHealth(_SenderRule):
    rule_id = "senders.health_below_threshold"
    title = "Sender health meets the configured threshold"
    category = RuleCategory.SENDERS
    severity = Severity.HIGH
    requires = (Capability.SENDERS, Capability.SENDER_HEALTH)
    options_model = SenderHealthOptions
    description = (
        "Compares each sender's provider-reported health score against "
        "senders.health.minimum_score. Senders with no score are reported as "
        "unassessed, never as healthy."
    )
    remediation = "Warm the low-scoring senders, or remove them from the campaign."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, SenderHealthOptions)
        if not ctx.senders:
            return self.not_applicable("No senders attached (see senders.none_attached).")
        scored = [s for s in ctx.senders if s.health_score is not None]
        unscored = [s for s in ctx.senders if s.health_score is None]
        if not scored:
            return self.unknown(
                f"No health score was reported for any of the {len(ctx.senders)} senders.",
                explanation=(
                    "The provider exposed the health capability but returned no "
                    "scores. Sender readiness is unverified."
                ),
                metadata={"unscored": len(unscored)},
            )
        below = [s for s in scored if (s.health_score or 0) < options.minimum_score]
        metadata = {
            "threshold": options.minimum_score,
            "scored": len(scored),
            "unscored": len(unscored),
            "lowest_score": min(s.health_score or 0 for s in scored),
        }
        if below and len(below) == len(ctx.senders):
            return self.failed(
                f"No attached sender meets the configured health threshold of "
                f"{options.minimum_score:g}.",
                severity=Severity.BLOCKER,
                affected=len(below),
                samples=self.sample(
                    [f"{s.email} (score {s.health_score:g})" for s in below],
                    self.limit(config),
                ),
                metadata=metadata,
            )
        if below:
            return self.warn(
                f"{len(below)} of {len(scored)} scored senders are below the "
                f"health threshold of {options.minimum_score:g}.",
                severity=Severity.HIGH,
                affected=len(below),
                samples=self.sample(
                    [f"{s.email} (score {s.health_score:g})" for s in below],
                    self.limit(config),
                ),
                metadata=metadata,
            )
        if unscored:
            return self.warn(
                f"{len(scored)} sender(s) meet the health threshold, but "
                f"{len(unscored)} have no score and were not assessed.",
                severity=Severity.MEDIUM,
                affected=len(unscored),
                samples=self.sample([s.email for s in unscored], self.limit(config)),
                remediation="Check the unscored senders in your provider dashboard.",
                metadata=metadata,
            )
        return self.passed(
            f"All {len(scored)} senders meet the health threshold of "
            f"{options.minimum_score:g}.",
            metadata=metadata,
        )


@dataclass(frozen=True)
class DailyCapacityOptions(RuleOptions):
    warn_ratio: float = 0.9
    """Warn when the campaign's per-sender share reaches this fraction of a limit."""


@register
class SenderDailyCapacity(_SenderRule):
    rule_id = "senders.daily_capacity"
    title = "No single sender is asked to exceed its daily limit"
    category = RuleCategory.SENDERS
    severity = Severity.HIGH
    options_model = DailyCapacityOptions
    description = (
        "Divides the campaign's daily limit evenly across enabled senders and "
        "compares the share against each sender's own daily limit."
    )
    remediation = "Lower the campaign daily limit or raise the sender limits."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DailyCapacityOptions)
        campaign = ctx.campaign
        if campaign is None or campaign.daily_limit is None:
            return self.unknown(
                "The campaign daily limit is unknown, so per-sender load cannot be derived."
            )
        usable = [s for s in ctx.senders if not self.is_disabled(s) and not self.is_errored(s)]
        if not usable:
            return self.not_applicable(
                "No usable sender to distribute volume across (see senders.all_unavailable)."
            )
        limited = [s for s in usable if s.daily_limit is not None]
        if not limited:
            return self.unknown(
                f"None of the {len(usable)} usable senders report a daily limit.",
                metadata={"usable_senders": len(usable)},
            )
        share = campaign.daily_limit / len(usable)
        over = [s for s in limited if share > (s.daily_limit or 0)]
        near = [
            s
            for s in limited
            if s not in over and share >= (s.daily_limit or 0) * options.warn_ratio
        ]
        metadata = {
            "campaign_daily_limit": campaign.daily_limit,
            "usable_senders": len(usable),
            "per_sender_share": round(share, 2),
            "senders_without_limit": len(usable) - len(limited),
        }
        if over:
            return self.failed(
                f"{len(over)} sender(s) would be asked to send {share:.0f}/day, above "
                f"their own daily limit.",
                affected=len(over),
                samples=self.sample(
                    [f"{s.email} (limit {s.daily_limit})" for s in over], self.limit(config)
                ),
                metadata=metadata,
            )
        if near:
            return self.warn(
                f"{len(near)} sender(s) would run at or above "
                f"{options.warn_ratio:.0%} of their daily limit.",
                severity=Severity.MEDIUM,
                affected=len(near),
                samples=self.sample(
                    [f"{s.email} (limit {s.daily_limit})" for s in near], self.limit(config)
                ),
                metadata=metadata,
            )
        return self.passed(
            f"Each of {len(usable)} senders would carry about {share:.0f}/day, "
            f"within their limits.",
            metadata=metadata,
        )


@register
class SenderAggregateCapacity(_SenderRule):
    rule_id = "senders.aggregate_capacity"
    title = "Campaign volume fits total sender capacity"
    category = RuleCategory.SENDERS
    severity = Severity.HIGH
    description = (
        "Sums the daily limits of usable senders and compares the total against "
        "the campaign's daily limit. Requires every usable sender to report a "
        "limit; a partial sum would understate capacity."
    )
    remediation = "Attach more senders, or lower the campaign daily limit."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        if campaign is None or campaign.daily_limit is None:
            return self.unknown(
                "The campaign daily limit is unknown, so aggregate capacity cannot be compared."
            )
        usable = [s for s in ctx.senders if not self.is_disabled(s) and not self.is_errored(s)]
        if not usable:
            return self.not_applicable(
                "No usable sender to sum capacity from (see senders.all_unavailable)."
            )
        missing = [s for s in usable if s.daily_limit is None]
        if missing:
            return self.unknown(
                f"Sender capacity is unavailable: {len(missing)} of {len(usable)} "
                f"senders report no daily limit.",
                explanation=(
                    "Summing only the senders that do report a limit would understate "
                    "capacity and could turn a real shortfall into a pass."
                ),
                samples=self.sample([s.email for s in missing], self.limit(config)),
                metadata={"senders_without_limit": len(missing), "usable_senders": len(usable)},
            )
        capacity = sum(s.daily_limit or 0 for s in usable)
        metadata = {
            "aggregate_capacity": capacity,
            "campaign_daily_limit": campaign.daily_limit,
            "usable_senders": len(usable),
        }
        if campaign.daily_limit > capacity:
            return self.failed(
                f"The campaign asks for {campaign.daily_limit}/day but the "
                f"{len(usable)} usable senders total only {capacity}/day.",
                metadata=metadata,
            )
        return self.passed(
            f"Campaign volume of {campaign.daily_limit}/day fits within "
            f"{capacity}/day of sender capacity.",
            metadata=metadata,
        )


@register
class AllSendersUnavailable(_SenderRule):
    rule_id = "senders.all_unavailable"
    title = "At least one sender is usable"
    category = RuleCategory.SENDERS
    severity = Severity.BLOCKER
    description = (
        "Every attached sender is disabled, errored, or still in setup. The "
        "campaign has senders on paper and none in practice."
    )
    remediation = "Fix or replace the attached senders before activating."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.senders:
            return self.not_applicable("No senders attached (see senders.none_attached).")
        unusable = [
            s
            for s in ctx.senders
            if self.is_disabled(s) or self.is_errored(s) or s.setup_pending is True
        ]
        if len(unusable) < len(ctx.senders):
            return self.passed(
                f"{len(ctx.senders) - len(unusable)} of {len(ctx.senders)} senders are usable."
            )
        return self.failed(
            f"All {len(ctx.senders)} attached senders are unusable (disabled, "
            f"errored, or pending setup).",
            affected=len(unusable),
            samples=self.sample(
                [f"{s.email} ({s.status_label or 'no status'})" for s in unusable],
                self.limit(config),
            ),
        )


@register
class SenderHealthUnavailable(Rule):
    rule_id = "senders.health_unavailable"
    title = "Sender health data was available"
    category = RuleCategory.SENDERS
    severity = Severity.MEDIUM
    description = (
        "Reports the availability of sender health data as its own finding, so a "
        "campaign whose sender readiness could not be assessed does not read as "
        "a campaign whose senders are fine."
    )
    remediation = (
        "Grant the API key accounts:read, or supply health_score values in your "
        "senders file."
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        status = ctx.capability_status(Capability.SENDER_HEALTH)
        detail = ctx.capability_detail(Capability.SENDER_HEALTH) or "no detail supplied"
        metadata = {"capability_status": status.value, "detail": detail}
        if status is CapabilityStatus.SUPPORTED_OK:
            scored = sum(1 for s in ctx.senders if s.health_score is not None)
            if scored == len(ctx.senders):
                return self.passed(
                    f"Health data was available for all {len(ctx.senders)} senders.",
                    metadata=metadata,
                )
            return self.warn(
                f"Health data was available for {scored} of {len(ctx.senders)} senders.",
                severity=Severity.LOW,
                affected=len(ctx.senders) - scored,
                metadata=metadata,
            )
        if status is CapabilityStatus.UNAVAILABLE_PERMISSIONS:
            return self.warn(
                "Sender health data is blocked by the current credentials.",
                severity=Severity.MEDIUM,
                explanation=f"Provider detail: {detail}",
                metadata=metadata,
            )
        return self.unknown(
            f"Sender health data was unavailable ({status.value}).",
            explanation=(
                "Sender readiness could not be verified. No deliverability score "
                f"is being invented to fill the gap. Provider detail: {detail}"
            ),
            metadata=metadata,
        )


@register
class SenderErrorState(_SenderRule):
    rule_id = "senders.error_state"
    title = "No sender is in a provider error state"
    category = RuleCategory.SENDERS
    severity = Severity.HIGH
    description = (
        "Connection errors, bounce errors, and suspensions reported by the "
        "provider for an attached mailbox."
    )
    remediation = "Reconnect or replace the affected mailboxes."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.senders:
            return self.not_applicable("No senders attached (see senders.none_attached).")
        errored = [s for s in ctx.senders if self.is_errored(s)]
        pending = [s for s in ctx.senders if s.setup_pending is True]
        warmup_issues = [
            s
            for s in ctx.senders
            if (s.warmup_status or "").strip().lower() in UNHEALTHY_WARMUP
        ]
        if not errored and not pending and not warmup_issues:
            return self.passed(f"No error state reported for {len(ctx.senders)} senders.")
        problems = [
            *(f"{s.email}: {s.status_label or 'error'}" for s in errored),
            *(f"{s.email}: setup pending" for s in pending),
            *(f"{s.email}: warmup {s.warmup_status}" for s in warmup_issues),
        ]
        severity = Severity.BLOCKER if len(errored) == len(ctx.senders) else Severity.HIGH
        return self.failed(
            f"{len(problems)} sender error state(s) reported by the provider.",
            severity=severity,
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
            metadata={
                "errored": len(errored),
                "setup_pending": len(pending),
                "warmup_issues": len(warmup_issues),
            },
        )
