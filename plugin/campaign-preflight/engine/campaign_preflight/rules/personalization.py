"""Personalization rules (checks 34-45).

Two families live here.

*Mechanical* checks -- missing variables, unresolved tokens, wrong company name
merged into the opener -- are deterministic and safe to act on.

*Claim* checks are deliberately conservative. Campaign Preflight will tell you a
claim has no evidence attached, that the evidence is stale, or that a number in
the claim appears nowhere in the evidence. It will NOT tell you a claim is false.
With no evidence supplied at all, these rules return UNKNOWN rather than
accusing your copy of fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass

import re
from collections import Counter, defaultdict
from typing import Any

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
from ..normalization import (
    collapse_whitespace,
    find_unresolved_tokens,
    hash_ref,
    normalize_domain,
)
from .base import Rule, register

__all__: list[str] = []


class _PersonalizationRule(Rule):
    category = RuleCategory.PERSONALIZATION
    requires = (Capability.LEADS,)

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples


def _variable_value(lead: Lead, name: str) -> str | None:
    """Resolve a template variable name against a lead, custom variables included."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    direct = {
        "first_name": lead.first_name,
        "firstname": lead.first_name,
        "last_name": lead.last_name,
        "lastname": lead.last_name,
        "email": lead.email,
        "company_name": lead.company_name,
        "company": lead.company_name,
        "companyname": lead.company_name,
        "company_domain": lead.company_domain,
        "domain": lead.company_domain,
        "website": lead.company_domain,
        "job_title": lead.job_title,
        "title": lead.job_title,
        "country": lead.country,
        "region": lead.region,
        "personalization": lead.personalization,
    }
    if key in direct:
        return direct[key]
    for variable, value in lead.custom_variables.items():
        if variable.strip().lower() == key:
            return value
    return None


# ---------------------------------------------------------------------------
# 34-37: mechanical merge checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredVariableOptions(RuleOptions):
    warning_ratio: float = 0.02
    blocker_ratio: float = 0.20


@register
class MissingRequiredVariable(_PersonalizationRule):
    rule_id = "personalization.missing_required_variable"
    title = "Every contact has the required personalization variables"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    options_model = RequiredVariableOptions
    description = (
        "Checks each contact against settings.required_variables. A contact "
        "missing one will send with an empty merge or a fallback."
    )
    remediation = "Backfill the missing variables, or remove those contacts."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, RequiredVariableOptions)
        required = config.settings.required_variables
        if not required:
            return self.not_applicable(
                "No required variables configured (set settings.required_variables)."
            )
        if not ctx.leads:
            return self.not_applicable("No leads to check.")
        missing_by_variable: Counter[str] = Counter()
        affected: list[Lead] = []
        for lead in ctx.leads:
            absent = [name for name in required if not _variable_value(lead, name)]
            if absent:
                affected.append(lead)
                missing_by_variable.update(absent)
        if not affected:
            return self.passed(
                f"All {len(ctx.leads)} contacts have values for: {', '.join(required)}."
            )
        ratio = len(affected) / len(ctx.leads)
        summary = (
            f"{len(affected)} of {len(ctx.leads)} contacts ({ratio:.1%}) are missing "
            f"a required personalization variable."
        )
        metadata: dict[str, Any] = {
            "by_variable": dict(missing_by_variable),
            "ratio": round(ratio, 4),
            "required": list(required),
        }
        samples = self.sample([lead.label for lead in affected], self.limit(config))
        if ratio >= options.blocker_ratio:
            return self.failed(
                summary,
                affected=len(affected),
                samples=samples,
                metadata=metadata,
            )
        return self.warn(
            summary, affected=len(affected), samples=samples, metadata=metadata
        )


@register
class UnresolvedToken(_PersonalizationRule):
    rule_id = "personalization.unresolved_token"
    title = "Personalization contains no unrendered template tokens"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.BLOCKER
    description = (
        "A literal {{first_name}} sitting in a contact's personalization field "
        "means the merge never ran. Recipients will see the raw token."
    )
    remediation = "Re-run the personalization step, or clear the affected values."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        affected: list[Lead] = []
        tokens: Counter[str] = Counter()
        for lead in ctx.leads:
            values = [lead.personalization, *lead.custom_variables.values()]
            found = [t for value in values if value for t in find_unresolved_tokens(value)]
            if found:
                affected.append(lead)
                tokens.update(found)
        if not affected:
            return self.passed("No unrendered template tokens in contact personalization.")
        return self.failed(
            f"{len(affected)} contact(s) have unrendered template tokens in their "
            f"personalization.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={"tokens": dict(tokens.most_common(10))},
        )


