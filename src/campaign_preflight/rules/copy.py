"""Campaign copy rules (checks 46-58).

Scope note: this module does not implement spam-word folklore. "Free", "act
now", and the rest of that list are not evidence of anything, and treating them
as blockers would train users to ignore the tool. What is here is either
structural (an empty subject, a broken link, a token that will render literally)
or an explicitly labelled heuristic.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import ClassVar

from ..config import PreflightConfig, RuleOptions
from ..models import (
    Campaign,
    CampaignStep,
    Capability,
    PreflightContext,
    RuleCategory,
    RuleResult,
    Severity,
)
from ..normalization import (
    collapse_whitespace,
    extract_urls,
    find_template_tokens,
    malformed_urls,
)
from .base import Rule, register

__all__: list[str] = []

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(html: str) -> str:
    """Strip tags so length and phrase checks see what a reader sees."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    return collapse_whitespace(_TAG_RE.sub(" ", text))


def _step_label(step: CampaignStep) -> str:
    if step.variant_index:
        return f"step {step.index + 1} variant {step.variant_index + 1}"
    return f"step {step.index + 1}"


def _enabled_steps(campaign: Campaign) -> list[CampaignStep]:
    return [s for s in campaign.steps if not s.disabled]


class _CopyRule(Rule):
    category = RuleCategory.COPY
    requires: ClassVar[tuple[Capability, ...]] = (Capability.CAMPAIGN,)

    @staticmethod
    def limit(config: PreflightConfig) -> int:
        return config.settings.max_samples

    def steps_or_na(self, ctx: PreflightContext) -> list[CampaignStep] | RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        steps = _enabled_steps(campaign)
        if not steps:
            return self.not_applicable(
                "The campaign has no enabled steps to check (see campaign.has_steps)."
            )
        return steps


@register
class EmptySubject(_CopyRule):
    rule_id = "copy.empty_subject"
    title = "Every step has a subject line"
    category = RuleCategory.COPY
    severity = Severity.BLOCKER
    description = (
        "A blank subject on the first step is a blocker. A blank subject on a "
        "follow-up is normal -- it threads the reply -- so only the first step is "
        "treated as a failure."
    )
    remediation = "Add a subject line to the first step."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        first_index = min(s.index for s in steps)
        blank_first = [s for s in steps if s.index == first_index and not s.subject.strip()]
        blank_later = [s for s in steps if s.index != first_index and not s.subject.strip()]
        if blank_first:
            return self.failed(
                f"{len(blank_first)} first-step variant(s) have no subject line.",
                affected=len(blank_first),
                samples=[_step_label(s) for s in blank_first],
            )
        if blank_later:
            return self.passed(
                f"The first step has a subject; {len(blank_later)} follow-up "
                f"variant(s) intentionally reuse the thread subject.",
                metadata={"threaded_followups": len(blank_later)},
            )
        return self.passed(f"All {len(steps)} step variants have a subject line.")


@register
class EmptyBody(_CopyRule):
    rule_id = "copy.empty_body"
    title = "Every step has a body"
    category = RuleCategory.COPY
    severity = Severity.BLOCKER
    description = "An enabled step with no body sends an empty email."
    remediation = "Add body copy to the affected steps, or disable them."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        empty = [s for s in steps if not _plain_text(s.body).strip()]
        if not empty:
            return self.passed(f"All {len(steps)} step variants have body copy.")
        return self.failed(
            f"{len(empty)} of {len(steps)} step variants have an empty body.",
            affected=len(empty),
            samples=[_step_label(s) for s in empty],
        )


