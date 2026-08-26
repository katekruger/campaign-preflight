"""A small, strict YAML-subset parser. No third-party dependencies.

Campaign Preflight ships inside a Cowork plugin, where the only guaranteed
runtime is the system ``python3`` with no packages installed. PyYAML is not
available, so this module parses the subset of YAML that campaign and config
files actually use.

Supported
    * Comments (``#``) and blank lines
    * Block mappings, nested by indentation
    * Block sequences (``- item``), including sequences of mappings
    * Flow sequences (``[a, b]``) and flow mappings (``{a: 1}``)
    * Scalars: strings, integers, floats, booleans, null
    * Single- and double-quoted strings, with escapes in double quotes
    * Block scalars: ``|``, ``|-``, ``|+``, ``>``, ``>-``
    * Documents that are a single mapping or a single sequence

Deliberately unsupported
    Anchors, aliases, tags, multiple documents, complex keys, and merge keys.
    Each raises :class:`YamlError` naming the line, rather than being silently
    ignored -- a config file that half-parses is worse than one that fails.

JSON is a subset of YAML, so ``safe_load`` also reads JSON documents.
"""

from __future__ import annotations

import datetime as _datetime
import json
import re
from typing import Any, List, Optional, Tuple

__all__ = ["YamlError", "safe_load"]


class YamlError(ValueError):
    """A YAML document could not be parsed."""

    def __init__(self, message: str, line_number: Optional[int] = None) -> None:
        self.line_number = line_number
        if line_number is not None:
            message = f"line {line_number}: {message}"
        super().__init__(message)


# Constructs this parser refuses rather than mis-handling.
_UNSUPPORTED = (
    (re.compile(r"^\s*<<\s*:"), "merge keys (<<:) are not supported"),
    (re.compile(r"^\s*---\s*$"), "multiple documents (---) are not supported"),
    (re.compile(r"^\s*\.\.\.\s*$"), "document end markers (...) are not supported"),
    (re.compile(r"(^|\s)[&*][A-Za-z0-9_-]+"), "anchors and aliases are not supported"),
    (re.compile(r"(^|\s)!!?[A-Za-z]"), "tags are not supported"),
)

_BLOCK_SCALAR = re.compile(r"^([|>])([+-]?)$")
_TRUE = frozenset({"true", "yes", "on"})
_FALSE = frozenset({"false", "no", "off"})
_NULL = frozenset({"null", "~", ""})
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
# YAML resolves unquoted ISO timestamps to date/datetime, and campaign files rely
# on that for start_date and end_date. Matching the behaviour here keeps this
# parser interchangeable with PyYAML for every document the tool reads.
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?$"
)


def _parse_timestamp(token: str) -> Any:
    """Resolve an ISO date or datetime, or return None if it is neither."""
    if _DATE.match(token):
        try:
            return _datetime.date(int(token[0:4]), int(token[5:7]), int(token[8:10]))
        except ValueError:
            return None
    if _DATETIME.match(token):
        normalized = token.replace("t", "T").replace("Z", "+00:00").replace("z", "+00:00")
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        try:
            return _datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


class _Line:
    """One significant line: its indent, its content, and its source number."""

    __slots__ = ("indent", "content", "number")

    def __init__(self, indent: int, content: str, number: int) -> None:
        self.indent = indent
        self.content = content
        self.number = number

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Line({self.indent}, {self.content!r}, {self.number})"


def _strip_comment(text: str) -> str:
    """Remove a trailing comment, respecting quotes.

    ``name: "a # b"`` keeps the hash; ``name: a  # b`` does not.
    """
    in_single = in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1] in " \t":
                return text[:index]
    return text


def _scan(source: str) -> List[_Line]:
    """Split into significant lines, rejecting unsupported constructs."""
    lines: List[_Line] = []
    raw_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    inside_block_scalar_until_indent: Optional[int] = None

    for number, raw in enumerate(raw_lines, start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise YamlError("tab used for indentation; use spaces", number)

        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))

        # Lines inside a block scalar are opaque: no comment stripping, no
        # construct checks. They are consumed by the block-scalar reader.
        if inside_block_scalar_until_indent is not None:
            if stripped and indent <= inside_block_scalar_until_indent:
                inside_block_scalar_until_indent = None
            else:
                lines.append(_Line(indent, raw, number))
                continue

        if not stripped or stripped.startswith("#"):
            continue

        content = _strip_comment(raw).rstrip()
        if not content.strip():
            continue

        for pattern, message in _UNSUPPORTED:
            if pattern.search(content):
                raise YamlError(message, number)

        lines.append(_Line(indent, content.strip(), number))

        # A value of "|" or ">" opens a block scalar; mark the following lines.
        value = content.split(":", 1)[-1].strip() if ":" in content else content.strip()
        if _BLOCK_SCALAR.match(value):
            inside_block_scalar_until_indent = indent

    return lines


