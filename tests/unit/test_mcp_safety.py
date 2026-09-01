"""MCP server safety.

This file is the reason it is safe to point an agent at a live campaign. If any
test here fails, the server can do something it must not be able to do.
"""

from __future__ import annotations

import inspect
import json

import pytest

from campaign_preflight.errors import PreflightError
from campaign_preflight.mcp.protocol import MCPServer
from campaign_preflight.mcp.server import (
    MUTATING_VERBS,
    READ_ONLY_ANNOTATIONS,
    READ_ONLY_PREFIX,
    _assert_read_only,
    build_server,
    list_tool_specs,
    tool_input_schema,
)

EXPECTED_TOOLS = {
    "preflight_demo",
    "preflight_files",
    "preflight_instantly_campaign",
    "list_preflight_rules",
    "explain_preflight_rule",
    "validate_preflight_config",
}


@pytest.fixture(scope="module")
def tools():
    return list_tool_specs(build_server())


def test_exactly_the_approved_tools_are_exposed(tools) -> None:
    """A new tool must be added to EXPECTED_TOOLS deliberately, with review."""
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_no_tool_name_contains_a_mutating_verb(tools) -> None:
    for tool in tools:
        words = set(tool.name.lower().split("_"))
        offending = words & MUTATING_VERBS
        assert not offending, f"{tool.name} contains mutating verb(s): {offending}"


def test_every_tool_declares_itself_read_only(tools) -> None:
    for tool in tools:
        assert (tool.description or "").strip().startswith(READ_ONLY_PREFIX), (
            f"{tool.name} must open its description with '{READ_ONLY_PREFIX}'"
        )


def test_no_tool_accepts_a_credential_argument(tools) -> None:
    """Keys come from the environment. A key in a tool argument is a leak."""
    forbidden = {"api_key", "apikey", "token", "secret", "password", "authorization", "key"}
    for tool in tools:
        properties = tool_input_schema(tool).get("properties", {})
        for name in properties:
            assert name.lower() not in forbidden, f"{tool.name} accepts {name}"


def test_file_arguments_are_explicit_paths_not_directories(tools) -> None:
    """No tool may take a directory to scan, glob, or walk."""
    for tool in tools:
        properties = tool_input_schema(tool).get("properties", {})
        for name in properties:
            lowered = name.lower()
            assert not lowered.endswith(("_dir", "_directory", "_glob", "_pattern")), (
                f"{tool.name} takes {name}, which implies directory traversal"
            )


def test_tool_schemas_are_serializable(tools) -> None:
    for tool in tools:
        json.dumps(tool_input_schema(tool))


def test_server_instructions_state_the_read_only_contract() -> None:
    server = build_server()
    instructions = (server.instructions or "").lower()
    assert "read-only" in instructions
    assert "cannot activate" in instructions


def test_no_module_level_function_performs_a_write() -> None:
    """A structural check: the MCP package must not import a writing client."""
    from campaign_preflight.mcp import server as module

    source = inspect.getsource(module)
    for verb in ("httpx.post", "httpx.patch", "httpx.delete", "requests.post"):
        assert verb not in source.lower()


def _server_with_one_tool(name: str, description: str, annotations: dict) -> MCPServer:
    """A minimal server carrying exactly one tool, for exercising _assert_read_only()."""
    server = MCPServer(name="test", version="0.0.0")
    server.add_tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        handler=lambda: None,
        annotations=annotations,
    )
    return server


