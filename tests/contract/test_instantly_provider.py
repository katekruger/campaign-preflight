"""Instantly provider contract: response shapes, enums, pagination, and failures.

Every fixture in this file mirrors the shapes documented in the official v2
reference. All requests are mocked; no test here touches the network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from campaign_preflight.errors import ProviderAuthError, ProviderError
from campaign_preflight.models import CapabilityStatus
from campaign_preflight.providers.instantly_provider import (
    ACCOUNT_STATUS,
    CAMPAIGN_STATUS,
    WARMUP_STATUS,
    InstantlyProvider,
)

CAMPAIGN_ID = "01a03960-aa51-777b-8a74-c93b2883a947"
FAKE_KEY = "ZmFrZS1rZXktZm9yLXRlc3Rpbmctb25seS1ub3QtcmVhbA=="

CAMPAIGN: dict[str, Any] = {
    "id": CAMPAIGN_ID,
    "name": "Enterprise Q3",
    "status": 0,
    "daily_limit": 90,
    "stop_on_reply": True,
    "stop_on_auto_reply": False,
    "email_list": ["dana@example.com", "mia@example.com"],
    "timestamp_created": "2026-01-04T09:00:00.000Z",
    "campaign_schedule": {
        "start_date": "2026-09-01",
        "end_date": "2026-12-31",
        "schedules": [
            {
                "name": "Business hours",
                "timing": {"from": "09:00", "to": "17:00"},
                "days": {"0": False, "1": True, "2": True, "3": True, "4": True, "5": True, "6": False},
                "timezone": "America/New_York",
            }
        ],
    },
    "sequences": [
        {
            "steps": [
                {
                    "type": "email",
                    "delay": 0,
                    "delay_unit": "days",
                    "variants": [
                        {"subject": "Hi {{first_name}}", "body": "Hello.<br/>Unsubscribe."},
                        {"subject": "Hey {{first_name}}", "body": "Hi there.<br/>Unsubscribe.", "v_disabled": True},
                    ],
                },
                {"type": "email", "delay": 3, "variants": [{"subject": "", "body": "Following up. Unsubscribe."}]},
            ]
        }
    ],
}

ACCOUNT: dict[str, Any] = {
    "email": "dana@example.com",
    "first_name": "Dana",
    "last_name": "Reyes",
    "status": 1,
    "warmup_status": 1,
    "provider_code": 2,
    "daily_limit": 60,
    "stat_warmup_score": 94,
    "setup_pending": False,
}

LEAD: dict[str, Any] = {
    "id": "9f3c2f4e-0000-4000-8000-000000000001",
    "email": "ana.diaz@corp.example.com",
    "first_name": "Ana",
    "last_name": "Diaz",
    "company_name": "Corp Industries",
    "company_domain": "corp.example.com",
    "job_title": "VP Operations",
    "personalization": "Corp opened a second facility.",
    "status": 1,
    "timestamp_last_contact": None,
    "payload": {"funding_stage": "Series B"},
}


def route(overrides: dict[str, Any] | None = None, recorder: list | None = None):
    """A MockTransport handler serving the documented shapes."""
    routes: dict[str, Any] = {
        f"/api/v2/campaigns/{CAMPAIGN_ID}": CAMPAIGN,
        "/api/v2/leads/list": {"items": [LEAD], "next_starting_after": None},
        "/api/v2/accounts/dana@example.com": ACCOUNT,
        "/api/v2/accounts/mia@example.com": {**ACCOUNT, "email": "mia@example.com"},
        "/api/v2/block-lists-entries": {
            "items": [{"id": "b1", "bl_value": "blocked.example.com", "is_domain": True}],
            "next_starting_after": None,
        },
        "/api/v2/campaigns/analytics": [{"campaign_id": CAMPAIGN_ID, "leads_count": 1}],
        "/api/v2/workspaces/current": {"id": "ws-1", "name": "Workspace"},
    }
    routes.update(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append((request.method, request.url.path, request.url.params))
        body = routes.get(request.url.path)
        if isinstance(body, httpx.Response):
            return body
        if callable(body):
            return body(request)
        if body is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def provider(transport: httpx.MockTransport, **kwargs: Any) -> InstantlyProvider:
    return InstantlyProvider(FAKE_KEY, transport=transport, max_retries=1, **kwargs)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the retry sleep so retry tests run instantly.

    Patches the provider's own backoff rather than ``asyncio.sleep``, which the
    test harness itself uses.
    """

    async def instant(self, attempt: int, retry_after: str | None = None) -> None:
        return None

    monkeypatch.setattr(InstantlyProvider, "_backoff", instant)