def _parse_scalar(text: str, line_number: int) -> Any:
    """Turn a scalar token into a Python value."""
    token = text.strip()
    if not token:
        return None
    if token[0] == '"':
        if len(token) < 2 or token[-1] != '"':
            raise YamlError("unterminated double-quoted string", line_number)
        try:
            return json.loads(token)
        except ValueError:
            # JSON is stricter than YAML about escapes; fall back to the raw body.
            return token[1:-1]
    if token[0] == "'":
        if len(token) < 2 or token[-1] != "'":
            raise YamlError("unterminated single-quoted string", line_number)
        return token[1:-1].replace("''", "'")
    if token.startswith("[") or token.startswith("{"):
        return _parse_flow(token, line_number)

    lowered = token.lower()
    if lowered in _NULL:
        return None
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    timestamp = _parse_timestamp(token)
    if timestamp is not None:
        return timestamp
    if _INT.match(token):
        return int(token)
    if _FLOAT.match(token) and not _INT.match(token):
        try:
            return float(token)
        except ValueError:  # pragma: no cover - regex already guarantees this
            return token
    return token


def _split_flow(body: str, line_number: int) -> List[str]:
    """Split a flow collection body on top-level commas."""
    parts: List[str] = []
    depth = 0
    in_single = in_double = False
    current: List[str] = []
    for char in body:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth < 0:
                    raise YamlError("unbalanced brackets in flow collection", line_number)
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    if in_single or in_double:
        raise YamlError("unterminated string in flow collection", line_number)
    if depth != 0:
        raise YamlError("unbalanced brackets in flow collection", line_number)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _parse_flow(token: str, line_number: int) -> Any:
    """Parse ``[a, b]`` or ``{a: 1, b: 2}``."""
    text = token.strip()
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError("unterminated flow sequence", line_number)
        return [_parse_scalar(part, line_number) for part in _split_flow(text[1:-1], line_number)]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YamlError("unterminated flow mapping", line_number)
        mapping: dict = {}
        for part in _split_flow(text[1:-1], line_number):
            if ":" not in part:
                raise YamlError(f"flow mapping entry {part!r} has no value", line_number)
            key, _, value = part.partition(":")
            mapping[str(_parse_scalar(key, line_number))] = _parse_scalar(value, line_number)
        return mapping
    raise YamlError(f"unrecognized flow collection: {text!r}", line_number)  # pragma: no cover


def _split_key(content: str, line_number: int) -> Optional[Tuple[str, str]]:
    """Split ``key: value`` at the first top-level colon, or return None."""
    in_single = in_double = False
    depth = 0
    for index, char in enumerate(content):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            elif char == ":" and depth == 0:
                after = content[index + 1 :]
                if after and after[0] not in " \t":
                    continue  # part of a value such as a URL or a time
                key = content[:index].strip()
                if not key:
                    raise YamlError("mapping entry has an empty key", line_number)
                return key, after.strip()
    return None