@register
class UnresolvedCopyTokens(_CopyRule):
    rule_id = "copy.unresolved_tokens"
    title = "Copy references only variables your contacts have"
    category = RuleCategory.COPY
    severity = Severity.HIGH
    requires: ClassVar[tuple[Capability, ...]] = (Capability.CAMPAIGN, Capability.LEADS)
    description = (
        "Cross-references every template variable used in the copy against the "
        "fields actually present on your contacts. A variable no contact carries "
        "will render empty or literal for the entire campaign."
    )
    remediation = "Add the missing field to your contacts, or remove the variable from the copy."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        from .personalization import _variable_value

        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        used: set[str] = set()
        for step in steps:
            used.update(find_template_tokens(step.subject))
            used.update(find_template_tokens(step.body))
        if not used:
            return self.passed("The copy uses no template variables.")
        if not ctx.leads:
            return self.unknown("No contacts are available to check the copy's variables against.")
        campaign = ctx.campaign
        assert campaign is not None
        # A campaign-level custom variable has one value for the whole campaign,
        # so it resolves regardless of what any individual contact carries.
        campaign_vars = {
            str(key).strip().lower()
            for key, value in campaign.custom_variables.items()
            if value not in (None, "")
        }
        # Bounded scan: a variable that resolves for none of the first 1,000
        # contacts resolves for none of them in practice, and this keeps the
        # rule linear in step count rather than in list size.
        sample_leads = ctx.leads[:1000]
        never_resolved = sorted(
            name
            for name in used
            if name.strip().lower() not in campaign_vars
            and not any(_variable_value(lead, name) for lead in sample_leads)
        )
        if not never_resolved:
            return self.passed(
                f"All {len(used)} template variable(s) resolve for at least some contacts.",
                metadata={"variables": sorted(used)},
            )
        return self.failed(
            f"{len(never_resolved)} template variable(s) used in the copy resolve "
            f"for no contact: {', '.join(never_resolved)}.",
            affected=len(never_resolved),
            samples=never_resolved[: self.limit(config)],
            metadata={"variables_used": sorted(used), "unresolvable": never_resolved},
        )


@register
class MissingRequiredCopyVariables(_CopyRule):
    rule_id = "copy.missing_required_variables"
    title = "Copy uses the required personalization variables"
    category = RuleCategory.COPY
    severity = Severity.LOW
    description = (
        "Checks that the copy actually references settings.required_variables. "
        "Requiring a field nobody's copy uses is a sign the config drifted."
    )
    remediation = "Use the required variables in the copy, or drop them from the config."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        required = config.settings.required_variables
        if not required:
            return self.not_applicable("No required variables are configured.")
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        used = {
            t.strip().lower()
            for step in steps
            for t in (*find_template_tokens(step.subject), *find_template_tokens(step.body))
        }
        unused = [name for name in required if name.strip().lower() not in used]
        if not unused:
            return self.passed(f"The copy references all {len(required)} required variable(s).")
        return self.warn(
            f"{len(unused)} required variable(s) are never used in the copy: {', '.join(unused)}.",
            affected=len(unused),
            samples=unused,
        )


