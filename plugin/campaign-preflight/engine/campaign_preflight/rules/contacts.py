"""Contact-data rules (checks 11-25).

Most of these are ratio-based: one lead missing a first name is noise, a quarter
of the list missing first names is a broken import. Thresholds are configurable,
and every rule reports both the count and the ratio so the number in the report
can be checked by hand.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, ClassVar

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Capability,
    Lead,
    PreflightContext,
    RuleCategory,
    RuleResult,
    RuleStatus,
    Severity,
)
from ..normalization import (
    FREE_EMAIL_DOMAINS,
    PLACEHOLDER_VALUES,
    ROLE_LOCAL_PARTS,
    collapse_whitespace,
    email_is_syntactically_valid,
    has_control_characters,
    is_formula_injection,
    normalize_domain,
)
from .base import Rule, register

__all__: list[str] = []

# ISO 3166-1 alpha-2 codes plus the country names most often typed by hand. Used
# only to spot values that cannot be a country at all -- an unrecognized value is
# reported as unverifiable, never as wrong.
_COMMON_COUNTRY_NAMES = frozenset(
    {
        "australia",
        "austria",
        "belgium",
        "brazil",
        "canada",
        "chile",
        "china",
        "colombia",
        "czechia",
        "czech republic",
        "denmark",
        "finland",
        "france",
        "germany",
        "greece",
        "hong kong",
        "hungary",
        "india",
        "indonesia",
        "ireland",
        "israel",
        "italy",
        "japan",
        "malaysia",
        "mexico",
        "netherlands",
        "new zealand",
        "norway",
        "philippines",
        "poland",
        "portugal",
        "romania",
        "singapore",
        "south africa",
        "south korea",
        "korea",
        "spain",
        "sweden",
        "switzerland",
        "taiwan",
        "thailand",
        "turkey",
        "ukraine",
        "united arab emirates",
        "united kingdom",
        "great britain",
        "uk",
        "united states",
        "united states of america",
        "usa",
        "us",
        "vietnam",
    }
)


@dataclass(frozen=True)
class RatioOptions(RuleOptions):
    """Shared shape for count/ratio driven contact rules."""

    warning_ratio: float = 0.05
    blocker_ratio: float = 0.25
    min_count: int = 1
    """Ignore the finding entirely below this absolute count."""


def _ratio_result(
    rule: Rule,
    options: RatioOptions,
    affected: list[Lead],
    total: int,
    *,
    what: str,
    max_samples: int,
    pass_summary: str,
    metadata: dict[str, Any] | None = None,
    blocker_severity: Severity = Severity.HIGH,
) -> RuleResult:
    """Turn an affected-lead list into a PASS/WARN/FAIL by configured ratio."""
    count = len(affected)
    meta: dict[str, Any] = {"affected": count, "total": total, **(metadata or {})}
    if total == 0:
        return rule.not_applicable("No leads to check.", metadata=meta)
    if count == 0 or count < options.min_count:
        return rule.passed(pass_summary, metadata=meta)

    ratio = count / total
    meta["ratio"] = round(ratio, 4)
    samples = rule.sample([lead.label for lead in affected], max_samples)
    summary = f"{count} of {total} contacts ({ratio:.1%}) {what}."

    if ratio >= options.blocker_ratio:
        return rule.failed(
            summary,
            severity=blocker_severity,
            affected=count,
            samples=samples,
            metadata={**meta, "threshold": options.blocker_ratio},
        )
    if ratio >= options.warning_ratio:
        return rule.warn(
            summary,
            affected=count,
            samples=samples,
            metadata={**meta, "threshold": options.warning_ratio},
        )
    return rule.warn(
        summary,
        severity=Severity.LOW,
        affected=count,
        samples=samples,
        metadata=meta,
    )


class _LeadRule(Rule):
    """Base for rules that scan every lead. Provides the sample limit."""

    category = RuleCategory.CONTACTS
    requires: ClassVar[tuple[Capability, ...]] = (Capability.LEADS,)
    options_model = RatioOptions

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples


# ---------------------------------------------------------------------------
# 11-14: identity and duplicates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailSyntaxOptions(RatioOptions):
    warning_ratio: float = 0.0
    blocker_ratio: float = 0.02


@register
class EmailSyntax(_LeadRule):
    rule_id = "contacts.email_syntax"
    title = "Email addresses are syntactically valid"
    category = RuleCategory.CONTACTS
    severity = Severity.HIGH
    options_model = EmailSyntaxOptions
    description = (
        "Syntax only. A syntactically valid address may still bounce; Campaign "
        "Preflight never verifies mailboxes over the network."
    )
    remediation = "Fix or remove the invalid addresses before importing."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, EmailSyntaxOptions)
        missing = [lead for lead in ctx.leads if not lead.email]
        invalid = [
            lead
            for lead in ctx.leads
            if lead.email and not email_is_syntactically_valid(lead.email)
        ]
        affected = missing + invalid
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="have a missing or malformed email address",
            max_samples=self.limit(config),
            pass_summary=f"All {len(ctx.leads)} contacts have a syntactically valid address.",
            metadata={"missing_email": len(missing), "malformed_email": len(invalid)},
        )


@dataclass(frozen=True)
class DuplicateOptions(RatioOptions):
    warning_ratio: float = 0.0
    blocker_ratio: float = 0.10


@register
class DuplicateEmail(_LeadRule):
    rule_id = "contacts.duplicate_email"
    title = "No exact duplicate email addresses"
    category = RuleCategory.CONTACTS
    severity = Severity.HIGH
    options_model = DuplicateOptions
    description = "Exact byte-for-byte repeats of the same address in the same campaign."
    remediation = "De-duplicate the list before importing."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DuplicateOptions)
        counts = Counter(lead.email for lead in ctx.leads if lead.email)
        repeated = {value for value, n in counts.items() if n > 1}
        affected = [lead for lead in ctx.leads if lead.email in repeated]
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="are exact duplicate addresses",
            max_samples=self.limit(config),
            pass_summary="No exact duplicate addresses.",
            metadata={"distinct_duplicated_values": len(repeated)},
        )


@register
class DuplicateNormalizedEmail(_LeadRule):
    rule_id = "contacts.duplicate_normalized_email"
    title = "No duplicate addresses after normalization"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = DuplicateOptions
    description = (
        "Catches duplicates that differ only by case or Unicode form "
        "(Ana@Corp.com vs ana@corp.com). Gmail dot and plus-tag folding is "
        "deliberately not applied: those are provider-specific and folding them "
        "would merge addresses you may consider distinct."
    )
    remediation = "Normalize addresses to lowercase and de-duplicate."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DuplicateOptions)
        exact = Counter(lead.email for lead in ctx.leads if lead.email)
        exact_dupes = {value for value, n in exact.items() if n > 1}
        counts = Counter(lead.normalized_email for lead in ctx.leads if lead.normalized_email)
        repeated = {value for value, n in counts.items() if n > 1}
        # Only report the ones an exact-match check would have missed.
        affected = [
            lead
            for lead in ctx.leads
            if lead.normalized_email in repeated and lead.email not in exact_dupes
        ]
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="collide with another contact once case and Unicode are normalized",
            max_samples=self.limit(config),
            pass_summary="No case-insensitive duplicate addresses.",
            metadata={"distinct_duplicated_values": len(repeated)},
        )


@register
class DuplicateCompanyContact(_LeadRule):
    rule_id = "contacts.duplicate_company_contact"
    title = "No repeated person-at-company combinations"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    options_model = DuplicateOptions
    description = (
        "The same person listed twice at one company under two addresses. Note "
        "that the same name at two different companies is a distinct person and "
        "is not reported."
    )
    remediation = "Merge the duplicate records so the person is contacted once."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DuplicateOptions)
        keys: dict[tuple[str, str, str], list[Lead]] = defaultdict(list)
        for lead in ctx.leads:
            first = (lead.first_name or "").strip().lower()
            last = (lead.last_name or "").strip().lower()
            company = (
                normalize_domain(lead.company_domain) or (lead.company_name or "").strip().lower()
            )
            if not first or not last or not company:
                continue
            keys[(first, last, company)].append(lead)
        affected = [lead for group in keys.values() if len(group) > 1 for lead in group]
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="are the same person at the same company under more than one record",
            max_samples=self.limit(config),
            pass_summary="No repeated person-at-company combinations.",
            metadata={"duplicate_groups": sum(1 for g in keys.values() if len(g) > 1)},
        )


# ---------------------------------------------------------------------------
# 15-18: field completeness
# ---------------------------------------------------------------------------


class _MissingFieldRule(_LeadRule):
    """Shared implementation for the four missing-field checks.

    Subclasses supply only the field name and the wording; the counting, the
    ratio thresholds, and the sampling are identical for all of them.
    """

    field: ClassVar[str] = ""
    phrase: ClassVar[str] = ""

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, RatioOptions)
        affected = [lead for lead in ctx.leads if not getattr(lead, self.field)]
        readable = self.field.replace("_", " ")
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what=self.phrase,
            max_samples=self.limit(config),
            pass_summary=f"Every contact has a {readable}.",
        )


@dataclass(frozen=True)
class _FirstNameOptions(RatioOptions):
    warning_ratio: float = 0.05
    blocker_ratio: float = 0.25


@register
class MissingFirstName(_MissingFieldRule):
    rule_id = "contacts.missing_first_name"
    title = "Contacts have a first name"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = _FirstNameOptions
    field = "first_name"
    phrase = "are missing a first name"
    description = (
        "Counts contacts with no usable first name. Copy that greets by name "
        "will render a bare 'Hi,' for each of them."
    )
    remediation = "Backfill the missing first names, or use a fallback in your copy."


@dataclass(frozen=True)
class _CompanyNameOptions(RatioOptions):
    warning_ratio: float = 0.05
    blocker_ratio: float = 0.25


@register
class MissingCompanyName(_MissingFieldRule):
    rule_id = "contacts.missing_company_name"
    title = "Contacts have a company name"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = _CompanyNameOptions
    field = "company_name"
    phrase = "are missing a company name"
    description = "Counts contacts with no company name for the copy to reference."
    remediation = "Backfill the missing company names before importing."


@dataclass(frozen=True)
class _CompanyDomainOptions(RatioOptions):
    warning_ratio: float = 0.20
    blocker_ratio: float = 0.60


@register
class MissingCompanyDomain(_MissingFieldRule):
    rule_id = "contacts.missing_company_domain"
    title = "Contacts have a company domain"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    options_model = _CompanyDomainOptions
    field = "company_domain"
    phrase = "are missing a company domain"
    description = (
        "Counts contacts with no company domain. Domain-level suppression cannot "
        "be applied to a contact whose company domain is unknown."
    )
    remediation = "Backfill company domains so domain-level suppression can be applied."


@dataclass(frozen=True)
class _JobTitleOptions(RatioOptions):
    warning_ratio: float = 0.25
    blocker_ratio: float = 0.75


@register
class MissingJobTitle(_MissingFieldRule):
    rule_id = "contacts.missing_job_title"
    title = "Contacts have a job title"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    options_model = _JobTitleOptions
    field = "job_title"
    phrase = "are missing a job title"
    description = "Counts contacts with no job title for targeting or copy to use."
    remediation = "Backfill job titles if your copy or targeting depends on them."


# ---------------------------------------------------------------------------
# 19-25: data hygiene
# ---------------------------------------------------------------------------

_CHECKED_TEXT_FIELDS = ("first_name", "last_name", "company_name", "job_title")


@register
class PlaceholderValues(_LeadRule):
    rule_id = "contacts.placeholder_values"
    title = "No placeholder values in contact fields"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    description = (
        "Values like 'TBD', 'test', 'N/A', or 'Acme Inc' that survived an import "
        "and would be merged straight into an email."
    )
    remediation = "Clear or correct the placeholder values before importing."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, RatioOptions)
        affected: list[Lead] = []
        hits: Counter[str] = Counter()
        for lead in ctx.leads:
            for field in _CHECKED_TEXT_FIELDS:
                value = getattr(lead, field)
                if value and collapse_whitespace(str(value)).lower() in PLACEHOLDER_VALUES:
                    affected.append(lead)
                    hits[field] += 1
                    break
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="contain a placeholder value in a field used for personalization",
            max_samples=self.limit(config),
            pass_summary="No placeholder values found in contact fields.",
            metadata={"by_field": dict(hits)},
        )


@dataclass(frozen=True)
class FreeDomainOptions(RatioOptions):
    warning_ratio: float = 0.05
    blocker_ratio: float = 0.40


@register
class FreeEmailDomain(_LeadRule):
    rule_id = "contacts.free_email_domain"
    title = "Contacts use business addresses"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    options_model = FreeDomainOptions
    description = (
        "Personal-mailbox domains (gmail.com, outlook.com, ...) in a B2B list. "
        "Set settings.allow_free_email_domains if this is expected for your motion."
    )
    remediation = "Remove personal addresses, or allow them in your config."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, FreeDomainOptions)
        if config.settings.allow_free_email_domains:
            return self.not_applicable("Personal email domains are allowed by configuration.")
        affected = [lead for lead in ctx.leads if (lead.email_domain or "") in FREE_EMAIL_DOMAINS]
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="use a free or personal email domain",
            max_samples=self.limit(config),
            pass_summary="No free or personal email domains found.",
        )


@dataclass(frozen=True)
class RoleAddressOptions(RatioOptions):
    warning_ratio: float = 0.02
    blocker_ratio: float = 0.20


@register
class RoleAddress(_LeadRule):
    rule_id = "contacts.role_address"
    title = "Contacts are individuals, not shared inboxes"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = RoleAddressOptions
    description = (
        "Role addresses (info@, sales@, support@) reach a shared inbox. They "
        "attract complaints and cannot be personalized meaningfully."
    )
    remediation = "Replace role addresses with named contacts, or allow them in your config."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, RoleAddressOptions)
        if config.settings.allow_role_addresses:
            return self.not_applicable("Role addresses are allowed by configuration.")
        affected = []
        for lead in ctx.leads:
            normalized = lead.normalized_email
            if not normalized or "@" not in normalized:
                continue
            local = normalized.split("@", 1)[0]
            if local in ROLE_LOCAL_PARTS or local.replace(".", "") in ROLE_LOCAL_PARTS:
                affected.append(lead)
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="are role or shared-inbox addresses",
            max_samples=self.limit(config),
            pass_summary="No role or shared-inbox addresses found.",
        )


@register
class InvalidRegion(_LeadRule):
    rule_id = "contacts.invalid_region"
    title = "Country and region values are usable"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    description = (
        "Flags country values that cannot be a country (a number, a single "
        "character, a placeholder). An unrecognized but plausible country name "
        "is reported as unverified, not as wrong."
    )
    remediation = "Normalize country values to ISO 3166-1 alpha-2 codes."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, RatioOptions)
        with_country = [lead for lead in ctx.leads if lead.country]
        if not with_country:
            return self.not_applicable("No contacts carry a country value.")
        affected = []
        unverified = 0
        for lead in with_country:
            value = collapse_whitespace(str(lead.country)).lower()
            if value in PLACEHOLDER_VALUES or value.isdigit() or len(value) < 2:
                affected.append(lead)
            elif not (len(value) == 2 and value.isalpha()) and value not in _COMMON_COUNTRY_NAMES:
                unverified += 1
        result = _ratio_result(
            self,
            options,
            affected,
            len(with_country),
            what="have a country value that cannot be a country",
            max_samples=self.limit(config),
            pass_summary=f"All {len(with_country)} country values are usable.",
            metadata={"unverified_country_names": unverified},
        )
        if result.status is RuleStatus.PASS and unverified:
            return self.passed(
                f"{len(with_country)} country values are usable; {unverified} are "
                f"free-text names this tool does not verify against ISO 3166.",
                metadata=dict(result.metadata),
            )
        return result


@dataclass(frozen=True)
class FieldLengthOptions(RatioOptions):
    max_length: int = 255
    warning_ratio: float = 0.0
    blocker_ratio: float = 0.10


@register
class FieldLength(_LeadRule):
    rule_id = "contacts.field_length"
    title = "Contact fields are not excessively long"
    category = RuleCategory.CONTACTS
    severity = Severity.LOW
    options_model = FieldLengthOptions
    description = (
        "Very long field values usually mean a shifted column or a pasted "
        "paragraph, and they break subject lines when merged."
    )
    remediation = "Trim or correct the oversized values."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, FieldLengthOptions)
        affected = []
        longest = 0
        for lead in ctx.leads:
            values = [getattr(lead, f) for f in (*_CHECKED_TEXT_FIELDS, "email")]
            values.extend(lead.custom_variables.values())
            over = [v for v in values if v and len(str(v)) > options.max_length]
            if over:
                longest = max(longest, max(len(str(v)) for v in over))
                affected.append(lead)
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what=f"have a field longer than {options.max_length} characters",
            max_samples=self.limit(config),
            pass_summary=f"No field exceeds {options.max_length} characters.",
            metadata={"longest_field": longest, "max_length": options.max_length},
        )


@dataclass(frozen=True)
class ControlCharacterOptions(RatioOptions):
    warning_ratio: float = 0.0
    blocker_ratio: float = 0.10


@register
class ControlCharacters(_LeadRule):
    rule_id = "contacts.control_characters"
    title = "Contact fields are free of control and bidi characters"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = ControlCharacterOptions
    description = (
        "Control characters, zero-width spaces, and bidirectional overrides. "
        "These corrupt rendered email and can be used to disguise text from a "
        "human reviewer while a mail client still displays it."
    )
    remediation = "Strip control and zero-width characters from the affected fields."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, ControlCharacterOptions)
        affected = []
        for lead in ctx.leads:
            values = [getattr(lead, f) for f in (*_CHECKED_TEXT_FIELDS, "email", "personalization")]
            values.extend(lead.custom_variables.values())
            if any(has_control_characters(v) for v in values if v):
                affected.append(lead)
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="contain control, zero-width, or bidirectional characters",
            max_samples=self.limit(config),
            pass_summary="No control or bidirectional characters found.",
        )


@dataclass(frozen=True)
class FormulaInjectionOptions(RatioOptions):
    warning_ratio: float = 0.0
    blocker_ratio: float = 1.01
    """Never a blocker by default: the risk lands on whoever opens an export."""


@register
class FormulaInjection(_LeadRule):
    rule_id = "contacts.formula_injection"
    title = "Contact fields are free of spreadsheet formula injection"
    category = RuleCategory.CONTACTS
    severity = Severity.MEDIUM
    options_model = FormulaInjectionOptions
    description = (
        "Values beginning with =, +, -, or @ are executed as formulas when a CSV "
        "export is opened in Excel or Sheets. Campaign Preflight neutralizes these "
        "in anything it writes; this rule reports them in your source data."
    )
    remediation = "Prefix the affected values with an apostrophe, or clean them at the source."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, FormulaInjectionOptions)
        affected = []
        for lead in ctx.leads:
            values = [getattr(lead, f) for f in (*_CHECKED_TEXT_FIELDS, "personalization")]
            values.extend(lead.custom_variables.values())
            if any(is_formula_injection(v) for v in values if v):
                affected.append(lead)
        return _ratio_result(
            self,
            options,
            affected,
            len(ctx.leads),
            what="contain a value a spreadsheet would execute as a formula",
            max_samples=self.limit(config),
            pass_summary="No spreadsheet formula-injection risks found.",
            blocker_severity=Severity.MEDIUM,
        )
