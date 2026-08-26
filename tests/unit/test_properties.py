"""Property-based tests for the invariants that must hold for *any* input.

Hypothesis generates the inputs; these tests assert the properties the design
promises, rather than the behaviour of one hand-picked example.
"""

from __future__ import annotations

from datetime import timezone

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from campaign_preflight.config import PreflightConfig
from campaign_preflight.engine import evaluate
from campaign_preflight.errors import redact_secrets
from campaign_preflight.models import (
    Capability,
    CapabilityStatus,
    Readiness,
    RuleCategory,
    RuleResult,
    RuleStatus,
    Severity,
)
from campaign_preflight.normalization import (
    coerce_bool,
    hash_ref,
    is_formula_injection,
    neutralize_formula,
    normalize_domain,
    normalize_email,
)
from campaign_preflight.reporting.redaction import redact_text
from campaign_preflight.scoring import decide_readiness, score_results
from helpers import make_context, make_lead

SLOW = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

text = st.text(max_size=60)
# Angle brackets and whitespace are structural boundaries in both email
# ("<ana@corp.com>") and HTML, so redaction treats them as token delimiters by
# design. They are excluded here because a local part containing one is not a
# mailbox -- it is two tokens.
local_parts = st.text(
    alphabet=st.characters(
        min_codepoint=33, max_codepoint=126, blacklist_characters="@<>\"' \t"
    ),
    min_size=1,
    max_size=20,
)
domains = st.from_regex(r"\A[a-z]{1,10}\.[a-z]{2,4}\Z", fullmatch=True)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@given(text)
def test_email_normalization_is_idempotent(value: str) -> None:
    once = normalize_email(value)
    assert normalize_email(once) == once


@given(local_parts, domains)
def test_email_normalization_is_case_insensitive(local: str, domain: str) -> None:
    address = f"{local}@{domain}"
    assert normalize_email(address.upper()) == normalize_email(address.lower())


@given(text)
def test_domain_normalization_is_idempotent(value: str) -> None:
    once = normalize_domain(value)
    assert normalize_domain(once) == once


@given(text)
def test_hash_ref_is_opaque_and_fixed_length(value: str) -> None:
    assume(value.strip())
    digest = hash_ref(value)
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)


@given(text)
def test_neutralized_values_are_never_formulas(value: str) -> None:
    """Whatever we write into a CSV must be inert in a spreadsheet."""
    assert not is_formula_injection(neutralize_formula(value))


@given(text)
def test_neutralization_only_ever_prefixes(value: str) -> None:
    result = neutralize_formula(value)
    assert result == value or result == f"'{value}"


@given(st.one_of(st.text(max_size=10), st.integers(), st.none(), st.booleans()))
def test_coerce_bool_returns_only_true_false_or_none(value: object) -> None:
    assert coerce_bool(value) in {True, False, None}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@given(text)
def test_redaction_is_idempotent(value: str) -> None:
    once = redact_text(value)
    assert redact_text(once) == once


@given(local_parts, domains)
def test_redaction_removes_the_mailbox_but_keeps_the_domain(local: str, domain: str) -> None:
    """Holds for malformed local parts too, not only well-formed mailboxes."""
    assume(len(local) > 2)
    assume("*" not in local)
    masked = redact_text(f"{local}@{domain}")
    assert local not in masked
    assert domain in masked