@dataclass(frozen=True)
class EmptyPersonalizationOptions(RuleOptions):
    warning_ratio: float = 0.10
    blocker_ratio: float = 0.50
    required: bool = False
    """When false, a campaign that uses no personalization field is NOT_APPLICABLE."""


@register
class EmptyPersonalization(_PersonalizationRule):
    rule_id = "personalization.empty"
    title = "Personalization values are populated"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.MEDIUM
    options_model = EmptyPersonalizationOptions
    description = (
        "Contacts with a blank personalization field in a campaign whose copy "
        "references one. Those contacts get a visibly truncated email."
    )
    remediation = "Generate personalization for the affected contacts, or add a fallback."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, EmptyPersonalizationOptions)
        if not ctx.leads:
            return self.not_applicable("No leads to check.")
        populated = [lead for lead in ctx.leads if lead.personalization]
        if not populated and not options.required:
            return self.not_applicable(
                "No contact carries a personalization value; this campaign does not "
                "appear to use one."
            )
        affected = [lead for lead in ctx.leads if not lead.personalization]
        if not affected:
            return self.passed(f"All {len(ctx.leads)} contacts have personalization.")
        ratio = len(affected) / len(ctx.leads)
        summary = (
            f"{len(affected)} of {len(ctx.leads)} contacts ({ratio:.1%}) have an "
            f"empty personalization value."
        )
        samples = self.sample([lead.label for lead in affected], self.limit(config))
        metadata = {"ratio": round(ratio, 4), "affected": len(affected)}
        if ratio >= options.blocker_ratio:
            return self.failed(
                summary, affected=len(affected), samples=samples, metadata=metadata
            )
        if ratio >= options.warning_ratio:
            return self.warn(
                summary, affected=len(affected), samples=samples, metadata=metadata
            )
        return self.warn(
            summary,
            severity=Severity.LOW,
            affected=len(affected),
            samples=samples,
            metadata=metadata,
        )


@dataclass(frozen=True)
class DuplicatePersonalizationOptions(RuleOptions):
    warning_ratio: float = 0.20
    blocker_ratio: float = 0.60
    min_group_size: int = 3


@register
class DuplicatePersonalization(_PersonalizationRule):
    rule_id = "personalization.duplicate_across_contacts"
    title = "Personalization is not identical across many contacts"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.MEDIUM
    options_model = DuplicatePersonalizationOptions
    heuristic = True
    description = (
        "HEURISTIC. Identical personalization across a large share of the list "
        "usually means a generation step failed and wrote the same fallback "
        "everywhere. Some campaigns legitimately reuse a line, so this is a "
        "signal, not a fact."
    )
    remediation = "Re-generate personalization for the repeated group."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, DuplicatePersonalizationOptions)
        populated = [lead for lead in ctx.leads if lead.personalization]
        if len(populated) < options.min_group_size:
            return self.not_applicable(
                "Too few contacts carry personalization to assess repetition."
            )
        groups: dict[str, list[Lead]] = defaultdict(list)
        for lead in populated:
            groups[collapse_whitespace(str(lead.personalization)).lower()].append(lead)
        repeated = {
            text: leads
            for text, leads in groups.items()
            if len(leads) >= options.min_group_size
        }
        affected = [lead for leads in repeated.values() for lead in leads]
        if not affected:
            return self.passed(
                f"Personalization is distinct across {len(populated)} contacts."
            )
        ratio = len(affected) / len(populated)
        largest = max(len(v) for v in repeated.values())
        summary = (
            f"{len(affected)} of {len(populated)} personalized contacts ({ratio:.1%}) "
            f"share an identical value; the largest group is {largest} contacts."
        )
        metadata = {
            "ratio": round(ratio, 4),
            "repeated_groups": len(repeated),
            "largest_group": largest,
        }
        samples = self.sample([lead.label for lead in affected], self.limit(config))
        if ratio >= options.blocker_ratio:
            return self.failed(
                summary,
                severity=Severity.HIGH,
                affected=len(affected),
                samples=samples,
                metadata=metadata,
            )
        if ratio >= options.warning_ratio:
            return self.warn(
                summary, affected=len(affected), samples=samples, metadata=metadata
            )
        return self.warn(
            summary,
            severity=Severity.LOW,
            affected=len(affected),
            samples=samples,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# 38-39: cross-field consistency
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"(?i)\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|"
    r"co|co\.|gmbh|ag|sa|s\.a\.|bv|b\.v\.|plc|pty|oy|ab|as|nv|srl|sarl)\b\.?"
)


