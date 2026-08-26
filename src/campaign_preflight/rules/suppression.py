"""Suppression and eligibility rules (checks 26-33).

A note on scope. Nothing here is a compliance check. Region rules in particular
encode *your organization's outreach policy*, configured by you, and say nothing
about GDPR, CAN-SPAM, CASL, or any other regime. See ``docs/limitations.md``.

The most important behavior in this module is the last rule: when the
suppression capability is unavailable, that fact is reported as its own finding
rather than letting every suppression check quietly report "clean".
"""

from __future__ import annotations

from dataclasses import dataclass

from collections import Counter
from typing import ClassVar

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Capability,
    CapabilityStatus,
    Lead,
    PreflightContext,
    RuleCategory,
    RuleResult,
    Severity,
)
from ..normalization import normalize_domain
from .base import Rule, register

__all__: list[str] = []


def _suppressed_values(ctx: PreflightContext) -> tuple[frozenset[str], frozenset[str]]:
    """Split the suppression list into (addresses, domains)."""
    addresses = {e.value for e in ctx.suppressions if not e.is_domain}
    domains = {e.value for e in ctx.suppressions if e.is_domain}
    return frozenset(addresses), frozenset(domains)


def _lead_domains(lead: Lead) -> set[str]:
    """Every domain a lead can be matched on: mailbox domain and company domain."""
    found = set()
    if lead.email_domain:
        found.add(lead.email_domain)
    company = normalize_domain(lead.company_domain)
    if company:
        found.add(company)
    return found


class _SuppressionRule(Rule):
    category = RuleCategory.SUPPRESSION
    requires = (Capability.LEADS, Capability.SUPPRESSIONS)

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples


@register
class ContactSuppressed(_SuppressionRule):
    rule_id = "suppression.contact_listed"
    title = "No contact is on the suppression list"
    category = RuleCategory.SUPPRESSION
    severity = Severity.BLOCKER
    description = (
        "Matches lead addresses against the supplied or provider suppression list. "
        "A single suppressed contact is treated as a blocker: contacting someone "
        "who asked not to be contacted is the failure this tool exists to prevent."
    )
    remediation = "Remove these contacts from the campaign before activation."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        addresses, _ = _suppressed_values(ctx)
        flagged_by_input = [lead for lead in ctx.leads if lead.suppressed is True]
        matched = [
            lead
            for lead in ctx.leads
            if lead.normalized_email and lead.normalized_email in addresses
        ]
        affected = {id(lead): lead for lead in (*matched, *flagged_by_input)}.values()
        count = len(affected)
        if count == 0:
            return self.passed(
                f"No contact matches any of the {len(ctx.suppressions)} suppression entries.",
                metadata={"suppression_entries": len(ctx.suppressions)},
            )
        return self.failed(
            f"{count} contact(s) appear on the active suppression list.",
            affected=count,
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={
                "matched_by_list": len(matched),
                "flagged_in_input": len(flagged_by_input),
            },
        )


@register
class DomainSuppressed(_SuppressionRule):
    rule_id = "suppression.domain_listed"
    title = "No contact is on a suppressed domain"
    category = RuleCategory.SUPPRESSION
    severity = Severity.BLOCKER
    description = (
        "Domain-level suppression covers everyone at an organization. Matches on "
        "both the mailbox domain and the company domain."
    )
    remediation = "Remove contacts at the suppressed domains before activation."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        _, domains = _suppressed_values(ctx)
        if not domains:
            return self.passed("The suppression list contains no domain entries.")
        affected = [lead for lead in ctx.leads if _lead_domains(lead) & domains]
        if not affected:
            return self.passed(
                f"No contact belongs to any of the {len(domains)} suppressed domains.",
                metadata={"suppressed_domains": len(domains)},
            )
        hit_domains = Counter(
            domain
            for lead in affected
            for domain in sorted(_lead_domains(lead) & domains)
        )
        return self.failed(
            f"{len(affected)} contact(s) belong to a suppressed domain.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={"domains": dict(hit_domains.most_common(10))},
        )