class TestCampaignParsing:
    async def test_campaign_fields(self) -> None:
        p = provider(route())
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        campaign = result.data
        assert result.status is CapabilityStatus.SUPPORTED_OK
        assert campaign.id == CAMPAIGN_ID
        assert campaign.name == "Enterprise Q3"
        assert campaign.status == "draft"
        assert campaign.daily_limit == 90
        assert campaign.stop_on_reply is True
        assert campaign.stop_on_auto_reply is False
        assert campaign.sender_emails == ("dana@example.com", "mia@example.com")

    async def test_schedule_parsing(self) -> None:
        p = provider(route())
        campaign = (await p.get_campaign(CAMPAIGN_ID)).data
        await p.aclose()
        window = campaign.schedule.windows[0]
        assert window.days == frozenset({1, 2, 3, 4, 5})
        assert str(window.start) == "09:00:00"
        assert window.timezone_name == "America/New_York"
        assert str(campaign.schedule.start_date) == "2026-09-01"

    async def test_sequences_flatten_into_ordered_steps(self) -> None:
        p = provider(route())
        campaign = (await p.get_campaign(CAMPAIGN_ID)).data
        await p.aclose()
        assert len(campaign.steps) == 3
        assert [s.index for s in campaign.steps] == [0, 0, 1]
        assert campaign.steps[1].disabled is True

    @pytest.mark.parametrize("code,label", sorted(CAMPAIGN_STATUS.items()))
    async def test_documented_campaign_statuses(self, code: int, label: str) -> None:
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": {**CAMPAIGN, "status": code}}))
        campaign = (await p.get_campaign(CAMPAIGN_ID)).data
        await p.aclose()
        assert campaign.status == label

    async def test_unknown_enum_value_is_marked_not_guessed(self) -> None:
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": {**CAMPAIGN, "status": 77}}))
        campaign = (await p.get_campaign(CAMPAIGN_ID)).data
        await p.aclose()
        assert campaign.status == "unknown:77"

    async def test_null_stop_on_reply_stays_null(self) -> None:
        """Instantly returns null for unconfigured settings. Null is not False."""
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": {**CAMPAIGN, "stop_on_reply": None}}))
        campaign = (await p.get_campaign(CAMPAIGN_ID)).data
        await p.aclose()
        assert campaign.stop_on_reply is None

    async def test_missing_optional_fields_do_not_crash(self) -> None:
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": {"id": CAMPAIGN_ID}}))
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.is_ok
        assert result.data.steps == ()

    async def test_no_campaign_id_is_a_configuration_problem(self) -> None:
        p = provider(route())
        result = await p.get_campaign(None)
        await p.aclose()
        assert result.status is CapabilityStatus.UNAVAILABLE_CONFIG


class TestLeads:
    async def test_leads_are_normalized(self) -> None:
        p = provider(route())
        result = await p.list_campaign_leads(CAMPAIGN_ID)
        await p.aclose()
        lead = result.data[0]
        assert lead.normalized_email == "ana.diaz@corp.example.com"
        assert lead.custom_variables == {"funding_stage": "Series B"}
        assert lead.status_label == "not_contacted"

    async def test_prior_contact_is_derived_from_a_documented_field(self) -> None:
        """The lead status enum's labels are undocumented, so we do not guess them."""
        contacted = {**LEAD, "timestamp_last_contact": "2026-02-01T10:00:00Z"}
        p = provider(route({"/api/v2/leads/list": {"items": [contacted], "next_starting_after": None}}))
        lead = (await p.list_campaign_leads(CAMPAIGN_ID)).data[0]
        await p.aclose()
        assert lead.status_label == "contacted"

    async def test_leads_list_uses_post_with_a_campaign_filter(self) -> None:
        seen: list = []
        bodies: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/api/v2/leads/list":
                bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"items": [], "next_starting_after": None})

        p = provider(httpx.MockTransport(handler))
        await p.list_campaign_leads(CAMPAIGN_ID)
        await p.aclose()
        assert ("POST", "/api/v2/leads/list") in seen
        assert bodies[0]["campaign"] == CAMPAIGN_ID
        assert bodies[0]["limit"] == 100