def _company_tokens(name: str) -> set[str]:
    """Distinctive words in a company name, with legal suffixes removed."""
    stripped = _COMPANY_SUFFIXES.sub(" ", name)
    words = re.findall(r"[\w']+", stripped.lower())
    return {w for w in words if len(w) >= 3}


@register
class CompanyMismatch(_PersonalizationRule):
    rule_id = "personalization.company_mismatch"
    title = "Personalization does not name a different company"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    heuristic = True
    description = (
        "HEURISTIC. Looks for a company name mentioned in the personalization "
        "that shares no distinctive word with the contact's own company. This is "
        "the signature of a shifted row or a mis-joined enrichment table."
    )
    remediation = "Verify the personalization was joined to the correct contact."

    _COMPANY_MENTION = re.compile(
        r"(?:\bat\s+|\bwith\s+|\bfor\s+|\bjoin(?:ing|ed)?\s+)"
        r"([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})"
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        candidates = [
            lead for lead in ctx.leads if lead.personalization and lead.company_name
        ]
        if not candidates:
            return self.not_applicable(
                "No contact has both a company name and a personalization value."
            )
        affected: list[Lead] = []
        examples: list[str] = []
        for lead in candidates:
            own = _company_tokens(str(lead.company_name))
            if not own:
                continue
            text = str(lead.personalization)
            if any(token in text.lower() for token in own):
                continue  # own company is named somewhere: good enough
            mentions = self._COMPANY_MENTION.findall(text)
            foreign = [m for m in mentions if _company_tokens(m) and not (_company_tokens(m) & own)]
            if foreign:
                affected.append(lead)
                if len(examples) < 3:
                    examples.append(f"{lead.label}: names '{foreign[0].strip()}'")
        if not affected:
            return self.passed(
                f"No personalization names a company other than the contact's own "
                f"across {len(candidates)} contacts."
            )
        return self.failed(
            f"{len(affected)} contact(s) have personalization naming a company that "
            f"does not match their own.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            evidence=tuple(examples),
        )


@register
class FirstNameMismatch(_PersonalizationRule):
    rule_id = "personalization.first_name_mismatch"
    title = "Personalization does not greet a different person"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    heuristic = True
    description = (
        "HEURISTIC. Extracts the greeting from the personalization and compares it "
        "with the contact's first name. Only a leading 'Hi X' / 'Hey X' / 'Hello X' "
        "is examined, so an unrelated capitalized word cannot trigger it."
    )
    remediation = "Verify the personalization was joined to the correct contact."

    _GREETING = re.compile(r"^\s*(?:hi|hey|hello|dear)[\s,]+([A-Za-z'\-]{2,})", re.IGNORECASE)

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        candidates = [
            lead for lead in ctx.leads if lead.personalization and lead.first_name
        ]
        if not candidates:
            return self.not_applicable(
                "No contact has both a first name and a personalization value."
            )
        affected: list[Lead] = []
        examples: list[str] = []
        for lead in candidates:
            match = self._GREETING.match(str(lead.personalization))
            if not match:
                continue
            greeted = match.group(1).strip().lower()
            own = str(lead.first_name).strip().lower()
            if greeted == own or greeted.startswith(own) or own.startswith(greeted):
                continue
            affected.append(lead)
            if len(examples) < 3:
                examples.append(f"{lead.label}: greets '{match.group(1)}', name is '{lead.first_name}'")
        if not affected:
            return self.passed(
                f"Every greeting matches the contact's first name across "
                f"{len(candidates)} contacts."
            )
        return self.failed(
            f"{len(affected)} contact(s) are greeted by a name that is not theirs.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            evidence=tuple(examples),
        )


# ---------------------------------------------------------------------------
# 40-43: claims and evidence
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|percent|million|billion|k\b|m\b)?")


