"""Report rendering: schema conformance, determinism, and snapshot stability."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.engine import run_preflight
from campaign_preflight.models import REPORT_SCHEMA_VERSION, RuleStatus
from campaign_preflight.providers import FixtureProvider
from campaign_preflight.reporting import (
    load_schema,
    render_json,
    render_markdown,
    render_terminal,
    report_to_dict,
    write_affected_csv,
)

PINNED = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.fixture
async def report():
    return await run_preflight(FixtureProvider.demo(), PreflightConfig(), now=PINNED)


class TestJson:
    def test_validates_against_the_published_schema(self, report) -> None:
        jsonschema.validate(json.loads(render_json(report)), load_schema())

    def test_schema_version_matches_the_model(self, report) -> None:
        assert json.loads(render_json(report))["report_schema_version"] == REPORT_SCHEMA_VERSION

    def test_schema_file_is_itself_a_valid_schema(self) -> None:
        jsonschema.Draft202012Validator.check_schema(load_schema())

    def test_output_is_byte_identical_across_runs(self, report) -> None:
        assert render_json(report) == render_json(report)

    async def test_two_runs_over_the_same_input_agree(self) -> None:
        first = await run_preflight(FixtureProvider.demo(), PreflightConfig(), now=PINNED)
        second = await run_preflight(FixtureProvider.demo(), PreflightConfig(), now=PINNED)
        a, b = report_to_dict(first), report_to_dict(second)
        a.pop("duration_seconds"), b.pop("duration_seconds")
        assert a == b

    def test_results_are_ordered_by_rule_id(self, report) -> None:
        ids = [r["rule_id"] for r in json.loads(render_json(report))["results"]]
        assert ids == sorted(ids)

    def test_samples_can_be_bounded(self, report) -> None:
        payload = json.loads(render_json(report, max_samples=1))
        assert all(len(r["affected_record_samples"]) <= 1 for r in payload["results"])

    def test_provider_is_always_declared_read_only(self, report) -> None:
        assert json.loads(render_json(report))["provider"]["read_only"] is True

    def test_score_breakdown_arithmetic_is_checkable(self, report) -> None:
        breakdown = json.loads(render_json(report))["score_breakdown"]
        total = sum(d["points"] for d in breakdown["deductions"])
        assert breakdown["final_score"] == max(0, round(100 - total))


class TestMarkdown:
    def test_leads_with_the_verdict(self, report) -> None:
        lines = render_markdown(report).splitlines()
        assert lines[0].startswith("# Campaign Preflight:")
        assert "NOT READY" in lines[2]

    def test_sections_appear_in_severity_order(self, report) -> None:
        rendered = render_markdown(report)
        assert rendered.index("## Blockers") < rendered.index("## Warnings")

    def test_unknown_section_is_explicit_about_not_being_a_pass(self, report) -> None:
        rendered = render_markdown(report)
        assert "not** passes" in rendered

    def test_pipes_in_content_cannot_break_a_table(self, report) -> None:
        for line in render_markdown(report).splitlines():
            if line.startswith("| ") and line.endswith(" |"):
                assert line.count("|") - line.count("\\|") >= 2

    def test_score_derivation_is_shown(self, report) -> None:
        assert "## How this score was computed" in render_markdown(report)

    def test_disclaimer_is_present(self, report) -> None:
        rendered = render_markdown(report)
        assert "does not guarantee" in rendered
        assert "never activates a campaign" in rendered

    def test_is_deterministic(self, report) -> None:
        assert render_markdown(report) == render_markdown(report)


class TestTerminal:
    def test_rule_ids_survive_rich_markup(self, report) -> None:
        """Bracketed rule ids would otherwise be swallowed as Rich markup."""
        rendered = render_terminal(report, color=False)
        assert "[campaign.stop_on_reply]" in rendered

    def test_no_ansi_codes_when_colour_is_off(self, report) -> None:
        assert "\x1b[" not in render_terminal(report, color=False)

    def test_quiet_mode_is_the_verdict_only(self, report) -> None:
        rendered = render_terminal(report, color=False, quiet=True)
        assert "BLOCKERS" not in rendered
        assert "Readiness:" in rendered

    def test_verbose_mode_shows_the_score_derivation(self, report) -> None:
        assert "Score derivation:" in render_terminal(report, color=False, verbose=True)

    def test_status_is_carried_by_words_not_only_colour(self, report) -> None:
        """Accessibility: the verdict must survive a monochrome terminal."""
        rendered = render_terminal(report, color=False)
        for word in ("BLOCKERS", "WARNINGS", "UNKNOWN", "Readiness:"):
            assert word in rendered

    def test_snapshot_note_is_always_printed(self, report) -> None:
        assert "Point-in-time snapshot" in render_terminal(report, color=False)

    def test_is_deterministic(self, report) -> None:
        assert render_terminal(report, color=False) == render_terminal(report, color=False)


class TestAffectedCsvExport:
    def test_writes_one_row_per_sample(self, report, tmp_path: Path) -> None:
        path = tmp_path / "affected.csv"
        count = write_affected_csv(report, path)
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == count
        assert set(rows[0]) == {"rule_id", "severity", "status", "affected_record", "remediation"}

    def test_formula_like_values_are_neutralized(self, tmp_path: Path) -> None:
        """We report formula injection; writing it back out would be indefensible."""
        from campaign_preflight.models import (
            PreflightReport,
            Readiness,
            RuleCategory,
            RuleResult,
            ScoreBreakdown,
            Severity,
        )

        malicious = RuleResult(
            rule_id="contacts.formula_injection",
            rule_version="1.0.0",
            title="t",
            category=RuleCategory.CONTACTS,
            severity=Severity.MEDIUM,
            status=RuleStatus.WARN,
            summary="s",
            affected_record_count=1,
            affected_record_samples=('=HYPERLINK("http://attacker.invalid","x")',),
            remediation="=1+1",
        )
        crafted = PreflightReport(
            tool_version="0.0.0",
            generated_at=PINNED,
            provider="fixture",
            readiness=Readiness.READY_WITH_WARNINGS,
            score=90,
            score_breakdown=ScoreBreakdown(),
            confidence=ScoreBreakdown().confidence,
            results=(malicious,),
        )
        path = tmp_path / "affected.csv"
        write_affected_csv(crafted, path)
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            for cell in next(csv.reader([line])):
                assert not cell.startswith(("=", "+", "@")) or cell.startswith("'")


class TestOutputPaths:
    def test_report_files_are_owner_only(self, report, tmp_path: Path) -> None:
        from campaign_preflight.cli import _write_output

        path = tmp_path / "report.json"
        _write_output(render_json(report), path)
        assert path.stat().st_mode & 0o077 == 0, "reports must not be group/world readable"

    def test_unwritable_directory_is_an_input_error(self, report, tmp_path: Path) -> None:
        from campaign_preflight.cli import _write_output
        from campaign_preflight.errors import InputError

        with pytest.raises(InputError, match="output directory does not exist"):
            _write_output("x", tmp_path / "missing" / "report.json")

    def test_no_partial_file_is_left_behind_on_success(self, report, tmp_path: Path) -> None:
        from campaign_preflight.cli import _write_output

        _write_output(render_json(report), tmp_path / "report.json")
        assert list(tmp_path.glob(".*.partial")) == []