class TestPagination:
    async def test_walks_multiple_pages(self) -> None:
        pages = {
            None: {"items": [{"bl_value": f"a{i}.example.com"} for i in range(100)], "next_starting_after": "p2"},
            "p2": {"items": [{"bl_value": "b.example.com"}], "next_starting_after": None},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            cursor = request.url.params.get("starting_after")
            return httpx.Response(200, json=pages[cursor])

        p = provider(httpx.MockTransport(handler))
        result = await p.list_suppressions()
        await p.aclose()
        assert len(result.data) == 101
        assert result.partial is False

    async def test_a_repeated_cursor_stops_the_walk(self) -> None:
        """A provider-side loop must not spin this tool forever."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"items": [{"bl_value": "x.example.com"}], "next_starting_after": "same"}
            )

        p = provider(httpx.MockTransport(handler))
        result = await p.list_suppressions()
        await p.aclose()
        assert result.partial is True
        assert any("cursor repeated" in w for w in p.warnings)

    async def test_the_page_cap_bounds_the_walk(self) -> None:
        counter = iter(range(10_000))

        def handler(request: httpx.Request) -> httpx.Response:
            n = next(counter)
            return httpx.Response(
                200, json={"items": [{"bl_value": f"x{n}.example.com"}], "next_starting_after": f"c{n}"}
            )

        p = provider(httpx.MockTransport(handler), page_cap=3)
        result = await p.list_suppressions()
        await p.aclose()
        assert len(result.data) == 3
        assert result.partial is True

    async def test_a_lead_limit_truncates_and_flags_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"items": [LEAD] * 5, "next_starting_after": "more"}
            )

        p = provider(httpx.MockTransport(handler))
        result = await p.list_campaign_leads(CAMPAIGN_ID, limit=3)
        await p.aclose()
        assert len(result.data) == 3
        assert result.partial is True


class TestSenders:
    async def test_accounts_are_fetched_for_each_attached_sender(self) -> None:
        seen: list = []
        p = provider(route(recorder=seen))
        await p.get_campaign(CAMPAIGN_ID)
        result = await p.list_campaign_senders(CAMPAIGN_ID)
        await p.aclose()
        assert {s.email for s in result.data} == {"dana@example.com", "mia@example.com"}
        assert result.data[0].health_score == 94.0
        assert result.data[0].enabled is True

    async def test_campaign_is_fetched_only_once(self) -> None:
        seen: list = []
        p = provider(route(recorder=seen))
        await p.get_campaign(CAMPAIGN_ID)
        await p.list_campaign_senders(CAMPAIGN_ID)
        await p.aclose()
        campaign_calls = [s for s in seen if s[1] == f"/api/v2/campaigns/{CAMPAIGN_ID}"]
        assert len(campaign_calls) == 1

    @pytest.mark.parametrize("code,label", sorted(ACCOUNT_STATUS.items()))
    async def test_documented_account_statuses(self, code: int, label: str) -> None:
        p = provider(
            route({"/api/v2/accounts/dana@example.com": {**ACCOUNT, "status": code}})
        )
        await p.get_campaign(CAMPAIGN_ID)
        senders = (await p.list_campaign_senders(CAMPAIGN_ID)).data
        await p.aclose()
        assert senders[0].status_label == label
        assert senders[0].status_is_error is (code < 0)

    @pytest.mark.parametrize("code,label", sorted(WARMUP_STATUS.items()))
    async def test_documented_warmup_statuses(self, code: int, label: str) -> None:
        p = provider(route({"/api/v2/accounts/dana@example.com": {**ACCOUNT, "warmup_status": code}}))
        await p.get_campaign(CAMPAIGN_ID)
        senders = (await p.list_campaign_senders(CAMPAIGN_ID)).data
        await p.aclose()
        assert senders[0].warmup_status == label

    async def test_an_unreadable_account_keeps_the_sender_without_health(self) -> None:
        """The campaign genuinely has the sender; we simply cannot assess it."""
        p = provider(
            route({"/api/v2/accounts/mia@example.com": httpx.Response(403, json={"message": "no scope"})})
        )
        await p.get_campaign(CAMPAIGN_ID)
        result = await p.list_campaign_senders(CAMPAIGN_ID)
        await p.aclose()
        assert len(result.data) == 2
        assert result.partial is True
        mia = next(s for s in result.data if s.email == "mia@example.com")
        assert mia.health_score is None

    async def test_all_accounts_forbidden_is_a_permissions_result(self) -> None:
        p = provider(
            route(
                {
                    "/api/v2/accounts/dana@example.com": httpx.Response(403, json={"message": "no"}),
                    "/api/v2/accounts/mia@example.com": httpx.Response(403, json={"message": "no"}),
                }
            )
        )
        await p.get_campaign(CAMPAIGN_ID)
        result = await p.list_campaign_senders(CAMPAIGN_ID)
        await p.aclose()
        assert result.status is CapabilityStatus.UNAVAILABLE_PERMISSIONS

    async def test_no_warmup_score_means_unsupported_health_not_a_zero(self) -> None:
        p = provider(route({"/api/v2/accounts/dana@example.com": {**ACCOUNT, "stat_warmup_score": None}}))
        await p.get_campaign(CAMPAIGN_ID)
        senders = (await p.list_campaign_senders(CAMPAIGN_ID)).data
        health = await p.get_sender_health([s for s in senders if s.email == "dana@example.com"])
        await p.aclose()
        assert health.status is CapabilityStatus.UNSUPPORTED

    async def test_sender_concurrency_is_bounded(self) -> None:
        import asyncio

        active = 0
        peak = 0

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            if request.url.path.startswith("/api/v2/accounts/"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return httpx.Response(200, json=ACCOUNT)
            return httpx.Response(200, json=CAMPAIGN)

        many = {**CAMPAIGN, "email_list": [f"s{i}@example.com" for i in range(12)]}
        transport = httpx.MockTransport(slow_handler)
        p = provider(transport)
        p._campaign_cache[CAMPAIGN_ID] = many
        await p.list_campaign_senders(CAMPAIGN_ID)
        await p.aclose()
        assert peak <= 3, f"sender fetches ran {peak} at a time; the cap is 3"


class TestErrorHandling:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, CapabilityStatus.SUPPORTED_FAILED),
            (401, CapabilityStatus.UNAVAILABLE_PERMISSIONS),
            (402, CapabilityStatus.UNAVAILABLE_PERMISSIONS),
            (403, CapabilityStatus.UNAVAILABLE_PERMISSIONS),
            (404, CapabilityStatus.SUPPORTED_FAILED),
            (500, CapabilityStatus.SUPPORTED_FAILED),
        ],
    )
    async def test_http_status_maps_to_a_capability_status(
        self, status: int, expected: CapabilityStatus
    ) -> None:
        p = provider(
            route({f"/api/v2/campaigns/{CAMPAIGN_ID}": httpx.Response(status, json={"message": "x"})})
        )
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.status is expected
        assert result.data is None, "a failed read must not yield data"

    async def test_malformed_json_is_reported_not_crashed(self) -> None:
        p = provider(
            route({f"/api/v2/campaigns/{CAMPAIGN_ID}": httpx.Response(200, content=b"{not json")})
        )
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.status is CapabilityStatus.SUPPORTED_FAILED
        assert "malformed JSON" in result.detail

    async def test_unexpected_response_shape_is_reported(self) -> None:
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": ["not", "an", "object"]}))
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.status is CapabilityStatus.SUPPORTED_FAILED

    async def test_network_timeout_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        p = provider(httpx.MockTransport(handler))
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.status is CapabilityStatus.SUPPORTED_FAILED
        assert "timed out" in result.detail

    async def test_rate_limiting_is_retried_then_reported(self, no_backoff) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "slow down"})

        p = InstantlyProvider(FAKE_KEY, transport=httpx.MockTransport(handler), max_retries=3)
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert attempts == 3, "retries are bounded"
        assert result.status is CapabilityStatus.SUPPORTED_FAILED

    async def test_a_transient_error_recovers(self, no_backoff) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"message": "unavailable"})
            return httpx.Response(200, json=CAMPAIGN)

        p = InstantlyProvider(FAKE_KEY, transport=httpx.MockTransport(handler), max_retries=3)
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert result.is_ok

    async def test_a_client_error_is_not_retried(self, no_backoff) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404, json={"message": "not found"})

        p = InstantlyProvider(FAKE_KEY, transport=httpx.MockTransport(handler), max_retries=3)
        await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert attempts == 1, "4xx other than 429 must not be retried"

    async def test_an_empty_key_is_refused_before_any_request(self) -> None:
        with pytest.raises(ProviderAuthError):
            InstantlyProvider("", transport=httpx.MockTransport(lambda r: httpx.Response(200)))


class TestSecrets:
    async def test_the_key_is_sent_as_a_bearer_header(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json=CAMPAIGN)

        p = provider(httpx.MockTransport(handler))
        await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert captured[0] == f"Bearer {FAKE_KEY}"

    async def test_the_key_never_appears_in_an_error(self) -> None:
        p = provider(
            route({f"/api/v2/campaigns/{CAMPAIGN_ID}": httpx.Response(401, json={"message": "bad key"})})
        )
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert FAKE_KEY not in (result.detail or "")

    async def test_a_key_echoed_by_the_provider_is_scrubbed(self) -> None:
        """Provider response poisoning: the API must not be able to leak our key back."""
        poisoned = httpx.Response(400, json={"message": f"your key {FAKE_KEY} is bad"})
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": poisoned}))
        result = await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert FAKE_KEY not in (result.detail or "")
        assert "[REDACTED]" in (result.detail or "")

    async def test_the_key_never_reaches_a_rendered_report(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight
        from campaign_preflight.reporting import render_json, render_markdown, render_terminal

        poisoned = httpx.Response(500, json={"message": f"Authorization: Bearer {FAKE_KEY}"})
        p = provider(route({f"/api/v2/campaigns/{CAMPAIGN_ID}": poisoned}))
        report = await run_preflight(p, PreflightConfig(), campaign_id=CAMPAIGN_ID)
        await p.aclose()
        for rendered in (
            render_json(report),
            render_markdown(report),
            render_terminal(report, color=False),
        ):
            assert FAKE_KEY not in rendered

    def test_from_env_reads_the_key_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("INSTANTLY_API_KEY", FAKE_KEY)
        p = InstantlyProvider.from_env(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        assert p.base_url == "https://api.instantly.ai"

    def test_from_env_without_a_key_raises_auth_error(self, monkeypatch) -> None:
        monkeypatch.delenv("INSTANTLY_API_KEY", raising=False)
        with pytest.raises(ProviderAuthError):
            InstantlyProvider.from_env(transport=httpx.MockTransport(lambda r: httpx.Response(200)))


class TestUserAgentAndBaseUrl:
    async def test_user_agent_identifies_the_tool_and_declares_read_only(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("user-agent", ""))
            return httpx.Response(200, json=CAMPAIGN)

        p = provider(httpx.MockTransport(handler))
        await p.get_campaign(CAMPAIGN_ID)
        await p.aclose()
        assert "campaign-preflight/" in captured[0]
        assert "read-only" in captured[0]

    async def test_base_url_is_configurable_for_tests(self) -> None:
        p = InstantlyProvider(
            FAKE_KEY,
            base_url="https://staging.example.com",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CAMPAIGN)),
        )
        assert p.base_url == "https://staging.example.com"
        await p.aclose()


class TestFullRun:
    async def test_a_complete_run_produces_a_report(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight

        p = provider(route())
        report = await run_preflight(p, PreflightConfig(), campaign_id=CAMPAIGN_ID)
        await p.aclose()
        assert report.provider == "instantly"
        assert report.campaign_name == "Enterprise Q3"
        assert report.lead_count == 1
        assert report.sender_count == 2
        assert report.provider_read_only is True

    async def test_capability_failures_become_unknown_checks_not_passes(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight
        from campaign_preflight.models import RuleStatus

        p = provider(
            route({"/api/v2/block-lists-entries": httpx.Response(403, json={"message": "no scope"})})
        )
        report = await run_preflight(p, PreflightConfig(), campaign_id=CAMPAIGN_ID)
        await p.aclose()
        suppression = {r.rule_id: r for r in report.results if r.rule_id.startswith("suppression.")}
        assert suppression["suppression.contact_listed"].status is RuleStatus.UNKNOWN
        assert any("block-list" in x or "403" in x for x in report.limitations)
