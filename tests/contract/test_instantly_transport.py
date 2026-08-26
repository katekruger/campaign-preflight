"""The read-only transport boundary.

If any test in this file fails, Campaign Preflight can mutate a customer's
campaign. Treat a failure here as a security incident, not a test failure.
"""

from __future__ import annotations

import itertools
import re

import httpx
import pytest

from campaign_preflight.providers.instantly_provider import InstantlyProvider
from campaign_preflight.providers.instantly_transport import (
    READ_ONLY_ALLOWLIST,
    ReadOnlyTransport,
    ReadOnlyViolation,
    audit_allowlist,
    is_allowed,
)

# Every mutating operation the Instantly v2 API exposes that this tool must
# never be able to reach, taken from the endpoints documented for campaigns,
# leads, accounts, block lists, and webhooks.
FORBIDDEN_REQUESTS = [
    ("POST", "/api/v2/campaigns"),
    ("PATCH", "/api/v2/campaigns/01a03960-aa51-777b-8a74-c93b2883a947"),
    ("DELETE", "/api/v2/campaigns/01a03960-aa51-777b-8a74-c93b2883a947"),
    ("POST", "/api/v2/campaigns/01a03960-aa51-777b-8a74-c93b2883a947/activate"),
    ("POST", "/api/v2/campaigns/01a03960-aa51-777b-8a74-c93b2883a947/pause"),
    ("POST", "/api/v2/leads"),
    ("POST", "/api/v2/leads/add"),
    ("POST", "/api/v2/leads/move"),
    ("POST", "/api/v2/leads/merge"),
    ("POST", "/api/v2/leads/update-interest-status"),
    ("PATCH", "/api/v2/leads/abc"),
    ("DELETE", "/api/v2/leads/abc"),
    ("POST", "/api/v2/lead-lists"),
    ("POST", "/api/v2/accounts"),
    ("PATCH", "/api/v2/accounts/dana@example.com"),
    ("DELETE", "/api/v2/accounts/dana@example.com"),
    ("POST", "/api/v2/accounts/dana@example.com/pause"),
    ("POST", "/api/v2/accounts/dana@example.com/resume"),
    ("POST", "/api/v2/block-lists-entries"),
    ("POST", "/api/v2/block-lists-entries/bulk-create"),
    ("POST", "/api/v2/block-lists-entries/bulk-delete"),
    ("DELETE", "/api/v2/block-lists-entries/abc"),
    ("POST", "/api/v2/emails/reply"),
    ("POST", "/api/v2/emails/forward"),
    ("POST", "/api/v2/webhooks"),
    ("DELETE", "/api/v2/webhooks/abc"),
    ("POST", "/api/v2/email-verification"),
]

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
SAMPLE_PATHS = [
    "/api/v2/campaigns",
    "/api/v2/campaigns/abc",
    "/api/v2/campaigns/analytics",
    "/api/v2/leads/list",
    "/api/v2/leads/abc",
    "/api/v2/accounts",
    "/api/v2/accounts/a@b.com",
    "/api/v2/block-lists-entries",
    "/api/v2/workspaces/current",
    "/api/v2/emails",
    "/",
]


class TestAllowlistShape:
    def test_only_one_non_get_entry_exists(self) -> None:
        non_get = [(m, p.pattern) for m, p, _ in READ_ONLY_ALLOWLIST if m != "GET"]
        assert non_get == [("POST", r"^/api/v2/leads/list$")], (
            "the only permitted non-GET is the documented leads/list read endpoint"
        )

    def test_no_mutating_method_is_allowlisted(self) -> None:
        methods = {m for m, _, _ in READ_ONLY_ALLOWLIST}
        assert methods <= {"GET", "POST"}

    def test_every_entry_is_anchored(self) -> None:
        """An unanchored pattern would allow a path prefix to smuggle a suffix."""
        for _, pattern, _ in READ_ONLY_ALLOWLIST:
            assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$")

    def test_the_real_allowlist_passes_its_own_audit(self) -> None:
        audit_allowlist()


class TestAllowlistAudit:
    """The guard that runs at import time. An untested guard is not a guard."""

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def test_a_mutating_method_is_rejected(self, method: str) -> None:
        entry = (method, re.compile(r"^/api/v2/campaigns$"), "should never be allowed")
        with pytest.raises(AssertionError, match="mutating method"):
            audit_allowlist((entry,))

    def test_an_undocumented_post_is_rejected(self) -> None:
        entry = ("POST", re.compile(r"^/api/v2/leads/add$"), "a write in disguise")
        with pytest.raises(AssertionError, match="undocumented POST"):
            audit_allowlist((entry,))

    def test_the_documented_post_is_accepted(self) -> None:
        audit_allowlist((("POST", re.compile(r"^/api/v2/leads/list$"), "the read exception"),))

    @pytest.mark.parametrize(
        "pattern", [r"/api/v2/campaigns$", r"^/api/v2/campaigns", r"/api/v2/campaigns"]
    )
    def test_an_unanchored_pattern_is_rejected(self, pattern: str) -> None:
        with pytest.raises(AssertionError, match="not anchored"):
            audit_allowlist((("GET", re.compile(pattern), "unanchored"),))

    def test_a_well_formed_get_is_accepted(self) -> None:
        audit_allowlist((("GET", re.compile(r"^/api/v2/campaigns$"), "fine"),))


