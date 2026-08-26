"""The preflight engine: gather context from a provider, then run every rule.

Two phases, deliberately separated.

**Gather** is the only place that talks to a provider. It calls each read-only
method once, records the capability outcome, and freezes everything into a
:class:`~campaign_preflight.models.PreflightContext`.

**Evaluate** is pure. It walks the registry in id order, short-circuits any rule
whose required capabilities are unavailable to ``UNKNOWN``, and collects one
result per rule. A rule that raises is reported as an UNKNOWN naming the rule --
one broken rule must not take down the run.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from . import __version__
from .config import PreflightConfig
from .errors import redact_secrets
from .models import (
    Campaign,
    Capability,
    CapabilityReport,
    CapabilityStatus,
    Lead,
    PersonalizationClaim,
    PreflightContext,
    PreflightReport,
    ProviderMetadata,
    RuleResult,
    RuleStatus,
    Sender,
    SourceEvidence,
    SuppressionEntry,
    utcnow,
)
from .providers.base import CampaignProvider, ProviderResult
from .rules import Rule, all_rules
from .scoring import decide_readiness, score_results

__all__ = ["gather_context", "evaluate", "run_preflight"]


async def gather_context(
    provider: CampaignProvider,
    *,
    campaign_id: str | None = None,
    lead_limit: int | None = None,
    now: datetime | None = None,
) -> PreflightContext:
    """Perform every read a preflight run needs, exactly once each.

    ``now`` overrides the clock. Rules that compare against the current time
    (schedule.dst_transition, campaign.start_in_past, personalization.stale_evidence)
    read it from the context, so pinning it makes a whole run reproducible.
    """
    moment = now or utcnow()
    capabilities: list[CapabilityReport] = []
    errors: list[str] = []
    warnings: list[str] = []

    def record(result: ProviderResult[Any], count: int | None = None) -> None:
        capabilities.append(result.to_report(count))
        if result.status is CapabilityStatus.SUPPORTED_FAILED and result.detail:
            errors.append(f"{result.capability.value}: {redact_secrets(result.detail)}")
        if result.is_ok and result.partial:
            warnings.append(
                f"{result.capability.value}: result was truncated "
                f"({redact_secrets(result.detail or 'no detail')})"
            )

    campaign_result = await provider.get_campaign(campaign_id)
    campaign: Campaign | None = campaign_result.data if campaign_result.is_ok else None
    record(campaign_result)

    leads_result = await provider.list_campaign_leads(campaign_id, limit=lead_limit)
    leads: list[Lead] = leads_result.unwrap_or([])
    record(leads_result, len(leads) if leads_result.is_ok else None)

    senders_result = await provider.list_campaign_senders(campaign_id)
    senders: list[Sender] = senders_result.unwrap_or([])
    record(senders_result, len(senders) if senders_result.is_ok else None)

    health_result = await provider.get_sender_health(senders)
    if health_result.is_ok and health_result.data is not None:
        senders = health_result.data
    record(health_result, len(senders) if health_result.is_ok else None)

    suppressions_result = await provider.list_suppressions()
    suppressions: list[SuppressionEntry] = suppressions_result.unwrap_or([])
    record(suppressions_result, len(suppressions) if suppressions_result.is_ok else None)

    evidence_result = await provider.list_evidence()
    evidence: list[SourceEvidence] = evidence_result.unwrap_or([])
    record(evidence_result, len(evidence) if evidence_result.is_ok else None)

    claims_result = await provider.list_claims()
    claims: list[PersonalizationClaim] = claims_result.unwrap_or([])

    analytics_result = await provider.get_campaign_analytics_context(campaign_id)
    analytics: dict[str, Any] = analytics_result.unwrap_or({})
    record(analytics_result)

    warnings.extend(redact_secrets(w) for w in getattr(provider, "warnings", []))

    metadata = ProviderMetadata(
        name=provider.name,
        version=provider.version,
        base_url=provider.base_url,
        read_only=True,
        capabilities=tuple(capabilities),
        errors=tuple(errors),
        fetched_at=moment,
    )

    lead_hint = None
    if leads_result.is_ok and leads_result.partial:
        lead_hint = analytics.get("leads_count") if isinstance(analytics, dict) else None
        lead_hint = lead_hint if isinstance(lead_hint, int) else len(leads)

    return PreflightContext(
        campaign=campaign,
        leads=tuple(leads),
        senders=tuple(senders),
        suppressions=tuple(suppressions),
        evidence=tuple(evidence),
        claims=tuple(claims),
        analytics=analytics if isinstance(analytics, dict) else {},
        provider=metadata,
        generated_at=moment,
        input_warnings=tuple(dict.fromkeys(warnings)),
        lead_total_hint=lead_hint,
    )


def _missing_capabilities(rule: Rule, ctx: PreflightContext) -> Capability | None:
    """The first required capability that is unavailable, if any."""
    for capability in rule.requires:
        if not ctx.capability_status(capability).is_ok:
            return capability
    return None


def evaluate(ctx: PreflightContext, config: PreflightConfig) -> tuple[RuleResult, ...]:
    """Run every enabled rule against ``ctx``. Order is stable by rule id."""
    results: list[RuleResult] = []
    for rule in all_rules():
        options = config.options_for(rule.rule_id, rule.options_model)
        if not options.enabled:
            continue

        missing = _missing_capabilities(rule, ctx)
        if missing is not None:
            results.append(rule.missing_capability_result(ctx, missing))
            continue

        try:
            result = rule.evaluate(ctx, options, config)
        except Exception as exc:  # noqa: BLE001 - one bad rule must not end the run
            results.append(
                rule.build(
                    RuleStatus.UNKNOWN,
                    f"The rule raised {type(exc).__name__} and could not complete.",
                    explanation=(
                        "This is a bug in Campaign Preflight, not a finding about "
                        f"your campaign. Detail: {redact_secrets(str(exc))[:300]}"
                    ),
                    remediation="Please report this at the project issue tracker.",
                    metadata={"error_type": type(exc).__name__},
                )
            )
            continue

        if options.severity is not None and result.status in {
            RuleStatus.FAIL,
            RuleStatus.WARN,
        }:
            result = result.with_severity(options.severity)
        results.append(result)

    return tuple(sorted(results, key=lambda r: r.rule_id))


def build_report(
    ctx: PreflightContext,
    results: tuple[RuleResult, ...],
    config: PreflightConfig,
    *,
    duration_seconds: float = 0.0,
    redacted: bool = True,
) -> PreflightReport:
    """Assemble the final report from an evaluated context."""
    breakdown = score_results(results, config)
    readiness = decide_readiness(results, breakdown, config)

    limitations: list[str] = []
    for report in ctx.provider.capabilities:
        if not report.is_ok:
            limitations.append(
                f"{report.capability.value}: {report.status.value} - "
                f"{report.detail or 'no detail supplied'}"
            )
    limitations.extend(ctx.input_warnings)

    campaign = ctx.campaign
    return PreflightReport(
        tool_version=__version__,
        generated_at=ctx.generated_at,
        provider=ctx.provider.name,
        provider_read_only=ctx.provider.read_only,
        campaign_id=campaign.id if campaign else None,
        campaign_name=campaign.name if campaign else None,
        campaign_status=campaign.status if campaign else None,
        readiness=readiness,
        score=breakdown.final_score,
        score_breakdown=breakdown,
        confidence=breakdown.confidence,
        lead_count=len(ctx.leads),
        lead_count_is_partial=ctx.lead_total_hint is not None,
        sender_count=len(ctx.senders),
        suppression_count=len(ctx.suppressions),
        results=results,
        blocker_count=sum(1 for r in results if r.is_blocking),
        warning_count=sum(1 for r in results if r.status is RuleStatus.WARN),
        failure_count=sum(1 for r in results if r.status is RuleStatus.FAIL),
        unknown_count=sum(1 for r in results if r.status is RuleStatus.UNKNOWN),
        passed_count=sum(1 for r in results if r.status is RuleStatus.PASS),
        not_applicable_count=sum(
            1 for r in results if r.status is RuleStatus.NOT_APPLICABLE
        ),
        limitations=tuple(dict.fromkeys(limitations)),
        provider_errors=ctx.provider.errors,
        redacted=redacted,
        duration_seconds=round(duration_seconds, 3),
    )


async def run_preflight(
    provider: CampaignProvider,
    config: PreflightConfig,
    *,
    campaign_id: str | None = None,
    lead_limit: int | None = None,
    redacted: bool = True,
    now: datetime | None = None,
) -> PreflightReport:
    """Gather, evaluate, and score in one call. The main entry point."""
    started = time.perf_counter()
    ctx = await gather_context(
        provider, campaign_id=campaign_id, lead_limit=lead_limit, now=now
    )
    results = evaluate(ctx, config)
    duration = time.perf_counter() - started
    return build_report(
        ctx, results, config, duration_seconds=duration, redacted=redacted
    )