@register
class MalformedUrls(_CopyRule):
    rule_id = "copy.malformed_urls"
    title = "Links in the copy are well-formed"
    category = RuleCategory.COPY
    severity = Severity.HIGH
    description = (
        "Detects links that are not usable as written. Links containing template "
        "variables are skipped, since they resolve at send time."
    )
    remediation = "Correct the malformed links."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems: list[str] = []
        total = 0
        for step in steps:
            total += len(extract_urls(step.subject)) + len(extract_urls(step.body))
            for url in (*malformed_urls(step.subject), *malformed_urls(step.body)):
                problems.append(f"{_step_label(step)}: {url[:100]}")
        if total == 0:
            return self.not_applicable("The copy contains no links.")
        if not problems:
            return self.passed(f"All {total} link(s) in the copy are well-formed.")
        return self.failed(
            f"{len(problems)} of {total} link(s) in the copy are malformed.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@dataclass(frozen=True)
class CopyLengthOptions(RuleOptions):
    max_body_characters: int = 2000
    max_subject_characters: int = 100


@register
class ExcessiveCopyLength(_CopyRule):
    rule_id = "copy.excessive_length"
    title = "Copy length is within configured limits"
    category = RuleCategory.COPY
    severity = Severity.LOW
    options_model = CopyLengthOptions
    heuristic = True
    description = (
        "HEURISTIC. Long cold emails and long subject lines correlate with lower "
        "reply rates and subject truncation on mobile. Thresholds are yours to set."
    )
    remediation = "Shorten the affected subject lines and bodies."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, CopyLengthOptions)
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems: list[str] = []
        for step in steps:
            body_length = len(_plain_text(step.body))
            if body_length > options.max_body_characters:
                problems.append(
                    f"{_step_label(step)}: body is {body_length} characters "
                    f"(limit {options.max_body_characters})"
                )
            subject_length = len(step.subject.strip())
            if subject_length > options.max_subject_characters:
                problems.append(
                    f"{_step_label(step)}: subject is {subject_length} characters "
                    f"(limit {options.max_subject_characters})"
                )
        if not problems:
            return self.passed(f"All {len(steps)} step variants are within length limits.")
        return self.warn(
            f"{len(problems)} length issue(s) across {len(steps)} step variants.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@dataclass(frozen=True)
class LinkCountOptions(RuleOptions):
    max_links_per_step: int = 3


@register
class ExcessiveLinks(_CopyRule):
    rule_id = "copy.excessive_links"
    title = "Steps do not carry an excessive number of links"
    category = RuleCategory.COPY
    severity = Severity.LOW
    options_model = LinkCountOptions
    heuristic = True
    description = (
        "HEURISTIC. Many links in a cold email reads as bulk mail to both readers "
        "and filters. This is a style signal, not a deliverability measurement."
    )
    remediation = "Reduce the number of links per step."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, LinkCountOptions)
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems = []
        for step in steps:
            count = len(extract_urls(step.subject)) + len(extract_urls(step.body))
            if count > options.max_links_per_step:
                problems.append(f"{_step_label(step)}: {count} links")
        if not problems:
            return self.passed(f"No step carries more than {options.max_links_per_step} links.")
        return self.warn(
            f"{len(problems)} step variant(s) carry more than {options.max_links_per_step} links.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@register
class GenerationArtifacts(_CopyRule):
    rule_id = "copy.generation_artifacts"
    title = "Copy is free of obvious generation artifacts"
    category = RuleCategory.COPY
    severity = Severity.MEDIUM
    heuristic = True
    description = (
        "HEURISTIC. Repeated punctuation, doubled spaces around punctuation, "
        "stray markdown fences, and model preambles such as 'Here is a draft'. "
        "These are artifacts of a generation step that was never reviewed."
    )
    remediation = "Read and clean the affected copy."

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("repeated punctuation", re.compile(r"[!?]{3,}|\.{4,}")),
        ("space before punctuation", re.compile(r"\s+[,.;:!?](?:\s|$)")),
        ("markdown code fence", re.compile(r"```")),
        # MULTILINE: the preamble lands at the top of the *body*, which is the
        # second line of the text this rule scans.
        (
            "model preamble",
            re.compile(
                r"(?im)^\s*(?:sure[,!]|certainly[,!]|here(?:'s| is) (?:a|the|your)\b|"
                r"as an ai\b|i(?:'m| am) (?:an? )?(?:ai|language model)\b)"
            ),
        ),
        ("unrendered markdown link", re.compile(r"\[[^\]]{1,80}\]\((?:https?://)?[^)]{1,200}\)")),
        (
            "bracketed instruction",
            re.compile(r"\[(?:insert|add|your|company|name|topic)[^\]]{0,40}\]", re.I),
        ),
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems: list[str] = []
        kinds: Counter[str] = Counter()
        for step in steps:
            text = f"{step.subject}\n{_plain_text(step.body)}"
            for label, pattern in self._PATTERNS:
                if pattern.search(text):
                    problems.append(f"{_step_label(step)}: {label}")
                    kinds[label] += 1
        if not problems:
            return self.passed(f"No generation artifacts found in {len(steps)} step variants.")
        return self.warn(
            f"{len(problems)} generation artifact(s) found in the copy.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
            metadata={"by_kind": dict(kinds), "assessment": "HEURISTIC"},
        )


@register
class PlaceholderCopy(_CopyRule):
    rule_id = "copy.placeholder_text"
    title = "Copy contains no placeholder text"
    category = RuleCategory.COPY
    severity = Severity.BLOCKER
    description = (
        "TODO markers, lorem ipsum, and example.com links in copy that is about "
        "to go to real inboxes."
    )
    remediation = "Replace the placeholder text before activating."

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("TODO marker", re.compile(r"(?i)\b(?:todo|tbd|fixme|xxx)\b")),
        ("lorem ipsum", re.compile(r"(?i)\blorem ipsum\b")),
        ("example domain", re.compile(r"(?i)\b(?:example|test)\.(?:com|org|net)\b")),
        ("placeholder name", re.compile(r"(?i)\b(?:john doe|jane doe|acme (?:inc|corp)\b)")),
        (
            "angle-bracket placeholder",
            re.compile(r"<(?:your|company|name|insert)[^>]{0,40}>", re.I),
        ),
    )

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems: list[str] = []
        for step in steps:
            # The raw body, not the tag-stripped text: an angle-bracket
            # placeholder such as <your company here> looks like an HTML tag and
            # would be stripped away before it could be found.
            text = f"{step.subject}\n{step.body}"
            for label, pattern in self._PATTERNS:
                match = pattern.search(text)
                if match:
                    problems.append(f"{_step_label(step)}: {label} ({match.group(0)[:40]!r})")
        if not problems:
            return self.passed(f"No placeholder text found in {len(steps)} step variants.")
        return self.failed(
            f"{len(problems)} placeholder(s) remain in the copy.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@register
class ConflictingCopyVariables(_CopyRule):
    rule_id = "copy.conflicting_variables"
    title = "Copy does not mix conflicting company or contact variables"
    category = RuleCategory.COPY
    severity = Severity.MEDIUM
    description = (
        "A single sentence referencing two different company variables (or two "
        "different name variables) renders as a contradiction. Usually a "
        "copy-paste between templates."
    )
    remediation = "Use one company variable and one contact-name variable per sentence."

    _COMPANY_VARS = frozenset({"company", "company_name", "companyname", "account", "organization"})
    _NAME_VARS = frozenset({"first_name", "firstname", "fname", "name", "full_name"})

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        problems: list[str] = []
        for step in steps:
            for field, text in (("subject", step.subject), ("body", _plain_text(step.body))):
                for sentence in re.split(r"(?<=[.!?])\s+", text):
                    tokens = {t.strip().lower() for t in find_template_tokens(sentence)}
                    companies = tokens & self._COMPANY_VARS
                    names = tokens & self._NAME_VARS
                    if len(companies) > 1:
                        problems.append(
                            f"{_step_label(step)} {field}: mixes {', '.join(sorted(companies))}"
                        )
                    if len(names) > 1:
                        problems.append(
                            f"{_step_label(step)} {field}: mixes {', '.join(sorted(names))}"
                        )
        if not problems:
            return self.passed("No conflicting company or contact variables in the copy.")
        return self.warn(
            f"{len(problems)} sentence(s) mix conflicting variables.",
            affected=len(problems),
            samples=self.sample(problems, self.limit(config)),
        )


@dataclass(frozen=True)
class OptOutOptions(RuleOptions):
    require_in_every_step: bool = False
    """When false, opt-out language is required somewhere in the sequence."""


@register
class OptOutLanguage(_CopyRule):
    rule_id = "copy.opt_out_language"
    title = "Copy contains configured opt-out language"
    category = RuleCategory.COPY
    severity = Severity.HIGH
    options_model = OptOutOptions
    description = (
        "Checks for the opt-out phrases configured in settings.opt_out_phrases. "
        "This is a check against YOUR configured policy. It is not a legal "
        "compliance determination and Campaign Preflight does not give legal advice."
    )
    remediation = "Add opt-out language to the copy, or adjust settings.opt_out_phrases."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, OptOutOptions)
        phrases = tuple(p.lower() for p in config.settings.opt_out_phrases if p.strip())
        if not phrases:
            return self.not_applicable(
                "No opt-out phrases configured (set settings.opt_out_phrases)."
            )
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps

        def has_opt_out(step: CampaignStep) -> bool:
            text = f"{step.subject} {_plain_text(step.body)}".lower()
            return any(phrase in text for phrase in phrases)

        if options.require_in_every_step:
            missing = [s for s in steps if not has_opt_out(s)]
            if not missing:
                return self.passed(f"All {len(steps)} step variants contain opt-out language.")
            return self.failed(
                f"{len(missing)} of {len(steps)} step variants contain no opt-out language.",
                affected=len(missing),
                samples=[_step_label(s) for s in missing],
            )
        if any(has_opt_out(s) for s in steps):
            covered = sum(1 for s in steps if has_opt_out(s))
            return self.passed(
                f"Opt-out language appears in {covered} of {len(steps)} step variants."
            )
        return self.failed(
            "No step in the sequence contains any of the configured opt-out phrases.",
            affected=len(steps),
            metadata={"phrases": list(phrases)},
        )


