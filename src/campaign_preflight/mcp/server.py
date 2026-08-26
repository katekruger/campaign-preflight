"""Read-only MCP server over stdio. No third-party dependencies.

Every tool here inspects. None of them changes anything, anywhere -- not in a
campaign, not in a provider, not on disk. That is the point of running this
server rather than giving an agent a provider API key: the agent gets the
answer to "is this campaign safe to launch?" and no ability to launch it.

Enforced properties, each covered by a test in ``tests/unit/test_mcp_safety.py``:

* No tool name contains a mutating verb (activate, launch, send, create,
  update, move, patch, delete, approve).
* Every tool description opens with ``READ-ONLY``.
* Every tool is annotated ``readOnlyHint`` and ``destructiveHint: false``.
* Credentials are read from the environment. No tool takes an API key argument.
* File access is limited to paths the caller passes explicitly. No directory is
  scanned, walked, or globbed.

The server refuses to start if any of the first three is violated.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Final, List, Optional

from .. import __version__
from ..config import PreflightConfig, load_config, option_defaults, safe_resolve
from ..engine import run_preflight
from ..errors import ConfigurationError, InputError, PreflightError, redact_secrets
from ..providers import CSVProvider, FixtureProvider
from .formatting import summarize_report
from .protocol import MCPServer, Tool

__all__ = [
    "build_server",
    "list_tool_specs",
    "tool_input_schema",
    "main",
    "MUTATING_VERBS",
    "READ_ONLY_PREFIX",
]

SERVER_NAME: Final = "campaign-preflight"
READ_ONLY_PREFIX: Final = "READ-ONLY"

# Any of these appearing in a tool name is a defect. Asserted at build time so
# the server refuses to start rather than exposing a write-shaped tool.
MUTATING_VERBS: Final[frozenset] = frozenset(
    {
        "activate", "launch", "send", "create", "update", "move", "patch",
        "delete", "approve", "write", "edit", "add", "remove", "start",
        "pause", "resume", "import", "upload", "set", "modify",
    }
)

MAX_SAMPLES_CEILING: Final = 25

# Applied to every tool. MCP clients surface these to the user, so an agent's
# operator can see the guarantee without reading the source.
READ_ONLY_ANNOTATIONS: Final[Dict[str, Any]] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

INSTRUCTIONS: Final = (
    "Campaign Preflight inspects outbound email campaigns before they are "
    "activated and returns a readiness decision: READY, READY_WITH_WARNINGS, "
    "NOT_READY, or INCOMPLETE.\n\n"
    "Every tool is READ-ONLY. This server cannot activate, pause, edit, "
    "import, or send anything, and exposes no tool that could.\n\n"
    "When reporting results, treat UNKNOWN checks as unanswered questions, "
    "not as passes: a campaign with unknown critical checks is INCOMPLETE, "
    "not safe. Output is redacted by default and mailbox local parts are "
    "masked."
)


def _error(message: str, *, hint: Optional[str] = None) -> Dict[str, Any]:
    """A structured error payload. Never raises out of a tool."""
    payload: Dict[str, Any] = {"ok": False, "error": redact_secrets(message)}
    if hint:
        payload["hint"] = redact_secrets(hint)
    return payload


def _resolve_input(path: str, label: str) -> Path:
    """Resolve one caller-supplied path. Refuses symlinks and missing files."""
    resolved = safe_resolve(path)
    if not resolved.is_file():
        raise InputError(f"{label} is not a readable file: {path}")
    return resolved


def _load_config(config_path: Optional[str]) -> PreflightConfig:
    return load_config(config_path) if config_path else PreflightConfig()


def _clamp_samples(value: Optional[int]) -> int:
    if value is None:
        return 5
    try:
        return max(0, min(MAX_SAMPLES_CEILING, int(value)))
    except (TypeError, ValueError):
        return 5


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def preflight_demo(max_samples: int = 5) -> Dict[str, Any]:
    import asyncio

    try:
        report = asyncio.run(run_preflight(FixtureProvider.demo(), PreflightConfig()))
    except PreflightError as exc:
        return _error(str(exc))
    return summarize_report(report, max_samples=_clamp_samples(max_samples))


def preflight_files(
    campaign_path: str,
    leads_path: str,
    senders_path: Optional[str] = None,
    suppressions_path: Optional[str] = None,
    evidence_path: Optional[str] = None,
    config_path: Optional[str] = None,
    output_format: str = "structured",
    max_samples: int = 5,
) -> Dict[str, Any]:
    import asyncio

    try:
        campaign = _resolve_input(campaign_path, "campaign_path")
        leads = _resolve_input(leads_path, "leads_path")
        senders = _resolve_input(senders_path, "senders_path") if senders_path else None
        suppressions = (
            _resolve_input(suppressions_path, "suppressions_path") if suppressions_path else None
        )
        evidence = _resolve_input(evidence_path, "evidence_path") if evidence_path else None
        config = _load_config(config_path)
    except (InputError, ConfigurationError) as exc:
        return _error(str(exc), hint=exc.hint)

    provider = CSVProvider(
        campaign_path=campaign,
        leads_path=leads,
        senders_path=senders,
        suppressions_path=suppressions,
        evidence_path=evidence,
    )
    try:
        provider.validate_required_inputs()
        report = asyncio.run(run_preflight(provider, config))
    except PreflightError as exc:
        return _error(str(exc), hint=exc.hint)

    return _shape(report, output_format, _clamp_samples(max_samples))


def preflight_instantly_campaign(
    campaign_id: str,
    config_path: Optional[str] = None,
    max_samples: int = 5,
    lead_limit: int = 5000,
) -> Dict[str, Any]:
    import asyncio

    if not os.environ.get("INSTANTLY_API_KEY", "").strip():
        return _error(
            "INSTANTLY_API_KEY is not set in this server's environment",
            hint=(
                "set it in the MCP client's server configuration; it cannot be "
                "passed as a tool argument"
            ),
        )
    try:
        from ..providers.instantly_provider import InstantlyProvider
    except ImportError:
        return _error(
            "the Instantly provider needs the optional 'httpx' package",
            hint="install with: pip install 'campaign-preflight[instantly]'",
        )

    try:
        config = _load_config(config_path)
    except (InputError, ConfigurationError) as exc:
        return _error(str(exc), hint=exc.hint)

    provider = InstantlyProvider.from_env()

    async def go() -> Any:
        try:
            return await run_preflight(
                provider,
                config,
                campaign_id=campaign_id,
                lead_limit=max(1, min(100_000, int(lead_limit))),
            )
        finally:
            await provider.aclose()

    try:
        report = asyncio.run(go())
    except PreflightError as exc:
        return _error(str(exc), hint=exc.hint)
    return summarize_report(report, max_samples=_clamp_samples(max_samples))


def list_preflight_rules(category: Optional[str] = None) -> Dict[str, Any]:
    from ..models import RuleCategory
    from ..rules import all_rules

    selected = None
    if category:
        try:
            selected = RuleCategory(str(category).strip().lower())
        except ValueError:
            return _error(
                f"unknown category {category!r}",
                hint=f"valid: {', '.join(c.value for c in RuleCategory)}",
            )
    rules = [r for r in all_rules() if selected is None or r.category is selected]
    return {
        "count": len(rules),
        "rules": [
            {
                "rule_id": r.rule_id,
                "version": r.version,
                "title": r.title,
                "category": r.category.value,
                "default_severity": r.severity.value,
                "heuristic": r.heuristic,
                "requires": [c.value for c in r.requires],
            }
            for r in rules
        ],
    }


def explain_preflight_rule(rule_id: str) -> Dict[str, Any]:
    import difflib

    from ..rules import get_rule, known_rule_ids

    try:
        rule = get_rule(rule_id)
    except KeyError:
        close = difflib.get_close_matches(rule_id, sorted(known_rule_ids()), n=3, cutoff=0.5)
        return _error(
            f"unknown rule id {rule_id!r}",
            hint=f"did you mean: {', '.join(close)}?" if close else None,
        )
    return {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "title": rule.title,
        "category": rule.category.value,
        "default_severity": rule.severity.value,
        "heuristic": rule.heuristic,
        "heuristic_note": (
            "This rule encodes a judgement call, not a verifiable fact. Report "
            "it as a signal to review, not as a defect."
        )
        if rule.heuristic
        else None,
        "requires": [c.value for c in rule.requires],
        "description": rule.description,
        "remediation": rule.remediation,
        "options": {
            name: repr(value) for name, value in option_defaults(rule.options_model).items()
        },
    }


def validate_preflight_config(config_path: str) -> Dict[str, Any]:
    try:
        _resolve_input(config_path, "config_path")
        config = load_config(config_path)
    except (InputError, ConfigurationError) as exc:
        return {"valid": False, "error": redact_secrets(str(exc)), "hint": exc.hint}
    disabled = sorted(
        rule_id for rule_id, options in config.rules.items() if options.get("enabled") is False
    )
    return {
        "valid": True,
        "config_version": config.version,
        "rules_configured": len(config.rules),
        "rules_disabled": disabled,
        "target_timezone": config.settings.target_timezone,
        "evidence_evaluator": config.evidence.evaluator,
        "sends_data_to_an_external_model": config.evidence.evaluator
        not in {"disabled", "fixture"},
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_PATH = {"type": "string", "description": "Absolute or relative path to an existing file."}
_SAMPLES = {
    "type": "integer",
    "minimum": 0,
    "maximum": MAX_SAMPLES_CEILING,
    "description": "Affected records shown per finding.",
}


def build_server() -> MCPServer:
    """Construct the server and register the six read-only tools."""
    server = MCPServer(SERVER_NAME, __version__, INSTRUCTIONS)

    server.add_tool(
        "preflight_demo",
        "READ-ONLY. Run the bundled synthetic demo campaign and return its readiness "
        "report. Uses synthetic data shipped with the package: no network calls and no "
        "credentials. Useful for showing what a report looks like.",
        _schema({"max_samples": _SAMPLES}),
        preflight_demo,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    server.add_tool(
        "preflight_files",
        "READ-ONLY. Check a campaign described by local files and return its readiness "
        "report. Reads only the paths given here; no directory is scanned or walked. "
        "Optional inputs that are omitted are reported as unavailable capabilities, so a "
        "run without suppressions_path says the suppression checks did not run rather "
        "than implying the list is clean.",
        _schema(
            {
                "campaign_path": _PATH,
                "leads_path": _PATH,
                "senders_path": _PATH,
                "suppressions_path": _PATH,
                "evidence_path": _PATH,
                "config_path": _PATH,
                "output_format": {
                    "type": "string",
                    "enum": ["structured", "terminal", "json", "markdown"],
                    "description": "Shape of the returned report.",
                },
                "max_samples": _SAMPLES,
            },
            required=["campaign_path", "leads_path"],
        ),
        preflight_files,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    server.add_tool(
        "preflight_instantly_campaign",
        "READ-ONLY. Inspect a live Instantly campaign and return its readiness report. "
        "Reads the campaign, its leads, its sending accounts, and the workspace block "
        "list. Every request is checked against a read-only allowlist before it leaves "
        "the process, so no write, activation, or lead-mutation call is reachable. The "
        "API key is read from the INSTANTLY_API_KEY environment variable and is never "
        "accepted as a tool argument.",
        _schema(
            {
                "campaign_id": {"type": "string", "description": "Instantly campaign UUID."},
                "config_path": _PATH,
                "max_samples": _SAMPLES,
                "lead_limit": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            required=["campaign_id"],
        ),
        preflight_instantly_campaign,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    server.add_tool(
        "list_preflight_rules",
        "READ-ONLY. List the preflight rule catalogue, optionally filtered by category: "
        "campaign, contacts, suppression, personalization, copy, schedule, or senders.",
        _schema(
            {
                "category": {
                    "type": "string",
                    "enum": [
                        "campaign", "contacts", "suppression", "personalization",
                        "copy", "schedule", "senders",
                    ],
                }
            }
        ),
        list_preflight_rules,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    server.add_tool(
        "explain_preflight_rule",
        "READ-ONLY. Explain one rule: what it checks, what data it needs, and its "
        "configurable options.",
        _schema(
            {"rule_id": {"type": "string", "description": "e.g. campaign.daily_volume"}},
            required=["rule_id"],
        ),
        explain_preflight_rule,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    server.add_tool(
        "validate_preflight_config",
        "READ-ONLY. Validate a rules configuration file without running any checks. "
        "Reports unknown rule ids, unknown options, and out-of-range values.",
        _schema({"config_path": _PATH}, required=["config_path"]),
        validate_preflight_config,
        annotations=READ_ONLY_ANNOTATIONS,
    )

    _assert_read_only(server)
    return server


def list_tool_specs(server: MCPServer) -> List[Tool]:
    """The server's registered tools."""
    return server.list_tools()


