"""Behavioural tests for the campaign copy rules."""

from __future__ import annotations

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.models import RuleStatus, Severity
from helpers import make_campaign, make_context, make_step, run_rule


def with_steps(*steps, **campaign_overrides):
    return make_context(campaign=make_campaign(steps=tuple(steps), **campaign_overrides))


class TestEmptyFields:
    def test_blank_first_subject_fails(self) -> None:
        ctx = with_steps(make_step(0, subject="  "))
        result = run_rule("copy.empty_subject", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_blank_followup_subject_is_a_threaded_reply_not_a_bug(self) -> None:
        ctx = with_steps(make_step(0), make_step(1, subject=""))
        result = run_rule("copy.empty_subject", ctx)
        assert result.status is RuleStatus.PASS
        assert result.metadata["threaded_followups"] == 1

    def test_empty_body_fails(self) -> None:
        ctx = with_steps(make_step(0, body="<p>  </p>"))
        assert run_rule("copy.empty_body", ctx).status is RuleStatus.FAIL

    def test_disabled_steps_are_ignored(self) -> None:
        ctx = with_steps(make_step(0), make_step(1, body="", disabled=True))
        assert run_rule("copy.empty_body", ctx).status is RuleStatus.PASS


class TestVariables:
    def test_variable_no_contact_has_fails(self) -> None:
        ctx = with_steps(make_step(0, body="Hi {{invented_field}}"))
        result = run_rule("copy.unresolved_tokens", ctx)
        assert result.status is RuleStatus.FAIL
        assert "invented_field" in result.summary

    def test_campaign_level_custom_variable_resolves(self) -> None:
        ctx = with_steps(
            make_step(0, body="About {{product_name}}"),
            custom_variables={"product_name": "Northwind Analytics"},
        )
        assert run_rule("copy.unresolved_tokens", ctx).status is RuleStatus.PASS

    def test_no_variables_passes(self) -> None:
        ctx = with_steps(make_step(0, subject="Hello", body="Plain text."))
        assert run_rule("copy.unresolved_tokens", ctx).status is RuleStatus.PASS

    def test_unused_required_variable_warns(self) -> None:
        ctx = with_steps(make_step(0, subject="Hello", body="Plain text."))
        result = run_rule("copy.missing_required_variables", ctx)
        assert result.status is RuleStatus.WARN
        assert "first_name" in result.summary

    def test_conflicting_company_variables_warn(self) -> None:
        ctx = with_steps(make_step(0, body="We work with {{company}} and {{company_name}}."))
        assert run_rule("copy.conflicting_variables", ctx).status is RuleStatus.WARN


class TestLinks:
    @pytest.mark.parametrize("url", ["htp:/broken.example", "https://", "http://.example.com"])
    def test_malformed_links_fail(self, url: str) -> None:
        ctx = with_steps(make_step(0, body=f"See {url} for details."))
        assert run_rule("copy.malformed_urls", ctx).status is RuleStatus.FAIL

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/path?q=1",
            "http://sub.example.co.uk/a/b",
            "www.example.com",
            "https://example.com/{{company_domain}}",
        ],
    )
    def test_well_formed_links_pass(self, url: str) -> None:
        ctx = with_steps(make_step(0, body=f"See {url} for details."))
        assert run_rule("copy.malformed_urls", ctx).status is RuleStatus.PASS

    def test_no_links_is_not_applicable(self) -> None:
        ctx = with_steps(make_step(0, body="No links here."))
        assert run_rule("copy.malformed_urls", ctx).status is RuleStatus.NOT_APPLICABLE

    def test_href_links_are_extracted(self) -> None:
        ctx = with_steps(make_step(0, body='<a href="htp:/bad">click</a>'))
        assert run_rule("copy.malformed_urls", ctx).status is RuleStatus.FAIL

    def test_too_many_links_warns(self) -> None:
        body = " ".join(f"https://example.com/{i}" for i in range(6))
        ctx = with_steps(make_step(0, body=body))
        result = run_rule("copy.excessive_links", ctx)
        assert result.status is RuleStatus.WARN
        assert result.heuristic


class TestLength:
    def test_long_body_warns(self) -> None:
        ctx = with_steps(make_step(0, body="word " * 700))
        assert run_rule("copy.excessive_length", ctx).status is RuleStatus.WARN

    def test_long_subject_warns(self) -> None:
        ctx = with_steps(make_step(0, subject="x" * 200))
        assert run_rule("copy.excessive_length", ctx).status is RuleStatus.WARN

    def test_limits_are_configurable(self) -> None:
        ctx = with_steps(make_step(0, subject="x" * 200))
        config = PreflightConfig(rules={"copy.excessive_length": {"max_subject_characters": 500}})
        assert run_rule("copy.excessive_length", ctx, config).status is RuleStatus.PASS