class TestAssertReadOnly:
    """_assert_read_only() is the fail-closed startup guard named in AGENTS.md's
    Non-negotiables section: its whole job is to stop a fork from shipping a
    write-shaped tool. The tests above only ever see the correctly-built real
    tool list, so they never exercise the guard's own failure branches -- these
    do, by constructing a server that violates exactly one rule at a time.
    """

    @pytest.mark.parametrize("verb", sorted(MUTATING_VERBS))
    def test_refuses_a_tool_name_containing_each_mutating_verb(self, verb: str) -> None:
        server = _server_with_one_tool(
            f"preflight_{verb}_thing",
            "READ-ONLY. Otherwise compliant.",
            dict(READ_ONLY_ANNOTATIONS),
        )
        with pytest.raises(PreflightError, match="mutating name"):
            _assert_read_only(server)

    @pytest.mark.parametrize("verb", sorted(MUTATING_VERBS))
    def test_refuses_regardless_of_case(self, verb: str) -> None:
        server = _server_with_one_tool(
            f"PREFLIGHT_{verb.upper()}_THING",
            "READ-ONLY. Otherwise compliant.",
            dict(READ_ONLY_ANNOTATIONS),
        )
        with pytest.raises(PreflightError, match="mutating name"):
            _assert_read_only(server)

    def test_does_not_flag_a_verb_as_a_substring_of_an_innocent_word(self) -> None:
        """The matcher splits a tool name on "_" and checks whole words against
        MUTATING_VERBS, not substrings. "preflight_senders" must not be refused
        just because "senders" contains "send" -- a substring match would be
        the over-broad failure direction, and that direction blocks legitimate
        work rather than catching a real defect.
        """
        server = _server_with_one_tool(
            "preflight_senders", "READ-ONLY. Otherwise compliant.", dict(READ_ONLY_ANNOTATIONS)
        )
        _assert_read_only(server)  # must not raise

    def test_refuses_a_description_not_opening_with_read_only(self) -> None:
        server = _server_with_one_tool(
            "preflight_widget", "Compliant in every other way.", dict(READ_ONLY_ANNOTATIONS)
        )
        with pytest.raises(PreflightError, match="does not declare itself READ-ONLY"):
            _assert_read_only(server)

    def test_refuses_a_tool_missing_the_read_only_hint(self) -> None:
        annotations = dict(READ_ONLY_ANNOTATIONS)
        del annotations["readOnlyHint"]
        server = _server_with_one_tool(
            "preflight_widget", "READ-ONLY. Otherwise compliant.", annotations
        )
        with pytest.raises(PreflightError, match="not annotated readOnlyHint"):
            _assert_read_only(server)

    def test_refuses_a_tool_whose_read_only_hint_is_false(self) -> None:
        annotations = dict(READ_ONLY_ANNOTATIONS)
        annotations["readOnlyHint"] = False
        server = _server_with_one_tool(
            "preflight_widget", "READ-ONLY. Otherwise compliant.", annotations
        )
        with pytest.raises(PreflightError, match="not annotated readOnlyHint"):
            _assert_read_only(server)

    def test_a_fully_compliant_server_starts(self) -> None:
        server = _server_with_one_tool(
            "preflight_widget", "READ-ONLY. Otherwise compliant.", dict(READ_ONLY_ANNOTATIONS)
        )
        _assert_read_only(server)  # must not raise


