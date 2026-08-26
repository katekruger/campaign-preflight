"""The ``campaign-preflight`` command line interface.

Built on :mod:`argparse` with no third-party dependencies: Campaign Preflight
ships inside a Cowork plugin, where the only guaranteed runtime is the system
``python3`` with nothing installed alongside it.

Two conventions worth knowing before reading the code:

* **Exit codes carry the verdict.** ``0`` ready, ``1`` ready-with-warnings,
  ``2`` not ready, ``3`` incomplete, ``4`` bad input, ``5`` provider failure,
  ``6`` internal error. ``--fail-on`` raises the bar at which a verdict becomes
  a nonzero exit; it never changes the verdict itself.
* **Redaction is on unless you turn it off.** Reports print masked mailboxes by
  default, and API keys are stripped from output whether you asked or not.

Credentials are read from the environment only. There is deliberately no
``--api-key`` flag: a key on the command line lands in shell history, in ``ps``
output, and in CI logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json as json_module
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import PreflightConfig, load_config, option_defaults, safe_resolve
from .engine import run_preflight
from .errors import (
    ConfigurationError,
    ExitCode,
    InputError,
    PreflightError,
    ProviderError,
    redact_secrets,
)
from .models import PreflightReport, Readiness, RuleCategory, RuleStatus, Severity
from .providers import CampaignProvider, CSVProvider, FixtureProvider

__all__ = ["build_parser", "exit_code_for", "main", "run"]

FORMATS = ("terminal", "json", "markdown")
FAIL_ON_LEVELS = ("none", "warning", "high", "blocker")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _stderr(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")


def _error(message: str) -> None:
    if sys.stderr.isatty():
        _stderr(f"\033[31m{message}\033[0m")
    else:
        _stderr(message)


def exit_code_for(report: PreflightReport, fail_on: str) -> ExitCode:
    """Map a report plus a threshold to a process exit code.

    ``--fail-on`` suppresses nonzero exits for findings *below* the threshold.
    ``INCOMPLETE`` is never suppressed except by ``--fail-on none``: it means a
    critical check did not run, which is a different problem from a low-severity
    finding and should not be silenced by a severity filter.
    """
    if fail_on == "none":
        return ExitCode.READY
    if report.readiness is Readiness.INCOMPLETE:
        return ExitCode.INCOMPLETE

    failures = [r for r in report.results if r.status is RuleStatus.FAIL]
    has_blocker = any(r.severity is Severity.BLOCKER for r in failures)
    has_high = any(r.severity is Severity.HIGH for r in failures)

    if fail_on == "blocker":
        return ExitCode.NOT_READY if has_blocker else ExitCode.READY
    if fail_on == "high":
        return ExitCode.NOT_READY if (has_blocker or has_high) else ExitCode.READY

    # fail_on == "warning": the full verdict mapping.
    if report.readiness is Readiness.NOT_READY:
        return ExitCode.NOT_READY
    if report.readiness is Readiness.READY_WITH_WARNINGS:
        return ExitCode.READY_WITH_WARNINGS
    return ExitCode.READY


def _render(
    report: PreflightReport,
    output_format: str,
    *,
    max_samples: int,
    verbose: bool,
    quiet: bool,
    color: bool,
) -> str:
    from .reporting import render_json, render_markdown, render_terminal

    if output_format == "json":
        return render_json(report, max_samples=max_samples)
    if output_format == "markdown":
        return render_markdown(report, max_samples=max_samples, verbose=verbose)
    return render_terminal(
        report, max_samples=max_samples, verbose=verbose, quiet=quiet, color=color
    )


def _write_output(text: str, path: Path) -> None:
    """Write a report to disk with owner-only permissions.

    Reports can contain contact data even when redacted, so they are not
    world-readable. Written to a temporary file in the same directory and then
    renamed, so an interrupted run never leaves a half-written report behind.
    """
    target = safe_resolve(path)
    parent = target.parent
    if not parent.is_dir():
        raise InputError(f"output directory does not exist: {parent}")
    temporary = parent / f".{target.name}.partial"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise InputError(
            f"could not write report to {path}: {exc.strerror}",
            hint="check the directory exists and is writable",
        ) from exc


def _emit(report: PreflightReport, args: argparse.Namespace) -> None:
    text = _render(
        report,
        args.format,
        max_samples=args.max_samples,
        verbose=args.verbose,
        quiet=args.quiet,
        color=args.output is None and sys.stdout.isatty(),
    )
    if args.output is not None:
        _write_output(text, Path(args.output))
        if not args.quiet:
            _stderr(f"Report written to {args.output}")
    else:
        sys.stdout.write(text)

    if getattr(args, "affected_csv", None):
        from .reporting import write_affected_csv

        count = write_affected_csv(
            report,
            Path(args.affected_csv),
            redacted=report.redacted,
            max_samples=args.max_samples,
        )
        if not args.quiet:
            _stderr(f"{count} affected record(s) written to {args.affected_csv}")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _load_config_or_fail(path: str | None) -> PreflightConfig:
    try:
        return load_config(path)
    except (ConfigurationError, InputError) as exc:
        _error(f"Configuration error: {exc}")
        raise SystemExit(int(ExitCode.CONFIG_ERROR)) from exc


async def _run_and_close(
    provider: CampaignProvider,
    config: PreflightConfig,
    *,
    campaign_id: str | None,
    redacted: bool,
    lead_limit: int | None,
) -> PreflightReport:
    try:
        return await run_preflight(
            provider,
            config,
            campaign_id=campaign_id,
            redacted=redacted,
            lead_limit=lead_limit,
        )
    finally:
        await provider.aclose()


def _execute(
    provider: CampaignProvider,
    config: PreflightConfig,
    args: argparse.Namespace,
    *,
    campaign_id: str | None = None,
    lead_limit: int | None = None,
) -> int:
    """Run a preflight, print it, and return the exit code."""
    try:
        report = asyncio.run(
            _run_and_close(
                provider,
                config,
                campaign_id=campaign_id,
                redacted=args.redact,
                lead_limit=lead_limit,
            )
        )
    except ProviderError as exc:
        _error(f"Provider error: {exc}")
        return int(ExitCode.PROVIDER_ERROR)
    except PreflightError as exc:
        _error(f"Error: {exc}")
        return int(exc.exit_code)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _stderr("Interrupted.")
        return int(ExitCode.INTERNAL_ERROR)

    _emit(report, args)
    return int(exit_code_for(report, args.fail_on))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the bundled synthetic campaign. No credentials, no network."""
    config = _load_config_or_fail(args.config)
    return _execute(FixtureProvider.demo(), config, args)


