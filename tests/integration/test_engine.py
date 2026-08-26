"""Engine behaviour and the edge cases documented in docs/limitations.md."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.engine import evaluate, gather_context, run_preflight
from campaign_preflight.models import (
    Campaign,
    Capability,
    CapabilityStatus,
    Lead,
    Readiness,
    RuleStatus,
    Sender,
    SuppressionEntry,
)
from campaign_preflight.providers import CSVProvider, FixtureProvider
from campaign_preflight.providers.base import failed, forbidden, misconfigured, ok, unsupported
from helpers import make_campaign, make_lead, make_leads, make_sender

PINNED = datetime(2026, 3, 1, tzinfo=timezone.utc)


def fixture(**kwargs) -> FixtureProvider:
    return FixtureProvider(**kwargs)


class TestGathering:
    async def test_every_capability_is_recorded(self) -> None:
        ctx = await gather_context(FixtureProvider.demo())
        recorded = {r.capability for r in ctx.provider.capabilities}
        assert recorded == set(Capability)

    async def test_provider_errors_are_surfaced(self) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=failed(Capability.LEADS, "the lead endpoint exploded"),
        )
        ctx = await gather_context(provider)
        assert any("exploded" in e for e in ctx.provider.errors)

    async def test_truncated_reads_become_limitations(self) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, list(make_leads(3)), detail="page cap", partial=True),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert any("truncated" in x for x in report.limitations)

    async def test_capability_statuses_survive_into_the_report(self) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, list(make_leads(2))),
            senders=ok(Capability.SENDERS, [make_sender()]),
            suppressions=forbidden(Capability.SUPPRESSIONS, "key lacks block-list scope"),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert any("UNAVAILABLE_PERMISSIONS" in x for x in report.limitations)


class TestEvaluation:
    async def test_one_result_per_rule(self) -> None:
        from campaign_preflight.rules import all_rules

        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        assert len(report.results) == len(all_rules())

    async def test_counts_add_up_to_the_rule_total(self) -> None:
        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        total = (
            report.passed_count
            + report.failure_count
            + report.warning_count
            + report.unknown_count
            + report.not_applicable_count
        )
        assert total == len(report.results)

    async def test_a_raising_rule_becomes_unknown_not_a_crash(self, monkeypatch) -> None:
        """One broken rule must not take down the run."""
        from campaign_preflight.rules import get_rule

        rule = get_rule("campaign.daily_volume")

        def explode(*args, **kwargs):
            raise RuntimeError("deliberate test failure")

        monkeypatch.setattr(rule, "evaluate", explode)
        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        broken = next(r for r in report.results if r.rule_id == "campaign.daily_volume")
        assert broken.status is RuleStatus.UNKNOWN
        assert "RuntimeError" in broken.summary
        assert "bug in Campaign Preflight" in broken.explanation

    async def test_a_raising_rule_never_leaks_a_secret(self, monkeypatch) -> None:
        from campaign_preflight.rules import get_rule

        rule = get_rule("campaign.daily_volume")
        secret = "ZmFrZS1rZXktbm90LXJlYWwtYXQtYWxsLW5vcGU="

        def explode(*args, **kwargs):
            raise RuntimeError(f"failed with api_key={secret}")

        monkeypatch.setattr(rule, "evaluate", explode)
        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        broken = next(r for r in report.results if r.rule_id == "campaign.daily_volume")
        assert secret not in broken.explanation

    async def test_a_severity_override_is_applied(self) -> None:
        config = PreflightConfig(
            rules={"campaign.daily_volume": {"warning_above": 1, "severity": "INFO"}}
        )
        report = await run_preflight(FixtureProvider.demo(), config)
        result = next(r for r in report.results if r.rule_id == "campaign.daily_volume")
        assert result.severity.value == "INFO"


class TestEdgeCases:
    async def test_zero_leads(self) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, []),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert report.readiness is Readiness.NOT_READY
        assert report.lead_count == 0

    async def test_zero_leads_differs_from_a_lead_listing_failure(self) -> None:
        empty = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, []),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        broken = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=failed(Capability.LEADS, "endpoint down"),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        empty_report = await run_preflight(empty, PreflightConfig())
        broken_report = await run_preflight(broken, PreflightConfig())
        empty_result = next(r for r in empty_report.results if r.rule_id == "campaign.has_leads")
        broken_result = next(r for r in broken_report.results if r.rule_id == "campaign.has_leads")
        assert empty_result.status is RuleStatus.FAIL
        assert broken_result.status is RuleStatus.UNKNOWN

    async def test_no_campaign_at_all(self) -> None:
        provider = fixture(campaign=failed(Capability.CAMPAIGN, "404 not found"))
        report = await run_preflight(provider, PreflightConfig())
        assert report.readiness is Readiness.INCOMPLETE
        assert report.campaign_id is None

    async def test_unicode_and_internationalized_addresses(self) -> None:
        leads = [
            make_lead(email="josé@empresa.example.com", id="a", first_name="José"),
            make_lead(email="用户@例子.example.com", id="b", first_name="用户"),
            make_lead(email="user@xn--bcher-kva.example.com", id="c"),
        ]
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, leads),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert report.lead_count == 3

    async def test_shared_inbox_and_catch_all_style_addresses(self) -> None:
        leads = [make_lead(email="info@corp.example.com", id="a", first_name=None, last_name=None)]
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, leads),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        report = await run_preflight(provider, PreflightConfig())
        role = next(r for r in report.results if r.rule_id == "contacts.role_address")
        assert role.status is not RuleStatus.PASS

    @pytest.mark.parametrize("status", ["active", "paused", "completed"])
    async def test_campaign_lifecycle_states(self, status: str) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign(status=status)),
            leads=ok(Capability.LEADS, list(make_leads(3))),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        report = await run_preflight(provider, PreflightConfig())
        result = next(r for r in report.results if r.rule_id == "campaign.status_suitable")
        assert result.status is not RuleStatus.FAIL

    async def test_provider_returning_null_for_required_looking_fields(self) -> None:
        bare = Campaign(id="c", name=None, status=None, schedule=None, steps=(), sender_emails=())
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, bare),
            leads=ok(Capability.LEADS, []),
            senders=ok(Capability.SENDERS, []),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert report.readiness is Readiness.NOT_READY, "no steps, no senders, no leads"

    async def test_suppression_capability_missing_makes_the_run_incomplete(self) -> None:
        provider = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, list(make_leads(3))),
            senders=ok(Capability.SENDERS, [make_sender()]),
            sender_health=ok(Capability.SENDER_HEALTH, [make_sender()]),
            suppressions=misconfigured(Capability.SUPPRESSIONS, "no list supplied"),
        )
        report = await run_preflight(provider, PreflightConfig())
        assert report.readiness is Readiness.INCOMPLETE
        assert "suppression.contact_listed" in report.score_breakdown.critical_unknown_rule_ids

    async def test_a_100_percent_unknown_run_is_never_ready(self) -> None:
        provider = fixture()  # everything unsupported
        report = await run_preflight(provider, PreflightConfig())
        assert report.readiness is not Readiness.READY
        assert report.confidence.value == "LOW"

    async def test_suppression_list_changing_mid_check_is_a_snapshot(self) -> None:
        """Documented behaviour: results describe one moment, not a guarantee."""
        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        assert "point-in-time snapshot" in report.snapshot_note.lower()


class TestDeterminism:
    async def test_identical_input_produces_identical_output(self) -> None:
        first = await run_preflight(FixtureProvider.demo(), PreflightConfig(), now=PINNED)
        second = await run_preflight(FixtureProvider.demo(), PreflightConfig(), now=PINNED)
        assert first.results == second.results
        assert first.score == second.score
        assert first.readiness is second.readiness

    async def test_lead_order_does_not_change_the_verdict(self) -> None:
        leads = list(make_leads(20))
        forward = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, leads),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        backward = fixture(
            campaign=ok(Capability.CAMPAIGN, make_campaign()),
            leads=ok(Capability.LEADS, list(reversed(leads))),
            senders=ok(Capability.SENDERS, [make_sender()]),
        )
        a = await run_preflight(forward, PreflightConfig(), now=PINNED)
        b = await run_preflight(backward, PreflightConfig(), now=PINNED)
        assert a.score == b.score
        assert [r.status for r in a.results] == [r.status for r in b.results]


class TestExamples:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("clean_campaign", Readiness.READY),
            ("risky_campaign", Readiness.NOT_READY),
            ("incomplete_campaign", Readiness.INCOMPLETE),
        ],
    )
    async def test_each_example_demonstrates_its_verdict(
        self, examples_dir: Path, name: str, expected: Readiness
    ) -> None:
        from campaign_preflight.config import load_config

        directory = examples_dir / name
        provider = CSVProvider(
            campaign_path=directory / "campaign.yaml",
            leads_path=directory / "leads.csv",
            suppressions_path=(
                directory / "suppressions.csv"
                if (directory / "suppressions.csv").is_file()
                else None
            ),
            evidence_path=(
                directory / "evidence.json" if (directory / "evidence.json").is_file() else None
            ),
        )
        config_path = directory / "config.yaml"
        config = load_config(config_path) if config_path.is_file() else PreflightConfig()
        report = await run_preflight(provider, config)
        assert report.readiness is expected
