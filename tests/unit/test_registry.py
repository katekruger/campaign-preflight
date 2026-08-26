"""Registry-wide invariants that must hold for every rule, present and future."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig, RuleOptions
from campaign_preflight.models import (
    Capability,
    CapabilityStatus,
    RuleCategory,
    RuleResult,
    RuleStatus,
    Severity,
)
from campaign_preflight.rules import all_rules, get_rule, known_rule_ids
from helpers import make_context, run_rule

ALL = all_rules()
RULE_IDS = [r.rule_id for r in ALL]


def test_registry_is_populated() -> None:
    assert len(ALL) >= 75, "the documented rule catalogue should be registered"


def test_rule_ids_are_unique_and_sorted() -> None:
    assert len(RULE_IDS) == len(set(RULE_IDS))
    assert sorted(RULE_IDS) == RULE_IDS, "all_rules() must return a stable order"


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_rule_metadata_is_complete(rule) -> None:
    assert rule.rule_id.count(".") == 1, "rule ids are 'category.name'"
    prefix = rule.rule_id.split(".", 1)[0]
    assert prefix == rule.category.value, f"{rule.rule_id} sits in the wrong category"
    assert rule.title and rule.title[0].isupper()
    assert rule.description, f"{rule.rule_id} needs a description for `rules explain`"
    assert isinstance(rule.category, RuleCategory)
    assert isinstance(rule.severity, Severity)
    assert rule.version.count(".") == 2, "versions are semver-shaped"
    assert issubclass(rule.options_model, RuleOptions)


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_heuristic_rules_are_never_blockers(rule) -> None:
    """A judgement call must not be able to block a launch on its own."""
    if rule.heuristic:
        assert rule.severity is not Severity.BLOCKER
        assert "HEURISTIC" in rule.description.upper()


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_rule_returns_a_result_on_a_healthy_context(rule) -> None:
    result = run_rule(rule.rule_id, make_context())
    assert isinstance(result, RuleResult)
    assert result.rule_id == rule.rule_id
    assert result.summary


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_rule_does_not_mutate_the_context(rule) -> None:
    """The context is frozen, so this asserts the frozen-model contract holds."""
    ctx = make_context()
    before = ctx.state_signature()
    run_rule(rule.rule_id, ctx)
    assert ctx.state_signature() == before


@pytest.mark.parametrize(
    "rule", [r for r in ALL if r.requires], ids=[r.rule_id for r in ALL if r.requires]
)
@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.SUPPORTED_FAILED,
        CapabilityStatus.UNSUPPORTED,
        CapabilityStatus.UNAVAILABLE_PERMISSIONS,
        CapabilityStatus.UNAVAILABLE_CONFIG,
    ],
)
def test_missing_required_data_never_passes(rule, status) -> None:
    """The invariant this whole design exists to protect."""
    for capability in rule.requires:
        ctx = make_context(capabilities={capability: status})
        result = run_rule(rule.rule_id, ctx)
        assert result.status is RuleStatus.UNKNOWN, (
            f"{rule.rule_id} returned {result.status} when {capability.value} was "
            f"{status.value}; missing data must never become a pass"
        )


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_rule_is_deterministic(rule) -> None:
    ctx = make_context()
    first = run_rule(rule.rule_id, ctx)
    second = run_rule(rule.rule_id, ctx)
    assert first == second


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_rule_can_be_disabled(rule) -> None:
    from campaign_preflight.engine import evaluate

    config = PreflightConfig(rules={rule.rule_id: {"enabled": False}})
    results = evaluate(make_context(), config)
    assert rule.rule_id not in {r.rule_id for r in results}


@pytest.mark.parametrize("rule", ALL, ids=RULE_IDS)
def test_actionable_results_carry_a_remediation(rule) -> None:
    """A finding a user cannot act on is noise."""
    result = run_rule(rule.rule_id, make_context())
    if result.status in {RuleStatus.FAIL, RuleStatus.WARN}:
        assert result.remediation, f"{rule.rule_id} reported a problem with no remediation"


def test_get_rule_rejects_unknown_ids() -> None:
    with pytest.raises(KeyError, match="unknown rule id"):
        get_rule("nope.not_a_rule")


def test_known_rule_ids_matches_all_rules() -> None:
    assert known_rule_ids() == frozenset(RULE_IDS)


def test_every_capability_is_required_by_at_least_one_rule() -> None:
    """A capability nothing consumes is a provider call nobody needed."""
    required = {c for rule in ALL for c in rule.requires}
    unused = set(Capability) - required - {Capability.ANALYTICS}
    assert not unused, f"capabilities fetched but never used by a rule: {unused}"