def cmd_check(args: argparse.Namespace) -> int:
    """Check a campaign described by local files."""
    config = _load_config_or_fail(args.config)
    provider = CSVProvider(
        campaign_path=args.campaign,
        leads_path=args.leads,
        senders_path=args.senders,
        suppressions_path=args.suppressions,
        evidence_path=args.evidence,
    )
    try:
        provider.validate_required_inputs()
    except InputError as exc:
        _error(f"Input error: {exc}")
        return int(ExitCode.CONFIG_ERROR)
    return _execute(provider, config, args)


def cmd_instantly(args: argparse.Namespace) -> int:
    """Check a live Instantly campaign, read-only."""
    config = _load_config_or_fail(args.config)
    api_key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not api_key:
        _error(
            "INSTANTLY_API_KEY is not set. Export it and retry; the key is never "
            "accepted as a command-line argument."
        )
        return int(ExitCode.PROVIDER_ERROR)

    try:
        from .providers.instantly_provider import InstantlyProvider
    except ImportError as exc:
        _error(
            "The Instantly provider needs the optional 'httpx' package.\n"
            "Install it with:  pip install 'campaign-preflight[instantly]'\n"
            "Everything else -- demo, check, rules, MCP -- works without it."
        )
        _stderr(f"(import error: {redact_secrets(str(exc))})")
        return int(ExitCode.PROVIDER_ERROR)

    return _execute(
        InstantlyProvider.from_env(),
        config,
        args,
        campaign_id=args.campaign_id,
        lead_limit=args.lead_limit,
    )


