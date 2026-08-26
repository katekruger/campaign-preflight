"""The read-only boundary for outbound HTTP.

Campaign Preflight must not be able to mutate a customer's campaign. Saying so
in a docstring is not a control; this module is the control.

Every request the Instantly provider makes passes through
:class:`ReadOnlyTransport`, which matches ``(method, path)`` against an explicit
allowlist and raises :class:`ReadOnlyViolation` on anything else. The check runs
at the transport layer, below the client and below the provider, so a future
code change that adds a ``PATCH`` call fails loudly at runtime instead of
quietly editing a campaign.

One entry is a POST: ``POST /api/v2/leads/list``. Instantly's lead listing takes
a filter body and is therefore modelled as a POST despite being a pure read. It
is allowlisted deliberately and is the only non-GET entry; the test suite
asserts that no second one appears.
"""

from __future__ import annotations

import re
from typing import Any, Final

try:
    import httpx
except ImportError as _exc:  # pragma: no cover - exercised by the optional-extra path
    raise ImportError(
        "The Instantly provider needs the optional 'httpx' package. "
        "Install it with: pip install 'campaign-preflight[instantly]'. "
        "The demo, file checks, rules, and MCP server all work without it."
    ) from _exc

__all__ = [
    "ReadOnlyViolation",
    "ReadOnlyTransport",
    "READ_ONLY_ALLOWLIST",
    "is_allowed",
]


class ReadOnlyViolation(RuntimeError):
    """Raised when a request would leave the read-only allowlist.

    This is a programming error inside Campaign Preflight, never a user error.
    """

    def __init__(self, method: str, path: str) -> None:
        self.method = method.upper()
        self.path = path
        super().__init__(
            f"blocked non-read-only request: {self.method} {path}. "
            f"Campaign Preflight is read-only; this is a bug, please report it."
        )


# (METHOD, compiled path pattern, human description). Patterns are anchored and
# match the path only -- query strings never affect the decision.
READ_ONLY_ALLOWLIST: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    ("GET", re.compile(r"^/api/v2/campaigns$"), "list campaigns"),
    ("GET", re.compile(r"^/api/v2/campaigns/analytics$"), "campaign analytics"),
    (
        "GET",
        re.compile(r"^/api/v2/campaigns/(?!analytics$)[^/]+$"),
        "get one campaign",
    ),
    ("GET", re.compile(r"^/api/v2/accounts$"), "list sending accounts"),
    ("GET", re.compile(r"^/api/v2/accounts/[^/]+$"), "get one sending account"),
    ("GET", re.compile(r"^/api/v2/block-lists-entries$"), "list block-list entries"),
    ("GET", re.compile(r"^/api/v2/workspaces/current$"), "current workspace"),
    # The documented read exception. See the module docstring.
    ("POST", re.compile(r"^/api/v2/leads/list$"), "list leads (read-only POST)"),
)

# Methods that can never appear in the allowlist, checked at import time so a
# mistaken entry is caught before the package is ever used.
_FORBIDDEN_METHODS: Final = frozenset({"PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ALLOWED_POST_PATHS: Final = frozenset({r"^/api/v2/leads/list$"})

for _method, _pattern, _ in READ_ONLY_ALLOWLIST:
    if _method in _FORBIDDEN_METHODS:
        raise AssertionError(f"allowlist contains a mutating method: {_method}")
    if _method == "POST" and _pattern.pattern not in _ALLOWED_POST_PATHS:
        raise AssertionError(
            f"allowlist contains an undocumented POST: {_pattern.pattern}. "
            f"Only the leads/list read endpoint may be a POST."
        )


def is_allowed(method: str, path: str) -> bool:
    """True when ``(method, path)`` is on the read-only allowlist."""
    upper = method.upper()
    return any(
        upper == allowed_method and pattern.match(path)
        for allowed_method, pattern, _ in READ_ONLY_ALLOWLIST
    )


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Wraps another transport and refuses anything off the allowlist.

    Composition rather than inheritance, so tests can slot an
    ``httpx.MockTransport`` underneath and still exercise the real guard.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self.blocked: list[tuple[str, str]] = []
        """Every blocked attempt, for tests and for post-mortem reporting."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if not is_allowed(method, path):
            self.blocked.append((method, path))
            raise ReadOnlyViolation(method, path)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