@dataclass(frozen=True)
class DuplicateInCampaignOptions(RuleOptions):
    severity_when_found: Severity = Severity.MEDIUM


@register
class DuplicateInCampaign(_SuppressionRule):
    rule_id = "suppression.duplicate_in_campaign"
    title = "No contact is already present in the campaign"
    category = RuleCategory.SUPPRESSION
    severity = Severity.MEDIUM
    requires = (Capability.LEADS,)
    description = (
        "Detects contacts the provider reports as already contacted or completed "
        "in this campaign. Only checkable when the provider exposes lead status; "
        "otherwise the result is UNKNOWN."
    )
    remediation = "Exclude already-contacted leads from the import."

    _ALREADY_TOUCHED = frozenset(
        {"contacted", "completed", "replied", "bounced", "unsubscribed", "in_progress"}
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        with_status = [lead for lead in ctx.leads if lead.status_label]
        if not with_status:
            return self.unknown(
                "No per-lead status was supplied, so prior contact cannot be determined.",
                explanation=(
                    "This check needs a lead status field. Add a 'status' column to "
                    "your CSV, or use a provider that reports lead status."
                ),
            )
        affected = [
            lead
            for lead in with_status
            if (lead.status_label or "").strip().lower() in self._ALREADY_TOUCHED
        ]
        if not affected:
            return self.passed(
                f"None of the {len(with_status)} contacts with a status have been contacted yet."
            )
        return self.warn(
            f"{len(affected)} contact(s) have already been contacted in this campaign.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
        )


class _DomainPolicyRule(_SuppressionRule):
    """Shared implementation for the configured-domain-list checks.

    Each subclass names a settings key holding a list of domains. With the list
    empty the check is NOT_APPLICABLE -- it is opt-in policy, so an unconfigured
    list is not a finding.
    """

    requires = (Capability.LEADS,)
    setting_name: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        configured = frozenset(getattr(config.settings, self.setting_name))
        if not configured:
            return self.not_applicable(
                f"No {self.label} domains are configured "
                f"(set settings.{self.setting_name} to enable this check)."
            )
        affected = [lead for lead in ctx.leads if _lead_domains(lead) & configured]
        if not affected:
            return self.passed(
                f"No contact belongs to one of the {len(configured)} configured "
                f"{self.label} domains."
            )
        return self.failed(
            f"{len(affected)} contact(s) belong to a configured {self.label} domain.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={"configured_domains": len(configured)},
        )


@register
class ExistingCustomer(_DomainPolicyRule):
    rule_id = "suppression.existing_customer"
    title = "No contact is at an existing-customer domain"
    category = RuleCategory.SUPPRESSION
    severity = Severity.HIGH
    setting_name = "customer_domains"
    label = "customer"
    description = (
        "Cold-outbound copy landing in a current customer's inbox is a recurring, "
        "avoidable embarrassment. Populate settings.customer_domains from your CRM."
    )
    remediation = "Remove existing customers from the prospecting campaign."


@register
class InternalDomain(_DomainPolicyRule):
    rule_id = "suppression.internal_domain"
    title = "No contact is at an internal domain"
    category = RuleCategory.SUPPRESSION
    severity = Severity.BLOCKER
    setting_name = "internal_domains"
    label = "internal"
    description = (
        "Your own employees receiving cold outbound is both noise and a sign the "
        "list was built without a filter."
    )
    remediation = "Remove internal addresses from the campaign."


@register
class CompetitorDomain(_DomainPolicyRule):
    rule_id = "suppression.competitor_domain"
    title = "No contact is at a competitor domain"
    category = RuleCategory.SUPPRESSION
    severity = Severity.MEDIUM
    setting_name = "competitor_domains"
    label = "competitor"
    description = (
        "Competitors receiving your sequence see your positioning, pricing hooks, "
        "and cadence."
    )
    remediation = "Remove competitor contacts from the campaign."


@register
class RestrictedRegion(_SuppressionRule):
    rule_id = "suppression.restricted_region"
    title = "No contact is in a restricted region"
    category = RuleCategory.SUPPRESSION
    severity = Severity.HIGH
    requires = (Capability.LEADS,)
    description = (
        "Contacts in regions your organization has chosen not to contact. This is "
        "an ORGANIZATION-CONFIGURED OUTREACH POLICY, not a legal compliance check. "
        "Campaign Preflight does not provide legal advice and does not determine "
        "whether outreach to any region is lawful."
    )
    remediation = "Remove contacts in restricted regions, or update settings.restricted_regions."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        restricted = frozenset(config.settings.restricted_regions)
        if not restricted:
            return self.not_applicable(
                "No restricted regions are configured "
                "(set settings.restricted_regions to enable this check)."
            )
        affected = []
        unknown_region = 0
        for lead in ctx.leads:
            values = {
                (lead.country or "").strip().upper(),
                (lead.region or "").strip().upper(),
            } - {""}
            if not values:
                unknown_region += 1
                continue
            if values & restricted:
                affected.append(lead)
        if affected:
            return self.failed(
                f"{len(affected)} contact(s) are in a region your outreach policy "
                f"excludes.",
                affected=len(affected),
                samples=self.sample([lead.label for lead in affected], self.limit(config)),
                metadata={
                    "restricted_regions": sorted(restricted),
                    "contacts_without_region": unknown_region,
                },
            )
        if unknown_region:
            return self.warn(
                f"No contact is in a restricted region, but {unknown_region} "
                f"contact(s) have no region value to check.",
                severity=Severity.LOW,
                affected=unknown_region,
                remediation="Populate country or region so this policy can be applied.",
                metadata={"contacts_without_region": unknown_region},
            )
        return self.passed(
            f"No contact is in one of the {len(restricted)} restricted regions."
        )


@register
class SuppressionCapability(Rule):
    rule_id = "suppression.capability_unavailable"
    title = "Suppression data was available"
    category = RuleCategory.SUPPRESSION
    severity = Severity.HIGH
    description = (
        "Reports the availability of suppression data as a finding in its own "
        "right. Without it, an unknown number of do-not-contact records may be in "
        "this campaign and the suppression checks above could not run."
    )
    remediation = (
        "Supply a suppressions file with --suppressions, or grant the API key "
        "block-list read access."
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        status = ctx.capability_status(Capability.SUPPRESSIONS)
        detail = ctx.capability_detail(Capability.SUPPRESSIONS) or "no detail supplied"
        metadata = {"capability_status": status.value, "detail": detail}

        if status is CapabilityStatus.SUPPORTED_OK:
            return self.passed(
                f"Suppression data was available ({len(ctx.suppressions)} entries).",
                metadata={**metadata, "entries": len(ctx.suppressions)},
            )
        if status is CapabilityStatus.UNAVAILABLE_CONFIG:
            return self.warn(
                "No suppression list was supplied, so suppression checks did not run.",
                severity=Severity.HIGH,
                explanation=(
                    "This is not the same as a clean list. Nothing was checked "
                    f"against a suppression source. Provider detail: {detail}"
                ),
                metadata=metadata,
            )
        if status is CapabilityStatus.UNAVAILABLE_PERMISSIONS:
            return self.warn(
                "Suppression data is blocked by the current credentials.",
                severity=Severity.HIGH,
                explanation=(
                    "The API key is valid but lacks block-list read access. "
                    f"Provider detail: {detail}"
                ),
                remediation="Grant the key block_list_entries:read, or supply a CSV.",
                metadata=metadata,
            )
        return self.unknown(
            f"Suppression data was unavailable ({status.value}).",
            explanation=(
                "Suppression checks could not run. Treating this as UNKNOWN rather "
                f"than a pass. Provider detail: {detail}"
            ),
            metadata=metadata,
        )
