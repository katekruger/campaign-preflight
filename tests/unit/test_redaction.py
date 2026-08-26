"""Redaction and secret-scrubbing.

The invariant here is absolute: an API key must never appear in a report, a log
line, an exception, or a test snapshot -- including when the user has explicitly
asked for unredacted output.
"""

from __future__ import annotations

import json

import pytest

from campaign_preflight.errors import (
    ProviderAuthError,
    ProviderError,
    PreflightError,
    redact_secrets,
)
from campaign_preflight.reporting.redaction import redact_email, redact_samples, redact_text

FAKE_KEY = "ZmFrZS1rZXktZm9yLXRlc3Rpbmctb25seS1ub3QtcmVhbA=="


class TestSecretScrubbing:
    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {FAKE_KEY}",
            f"bearer {FAKE_KEY}",
            f'{{"api_key": "{FAKE_KEY}"}}',
            f"INSTANTLY_API_KEY={FAKE_KEY}",
            f"access_token: {FAKE_KEY}",
            f"password = {FAKE_KEY}",
            f"the key is {FAKE_KEY} ok",
        ],
    )
    def test_credential_shapes_are_masked(self, text: str) -> None:
        assert FAKE_KEY not in redact_secrets(text)
        assert "[REDACTED]" in redact_secrets(text)

    def test_ordinary_text_is_untouched(self) -> None:
        text = "8 blockers, 4 warnings, 1 unknown"
        assert redact_secrets(text) == text

    def test_scrubbing_survives_no_redact(self) -> None:
        """--no-redact turns off PII masking, never credential masking."""
        assert FAKE_KEY not in redact_text(f"Bearer {FAKE_KEY}", redacted=False)

    def test_errors_scrub_their_message(self) -> None:
        error = ProviderError(f"failed with Authorization: Bearer {FAKE_KEY}")
        assert FAKE_KEY not in str(error)

    def test_error_hints_are_scrubbed(self) -> None:
        error = PreflightError("boom", hint=f"api_key={FAKE_KEY}")
        assert FAKE_KEY not in str(error)

    def test_auth_error_never_echoes_the_key(self) -> None:
        error = ProviderAuthError("Instantly returned 401", status=401, endpoint="/api/v2/campaigns")
        assert "Bearer" not in str(error)


class TestEmailMasking:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("ana.diaz@corp.example.com", "a******z@corp.example.com"),
            ("ab@corp.example.com", "**@corp.example.com"),
            ("a@corp.example.com", "*@corp.example.com"),
        ],
    )
    def test_local_part_is_masked_domain_is_kept(self, value: str, expected: str) -> None:
        assert redact_email(value) == expected

    def test_domains_survive_because_findings_depend_on_them(self) -> None:
        assert "corp.example.com" in redact_text("ana@corp.example.com")

    def test_addresses_inside_prose_are_masked(self) -> None:
        masked = redact_text("2 contacts (ana@corp.example.com, bob@corp.example.com) matched")
        assert "ana@" not in masked and "bob@" not in masked
        assert "matched" in masked

    def test_malformed_addresses_are_still_masked(self) -> None:
        """An invalid address is exactly what a syntax finding reports."""
        masked = redact_text("dana.whitfield@@meridian..example.com")
        assert "dana.whitfield" not in masked

    def test_no_redact_leaves_addresses_intact(self) -> None:
        assert redact_text("ana@corp.example.com", redacted=False) == "ana@corp.example.com"

    def test_samples_are_bounded(self) -> None:
        samples = [f"p{i}@c.example.com" for i in range(50)]
        assert len(redact_samples(samples, limit=5)) == 5

    def test_masking_is_idempotent(self) -> None:
        once = redact_text("ana.diaz@corp.example.com")
        assert redact_text(once) == once


class TestReportRedaction:
    async def test_report_masks_addresses_by_default(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight
        from campaign_preflight.providers import FixtureProvider
        from campaign_preflight.reporting import render_json, render_markdown, render_terminal

        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        for rendered in (
            render_json(report),
            render_markdown(report),
            render_terminal(report, color=False),
        ):
            assert "marcus.reyes@" not in rendered
            assert "stonebridge.example.com" in rendered, "domains stay useful"

    async def test_unredacted_report_says_so_loudly(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight
        from campaign_preflight.providers import FixtureProvider
        from campaign_preflight.reporting import render_markdown, render_terminal

        report = await run_preflight(FixtureProvider.demo(), PreflightConfig(), redacted=False)
        assert "UNREDACTED" in render_terminal(report, color=False)
        assert "UNREDACTED" in render_markdown(report)

    async def test_json_report_records_its_redaction_state(self) -> None:
        from campaign_preflight.config import PreflightConfig
        from campaign_preflight.engine import run_preflight
        from campaign_preflight.providers import FixtureProvider
        from campaign_preflight.reporting import render_json

        report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
        assert json.loads(render_json(report))["redacted"] is True