def tool_input_schema(tool: Any) -> Dict[str, Any]:
    """A tool's input JSON Schema, whatever the attribute is called."""
    for attribute in ("input_schema", "inputSchema"):
        schema = getattr(tool, attribute, None)
        if isinstance(schema, dict):
            return schema
    return {}


def _shape(report: Any, output_format: str, max_samples: int) -> Dict[str, Any]:
    """Structured summary, optionally with a rendered document attached."""
    payload = summarize_report(report, max_samples=max_samples)
    fmt = (output_format or "structured").strip().lower()
    if fmt == "structured":
        return payload
    from ..reporting import render_json, render_markdown, render_terminal

    renderers = {
        "json": lambda: render_json(report, max_samples=max_samples),
        "markdown": lambda: render_markdown(report, max_samples=max_samples),
        "terminal": lambda: render_terminal(report, max_samples=max_samples, color=False),
    }
    if fmt not in renderers:
        payload["warning"] = (
            f"unknown output_format {output_format!r}; returned the structured summary"
        )
        return payload
    payload["rendered_format"] = fmt
    payload["rendered"] = renderers[fmt]()
    return payload


def _assert_read_only(server: MCPServer) -> None:
    """Fail closed if a tool ever looks like a write.

    Runs at startup, not only in tests, so a mistake in a fork cannot ship a
    write tool to somebody's Cowork or Claude Desktop.
    """
    for tool in server.list_tools():
        words = set(tool.name.lower().split("_"))
        offending = words & MUTATING_VERBS
        if offending:
            raise PreflightError(
                f"refusing to start: tool {tool.name!r} has a mutating name "
                f"({', '.join(sorted(offending))})",
                hint="Campaign Preflight's MCP server must expose read-only tools only",
            )
        if not (tool.description or "").strip().startswith(READ_ONLY_PREFIX):
            raise PreflightError(
                f"refusing to start: tool {tool.name!r} does not declare itself READ-ONLY",
                hint="every tool description must begin with 'READ-ONLY.'",
            )
        if tool.annotations.get("readOnlyHint") is not True:
            raise PreflightError(
                f"refusing to start: tool {tool.name!r} is not annotated readOnlyHint",
                hint="apply READ_ONLY_ANNOTATIONS to every tool",
            )


def main() -> None:
    """Console-script entry point: serve over stdio."""
    import logging

    # stdio transport owns stdout, so every log line must go to stderr.
    logging.basicConfig(
        level=os.environ.get("CAMPAIGN_PREFLIGHT_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        build_server().serve_stdio()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
