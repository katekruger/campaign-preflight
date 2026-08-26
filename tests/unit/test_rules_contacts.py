"""Behavioural tests for the contact-data rules."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import RuleStatus, Severity
from helpers import make_context, make_lead, make_leads, run_rule


class TestEmailSyntax:
    @pytest.mark.parametrize(
        "email",
        [
            "ana@corp.example.com",
            "ana.diaz+tag@corp.example.co.uk",
            "ana_diaz@sub.corp.example.com",
            "josé@empresa.example.com",
            "user@xn--bcher-kva.example.com",
        ],
    )
    def test_valid_addresses_pass(self, email: str) -> None:
        ctx = make_context(leads=[make_lead(email=email)])
        assert run_rule("contacts.email_syntax", ctx).status is RuleStatus.PASS

    @pytest.mark.parametrize(
        "email",
        [
            "no-at-sign.example.com",
            "two@@corp.example.com",
            "trailing@corp.example.com.",
            "double..dot@corp..example.com",
            "spaces in@corp.example.com",
            "@corp.example.com",
            "ana@",
            "ana@localhost",
        ],
    )
    def test_invalid_addresses_are_reported(self, email: str) -> None:
        leads = [make_lead(email=email), *make_leads(1)]
        result = run_rule("contacts.email_syntax", make_context(leads=leads))
        assert result.status is not RuleStatus.PASS
        assert result.affected_record_count == 1

    def test_missing_email_counts_separately(self) -> None:
        leads = [make_lead(email=None), *make_leads(9)]
        result = run_rule("contacts.email_syntax", make_context(leads=leads))
        assert result.metadata["missing_email"] == 1
        assert result.metadata["malformed_email"] == 0

    def test_high_invalid_ratio_fails(self) -> None:
        leads = [make_lead(email="bad", id=f"L-{i}") for i in range(5)]
        result = run_rule("contacts.email_syntax", make_context(leads=leads))
        assert result.status is RuleStatus.FAIL

    def test_no_leads_is_not_applicable(self) -> None:
        result = run_rule("contacts.email_syntax", make_context(leads=[]))
        assert result.status is RuleStatus.NOT_APPLICABLE


class TestDuplicates:
    def test_exact_duplicates_are_found(self) -> None:
        leads = [
            make_lead(email="ana@corp.example.com", id="a"),
            make_lead(email="ana@corp.example.com", id="b"),
            *make_leads(8),
        ]
        result = run_rule("contacts.duplicate_email", make_context(leads=leads))
        assert result.affected_record_count == 2

    def test_case_only_duplicates_are_found_by_the_normalized_rule(self) -> None:
        leads = [
            make_lead(email="Ana@Corp.Example.com", id="a"),
            make_lead(email="ana@corp.example.com", id="b"),
            *make_leads(8),
        ]
        assert (
            run_rule("contacts.duplicate_email", make_context(leads=leads)).status
            is RuleStatus.PASS
        )
        result = run_rule("contacts.duplicate_normalized_email", make_context(leads=leads))
        assert result.affected_record_count == 2

    def test_normalized_rule_does_not_double_report_exact_duplicates(self) -> None:
        leads = [
            make_lead(email="ana@corp.example.com", id="a"),
            make_lead(email="ana@corp.example.com", id="b"),
            *make_leads(8),
        ]
        result = run_rule("contacts.duplicate_normalized_email", make_context(leads=leads))
        assert result.status is RuleStatus.PASS

    def test_plus_tags_are_not_folded(self) -> None:
        """Deliberate: plus-tag folding is provider-specific, so we do not do it."""
        leads = [
            make_lead(email="ana+q3@corp.example.com", id="a"),
            make_lead(email="ana@corp.example.com", id="b"),
            *make_leads(8),
        ]
        assert (
            run_rule("contacts.duplicate_normalized_email", make_context(leads=leads)).status
            is RuleStatus.PASS
        )

    def test_same_person_at_same_company_is_reported(self) -> None:
        leads = [
            make_lead(email="a@corp.example.com", id="a", first_name="Ana", last_name="Diaz"),
            make_lead(
                email="ana.diaz@corp.example.com", id="b", first_name="Ana", last_name="Diaz"
            ),
            *make_leads(8),
        ]
        result = run_rule("contacts.duplicate_company_contact", make_context(leads=leads))
        assert result.affected_record_count == 2

    def test_same_person_at_different_companies_is_not_a_duplicate(self) -> None:
        leads = [
            make_lead(
                email="a@one.example.com",
                id="a",
                first_name="Ana",
                last_name="Diaz",
                company_name="One",
                company_domain="one.example.com",
            ),
            make_lead(
                email="a@two.example.com",
                id="b",
                first_name="Ana",
                last_name="Diaz",
                company_name="Two",
                company_domain="two.example.com",
            ),
            *make_leads(8),
        ]
        result = run_rule("contacts.duplicate_company_contact", make_context(leads=leads))
        assert result.status is RuleStatus.PASS


class TestMissingFields:
    @pytest.mark.parametrize(
        ("rule_id", "field"),
        [
            ("contacts.missing_first_name", "first_name"),
            ("contacts.missing_company_name", "company_name"),
            ("contacts.missing_company_domain", "company_domain"),
            ("contacts.missing_job_title", "job_title"),
        ],
    )
    def test_complete_data_passes(self, rule_id: str, field: str) -> None:
        assert run_rule(rule_id, make_context(leads=make_leads(10))).status is RuleStatus.PASS

    @pytest.mark.parametrize(
        ("rule_id", "field"),
        [
            ("contacts.missing_first_name", "first_name"),
            ("contacts.missing_company_name", "company_name"),
            ("contacts.missing_company_domain", "company_domain"),
            ("contacts.missing_job_title", "job_title"),
        ],
    )
    def test_all_missing_fails(self, rule_id: str, field: str) -> None:
        leads = [make_lead(id=f"L-{i}", **{field: None}) for i in range(10)]
        result = run_rule(rule_id, make_context(leads=leads))
        assert result.status is RuleStatus.FAIL
        assert result.affected_record_count == 10

    def test_ratio_drives_severity(self) -> None:
        leads = [*[make_lead(id=f"L-{i}", first_name=None) for i in range(1)], *make_leads(19)]
        result = run_rule("contacts.missing_first_name", make_context(leads=leads))
        assert result.status is RuleStatus.WARN
        assert result.metadata["ratio"] == pytest.approx(0.05)

    def test_thresholds_are_configurable(self) -> None:
        leads = [make_lead(id=f"L-{i}", first_name=None) for i in range(10)]
        config = PreflightConfig(
            rules={"contacts.missing_first_name": {"blocker_ratio": 1.5, "warning_ratio": 1.5}}
        )
        result = run_rule("contacts.missing_first_name", make_context(leads=leads), config)
        assert result.status is RuleStatus.WARN
        assert result.severity is Severity.LOW


class TestHygiene:
    def test_placeholder_values_are_found(self) -> None:
        leads = [make_lead(id="a", first_name="TBD"), *make_leads(9)]
        result = run_rule("contacts.placeholder_values", make_context(leads=leads))
        assert result.affected_record_count == 1
        assert result.metadata["by_field"] == {"first_name": 1}

    def test_free_domains_are_found(self) -> None:
        leads = [make_lead(id="a", email="kaya@gmail.com"), *make_leads(9)]
        result = run_rule("contacts.free_email_domain", make_context(leads=leads))
        assert result.affected_record_count == 1

    def test_free_domains_can_be_allowed(self) -> None:
        leads = [make_lead(id="a", email="kaya@gmail.com"), *make_leads(9)]
        config = PreflightConfig(settings={"allow_free_email_domains": True})
        result = run_rule("contacts.free_email_domain", make_context(leads=leads), config)
        assert result.status is RuleStatus.NOT_APPLICABLE

    @pytest.mark.parametrize("local", ["info", "sales", "support", "no-reply"])
    def test_role_addresses_are_found(self, local: str) -> None:
        leads = [make_lead(id="a", email=f"{local}@corp.example.com"), *make_leads(9)]
        result = run_rule("contacts.role_address", make_context(leads=leads))
        assert result.affected_record_count == 1

    def test_named_addresses_are_not_role_addresses(self) -> None:
        leads = make_leads(10)
        assert (
            run_rule("contacts.role_address", make_context(leads=leads)).status is RuleStatus.PASS
        )

    def test_impossible_country_values_are_found(self) -> None:
        leads = [make_lead(id="a", country="9"), *make_leads(9)]
        result = run_rule("contacts.invalid_region", make_context(leads=leads))
        assert result.affected_record_count == 1

    def test_unverified_country_names_are_reported_as_unverified_not_wrong(self) -> None:
        leads = [make_lead(id="a", country="Ruritania"), *make_leads(9)]
        result = run_rule("contacts.invalid_region", make_context(leads=leads))
        assert result.status is RuleStatus.PASS
        assert result.metadata["unverified_country_names"] == 1

    def test_oversized_fields_are_found(self) -> None:
        leads = [make_lead(id="a", company_name="x" * 400), *make_leads(19)]
        result = run_rule("contacts.field_length", make_context(leads=leads))
        assert result.affected_record_count == 1
        assert result.metadata["longest_field"] == 400

    def test_control_characters_are_found(self) -> None:
        leads = [make_lead(id="a", first_name="An​a"), *make_leads(19)]
        result = run_rule("contacts.control_characters", make_context(leads=leads))
        assert result.affected_record_count == 1

    def test_bidi_override_is_found(self) -> None:
        leads = [make_lead(id="a", company_name="Corp‮evil"), *make_leads(19)]
        result = run_rule("contacts.control_characters", make_context(leads=leads))
        assert result.affected_record_count == 1

    @pytest.mark.parametrize(
        "value", ["=1+1", "+SUM(A1)", "@SUM(A1)", '=HYPERLINK("http://x.invalid","a")', "-cmd|calc"]
    )
    def test_formula_injection_is_found(self, value: str) -> None:
        leads = [make_lead(id="a", company_name=value), *make_leads(19)]
        result = run_rule("contacts.formula_injection", make_context(leads=leads))
        assert result.affected_record_count == 1

    @pytest.mark.parametrize("value", ["-5", "+44 20 7946 0000", "Corp Ltd", "-1.5"])
    def test_plain_values_are_not_formula_injection(self, value: str) -> None:
        leads = [make_lead(id="a", company_name=value), *make_leads(19)]
        result = run_rule("contacts.formula_injection", make_context(leads=leads))
        assert result.status is RuleStatus.PASS

    def test_formula_injection_never_blocks_by_default(self) -> None:
        """The risk lands on whoever opens an export, not on the recipients."""
        leads = [make_lead(id=f"L-{i}", company_name="=1+1") for i in range(10)]
        result = run_rule("contacts.formula_injection", make_context(leads=leads))
        assert result.severity is not Severity.BLOCKER