def _lead_refs(lead: Lead) -> set[str]:
    """Every identifier evidence might use to point at this lead."""
    refs = set()
    if lead.id:
        refs.add(lead.id.strip().lower())
    if lead.normalized_email:
        refs.add(lead.normalized_email)
        refs.add(hash_ref(lead.normalized_email))
    return refs


class _ClaimRule(_PersonalizationRule):
    """Base for rules that need the evidence capability."""

    requires = (Capability.LEADS, Capability.EVIDENCE)


@register
class UnsupportedClaim(_ClaimRule):
    rule_id = "personalization.unsupported_claim"
    title = "Numeric claims are supported by the supplied evidence"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    description = (
        "For each claim, checks whether every number it states also appears in its "
        "attached evidence. A mismatch means the figure was not read from the "
        "cited source. This is a deterministic string check, not a judgement about "
        "truth: a claim can be true and still fail if the evidence excerpt is thin."
    )
    remediation = "Correct the figure, or attach evidence that contains it."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.claims:
            return self.unknown(
                "No personalization claims were supplied, so factual support "
                "cannot be assessed.",
                explanation=(
                    "Claim checking needs structured claims in the evidence file. "
                    "Without them this tool will not guess whether your copy is "
                    "accurate, and will not accuse it of fabrication."
                ),
            )
        by_id = {e.evidence_id: e for e in ctx.evidence}
        unsupported: list[str] = []
        checked = 0
        for claim in ctx.claims:
            numbers = {
                match.group(1).replace(",", "")
                for match in _NUMBER_RE.finditer(claim.text)
            }
            numbers |= {n.replace(",", "") for n in claim.numeric_values}
            if not numbers:
                continue
            attached = [by_id[e] for e in claim.evidence_ids if e in by_id]
            if not attached:
                continue  # covered by personalization.claim_without_evidence
            checked += 1
            corpus = " ".join(
                f"{e.excerpt} {e.title or ''}".replace(",", "") for e in attached
            )
            missing = sorted(n for n in numbers if n not in corpus)
            if missing:
                unsupported.append(
                    f"{claim.claim_id}: {', '.join(missing)} not found in attached evidence"
                )
        if checked == 0:
            return self.unknown(
                "No claim carried both a number and attached evidence to check it against."
            )
        if not unsupported:
            return self.passed(
                f"All numbers in {checked} evidence-backed claim(s) appear in their evidence."
            )
        return self.failed(
            f"{len(unsupported)} of {checked} evidence-backed claims state a number "
            f"that does not appear in their evidence.",
            affected=len(unsupported),
            samples=self.sample(unsupported, self.limit(config)),
            metadata={"assessment": "DETERMINISTIC", "checked": checked},
        )


@register
class ClaimWithoutEvidence(_ClaimRule):
    rule_id = "personalization.claim_without_evidence"
    title = "Every claim has an evidence reference"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.MEDIUM
    description = (
        "Claims with no evidence id, or with an id that does not resolve to a "
        "supplied evidence record."
    )
    remediation = "Attach a source to each claim, or remove the claim from the copy."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.claims:
            return self.unknown("No personalization claims were supplied.")
        known = {e.evidence_id for e in ctx.evidence}
        orphans: list[str] = []
        dangling: list[str] = []
        for claim in ctx.claims:
            if not claim.evidence_ids:
                orphans.append(f"{claim.claim_id}: no evidence attached")
            elif not any(e in known for e in claim.evidence_ids):
                dangling.append(
                    f"{claim.claim_id}: references unknown evidence "
                    f"{', '.join(claim.evidence_ids)}"
                )
        problems = orphans + dangling
        if not problems:
            return self.passed(f"All {len(ctx.claims)} claims resolve to supplied evidence.")
        return self.warn(
            f"{len(problems)} of {len(ctx.claims)} claims have no usable evidence "
            f"reference.",
            severity=Severity.HIGH if len(problems) == len(ctx.claims) else Severity.MEDIUM,
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
            metadata={"no_evidence": len(orphans), "dangling_reference": len(dangling)},
        )