class TestToolBehaviour:
    def test_demo_tool_returns_a_readiness_report(self) -> None:
        server = build_server()
        result = server.call_tool("preflight_demo", {})
        payload = _payload(result)
        assert payload["readiness"] in {"READY", "READY_WITH_WARNINGS", "NOT_READY", "INCOMPLETE"}
        assert payload["read_only"] is True
        assert "unknown_checks" in payload
        assert "recommended_remediations" in payload

    def test_demo_tool_warns_against_treating_unknown_as_pass(self) -> None:
        server = build_server()
        payload = _payload(server.call_tool("preflight_demo", {}))
        assert "not a pass" in payload["unknown_checks_note"].lower()

    def test_files_tool_reports_a_missing_file_without_raising(self) -> None:
        server = build_server()
        payload = _payload(
            server.call_tool(
                "preflight_files",
                {"campaign_path": "/nonexistent/c.yaml", "leads_path": "/nonexistent/l.csv"},
            )
        )
        assert payload["ok"] is False
        assert "not a readable file" in payload["error"]

    def test_files_tool_checks_the_bundled_examples(self, examples_dir) -> None:
        server = build_server()
        payload = _payload(
            server.call_tool(
                "preflight_files",
                {
                    "campaign_path": str(examples_dir / "clean_campaign" / "campaign.yaml"),
                    "leads_path": str(examples_dir / "clean_campaign" / "leads.csv"),
                    "suppressions_path": str(examples_dir / "clean_campaign" / "suppressions.csv"),
                    "evidence_path": str(examples_dir / "clean_campaign" / "evidence.json"),
                },
            )
        )
        assert payload["readiness"] == "READY"

    def test_instantly_tool_refuses_without_a_key_in_the_environment(self, monkeypatch) -> None:
        monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
        server = build_server()
        payload = _payload(server.call_tool("preflight_instantly_campaign", {"campaign_id": "x"}))
        assert payload["ok"] is False
        assert "INSTANTLY_API_KEY" in payload["error"]
        assert "cannot be passed as a tool argument" in payload["hint"]

    def test_rule_listing(self) -> None:
        server = build_server()
        payload = _payload(server.call_tool("list_preflight_rules", {}))
        assert payload["count"] >= 75

    def test_rule_listing_rejects_an_unknown_category(self) -> None:
        server = build_server()
        payload = _payload(server.call_tool("list_preflight_rules", {"category": "nope"}))
        assert payload["ok"] is False

    def test_rule_explanation(self) -> None:
        server = build_server()
        payload = _payload(
            server.call_tool("explain_preflight_rule", {"rule_id": "campaign.daily_volume"})
        )
        assert payload["rule_id"] == "campaign.daily_volume"
        assert "warning_above" in payload["options"]

    def test_rule_explanation_suggests_a_correction(self) -> None:
        server = build_server()
        payload = _payload(
            server.call_tool("explain_preflight_rule", {"rule_id": "campaign.daily_volum"})
        )
        assert payload["ok"] is False
        assert "campaign.daily_volume" in payload["hint"]

    def test_config_validation_reports_an_unknown_rule(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("version: 1\nrules:\n  not.a_rule: {}\n", encoding="utf-8")
        server = build_server()
        payload = _payload(
            server.call_tool("validate_preflight_config", {"config_path": str(path)})
        )
        assert payload["valid"] is False
        assert "unknown rule id" in payload["error"]

    def test_config_validation_flags_external_model_use(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("version: 1\nevidence:\n  evaluator: openai_compatible\n", encoding="utf-8")
        server = build_server()
        payload = _payload(
            server.call_tool("validate_preflight_config", {"config_path": str(path)})
        )
        assert payload["sends_data_to_an_external_model"] is True

    def test_reports_are_redacted_by_default(self) -> None:
        server = build_server()
        payload = _payload(server.call_tool("preflight_demo", {}))
        assert payload["redacted"] is True
        assert "marcus.reyes@" not in json.dumps(payload)

    def test_report_id_is_stable_across_runs(self) -> None:
        server = build_server()
        first = _payload(server.call_tool("preflight_demo", {}))
        second = _payload(server.call_tool("preflight_demo", {}))
        assert first["report_id"] == second["report_id"]

    def test_sample_count_is_clamped(self) -> None:
        server = build_server()
        payload = _payload(server.call_tool("preflight_demo", {"max_samples": 10_000}))
        for finding in payload["blockers"] + payload["warnings"]:
            assert len(finding["affected_record_samples"]) <= 25


def _payload(result: dict) -> dict:
    """Unwrap the ``CallToolResult`` a tool call returns.

    The transport wraps a tool's return value in ``structuredContent`` alongside
    a JSON text block; tests want the value itself.
    """
    assert isinstance(result, dict), f"unexpected tool result: {type(result)!r}"
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    content = result.get("content") or []
    if content:
        return json.loads(content[0]["text"])
    raise AssertionError(f"could not unwrap tool result: {result!r}")
