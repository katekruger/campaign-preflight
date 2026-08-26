"""Context builders shared by the test suite.

``make_context`` is the workhorse: it builds a fully-formed
:class:`PreflightContext` from keyword arguments, defaulting every capability to
``SUPPORTED_OK`` so a rule test only has to describe the thing it is testing.
Pass a capability explicitly to simulate a provider that could not answer.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import (
    Campaign,
    CampaignSchedule,
    CampaignStep,
    Capability,
    CapabilityReport,
    CapabilityStatus,
    Lead,
    PersonalizationClaim,
    PreflightContext,
    ProviderMetadata,
    Sender,
    SendingWindow,
    SourceEvidence,
    SuppressionEntry,
)
from campaign_preflight.normalization import normalize_email

FIXED_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
"""A pinned clock, so any time-dependent rule is reproducible."""

WEEKDAYS = frozenset({1, 2, 3, 4, 5})

ALL_CAPABILITIES = tuple(Capability)


def make_lead(email: str | None = "ana.diaz@corp.example.com", **overrides: Any) -> Lead:
    """A complete, valid lead. Override any field to make it invalid."""
    defaults: dict[str, Any] = {
        "id": overrides.pop("id", None) or "L-1",
        "email": email,
        "normalized_email": normalize_email(email),
        "first_name": "Ana",
        "last_name": "Diaz",
        "company_name": "Corp Industries",
        "company_domain": "corp.example.com",
        "job_title": "VP Operations",
        "country": "US",
        "personalization": "Corp Industries opened a second facility this year.",
        "custom_variables": {},
        "source_row": 2,
        "source_name": "leads.csv",
        "status_label": "not_contacted",
    }
    defaults.update(overrides)
    if "email" in overrides and "normalized_email" not in overrides:
        defaults["normalized_email"] = normalize_email(overrides["email"])
    return Lead(**defaults)


def make_leads(count: int, **overrides: Any) -> tuple[Lead, ...]:
    """``count`` fully distinct valid leads.

    Personalization differs per lead: sharing one line across the batch would
    trip personalization.duplicate_across_contacts in every test that uses the
    helper, which is a fixture artefact rather than a finding.
    """
    defaults: dict[str, Any] = {}
    if "personalization" not in overrides:
        defaults["personalization"] = None  # filled in per lead below
    return tuple(
        make_lead(
            email=f"person{i}@company{i}.example.com",
            id=f"L-{i}",
            first_name=f"Person{i}",
            company_name=f"Company {i}",
            company_domain=f"company{i}.example.com",
            source_row=i + 2,
            **{
                **(
                    {"personalization": f"Company {i} opened a new site this year."}
                    if "personalization" not in overrides
                    else {}
                ),
                **overrides,
            },
        )
        for i in range(count)
    )


def make_sender(email: str = "dana@example.com", **overrides: Any) -> Sender:
    defaults: dict[str, Any] = {
        "email": email,
        "display_name": "Dana Reyes",
        "enabled": True,
        "status_label": "active",
        "status_is_error": False,
        # Comfortably above the default campaign limit of 80, so the baseline
        # context is internally coherent and capacity rules pass on it.
        "daily_limit": 100,
        "health_score": 92.0,
        "warmup_status": "active",
        "setup_pending": False,
    }
    defaults.update(overrides)
    return Sender(**defaults)


def make_window(**overrides: Any) -> SendingWindow:
    defaults: dict[str, Any] = {
        "name": "Business hours",
        "start": time(9, 0),
        "end": time(17, 0),
        "days": WEEKDAYS,
        "timezone_name": "America/Phoenix",
    }
    defaults.update(overrides)
    return SendingWindow(**defaults)


def make_schedule(**overrides: Any) -> CampaignSchedule:
    defaults: dict[str, Any] = {
        "start_date": None,
        "end_date": None,
        "windows": (make_window(),),
        "timezone_name": "America/Phoenix",
    }
    defaults.update(overrides)
    return CampaignSchedule(**defaults)


def make_step(index: int = 0, **overrides: Any) -> CampaignStep:
    defaults: dict[str, Any] = {
        "index": index,
        "step_type": "email",
        "delay": 0.0,
        "delay_unit": "days",
        "subject": "{{first_name}}, a question about {{company_name}}",
        "body": (
            "Hi {{first_name}},\n\nWe help teams like {{company_name}}.\n\n"
            "Reply unsubscribe to opt out."
        ),
        "variant_index": 0,
        "disabled": False,
    }
    defaults.update(overrides)
    return CampaignStep(**defaults)


def make_campaign(**overrides: Any) -> Campaign:
    defaults: dict[str, Any] = {
        "id": "cmp-1",
        "name": "Test Campaign",
        "status": "draft",
        "timezone_name": "America/Phoenix",
        "schedule": make_schedule(),
        "daily_limit": 80,
        "stop_on_reply": True,
        "stop_on_auto_reply": True,
        "steps": (make_step(),),
        "sender_emails": ("dana@example.com",),
        "custom_variables": {},
    }
    defaults.update(overrides)
    return Campaign(**defaults)


def make_context(
    *,
    campaign: Campaign | None = ...,  # type: ignore[assignment]
    leads: tuple[Lead, ...] | list[Lead] | None = None,
    senders: tuple[Sender, ...] | list[Sender] | None = None,
    suppressions: tuple[SuppressionEntry, ...] | list[SuppressionEntry] = (),
    evidence: tuple[SourceEvidence, ...] | list[SourceEvidence] = (),
    claims: tuple[PersonalizationClaim, ...] | list[PersonalizationClaim] = (),
    capabilities: dict[Capability, CapabilityStatus] | None = None,
    capability_details: dict[Capability, str] | None = None,
    provider_name: str = "fixture",
    now: datetime = FIXED_NOW,
    analytics: dict[str, Any] | None = None,
    lead_total_hint: int | None = None,
) -> PreflightContext:
    """Build a context. Every capability defaults to SUPPORTED_OK."""
    statuses = dict.fromkeys(ALL_CAPABILITIES, CapabilityStatus.SUPPORTED_OK)
    statuses.update(capabilities or {})
    details = capability_details or {}

    return PreflightContext(
        campaign=make_campaign() if campaign is ... else campaign,
        leads=tuple(leads if leads is not None else make_leads(3)),
        senders=tuple(senders if senders is not None else (make_sender(),)),
        suppressions=tuple(suppressions),
        evidence=tuple(evidence),
        claims=tuple(claims),
        analytics=analytics or {},
        provider=ProviderMetadata(
            name=provider_name,
            read_only=True,
            capabilities=tuple(
                CapabilityReport(
                    capability=capability,
                    status=status,
                    detail=details.get(capability, f"{capability.value} fixture"),
                )
                for capability, status in statuses.items()
            ),
            fetched_at=now,
        ),
        generated_at=now,
        lead_total_hint=lead_total_hint,
    )


def run_rule(rule_id: str, ctx: PreflightContext, config: PreflightConfig | None = None):
    """Evaluate one rule the way the engine does, honouring capability gating."""
    from campaign_preflight.rules import get_rule

    resolved = config or PreflightConfig()
    rule = get_rule(rule_id)
    options = resolved.options_for(rule.rule_id, rule.options_model)
    for capability in rule.requires:
        if not ctx.capability_status(capability).is_ok:
            return rule.missing_capability_result(ctx, capability)
    return rule.evaluate(ctx, options, resolved)
