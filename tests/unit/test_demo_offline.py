"""The demo must work with no credentials and no network. Ever.

Enforced by replacing socket creation with a function that raises, so any
attempt to open a connection during the demo fails the test loudly.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from campaign_preflight.config import PreflightConfig
from campaign_preflight.engine import run_preflight
from campaign_preflight.providers import FixtureProvider
from campaign_preflight.providers.fixture_provider import demo_paths


class NetworkAccessAttempted(AssertionError):  # noqa: N818 - reads better than ...Error
    """Raised if anything tries to open a socket during an offline test."""


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block outbound network sockets.

    AF_UNIX is left alone: asyncio builds its own self-pipe from a local
    socketpair, and blocking that would fail the test for a reason unrelated to
    network access.
    """
    real_socket = socket.socket

    def guarded(family: int = socket.AF_INET, *args: object, **kwargs: object):
        if family in {socket.AF_INET, socket.AF_INET6}:
            raise NetworkAccessAttempted(
                f"the demo attempted to open a network socket (family={family!r})"
            )
        return real_socket(family, *args, **kwargs)

    def blocked(*args: object, **kwargs: object):
        raise NetworkAccessAttempted("the demo attempted to resolve or connect to a host")

    monkeypatch.setattr(socket, "socket", guarded)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("INSTANTLY_API_KEY", "INSTANTLY_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


async def test_demo_runs_offline_without_credentials(no_network, no_credentials) -> None:
    report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
    assert report.lead_count > 0
    assert report.provider == "demo"


async def test_demo_exercises_every_result_status(no_network, no_credentials) -> None:
    """The point of the demo is to show what each verdict looks like."""
    report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
    assert report.blocker_count > 0, "demo should show blockers"
    assert report.warning_count > 0, "demo should show warnings"
    assert report.unknown_count > 0, "demo should show a check that could not run"
    assert report.passed_count > 0, "demo should show passing checks"
    assert report.not_applicable_count > 0, "demo should show a non-applicable check"


async def test_demo_renders_in_every_format(no_network, no_credentials) -> None:
    from campaign_preflight.reporting import render_json, render_markdown, render_terminal

    report = await run_preflight(FixtureProvider.demo(), PreflightConfig())
    assert render_json(report).strip().startswith("{")
    assert render_markdown(report).startswith("# Campaign Preflight")
    assert "CAMPAIGN PREFLIGHT" in render_terminal(report, color=False)


class TestDemoData:
    def test_every_bundled_file_exists(self) -> None:
        for name, path in demo_paths().items():
            assert path.is_file(), f"missing demo file: {name} at {path}"

    def test_demo_data_uses_only_reserved_domains(self) -> None:
        """No real person's address may ship in this repository."""
        allowed_suffixes = (".example.com", ".example.org", ".example.net", ".invalid")
        allowed_exact = {"example.com", "gmail.com"}  # gmail.com appears as a *domain* only
        offenders: list[str] = []
        for path in demo_paths().values():
            for line in path.read_text(encoding="utf-8").splitlines():
                for token in line.replace(",", " ").replace('"', " ").split():
                    if "@" not in token or token.count("@") != 1:
                        continue
                    domain = token.rsplit("@", 1)[1].strip(".,;:<>'\")").lower()
                    if domain in allowed_exact or domain.endswith(allowed_suffixes):
                        continue
                    offenders.append(f"{path.name}: {domain}")
        assert not offenders, f"non-reserved domains in demo data: {offenders}"

    def test_demo_contains_no_credential_shaped_strings(self) -> None:
        from campaign_preflight.errors import redact_secrets

        for path in demo_paths().values():
            content = path.read_text(encoding="utf-8")
            assert redact_secrets(content) == content, f"{path.name} looks like it holds a secret"

    def test_demo_documents_why_each_defect_is_present(self, demo_dir: Path) -> None:
        campaign = (demo_dir / "campaign.yaml").read_text(encoding="utf-8")
        assert "BLOCKER" in campaign, "the demo campaign should explain its own defects"
        assert "SYNTHETIC" in campaign.upper()


async def test_demo_is_fast(no_network, no_credentials) -> None:
    """Documented target: the demo completes in under two seconds."""
    import time

    started = time.perf_counter()
    await run_preflight(FixtureProvider.demo(), PreflightConfig())
    assert time.perf_counter() - started < 2.0