@register
class StopCondition(_CopyRule):
    rule_id = "copy.stop_condition"
    title = "The sequence has a working stop condition"
    category = RuleCategory.COPY
    severity = Severity.BLOCKER
    description = (
        "A multi-step sequence with stop-on-reply disabled will keep emailing "
        "someone after they answer. Duplicates campaign.stop_on_reply from the "
        "copy's point of view, because the consequence is a copy problem: the "
        "follow-up text will not make sense to someone who already replied."
    )
    remediation = "Enable stop-on-reply before activating this sequence."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        campaign = ctx.campaign
        assert campaign is not None
        steps = _enabled_steps(campaign)
        distinct = len({s.index for s in steps})
        if distinct <= 1:
            return self.not_applicable("Single-step campaign: there is no follow-up to stop.")
        if campaign.stop_on_reply is None:
            return self.unknown(
                "The stop-on-reply setting is unavailable, so the sequence's stop "
                "condition cannot be confirmed.",
                metadata={"steps": distinct},
            )
        if campaign.stop_on_reply is False:
            return self.failed(
                f"This {distinct}-step sequence has stop-on-reply disabled: a "
                f"contact who replies will still receive every follow-up.",
                affected=distinct,
                metadata={"steps": distinct},
            )
        return self.passed(
            f"Stop-on-reply is enabled for this {distinct}-step sequence.",
            metadata={"steps": distinct},
        )


