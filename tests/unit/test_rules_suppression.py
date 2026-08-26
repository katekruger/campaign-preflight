"""Behavioural tests for the suppression and eligibility rules."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import (
    Capability,
    CapabilityStatus,
    RuleStatus,
    Severity,
    SuppressionEntry,
)
from helpers import make_context, make_lead, make_leads, run_rule


def entry(value: str, *, is_domain: bool = False) -> SuppressionEntry:
    return SuppressionEntry(value=value, is_domain=is_domain, reason="test")


class TestContactSuppressed:
    def test_clean_list_passes(self) -> None:
        ctx = make_context(leads=make_leads(5), suppressions=[entry("other@x.example.com")])
        assert run_rule("suppression.contact_listed", ctx).status is RuleStatus.PASS

    def test_a_single_match_is_a_blocker(self) -> None:
        leads = [make_lead(email="ana@corp.example.com"), *make_leads(99)]
        ctx = make_context(leads=leads, suppressions=[entry("ana@corp.example.com")])
        result = run_rule("suppression.contact_listed", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER
        assert result.affected_record_count == 1

    def test_matching_is_case_insensitive(self) -> None:
        leads = [make_lead(email="Ana@Corp.Example.com")]
        ctx = make_context(leads=leads, suppressions=[entry("ana@corp.example.com")])
        assert run_rule("suppression.contact_listed", ctx).status is RuleStatus.FAIL

    def test_input_flag_is_honoured(self) -> None:
        leads = [make_lead(suppressed=True), *make_leads(5)]
        ctx = make_context(leads=leads, suppressions=[])
        result = run_rule("suppression.contact_listed", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.metadata["flagged_in_input"] == 1

    def test_unavailable_suppressions_never_pass(self) -> None:
        ctx = make_context(
            capabilities={Capability.SUPPRESSIONS: CapabilityStatus.UNAVAILABLE_CONFIG}
        )
        assert run_rule("suppression.contact_listed", ctx).status is RuleStatus.UNKNOWN


class TestDomainSuppressed:
    def test_mailbox_domain_match(self) -> None:
        leads = [make_lead(email="ana@blocked.example.com", company_domain=None)]
        ctx = make_context(leads=leads, suppressions=[entry("blocked.example.com", is_domain=True)])
        assert run_rule("suppression.domain_listed", ctx).status is RuleStatus.FAIL

    def test_company_domain_match(self) -> None:
        leads = [make_lead(email="ana@personal.example.net", company_domain="blocked.example.com")]
        ctx = make_context(leads=leads, suppressions=[entry("blocked.example.com", is_domain=True)])
        assert run_rule("suppression.domain_listed", ctx).status is RuleStatus.FAIL

    def test_no_domain_entries_passes(self) -> None:
        ctx = make_context(suppressions=[entry("a@b.example.com")])
        assert run_rule("suppression.domain_listed", ctx).status is RuleStatus.PASS


class TestDuplicateInCampaign:
    def test_no_status_is_unknown_not_a_pass(self) -> None:
        leads = [make_lead(status_label=None)]
        assert (
            run_rule("suppression.duplicate_in_campaign", make_context(leads=leads)).status
            is RuleStatus.UNKNOWN
        )

    def test_already_contacted_warns(self) -> None:
        leads = [make_lead(id="a", status_label="contacted"), *make_leads(5)]
        result = run_rule("suppression.duplicate_in_campaign", make_context(leads=leads))
        assert result.status is RuleStatus.WARN
        assert result.affected_record_count == 1

    def test_not_contacted_passes(self) -> None:
        assert (
            run_rule("suppression.duplicate_in_campaign", make_context(leads=make_leads(5))).status
            is RuleStatus.PASS
        )


class TestDomainPolicyRules:
    @pytest.mark.parametrize(
        ("rule_id", "setting"),
        [
            ("suppression.existing_customer", "customer_domains"),
            ("suppression.internal_domain", "internal_domains"),
            ("suppression.competitor_domain", "competitor_domains"),
        ],
    )
    def test_unconfigured_is_not_applicable(self, rule_id: str, setting: str) -> None:
        assert run_rule(rule_id, make_context()).status is RuleStatus.NOT_APPLICABLE

    @pytest.mark.parametrize(
        ("rule_id", "setting"),
        [
            ("suppression.existing_customer", "customer_domains"),
            ("suppression.internal_domain", "internal_domains"),
            ("suppression.competitor_domain", "competitor_domains"),
        ],
    )
    def test_configured_domain_match_fails(self, rule_id: str, setting: str) -> None:
        leads = [make_lead(email="ana@flagged.example.com", company_domain="flagged.example.com")]
        config = PreflightConfig(settings={setting: ["flagged.example.com"]})
        result = run_rule(rule_id, make_context(leads=leads), config)
        assert result.status is RuleStatus.FAIL
        assert result.affected_record_count == 1

    def test_internal_domain_is_a_blocker(self) -> None:
        leads = [make_lead(email="dana@example.com", company_domain="example.com")]
        config = PreflightConfig(settings={"internal_domains": ["example.com"]})
        result = run_rule("suppression.internal_domain", make_context(leads=leads), config)
        assert result.severity is Severity.BLOCKER

    def test_domain_lists_are_normalized(self) -> None:
        leads = [make_lead(email="ana@flagged.example.com", company_domain=None)]
        config = PreflightConfig(settings={"customer_domains": ["  @Flagged.Example.COM "]})
        result = run_rule("suppression.existing_customer", make_context(leads=leads), config)
        assert result.status is RuleStatus.FAIL


class TestRestrictedRegion:
    def test_unconfigured_is_not_applicable(self) -> None:
        assert (
            run_rule("suppression.restricted_region", make_context()).status
            is RuleStatus.NOT_APPLICABLE
        )

    def test_matching_region_fails(self) -> None:
        leads = [make_lead(country="DE"), *make_leads(3)]
        config = PreflightConfig(settings={"restricted_regions": ["de"]})
        result = run_rule("suppression.restricted_region", make_context(leads=leads), config)
        assert result.status is RuleStatus.FAIL
        assert result.affected_record_count == 1

    def test_missing_region_warns_rather_than_passing_silently(self) -> None:
        leads = [make_lead(country=None, region=None)]
        config = PreflightConfig(settings={"restricted_regions": ["DE"]})
        result = run_rule("suppression.restricted_region", make_context(leads=leads), config)
        assert result.status is RuleStatus.WARN
        assert result.metadata["contacts_without_region"] == 1

    def test_description_disclaims_legal_advice(self) -> None:
        from campaign_preflight.rules import get_rule

        description = get_rule("suppression.restricted_region").description.upper()
        assert "NOT A LEGAL COMPLIANCE CHECK" in description


class TestSuppressionCapability:
    def test_available_passes(self) -> None:
        ctx = make_context(suppressions=[entry("a@b.example.com")])
        assert run_rule("suppression.capability_unavailable", ctx).status is RuleStatus.PASS

    def test_missing_file_is_a_high_warning_not_a_pass(self) -> None:
        ctx = make_context(
            capabilities={Capability.SUPPRESSIONS: CapabilityStatus.UNAVAILABLE_CONFIG}
        )
        result = run_rule("suppression.capability_unavailable", ctx)
        assert result.status is RuleStatus.WARN
        assert result.severity is Severity.HIGH
        assert "not the same as a clean list" in result.explanation

    def test_permission_denied_is_reported_distinctly(self) -> None:
        ctx = make_context(
            capabilities={Capability.SUPPRESSIONS: CapabilityStatus.UNAVAILABLE_PERMISSIONS}
        )
        result = run_rule("suppression.capability_unavailable", ctx)
        assert result.status is RuleStatus.WARN
        assert "credentials" in result.summary

    def test_endpoint_failure_is_unknown(self) -> None:
        ctx = make_context(
            capabilities={Capability.SUPPRESSIONS: CapabilityStatus.SUPPORTED_FAILED}
        )
        assert (
            run_rule("suppression.capability_unavailable", ctx).status is RuleStatus.UNKNOWN
        )

    def test_empty_list_differs_from_unavailable_list(self) -> None:
        """The core distinction: nothing found is not the same as could not look."""
        found_nothing = run_rule("suppression.contact_listed", make_context(suppressions=[]))
        could_not_look = run_rule(
            "suppression.contact_listed",
            make_context(capabilities={Capability.SUPPRESSIONS: CapabilityStatus.SUPPORTED_FAILED}),
        )
        assert found_nothing.status is RuleStatus.PASS
        assert could_not_look.status is RuleStatus.UNKNOWN