class TestAllowlistDecisions:
    @pytest.mark.parametrize(("method", "path"), FORBIDDEN_REQUESTS)
    def test_known_mutating_endpoints_are_rejected(self, method: str, path: str) -> None:
        assert not is_allowed(method, path)

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v2/campaigns"),
            ("GET", "/api/v2/campaigns/01a03960-aa51-777b-8a74-c93b2883a947"),
            ("GET", "/api/v2/campaigns/analytics"),
            ("POST", "/api/v2/leads/list"),
            ("GET", "/api/v2/accounts"),
            ("GET", "/api/v2/accounts/dana@example.com"),
            ("GET", "/api/v2/block-lists-entries"),
            ("GET", "/api/v2/workspaces/current"),
        ],
    )
    def test_the_reads_this_tool_needs_are_permitted(self, method: str, path: str) -> None:
        assert is_allowed(method, path)

    @pytest.mark.parametrize(("method", "path"), list(itertools.product(ALL_METHODS, SAMPLE_PATHS)))
    def test_the_full_method_path_matrix_matches_the_allowlist(
        self, method: str, path: str
    ) -> None:
        """Exhaustive: nothing outside the allowlist is reachable for any method."""
        expected = any(
            method == allowed and pattern.match(path) for allowed, pattern, _ in READ_ONLY_ALLOWLIST
        )
        assert is_allowed(method, path) is expected

    def test_path_traversal_cannot_reach_a_write(self) -> None:
        assert not is_allowed("GET", "/api/v2/campaigns/abc/../../leads/add")

    def test_a_sub_path_of_an_allowed_endpoint_is_not_allowed(self) -> None:
        assert not is_allowed("GET", "/api/v2/campaigns/abc/activate")

    def test_method_matching_is_case_insensitive(self) -> None:
        assert is_allowed("get", "/api/v2/campaigns")
        assert not is_allowed("delete", "/api/v2/campaigns")


class TestTransportEnforcement:
    async def test_the_transport_raises_on_a_forbidden_request(self) -> None:
        guard = ReadOnlyTransport(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        async with httpx.AsyncClient(
            transport=guard, base_url="https://api.instantly.ai"
        ) as client:
            with pytest.raises(ReadOnlyViolation):
                await client.post("/api/v2/campaigns/abc/activate")

    async def test_a_blocked_request_never_reaches_the_inner_transport(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={})

        guard = ReadOnlyTransport(httpx.MockTransport(handler))
        async with httpx.AsyncClient(
            transport=guard, base_url="https://api.instantly.ai"
        ) as client:
            for method, path in FORBIDDEN_REQUESTS:
                with pytest.raises(ReadOnlyViolation):
                    await client.request(method, path)
        assert seen == [], "a blocked request must not be forwarded"

    async def test_blocked_attempts_are_recorded(self) -> None:
        guard = ReadOnlyTransport(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        async with httpx.AsyncClient(
            transport=guard, base_url="https://api.instantly.ai"
        ) as client:
            with pytest.raises(ReadOnlyViolation):
                await client.delete("/api/v2/leads/abc")
        assert guard.blocked == [("DELETE", "/api/v2/leads/abc")]

    async def test_allowed_requests_pass_through(self) -> None:
        guard = ReadOnlyTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        )
        async with httpx.AsyncClient(
            transport=guard, base_url="https://api.instantly.ai"
        ) as client:
            response = await client.get("/api/v2/campaigns")
        assert response.json() == {"ok": True}


class TestProviderEnforcement:
    @pytest.mark.parametrize(("method", "path"), FORBIDDEN_REQUESTS)
    async def test_the_provider_cannot_issue_a_write(self, method: str, path: str) -> None:
        """Even a deliberate attempt from inside the provider is stopped."""
        provider = InstantlyProvider(
            "test-key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
        try:
            with pytest.raises(ReadOnlyViolation):
                await provider._request(method, path.replace("/api/v2", ""))
        finally:
            await provider.aclose()

    async def test_a_full_run_issues_only_allowlisted_requests(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            return httpx.Response(200, json={"items": [], "next_starting_after": None})

        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight

        provider = InstantlyProvider("test-key", transport=httpx.MockTransport(handler))
        await run_preflight(provider, PreflightConfig(), campaign_id="abc")
        await provider.aclose()

        assert seen, "the run should have made requests"
        for method, path in seen:
            assert is_allowed(method, path), f"{method} {path} escaped the allowlist"

    def test_the_provider_exposes_no_write_method(self) -> None:
        """A structural check on the public surface of the provider."""
        forbidden = ("create", "update", "delete", "activate", "pause", "resume", "add", "move")
        public = [name for name in dir(InstantlyProvider) if not name.startswith("_")]
        for name in public:
            assert not any(name.lower().startswith(verb) for verb in forbidden), name