@dataclass(frozen=True)
class IdenticalStepsOptions(RuleOptions):
    treat_identical_body_as: str = "warn"
    """``warn`` | ``pass`` -- some sequences intentionally resend the same email."""


@register
class IdenticalSteps(_CopyRule):
    rule_id = "copy.identical_steps"
    title = "Steps are not byte-identical to one another"
    category = RuleCategory.COPY
    severity = Severity.MEDIUM
    options_model = IdenticalStepsOptions
    description = (
        "Follow-ups identical to the first email. Occasionally deliberate (a "
        "'bumping this up' resend), usually a copy-paste that was never edited."
    )
    remediation = "Differentiate the follow-up copy."

    def evaluate(
        self, ctx: PreflightContext, options: RuleOptions, config: PreflightConfig
    ) -> RuleResult:
        assert isinstance(options, IdenticalStepsOptions)
        if options.treat_identical_body_as not in {"warn", "pass"}:
            return self.unknown(
                f"Unsupported option treat_identical_body_as={options.treat_identical_body_as!r}"
            )
        steps = self.steps_or_na(ctx)
        if isinstance(steps, RuleResult):
            return steps
        if len({s.index for s in steps}) <= 1:
            return self.not_applicable("Single-step campaign: nothing to compare.")
        seen: dict[str, list[CampaignStep]] = {}
        for step in steps:
            key = f"{step.subject.strip().lower()}|{_plain_text(step.body).lower()}"
            seen.setdefault(key, []).append(step)
        duplicates = [group for group in seen.values() if len(group) > 1]
        if not duplicates:
            return self.passed(f"All {len(steps)} step variants have distinct copy.")
        labels = [" == ".join(_step_label(s) for s in group) for group in duplicates]
        count = sum(len(group) for group in duplicates)
        if options.treat_identical_body_as == "pass":
            return self.passed(
                f"{count} step variants share identical copy; allowed by configuration.",
                metadata={"groups": labels},
            )
        return self.warn(
            f"{count} step variants across {len(duplicates)} group(s) have identical "
            f"subject and body.",
            affected=count,
            samples=self.sample(labels, self.limit(config)),
        )
