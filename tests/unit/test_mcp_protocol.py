"""The MCP wire protocol.

This is the layer an agent actually talks to. Two properties matter most and are
easy to break: **stdout carries protocol frames and nothing else**, and **a
failing tool becomes a result, never an exception that drops the connection**.
"""

from __future__ import annotations

import io
import json

import pytest

from campaign_preflight.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    MCPServer,
)


def make_server() -> MCPServer:
    server = MCPServer("test-server", "1.2.3", "how to use this server")
    server.add_tool(
        "echo",
        "READ-ONLY. Return what it was given.",
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        lambda value: {"echoed": value},
        annotations={"readOnlyHint": True},
    )
    server.add_tool(
        "with_default",
        "READ-ONLY. Has an optional argument.",
        {"type": "object", "properties": {"count": {"type": "integer"}}},
        lambda count=7: {"count": count},
    )

    def explode() -> dict:
        raise RuntimeError("deliberate failure")

    server.add_tool("explodes", "READ-ONLY. Always raises.", {"type": "object"}, explode)

    async def async_tool() -> dict:
        return {"async": True}

    server.add_tool("asynchronous", "READ-ONLY. A coroutine.", {"type": "object"}, async_tool)
    return server


def drive(server: MCPServer, *messages: dict) -> list[dict]:
    """Feed messages through the stdio loop and collect the responses."""
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


class TestRegistration:
    def test_tools_are_listed_in_registration_order(self) -> None:
        names = [t.name for t in make_server().list_tools()]
        assert names == ["echo", "with_default", "explodes", "asynchronous"]

    def test_duplicate_names_are_refused(self) -> None:
        server = make_server()
        with pytest.raises(ValueError, match="duplicate tool name"):
            server.add_tool("echo", "READ-ONLY. Again.", {}, lambda: None)

    def test_spec_carries_the_schema_and_annotations(self) -> None:
        spec = make_server().list_tools()[0].spec()
        assert spec["name"] == "echo"
        assert spec["inputSchema"]["required"] == ["value"]
        assert spec["annotations"]["readOnlyHint"] is True

    def test_annotations_are_omitted_when_empty(self) -> None:
        spec = make_server().list_tools()[1].spec()
        assert "annotations" not in spec