@given(st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_", min_size=24, max_size=64))
def test_base64_shaped_keys_are_always_scrubbed(body: str) -> None:
    assert body not in redact_secrets(f"{body}==")


@given(text)
def test_scrubbing_never_lengthens_a_string_unboundedly(value: str) -> None:
    assert len(redact_secrets(value)) <= len(value) + 12 * (len(value) + 1)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

statuses = st.sampled_from(list(RuleStatus))
severities = st.sampled_from(list(Severity))


@st.composite
def rule_results(draw: st.DrawFn) -> RuleResult:
    return RuleResult(
        rule_id=f"campaign.rule_{draw(st.integers(0, 40))}",
        rule_version="1.0.0",
        title="t",
        category=RuleCategory.CAMPAIGN,
        severity=draw(severities),
        status=draw(statuses),
        summary="s",
    )


result_lists = st.lists(rule_results(), max_size=25)


@given(result_lists)
def test_score_is_always_in_range(results: list[RuleResult]) -> None:
    assert 0 <= score_results(results, PreflightConfig()).final_score <= 100


@given(result_lists)
def test_score_is_order_independent(results: list[RuleResult]) -> None:
    config = PreflightConfig()
    forward = score_results(results, config).final_score
    backward = score_results(list(reversed(results)), config).final_score
    assert forward == backward


@given(result_lists)
def test_a_blocker_failure_always_prevents_ready(results: list[RuleResult]) -> None:
    """The single most important invariant in the product."""
    config = PreflightConfig()
    breakdown = score_results(results, config)
    readiness = decide_readiness(results, breakdown, config)
    has_blocker = any(
        r.status is RuleStatus.FAIL and r.severity is Severity.BLOCKER for r in results
    )
    if has_blocker:
        assert readiness is Readiness.NOT_READY


@given(result_lists)
def test_a_high_score_never_overrides_a_blocker(results: list[RuleResult]) -> None:
    config = PreflightConfig()
    breakdown = score_results(results, config)
    readiness = decide_readiness(results, breakdown, config)
    if breakdown.final_score >= 90 and readiness is Readiness.NOT_READY:
        assert any(
            r.status is RuleStatus.FAIL and r.severity in {Severity.BLOCKER, Severity.HIGH}
            for r in results
        )


@given(result_lists)
def test_unknown_and_not_applicable_never_deduct(results: list[RuleResult]) -> None:
    breakdown = score_results(results, PreflightConfig())
    scored = {d.rule_id for d in breakdown.deductions}
    inert = {
        r.rule_id
        for r in results
        if r.status in {RuleStatus.UNKNOWN, RuleStatus.NOT_APPLICABLE}
    }
    actionable = {
        r.rule_id for r in results if r.status in {RuleStatus.FAIL, RuleStatus.WARN}
    }
    assert not (scored & (inert - actionable))


@given(result_lists)
def test_ready_implies_no_failures_and_no_warnings(results: list[RuleResult]) -> None:
    config = PreflightConfig()
    breakdown = score_results(results, config)
    if decide_readiness(results, breakdown, config) is Readiness.READY:
        assert all(r.status not in {RuleStatus.FAIL, RuleStatus.WARN} for r in results)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

capability_statuses = st.sampled_from(list(CapabilityStatus))


@given(
    st.dictionaries(
        st.sampled_from(list(Capability)), capability_statuses, max_size=len(Capability)
    )
)
@SLOW
def test_engine_always_returns_one_result_per_enabled_rule(
    capabilities: dict[Capability, CapabilityStatus],
) -> None:
    from campaign_preflight.rules import all_rules

    results = evaluate(make_context(capabilities=capabilities), PreflightConfig())
    assert len(results) == len(all_rules())
    assert len({r.rule_id for r in results}) == len(results)


@given(
    st.dictionaries(
        st.sampled_from(list(Capability)), capability_statuses, max_size=len(Capability)
    )
)
@SLOW
def test_a_rule_never_passes_on_a_capability_it_could_not_read(
    capabilities: dict[Capability, CapabilityStatus],
) -> None:
    from campaign_preflight.rules import get_rule

    ctx = make_context(capabilities=capabilities)
    for result in evaluate(ctx, PreflightConfig()):
        rule = get_rule(result.rule_id)
        unreadable = [c for c in rule.requires if not ctx.capability_status(c).is_ok]
        if unreadable:
            assert result.status is RuleStatus.UNKNOWN


@given(st.lists(st.tuples(local_parts, domains), min_size=1, max_size=12))
@SLOW
def test_duplicate_detection_is_case_insensitive(pairs: list[tuple[str, str]]) -> None:
    """Casing must never change whether two contacts are the same contact."""
    from campaign_preflight.rules import get_rule

    config = PreflightConfig()
    rule = get_rule("contacts.duplicate_normalized_email")
    options = config.options_for(rule.rule_id, rule.options_model)

    lower = [make_lead(email=f"{lo}@{do}", id=f"L{i}") for i, (lo, do) in enumerate(pairs)]
    upper = [
        make_lead(email=f"{lo}@{do.upper()}", id=f"L{i}") for i, (lo, do) in enumerate(pairs)
    ]
    a = rule.evaluate(make_context(leads=lower), options, config)
    b = rule.evaluate(make_context(leads=upper), options, config)
    assert a.affected_record_count == b.affected_record_count


@given(st.integers(min_value=0, max_value=30))
@SLOW
def test_affected_samples_are_always_bounded(lead_count: int) -> None:
    config = PreflightConfig(settings={"max_samples": 3})
    leads = [make_lead(email="dup@corp.example.com", id=f"L{i}") for i in range(lead_count)]
    for result in evaluate(make_context(leads=leads), config):
        assert len(result.affected_record_samples) <= 3


@given(st.datetimes(timezones=st.just(timezone.utc)))
@SLOW
def test_reports_are_reproducible_for_a_pinned_clock(moment) -> None:
    ctx = make_context(now=moment)
    assert evaluate(ctx, PreflightConfig()) == evaluate(ctx, PreflightConfig())
