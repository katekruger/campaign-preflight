"""End-to-end CLI behaviour, including the documented exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from campaign_preflight.cli import run
from campaign_preflight.errors import ExitCode
from campaign_preflight.reporting import load_schema


class Result:
    """What one CLI invocation produced: exit code plus captured streams."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


WARNING_CONFIG = """\
version: 1
settings:
  target_timezone: America/Phoenix
rules:
  campaign.daily_volume:
    warning_above: 50
    blocker_above: 250
"""


def invoke(*args: str) -> Result:
    """Run the CLI in-process and capture both streams and the exit code."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = run(list(args))
        except SystemExit as exc:  # argparse exits directly on a usage error
            code = int(exc.code or 0)
    return Result(code, out.getvalue(), err.getvalue())


def example_args(examples_dir: Path, name: str) -> list[str]:
    directory = examples_dir / name
    args = [
        "check",
        "--campaign",
        str(directory / "campaign.yaml"),
        "--leads",
        str(directory / "leads.csv"),
    ]
    for flag, filename in (
        ("--suppressions", "suppressions.csv"),
        ("--evidence", "evidence.json"),
        ("--config", "config.yaml"),
    ):
        if (directory / filename).is_file():
            args += [flag, str(directory / filename)]
    return args


class TestVersionAndRules:
    def test_version(self) -> None:
        result = invoke("version")
        assert result.exit_code == 0
        assert "campaign-preflight" in result.stdout
        assert "report schema" in result.stdout

    def test_rules_list(self) -> None:
        result = invoke("rules", "list")
        assert result.exit_code == 0
        assert "campaign.daily_volume" in result.stdout

    def test_rules_list_json(self) -> None:
        result = invoke("rules", "list", "--json")
        payload = json.loads(result.stdout)
        assert len(payload) >= 75
        assert {"rule_id", "severity", "category"} <= set(payload[0])

    def test_rules_list_by_category(self) -> None:
        result = invoke("rules", "list", "--category", "senders")
        assert "senders." in result.stdout
        assert "campaign.daily_volume" not in result.stdout

    def test_rules_explain(self) -> None:
        result = invoke("rules", "explain", "campaign.daily_volume")
        assert result.exit_code == 0
        assert "warning_above" in result.stdout

    def test_rules_explain_unknown_id_suggests_a_correction(self) -> None:
        result = invoke("rules", "explain", "campaign.daily_volum")
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "Did you mean" in result.stderr
        assert "campaign.daily_volume" in result.stderr

    def test_no_args_shows_usage(self) -> None:
        result = invoke()
        combined = result.stdout + result.stderr
        assert "usage:" in combined.lower()
        assert "demo" in combined


class TestDemo:
    def test_demo_runs_and_does_not_fail_the_shell(self) -> None:
        result = invoke("demo")
        assert result.exit_code == ExitCode.READY, "demo defaults to --fail-on none"
        assert "CAMPAIGN PREFLIGHT" in result.stdout

    def test_demo_json_validates(self) -> None:
        result = invoke("demo", "--format", "json")
        jsonschema.validate(json.loads(result.stdout), load_schema())

    def test_demo_markdown(self) -> None:
        assert invoke("demo", "--format", "markdown").stdout.startswith("# Campaign Preflight")

    def test_demo_quiet(self) -> None:
        result = invoke("demo", "--quiet")
        assert "BLOCKERS" not in result.stdout
        assert "Readiness:" in result.stdout

    def test_demo_writes_to_a_file(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        result = invoke("demo", "--format", "json", "--output", str(target))
        assert result.exit_code == ExitCode.READY
        jsonschema.validate(json.loads(target.read_text(encoding="utf-8")), load_schema())

    def test_demo_can_be_made_to_fail(self) -> None:
        assert invoke("demo", "--fail-on", "blocker").exit_code == ExitCode.NOT_READY


class TestExitCodes:
    def test_ready_exits_zero(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "clean_campaign"))
        assert result.exit_code == ExitCode.READY

    def test_not_ready_exits_two(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "risky_campaign"))
        assert result.exit_code == ExitCode.NOT_READY

    def test_incomplete_exits_three(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "incomplete_campaign"))
        assert result.exit_code == ExitCode.INCOMPLETE

    def test_ready_with_warnings_exits_one(self, tmp_path: Path, examples_dir: Path) -> None:
        # The clean campaign, with the volume warning threshold lowered below its
        # daily limit of 80. One WARN, no failures.
        config = tmp_path / "config.yaml"
        config.write_text(WARNING_CONFIG, encoding="utf-8")
        directory = examples_dir / "clean_campaign"
        result = invoke(
            "check",
            "--campaign",
            str(directory / "campaign.yaml"),
            "--leads",
            str(directory / "leads.csv"),
            "--suppressions",
            str(directory / "suppressions.csv"),
            "--evidence",
            str(directory / "evidence.json"),
            "--config",
            str(config),
        )
        assert result.exit_code == ExitCode.READY_WITH_WARNINGS

    def test_bad_input_exits_four(self, tmp_path: Path) -> None:
        result = invoke(
            "check",
            "--campaign",
            str(tmp_path / "nope.yaml"),
            "--leads",
            str(tmp_path / "nope.csv"),
        )
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_bad_config_exits_four(self, tmp_path: Path, examples_dir: Path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("version: 1\nrules:\n  not.a_rule: {}\n", encoding="utf-8")
        result = invoke(*example_args(examples_dir, "clean_campaign"), "--config", str(config))
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "unknown rule id" in result.stderr

    def test_missing_credentials_exits_five(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
        result = invoke("instantly", "--campaign-id", "x")
        assert result.exit_code == ExitCode.PROVIDER_ERROR
        assert "INSTANTLY_API_KEY" in result.stderr


class TestFailOn:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            ("none", ExitCode.READY),
            ("blocker", ExitCode.NOT_READY),
            ("high", ExitCode.NOT_READY),
            ("warning", ExitCode.NOT_READY),
        ],
    )
    def test_thresholds_on_a_risky_campaign(
        self, examples_dir: Path, level: str, expected: ExitCode
    ) -> None:
        result = invoke(*example_args(examples_dir, "risky_campaign"), "--fail-on", level)
        assert result.exit_code == expected

    def test_warnings_only_pass_a_blocker_threshold(
        self, tmp_path: Path, examples_dir: Path
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text(WARNING_CONFIG, encoding="utf-8")
        directory = examples_dir / "clean_campaign"
        base = [
            "check",
            "--campaign",
            str(directory / "campaign.yaml"),
            "--leads",
            str(directory / "leads.csv"),
            "--suppressions",
            str(directory / "suppressions.csv"),
            "--evidence",
            str(directory / "evidence.json"),
            "--config",
            str(config),
        ]
        assert invoke(*base).exit_code == ExitCode.READY_WITH_WARNINGS
        assert invoke(*base, "--fail-on", "blocker").exit_code == ExitCode.READY

    def test_incomplete_is_not_silenced_by_a_severity_threshold(self, examples_dir: Path) -> None:
        """A check that could not run is not a low-severity finding."""
        result = invoke(*example_args(examples_dir, "incomplete_campaign"), "--fail-on", "blocker")
        assert result.exit_code == ExitCode.INCOMPLETE

    def test_fail_on_none_always_exits_zero(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "risky_campaign"), "--fail-on", "none")
        assert result.exit_code == ExitCode.READY


class TestRedaction:
    def test_redacted_by_default(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "risky_campaign"))
        assert "marcus.reyes@" not in result.stdout

    def test_no_redact_is_opt_in_and_announced(self, examples_dir: Path) -> None:
        result = invoke(*example_args(examples_dir, "risky_campaign"), "--no-redact")
        assert "marcus.reyes@stonebridge.example.com" in result.stdout
        assert "UNREDACTED" in result.stdout

    def test_no_command_accepts_an_api_key_argument(self) -> None:
        """A key on the command line lands in shell history and CI logs."""
        result = invoke("instantly", "--api-key", "secret", "--campaign-id", "x")
        assert result.exit_code != 0
        assert "unrecognized arguments" in result.stderr.lower()

    def test_no_parser_anywhere_defines_a_credential_flag(self) -> None:
        from campaign_preflight.cli import build_parser

        forbidden = ("--api-key", "--apikey", "--token", "--secret", "--password")
        found: list[str] = []

        def walk(parser) -> None:
            for action in parser._actions:
                found.extend(action.option_strings)
                # Only a subparsers action maps names to parsers; `choices` on a
                # plain option is a tuple of allowed values.
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    for sub in choices.values():
                        if hasattr(sub, "_actions"):
                            walk(sub)

        walk(build_parser())
        assert not [f for f in found if f.lower() in forbidden]


class TestValidateConfig:
    def test_valid_config(self, examples_dir: Path) -> None:
        result = invoke("validate-config", str(examples_dir / "clean_campaign" / "config.yaml"))
        assert result.exit_code == 0
        assert "Valid." in result.stdout

    def test_invalid_config(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("version: 1\nrules:\n  nope.nope: {}\n", encoding="utf-8")
        result = invoke("validate-config", str(path))
        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_external_model_use_is_called_out(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("version: 1\nevidence:\n  evaluator: openai_compatible\n", encoding="utf-8")
        result = invoke("validate-config", str(path))
        assert "external model" in result.stdout


class TestOutputOptions:
    def test_max_samples_is_honoured(self, examples_dir: Path) -> None:
        result = invoke(
            *example_args(examples_dir, "risky_campaign"), "--format", "json", "--max-samples", "1"
        )
        payload = json.loads(result.stdout)
        assert all(len(r["affected_record_samples"]) <= 1 for r in payload["results"])

    def test_affected_csv_export(self, examples_dir: Path, tmp_path: Path) -> None:
        target = tmp_path / "affected.csv"
        invoke(*example_args(examples_dir, "risky_campaign"), "--affected-csv", str(target))
        assert target.is_file()
        assert target.read_text(encoding="utf-8").startswith("rule_id,severity,status")

    def test_markdown_renders_for_every_example(self, examples_dir: Path) -> None:
        for name in ("clean_campaign", "risky_campaign", "incomplete_campaign"):
            result = invoke(*example_args(examples_dir, name), "--format", "markdown")
            assert result.stdout.startswith("# Campaign Preflight")

    def test_json_validates_for_every_example(self, examples_dir: Path) -> None:
        schema = load_schema()
        for name in ("clean_campaign", "risky_campaign", "incomplete_campaign"):
            result = invoke(*example_args(examples_dir, name), "--format", "json")
            jsonschema.validate(json.loads(result.stdout), schema)