class TestHandshake:
    def test_initialize_reports_server_identity(self) -> None:
        [response] = drive(
            make_server(),
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        result = response["result"]
        assert result["serverInfo"] == {"name": "test-server", "version": "1.2.3"}
        assert result["instructions"] == "how to use this server"
        assert result["capabilities"]["tools"] == {"listChanged": False}

    def test_initialize_echoes_the_client_protocol_version(self) -> None:
        [response] = drive(
            make_server(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        assert response["result"]["protocolVersion"] == "2024-11-05"

    def test_initialize_defaults_the_protocol_version(self) -> None:
        [response] = drive(
            make_server(), {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert response["result"]["protocolVersion"] == PROTOCOL_VERSION

    def test_notifications_produce_no_response(self) -> None:
        responses = drive(make_server(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert responses == []

    def test_ping(self) -> None:
        [response] = drive(make_server(), {"jsonrpc": "2.0", "id": 9, "method": "ping"})
        assert response == {"jsonrpc": "2.0", "id": 9, "result": {}}


class TestToolCalls:
    def test_successful_call_returns_content_and_structured_output(self) -> None:
        result = make_server().call_tool("echo", {"value": "hi"})
        assert result["isError"] is False
        assert result["structuredContent"] == {"echoed": "hi"}
        assert json.loads(result["content"][0]["text"]) == {"echoed": "hi"}

    def test_optional_arguments_use_their_default(self) -> None:
        assert make_server().call_tool("with_default", {})["structuredContent"] == {"count": 7}

    def test_coroutine_tools_are_awaited(self) -> None:
        assert make_server().call_tool("asynchronous", {})["structuredContent"] == {"async": True}

    def test_unknown_tool_is_a_tool_error_not_a_crash(self) -> None:
        result = make_server().call_tool("nope", {})
        assert result["isError"] is True
        assert "unknown tool" in result["structuredContent"]["error"]

    def test_missing_required_argument_is_reported(self) -> None:
        result = make_server().call_tool("echo", {})
        assert result["isError"] is True
        assert "requires: value" in result["structuredContent"]["error"]

    def test_unexpected_argument_is_reported(self) -> None:
        result = make_server().call_tool("echo", {"value": "x", "surprise": 1})
        assert result["isError"] is True
        assert "does not accept argument(s): surprise" in result["structuredContent"]["error"]

    def test_a_raising_tool_becomes_a_result_not_an_exception(self) -> None:
        """A dropped connection is much worse than an error message."""
        result = make_server().call_tool("explodes", {})
        assert result["isError"] is True
        assert "RuntimeError" in result["structuredContent"]["error"]
        assert "deliberate failure" in result["structuredContent"]["error"]

    def test_a_raising_tool_never_leaks_a_credential(self) -> None:
        server = MCPServer("s", "1")

        def leaky() -> dict:
            raise RuntimeError("failed with api_key=ZmFrZS1rZXktZm9yLXRlc3Rpbmctbm90LXJlYWw=")

        server.add_tool("leaky", "READ-ONLY.", {}, leaky)
        error = server.call_tool("leaky", {})["structuredContent"]["error"]
        assert "ZmFrZS1rZXkt" not in error
        assert "[REDACTED]" in error

    def test_a_tool_returning_ok_false_is_flagged_as_an_error(self) -> None:
        server = MCPServer("s", "1")
        server.add_tool("failing", "READ-ONLY.", {}, lambda: {"ok": False, "error": "nope"})
        assert server.call_tool("failing", {})["isError"] is True

    def test_a_non_dict_return_is_wrapped(self) -> None:
        server = MCPServer("s", "1")
        server.add_tool("listy", "READ-ONLY.", {}, lambda: [1, 2, 3])
        assert server.call_tool("listy", {})["structuredContent"] == {"result": [1, 2, 3]}


class TestDispatchOverTheWire:
    def test_tools_list(self) -> None:
        [response] = drive(make_server(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in response["result"]["tools"]] == [
            "echo",
            "with_default",
            "explodes",
            "asynchronous",
        ]

    def test_tools_call(self) -> None:
        [response] = drive(
            make_server(),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "over the wire"}},
            },
        )
        assert response["result"]["structuredContent"] == {"echoed": "over the wire"}

    def test_tools_call_without_a_name(self) -> None:
        [response] = drive(
            make_server(), {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {}}
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_tools_call_with_non_object_arguments(self) -> None:
        [response] = drive(
            make_server(),
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": ["not", "an", "object"]},
            },
        )
        assert response["error"]["code"] == INVALID_PARAMS

    def test_unknown_method(self) -> None:
        [response] = drive(make_server(), {"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        assert response["error"]["code"] == METHOD_NOT_FOUND
        assert "resources/list" in response["error"]["message"]

    def test_a_full_session(self) -> None:
        responses = drive(
            make_server(),
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "done"}},
            },
        )
        assert [r["id"] for r in responses] == [1, 2, 3], "the notification gets no reply"


class TestMalformedInput:
    def test_invalid_json_produces_a_parse_error(self) -> None:
        stdout = io.StringIO()
        make_server().serve_stdio(stdin=io.StringIO("{not json\n"), stdout=stdout)
        assert json.loads(stdout.getvalue())["error"]["code"] == PARSE_ERROR

    def test_a_json_scalar_is_an_invalid_request(self) -> None:
        stdout = io.StringIO()
        make_server().serve_stdio(stdin=io.StringIO("42\n"), stdout=stdout)
        assert json.loads(stdout.getvalue())["error"]["code"] == INVALID_REQUEST

    def test_blank_lines_are_ignored(self) -> None:
        stdout = io.StringIO()
        make_server().serve_stdio(stdin=io.StringIO("\n\n   \n"), stdout=stdout)
        assert stdout.getvalue() == ""

    def test_the_loop_survives_a_handler_exception(self, monkeypatch) -> None:
        server = make_server()
        monkeypatch.setattr(
            server, "handle_message", lambda m: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        stdout = io.StringIO()
        server.serve_stdio(
            stdin=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), stdout=stdout
        )
        assert json.loads(stdout.getvalue())["error"]["code"] == INTERNAL_ERROR

    def test_a_bad_message_does_not_stop_later_ones(self) -> None:
        stdout = io.StringIO()
        make_server().serve_stdio(
            stdin=io.StringIO('{bad\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'),
            stdout=stdout,
        )
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        assert responses[0]["error"]["code"] == PARSE_ERROR
        assert responses[1]["result"] == {}


class TestFraming:
    def test_every_frame_is_one_line_of_json(self) -> None:
        """stdout carries protocol frames and nothing else; a stray newline breaks a client."""
        stdout = io.StringIO()
        make_server().serve_stdio(
            stdin=io.StringIO(
                '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
            ),
            stdout=stdout,
        )
        lines = stdout.getvalue().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert parsed["jsonrpc"] == "2.0"

    def test_responses_are_valid_json_rpc(self) -> None:
        for response in drive(
            make_server(),
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        ):
            assert response["jsonrpc"] == "2.0"
            assert "id" in response
            assert ("result" in response) != ("error" in response)