@register
class StaleEvidence(_ClaimRule):
    rule_id = "personalization.stale_evidence"
    title = "Evidence is recent enough to cite"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.MEDIUM
    description = (
        "Evidence older than evidence.max_age_days, or with no retrieval date at "
        "all. Stale research is how a sequence congratulates someone on a role "
        "they left a year ago."
    )
    remediation = "Re-run enrichment for the affected contacts."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        if not ctx.evidence:
            return self.unknown("No evidence records were supplied.")
        max_age = config.evidence.max_age_days
        stale: list[str] = []
        undated: list[str] = []
        empty: list[str] = []
        for record in ctx.evidence:
            if not record.excerpt.strip():
                empty.append(f"{record.evidence_id}: empty excerpt")
            if record.retrieved_at is None:
                undated.append(f"{record.evidence_id}: no retrieval date")
                continue
            age = (ctx.generated_at - record.retrieved_at).days
            if age > max_age:
                stale.append(f"{record.evidence_id}: {age} days old")
        problems = stale + undated + empty
        if not problems:
            return self.passed(
                f"All {len(ctx.evidence)} evidence records are dated and within "
                f"{max_age} days."
            )
        return self.warn(
            f"{len(problems)} evidence record issue(s): {len(stale)} stale, "
            f"{len(undated)} undated, {len(empty)} with an empty excerpt.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
            metadata={
                "stale": len(stale),
                "undated": len(undated),
                "empty_excerpt": len(empty),
                "max_age_days": max_age,
            },
        )


@register
class EvidenceLeadMismatch(_ClaimRule):
    rule_id = "personalization.evidence_lead_mismatch"
    title = "Evidence is attached to the right contact"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    description = (
        "Evidence whose lead reference matches no contact, or whose company name "
        "conflicts with the referenced contact's company."
    )
    remediation = "Re-join evidence to contacts; the reference key appears to be wrong."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        referenced = [e for e in ctx.evidence if e.lead_ref]
        if not referenced:
            return self.not_applicable("No evidence record carries a lead reference.")
        index: dict[str, Lead] = {}
        for lead in ctx.leads:
            for ref in _lead_refs(lead):
                index[ref] = lead
        problems: list[str] = []
        for record in referenced:
            key = str(record.lead_ref).strip().lower()
            lead = index.get(key)
            if lead is None:
                problems.append(f"{record.evidence_id}: lead_ref matches no contact")
                continue
            if record.company_name and lead.company_name:
                if not (_company_tokens(record.company_name) & _company_tokens(lead.company_name)):
                    problems.append(
                        f"{record.evidence_id}: company '{record.company_name}' does not "
                        f"match contact company '{lead.company_name}'"
                    )
        if not problems:
            return self.passed(
                f"All {len(referenced)} referenced evidence records match a contact."
            )
        return self.failed(
            f"{len(problems)} of {len(referenced)} evidence records are attached to "
            f"the wrong contact or to no contact at all.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@dataclass(frozen=True)
class SensitiveInferenceOptions(RuleOptions):
    extra_terms: tuple[str, ...] = ()


@register
class SensitiveInference(_PersonalizationRule):
    rule_id = "personalization.sensitive_inference"
    title = "Personalization avoids sensitive inferences"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    options_model = SensitiveInferenceOptions
    heuristic = True
    description = (
        "HEURISTIC. Flags personalization that appears to reference health, "
        "religion, ethnicity, sexuality, political affiliation, immigration "
        "status, or financial distress. Term matching only, so it will produce "
        "false positives; it is a prompt to read the copy, not a verdict."
    )
    remediation = "Read the flagged personalization and rewrite it around business context."

    _TERMS = (
        "pregnan", "maternity leave", "cancer", "diagnos", "illness", "disabilit",
        "mental health", "depression", "rehab", "divorce", "bankrupt",
        "foreclosure", "laid off", "layoff", "fired from", "immigration status",
        "visa status", "green card", "religio", "church", "mosque", "synagogue",
        "ethnicit", "race", "sexual orientation", "lgbt", "political party",
        "voted for", "campaign donation",
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, SensitiveInferenceOptions)
        terms = tuple(t.lower() for t in (*self._TERMS, *options.extra_terms))
        populated = [lead for lead in ctx.leads if lead.personalization]
        if not populated:
            return self.not_applicable("No contact carries a personalization value.")
        affected: list[Lead] = []
        hits: Counter[str] = Counter()
        for lead in populated:
            text = str(lead.personalization).lower()
            matched = [t for t in terms if t in text]
            if matched:
                affected.append(lead)
                hits.update(matched)
        if not affected:
            return self.passed(
                f"No sensitive-topic terms found in {len(populated)} personalization values."
            )
        return self.warn(
            f"{len(affected)} personalization value(s) reference a potentially "
            f"sensitive topic and should be read before sending.",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={"terms": dict(hits.most_common(10)), "assessment": "HEURISTIC"},
        )


@dataclass(frozen=True)
class PersonalizationLengthOptions(RuleOptions):
    max_characters: int = 500
    warning_ratio: float = 0.05


@register
class ExcessivePersonalizationLength(_PersonalizationRule):
    rule_id = "personalization.excessive_length"
    title = "Personalization is not excessively long"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.LOW
    options_model = PersonalizationLengthOptions
    description = (
        "An opener several paragraphs long usually means a research summary was "
        "written into the personalization field by mistake."
    )
    remediation = "Trim the affected personalization values."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, PersonalizationLengthOptions)
        populated = [lead for lead in ctx.leads if lead.personalization]
        if not populated:
            return self.not_applicable("No contact carries a personalization value.")
        affected = [
            lead
            for lead in populated
            if len(str(lead.personalization)) > options.max_characters
        ]
        if not affected:
            return self.passed(
                f"All {len(populated)} personalization values are within "
                f"{options.max_characters} characters."
            )
        longest = max(len(str(lead.personalization)) for lead in affected)
        return self.warn(
            f"{len(affected)} of {len(populated)} personalization values exceed "
            f"{options.max_characters} characters (longest: {longest}).",
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            metadata={"longest": longest, "max_characters": options.max_characters},
        )