def cmd_rules_list(args: argparse.Namespace) -> int:
    """List every rule in the catalogue."""
    from .rules import all_rules

    category = RuleCategory(args.category) if args.category else None
    rules = [r for r in all_rules() if category is None or r.category is category]

    if args.json:
        payload: list[dict[str, Any]] = [
            {
                "rule_id": r.rule_id,
                "version": r.version,
                "title": r.title,
                "category": r.category.value,
                "severity": r.severity.value,
                "heuristic": r.heuristic,
                "requires": [c.value for c in r.requires],
            }
            for r in rules
        ]
        print(json_module.dumps(payload, indent=2))
        return 0

    current: str | None = None
    for rule in rules:
        if rule.category.value != current:
            current = rule.category.value
            print(f"\n{current.upper()}")
        marker = " [heuristic]" if rule.heuristic else ""
        print(f"  {rule.rule_id:45} {rule.severity.value:8} {rule.title}{marker}")
    print(f"\n{len(rules)} rule(s).")
    return 0


def cmd_rules_explain(args: argparse.Namespace) -> int:
    """Explain one rule: what it checks, what it needs, and how to configure it."""
    import difflib

    from .rules import get_rule, known_rule_ids

    try:
        rule = get_rule(args.rule_id)
    except KeyError:
        close = difflib.get_close_matches(args.rule_id, sorted(known_rule_ids()), n=3, cutoff=0.5)
        _error(f"Unknown rule id: {args.rule_id}")
        if close:
            _stderr(f"Did you mean: {', '.join(close)}?")
        return int(ExitCode.CONFIG_ERROR)

    print(f"{rule.rule_id}  (v{rule.version})")
    print(f"Title:     {rule.title}")
    print(f"Category:  {rule.category.value}")
    print(f"Severity:  {rule.severity.value}")
    print(f"Heuristic: {'yes - a judgement call, not a fact' if rule.heuristic else 'no'}")
    print(f"Requires:  {', '.join(c.value for c in rule.requires) or 'nothing (always runs)'}")
    print()
    print("What it checks:")
    for sentence in (rule.description or "No description supplied.").split(". "):
        if sentence.strip():
            print(f"  {sentence.strip().rstrip('.')}.")
    if rule.remediation:
        print()
        print(f"Remediation: {rule.remediation}")

    options = {
        name: value
        for name, value in option_defaults(rule.options_model).items()
        if name not in {"enabled", "severity"}
    }
    print()
    print("Configuration:")
    print(f"  rules:\n    {rule.rule_id}:\n      enabled: true")
    for name, value in options.items():
        print(f"      {name}: {value!r}")
    if not options:
        print("      # this rule takes no options beyond enabled/severity")
    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    """Validate a rules configuration file and report what it changes."""
    try:
        config = load_config(args.path)
    except (ConfigurationError, InputError) as exc:
        _error(f"Invalid: {exc}")
        return int(ExitCode.CONFIG_ERROR)

    disabled = [
        rule_id
        for rule_id, options in sorted(config.rules.items())
        if options.get("enabled") is False
    ]
    print(f"Valid. {args.path}")
    print(f"  config version:   {config.version}")
    print(f"  rules configured: {len(config.rules)}")
    if disabled:
        print(f"  rules disabled:   {len(disabled)}")
        for rule_id in disabled:
            print(f"    - {rule_id}")
    if config.settings.target_timezone:
        print(f"  target timezone:  {config.settings.target_timezone}")
    if config.evidence.evaluator != "disabled":
        print(
            f"  evidence evaluator: {config.evidence.evaluator} "
            f"(claims may be sent to an external model)"
        )
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print the tool version and the report schema version."""
    from .models import REPORT_SCHEMA_VERSION
    from .rules import all_rules

    print(f"campaign-preflight {__version__}")
    print(f"report schema      {REPORT_SCHEMA_VERSION}")
    print(f"rules registered   {len(all_rules())}")
    print(f"python             {sys.version.split()[0]}")
    print("dependencies       none (standard library only)")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_report_options(parser: argparse.ArgumentParser, *, default_fail_on: str) -> None:
    parser.add_argument(
        "-f", "--format", choices=FORMATS, default="terminal", help="Report format."
    )
    parser.add_argument(
        "-o", "--output", metavar="PATH", help="Write the report here instead of stdout."
    )
    redaction = parser.add_mutually_exclusive_group()
    redaction.add_argument(
        "--redact",
        dest="redact",
        action="store_true",
        default=True,
        help="Mask contact mailboxes in output (default).",
    )
    redaction.add_argument(
        "--no-redact",
        dest="redact",
        action="store_false",
        help="Show full contact addresses. Credentials are still masked.",
    )
    parser.add_argument(
        "--fail-on",
        choices=FAIL_ON_LEVELS,
        default=default_fail_on,
        help=f"Lowest severity producing a nonzero exit (default: {default_fail_on}).",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=5,
        metavar="N",
        help="Affected records shown per finding (0-100).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show explanations.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Print only the verdict.")
    parser.add_argument("-c", "--config", metavar="PATH", help="Rules configuration file.")


def _print_rules_help(args: argparse.Namespace) -> int:
    """`rules` with no subcommand prints its own help."""
    build_parser().parse_args(["rules", "--help"])
    return 0  # pragma: no cover - --help exits first


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign-preflight",
        description=(
            "Read-only preflight checks for outbound email campaigns. "
            "Never writes to a provider and never activates a campaign."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes:\n"
            "  0 ready            3 incomplete (a critical check could not run)\n"
            "  1 ready w/warnings 4 configuration or input error\n"
            "  2 not ready        5 provider or authentication error\n"
            "                     6 unexpected internal error\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    demo = subparsers.add_parser(
        "demo", help="Run the bundled synthetic demo. No credentials, no network."
    )
    _add_report_options(demo, default_fail_on="none")
    demo.set_defaults(handler=cmd_demo)

    check = subparsers.add_parser("check", help="Check a campaign from local files.")
    check.add_argument("--campaign", required=True, metavar="PATH", help="Campaign YAML or JSON.")
    check.add_argument("--leads", required=True, metavar="PATH", help="Leads CSV.")
    check.add_argument("--senders", metavar="PATH", help="Optional sender YAML/JSON.")
    check.add_argument("--suppressions", metavar="PATH", help="Optional suppressions CSV.")
    check.add_argument("--evidence", metavar="PATH", help="Optional evidence JSON bundle.")
    check.add_argument(
        "--affected-csv", metavar="PATH", help="Also write affected records to this CSV."
    )
    _add_report_options(check, default_fail_on="warning")
    check.set_defaults(handler=cmd_check)

    instantly = subparsers.add_parser(
        "instantly", help="Check a live Instantly campaign, read-only."
    )
    instantly.add_argument("--campaign-id", required=True, metavar="ID", help="Campaign UUID.")
    instantly.add_argument(
        "--lead-limit", type=int, default=5000, metavar="N", help="Maximum leads to retrieve."
    )
    instantly.add_argument(
        "--affected-csv", metavar="PATH", help="Also write affected records to this CSV."
    )
    _add_report_options(instantly, default_fail_on="warning")
    instantly.set_defaults(handler=cmd_instantly)

    rules = subparsers.add_parser("rules", help="Inspect the rule catalogue.")
    rules_sub = rules.add_subparsers(dest="rules_command", metavar="SUBCOMMAND")
    rules_list = rules_sub.add_parser("list", help="List every rule.")
    rules_list.add_argument(
        "--category", choices=[c.value for c in RuleCategory], help="Filter by category."
    )
    rules_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    rules_list.set_defaults(handler=cmd_rules_list)
    rules_explain = rules_sub.add_parser("explain", help="Explain one rule.")
    rules_explain.add_argument("rule_id", help="e.g. campaign.daily_volume")
    rules_explain.set_defaults(handler=cmd_rules_explain)
    rules.set_defaults(handler=_print_rules_help)

    validate = subparsers.add_parser("validate-config", help="Validate a rules configuration file.")
    validate.add_argument("path", help="Path to the configuration file.")
    validate.set_defaults(handler=cmd_validate_config)

    version = subparsers.add_parser("version", help="Print version information.")
    version.set_defaults(handler=cmd_version)

    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    if getattr(args, "max_samples", 0) and not 0 <= args.max_samples <= 100:
        _error("--max-samples must be between 0 and 100")
        return int(ExitCode.CONFIG_ERROR)
    return int(args.handler(args))


def main() -> None:
    """Console-script entry point with a last-resort error boundary."""
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except PreflightError as exc:  # pragma: no cover - defence in depth
        _error(f"Error: {exc}")
        raise SystemExit(int(exc.exit_code)) from exc
    except Exception as exc:
        _error(f"Internal error: {type(exc).__name__}: {redact_secrets(str(exc))[:200]}")
        raise SystemExit(int(ExitCode.INTERNAL_ERROR)) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