class TestArtifactsAndPlaceholders:
    @pytest.mark.parametrize(
        "text",
        [
            "Great news!!!",
            "```python",
            "Sure! Here is a draft for you.",
            "As an AI language model, I suggest",
            "Check [our site](https://example.com)",
            "Contact [insert name] today",
        ],
    )
    def test_generation_artifacts_warn(self, text: str) -> None:
        ctx = with_steps(make_step(0, body=text))
        result = run_rule("copy.generation_artifacts", ctx)
        assert result.status is RuleStatus.WARN
        assert result.metadata["assessment"] == "HEURISTIC"

    def test_clean_copy_has_no_artifacts(self) -> None:
        assert run_rule("copy.generation_artifacts", make_context()).status is RuleStatus.PASS

    @pytest.mark.parametrize(
        "text",
        [
            "TODO: finish this",
            "Lorem ipsum dolor sit amet",
            "Email us at hello@example.com",
            "Regards, John Doe",
            "<your company here>",
        ],
    )
    def test_placeholder_text_is_a_blocker(self, text: str) -> None:
        ctx = with_steps(make_step(0, body=text))
        result = run_rule("copy.placeholder_text", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_no_spam_word_folklore_is_implemented(self) -> None:
        """"Free" and "act now" are not evidence of anything; we do not flag them."""
        ctx = with_steps(make_step(0, subject="Act now for a free trial", body="Limited offer, buy now."))
        for rule_id in ("copy.placeholder_text", "copy.generation_artifacts"):
            assert run_rule(rule_id, ctx).status is RuleStatus.PASS


class TestOptOutAndStop:
    def test_missing_opt_out_fails(self) -> None:
        ctx = with_steps(make_step(0, body="No way out of this one."))
        result = run_rule("copy.opt_out_language", ctx)
        assert result.status is RuleStatus.FAIL

    def test_opt_out_in_any_step_passes_by_default(self) -> None:
        ctx = with_steps(make_step(0, body="Hello."), make_step(1, body="Reply unsubscribe."))
        assert run_rule("copy.opt_out_language", ctx).status is RuleStatus.PASS

    def test_every_step_can_be_required(self) -> None:
        ctx = with_steps(make_step(0, body="Hello."), make_step(1, body="Reply unsubscribe."))
        config = PreflightConfig(rules={"copy.opt_out_language": {"require_in_every_step": True}})
        assert run_rule("copy.opt_out_language", ctx, config).status is RuleStatus.FAIL

    def test_no_phrases_configured_is_not_applicable(self) -> None:
        config = PreflightConfig(settings={"opt_out_phrases": []})
        assert (
            run_rule("copy.opt_out_language", make_context(), config).status
            is RuleStatus.NOT_APPLICABLE
        )

    def test_single_step_needs_no_stop_condition(self) -> None:
        ctx = with_steps(make_step(0), stop_on_reply=False)
        assert run_rule("copy.stop_condition", ctx).status is RuleStatus.NOT_APPLICABLE

    def test_multi_step_without_stop_on_reply_is_a_blocker(self) -> None:
        ctx = with_steps(make_step(0), make_step(1), stop_on_reply=False)
        result = run_rule("copy.stop_condition", ctx)
        assert result.status is RuleStatus.FAIL
        assert result.severity is Severity.BLOCKER

    def test_unknown_stop_on_reply_is_unknown(self) -> None:
        ctx = with_steps(make_step(0), make_step(1), stop_on_reply=None)
        assert run_rule("copy.stop_condition", ctx).status is RuleStatus.UNKNOWN


class TestIdenticalSteps:
    def test_identical_steps_warn(self) -> None:
        ctx = with_steps(make_step(0), make_step(1))
        result = run_rule("copy.identical_steps", ctx)
        assert result.status is RuleStatus.WARN

    def test_identical_steps_can_be_allowed(self) -> None:
        ctx = with_steps(make_step(0), make_step(1))
        config = PreflightConfig(
            rules={"copy.identical_steps": {"treat_identical_body_as": "pass"}}
        )
        assert run_rule("copy.identical_steps", ctx, config).status is RuleStatus.PASS

    def test_invalid_option_value_is_unknown_not_a_crash(self) -> None:
        ctx = with_steps(make_step(0), make_step(1))
        config = PreflightConfig(
            rules={"copy.identical_steps": {"treat_identical_body_as": "explode"}}
        )
        assert run_rule("copy.identical_steps", ctx, config).status is RuleStatus.UNKNOWN

    def test_distinct_steps_pass(self) -> None:
        ctx = with_steps(make_step(0), make_step(1, subject="Different", body="Different body."))
        assert run_rule("copy.identical_steps", ctx).status is RuleStatus.PASS


def test_all_copy_rules_are_not_applicable_without_steps() -> None:
    ctx = make_context(campaign=make_campaign(steps=()))
    for rule_id in (
        "copy.empty_subject",
        "copy.empty_body",
        "copy.malformed_urls",
        "copy.placeholder_text",
    ):
        assert run_rule(rule_id, ctx).status is RuleStatus.NOT_APPLICABLE