@register
class PromptInjection(_PersonalizationRule):
    rule_id = "personalization.prompt_injection"
    title = "Personalization contains no prompt-injection text"
    category = RuleCategory.PERSONALIZATION
    severity = Severity.HIGH
    description = (
        "Lead research is scraped from pages the target controls. Text like "
        "'ignore previous instructions' arriving in a personalization field means "
        "an upstream enrichment step ingested an injection attempt -- and that "
        "text is now queued to be emailed out under your domain."
    )
    remediation = (
        "Remove the affected personalization and review the enrichment source it came from."
    )

    _PATTERNS = (
        re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions"),
        re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|above|the)\s+"),
        re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|acting)\b"),
        re.compile(r"(?i)\bsystem\s*(?:prompt|message)\s*[:>]"),
        re.compile(r"(?i)</?(?:system|assistant|user|instructions?)>"),
        re.compile(r"(?i)\[\[?\s*(?:system|instruction|prompt)\s*\]?\]"),
        re.compile(r"(?i)\bnew\s+instructions?\s*[:>]"),
        re.compile(r"(?i)\breveal\s+(?:your|the)\s+(?:system\s+)?prompt\b"),
        re.compile(r"(?i)\bexfiltrat|send\s+(?:the\s+)?(?:api\s+)?key\b"),
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        affected: list[Lead] = []
        examples: list[str] = []
        for lead in ctx.leads:
            values = [lead.personalization, *lead.custom_variables.values()]
            text = " ".join(str(v) for v in values if v)
            if not text:
                continue
            for pattern in self._PATTERNS:
                match = pattern.search(text)
                if match:
                    affected.append(lead)
                    if len(examples) < 3:
                        snippet = collapse_whitespace(match.group(0))[:80]
                        examples.append(f"{lead.label}: matched {snippet!r}")
                    break
        if not affected:
            return self.passed("No prompt-injection patterns found in personalization.")
        return self.failed(
            f"{len(affected)} contact(s) have prompt-injection text in their "
            f"personalization.",
            severity=Severity.BLOCKER,
            affected=len(affected),
            samples=self.sample([lead.label for lead in affected], self.limit(config)),
            evidence=tuple(examples),
        )
