"""A minimal MCP server over stdio, built on the standard library.

MCP's stdio transport is newline-delimited JSON-RPC 2.0. That is small enough to
implement directly, which is what this module does -- so the plugin needs no
third-party SDK and runs on the system ``python3``.

Implemented methods: ``initialize``, ``notifications/initialized``,
``tools/list``, ``tools/call``, and ``ping``. Anything else returns a proper
JSON-RPC "method not found" rather than a crash.

Two invariants this transport maintains, because violating either one breaks a
client in ways that are hard to debug:

* **stdout carries protocol frames and nothing else.** Every diagnostic goes to
  stderr. A stray ``print`` would corrupt the stream.
* **A tool never raises out of the server.** A failing tool becomes a JSON-RPC
  result with ``isError`` set, so the client sees a message rather than a
  dropped connection.
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = ["Tool", "MCPServer", "PROTOCOL_VERSION"]

PROTOCOL_VERSION = "2025-06-18"
"""The MCP revision this server advertises. Clients may negotiate a different one."""

# JSON-RPC error codes, from the specification.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class Tool:
    """One registered tool: its schema and the callable behind it."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]
    annotations: Dict[str, Any] = field(default_factory=dict)

    def spec(self) -> Dict[str, Any]:
        """The wire representation sent in a ``tools/list`` response."""
        payload: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.annotations:
            payload["annotations"] = self.annotations
        return payload


class MCPServer:
    """A tiny MCP server. Register tools, then :meth:`serve_stdio`."""

    def __init__(self, name: str, version: str, instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: Dict[str, Tool] = {}

    # -- registration ------------------------------------------------------

    def add_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[..., Any],
        *,
        annotations: Optional[Dict[str, Any]] = None,
    ) -> Tool:
        if name in self._tools:
            raise ValueError(f"duplicate tool name: {name}")
        tool = Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            annotations=dict(annotations or {}),
        )
        self._tools[name] = tool
        return tool

    def list_tools(self) -> List[Tool]:
        """Registered tools, in registration order."""
        return list(self._tools.values())

    # -- dispatch ----------------------------------------------------------

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke a tool and return an MCP ``CallToolResult`` payload."""
        tool = self._tools.get(name)
        if tool is None:
            return _tool_error(f"unknown tool: {name}")

        supplied = dict(arguments or {})
        signature = inspect.signature(tool.handler)
        accepted = set(signature.parameters)
        unexpected = sorted(set(supplied) - accepted)
        if unexpected:
            return _tool_error(
                f"{name} does not accept argument(s): {', '.join(unexpected)}"
            )
        missing = [
            parameter
            for parameter, spec in signature.parameters.items()
            if spec.default is inspect.Parameter.empty and parameter not in supplied
        ]
        if missing:
            return _tool_error(f"{name} requires: {', '.join(missing)}")

        try:
            result = tool.handler(**supplied)
            if inspect.isawaitable(result):
                import asyncio

                result = asyncio.run(result)
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the server
            from ..errors import redact_secrets

            return _tool_error(
                f"{name} failed: {type(exc).__name__}: {redact_secrets(str(exc))[:400]}"
            )

        text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result if isinstance(result, dict) else {"result": result},
            "isError": bool(isinstance(result, dict) and result.get("ok") is False),
        }

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC message. Returns a response, or None for a notification."""
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and expect no response.
        if message_id is None:
            return None

        if method == "initialize":
            return _result(
                message_id,
                {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": self.instructions,
                },
            )
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": [t.spec() for t in self.list_tools()]})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return _error(message_id, INVALID_PARAMS, "tools/call requires a tool name")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(message_id, INVALID_PARAMS, "arguments must be an object")
            return _result(message_id, self.call_tool(name, arguments))

        return _error(message_id, METHOD_NOT_FOUND, f"method not found: {method}")

    # -- transport ---------------------------------------------------------

    def serve_stdio(self, stdin: Any = None, stdout: Any = None) -> None:
        """Read newline-delimited JSON-RPC from stdin and write responses to stdout."""
        source = stdin if stdin is not None else sys.stdin
        sink = stdout if stdout is not None else sys.stdout

        for line in source:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                _write(sink, _error(None, PARSE_ERROR, "invalid JSON"))
                continue
            if not isinstance(message, dict):
                _write(sink, _error(None, INVALID_REQUEST, "expected a JSON-RPC object"))
                continue
            try:
                response = self.handle_message(message)
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                from ..errors import redact_secrets

                response = _error(
                    message.get("id"),
                    INTERNAL_ERROR,
                    f"{type(exc).__name__}: {redact_secrets(str(exc))[:200]}",
                )
            if response is not None:
                _write(sink, response)


def _write(sink: Any, payload: Dict[str, Any]) -> None:
    """Write one protocol frame. stdout carries frames and nothing else."""
    sink.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sink.flush()


def _result(message_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> Dict[str, Any]:
    """A tool-level failure: a successful JSON-RPC result carrying isError."""
    payload = {"ok": False, "error": message}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "structuredContent": payload,
        "isError": True,
    }