class _Parser:
    def __init__(self, lines: List[_Line]) -> None:
        self.lines = lines
        self.position = 0

    def peek(self) -> Optional[_Line]:
        return self.lines[self.position] if self.position < len(self.lines) else None

    def parse_document(self) -> Any:
        line = self.peek()
        if line is None:
            return None
        return self.parse_block(line.indent)

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if line.content.startswith("- ") or line.content == "-":
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return items
            if line.indent > indent:
                raise YamlError("unexpected indentation in sequence", line.number)
            if not (line.content.startswith("- ") or line.content == "-"):
                return items

            self.position += 1
            body = line.content[1:].strip()
            item_indent = line.indent + 2

            if not body:
                nested = self.peek()
                if nested is not None and nested.indent > line.indent:
                    items.append(self.parse_block(nested.indent))
                else:
                    items.append(None)
                continue

            pair = _split_key(body, line.number)
            if pair is not None:
                # "- key: value" starts a mapping whose first entry is on this line.
                key, value = pair
                mapping = self.parse_inline_mapping_start(key, value, item_indent, line)
                items.append(mapping)
                continue

            block = _BLOCK_SCALAR.match(body)
            if block:
                items.append(self.read_block_scalar(block.group(1), block.group(2), line.indent))
                continue

            items.append(_parse_scalar(body, line.number))

    def parse_inline_mapping_start(
        self, key: str, value: str, item_indent: int, line: _Line
    ) -> dict:
        """Handle ``- key: value`` plus any further keys indented beneath it."""
        mapping: dict = {}
        block = _BLOCK_SCALAR.match(value)
        if block:
            mapping[key] = self.read_block_scalar(block.group(1), block.group(2), item_indent - 1)
        elif value:
            mapping[key] = _parse_scalar(value, line.number)
        else:
            nested = self.peek()
            if nested is not None and nested.indent >= item_indent:
                mapping[key] = self.parse_block(nested.indent)
            else:
                mapping[key] = None

        following = self.peek()
        if following is not None and following.indent == item_indent:
            rest = self.parse_mapping(item_indent)
            for extra_key, extra_value in rest.items():
                if extra_key in mapping:
                    raise YamlError(f"duplicate key {extra_key!r}", following.number)
                mapping[extra_key] = extra_value
        return mapping

    def parse_mapping(self, indent: int) -> dict:
        mapping: dict = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent:
                return mapping
            if line.indent > indent:
                raise YamlError("unexpected indentation in mapping", line.number)
            if line.content.startswith("- "):
                return mapping

            pair = _split_key(line.content, line.number)
            if pair is None:
                raise YamlError(f"expected 'key: value', got {line.content!r}", line.number)
            key, value = pair
            if key in mapping:
                raise YamlError(f"duplicate key {key!r}", line.number)
            self.position += 1

            block = _BLOCK_SCALAR.match(value)
            if block:
                mapping[key] = self.read_block_scalar(block.group(1), block.group(2), indent)
                continue
            if value:
                mapping[key] = _parse_scalar(value, line.number)
                continue

            nested = self.peek()
            if nested is None or nested.indent <= indent:
                # A bare "key:" with nothing under it, unless a sequence follows
                # at the same indent (valid YAML: sequences may not be indented).
                if (
                    nested is not None
                    and nested.indent == indent
                    and (nested.content.startswith("- ") or nested.content == "-")
                ):
                    mapping[key] = self.parse_sequence(indent)
                else:
                    mapping[key] = None
                continue
            mapping[key] = self.parse_block(nested.indent)

    def read_block_scalar(self, style: str, chomp: str, parent_indent: int) -> str:
        """Consume the indented lines belonging to a ``|`` or ``>`` scalar."""
        raw_lines: List[str] = []
        while True:
            line = self.peek()
            if line is None:
                break
            if line.content.strip() and line.indent <= parent_indent:
                break
            raw_lines.append(line.content)
            self.position += 1

        if not raw_lines:
            return ""

        widths = [len(x) - len(x.lstrip(" ")) for x in raw_lines if x.strip()]
        block_indent = min(widths) if widths else 0
        body = [x[block_indent:] if len(x) >= block_indent else x.lstrip(" ") for x in raw_lines]

        if style == ">":
            # Folded: blank lines become paragraph breaks, others join with a space.
            folded: List[str] = []
            for entry in body:
                if not entry.strip():
                    folded.append("\n")
                elif folded and folded[-1] != "\n":
                    folded[-1] = f"{folded[-1]} {entry.strip()}"
                else:
                    folded.append(entry.strip())
            text = "".join(x if x == "\n" else x for x in folded)
            text = " ".join(part for part in text.split("\n") if part).strip()
            return text if chomp == "-" else f"{text}\n"

        text = "\n".join(body).rstrip("\n")
        if chomp == "-":
            return text
        if chomp == "+":
            return "\n".join(body)
        return f"{text}\n"


def safe_load(source: str) -> Any:
    """Parse a YAML (or JSON) document into Python data.

    Constructs this parser does not support raise :class:`YamlError` naming the
    line, rather than being silently dropped.
    """
    if not isinstance(source, str):
        raise YamlError(f"expected a string, got {type(source).__name__}")

    text = source.lstrip("﻿")
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        # Unambiguously JSON-shaped: use the stdlib parser, which is stricter
        # and handles nested structures this subset parser does not need to.
        try:
            return json.loads(stripped)
        except ValueError:
            pass  # not JSON after all; fall through to the YAML path

    lines = _scan(text)
    if not lines:
        return None
    parser = _Parser(lines)
    document = parser.parse_document()
    remaining = parser.peek()
    if remaining is not None:
        raise YamlError(f"unexpected content: {remaining.content!r}", remaining.number)
    return document
